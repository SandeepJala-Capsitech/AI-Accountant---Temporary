"""A client and a person's changes to its rows (design of 2026-10-06), checked before the store saves them: the
client's settings and bank accounts, the client summaries, and the row and upload actions, each returning the
ledger of the year the change was made in, and refused in a closed year. books.py works out a year's ledger and
trial balance."""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional
from weakref import WeakKeyDictionary

from .accounts import BANK_SLOTS, choosable
from .checks import long_date
from .errors import ClientArchived, InvalidInput, NotFound, YearClosed
from .models import (BankAccount, BusinessSettings, Client, ClientFields, ClientPatch, ClientSummary, Ledger,
                     ManualUpload, RowPatch, Transaction)
from .rows import WHOLE_DOCUMENT, StoredRow
from .store_mongo import MongoStore
from .years import year_bounds, year_index


def bank_codes(client: Client) -> tuple[str, ...]:
    """The codes of the client's bank accounts and cards."""
    return tuple(a.code for a in client.bank_accounts)


def settings_for(client: Client) -> BusinessSettings:
    """How a client's rows are booked: its name (whose books), its VAT registration and kind of business, and its
    bank accounts and cards. Its first current account is the default bank (its first account if it has none)."""
    codes = bank_codes(client)
    default = next((a.code for a in client.bank_accounts if a.kind == "current"), codes[0])
    return BusinessSettings(business_name=client.name, vat_registered=client.vat_registered,
                            business_type=client.business_type, bank_accounts=codes, bank_account=default,
                            books_start=year_bounds(client.first_year_end, 0)[0])


def bank_choices(client: Client, upload_bank: Optional[str] = None) -> tuple[str, ...]:
    """The client's bank accounts a row may be coded to, for accounts.choosable: none for a client with one account,
    which keeps the chart's usual offer; for a client with several, all of them (a transfer between two), and when
    reading an upload, all but the upload's own."""
    codes = bank_codes(client)
    if len(codes) == 1:
        return ()
    return tuple(c for c in codes if c != upload_bank)


def _choosable_codes(client: Client) -> set[str]:
    """The accounts a row of this client can be coded to."""
    return {a.code for a in choosable(client.business_type, bank_choices(client))}


def check_bank_account(client: Client, code: str) -> None:
    if code not in bank_codes(client):
        raise InvalidInput(f"Choose one of {client.name}'s bank accounts.")


def require_active(client: Client) -> None:
    if client.archived:
        raise ClientArchived(f"Restore {client.name} to add documents or make changes.")


def ensure_open(client: Client, days: Iterable[Optional[dt.date]], year_start: Optional[dt.date] = None) -> None:
    """Refuses a change to rows of a closed year: rows dated on the days, a day of None counting as year_start (the
    year an undated row waits in), and a day before the first year as the first, which shows it."""
    closed = {c.start for c in client.closed_years}
    if not closed:
        return
    for day in days:
        day = day or year_start
        if day is None:
            continue
        start, end = year_bounds(client.first_year_end, year_index(client.first_year_end, day) or 0)
        if start in closed:
            raise YearClosed(f"The year to {long_date(end)} is closed. Reopen it to change it.")


def _days(store: MongoStore, client: Client, rows: Iterable[StoredRow]) -> list[Optional[dt.date]]:
    """Each row's date, or for an undated row the start of the year it waits in."""
    uploads = {u["id"]: u for u in store.uploads(client.id)}
    days = []
    for row in rows:
        n = None if row.tx.date else _books().year_of_row(client, row, uploads)
        days.append(row.tx.date or (year_bounds(client.first_year_end, n)[0] if n is not None else None))
    return days


# How a message names each kind of bank account.
_KIND_NAMES = {"current": "current", "savings": "savings", "credit_card": "credit card", "petty_cash": "petty cash"}


def check_client(fields: ClientFields) -> None:
    """A client needs a company name and a responsible person, and an email needs an @. It has at least one bank
    account, each with a name of its own, and no more of a kind than the chart has codes for."""
    if not fields.name:
        raise InvalidInput("Enter the company name.")
    if not fields.contact_name:
        raise InvalidInput("Enter the responsible person.")
    if fields.first_year_end is None:
        raise InvalidInput("Enter the client's first year end.")
    if fields.contact_email and "@" not in fields.contact_email:
        raise InvalidInput("Enter a valid email.")
    if not fields.bank_accounts:
        raise InvalidInput("Add at least one bank account.")
    names: set[str] = set()
    for account in fields.bank_accounts:
        if not account.name:
            raise InvalidInput("Name each bank account.")
        if account.name.lower() in names:
            raise InvalidInput(f"Two bank accounts are called {account.name}: give each its own name.")
        names.add(account.name.lower())
    for kind, codes in BANK_SLOTS.items():
        if sum(a.kind == kind for a in fields.bank_accounts) > len(codes):
            raise InvalidInput(f"You can add up to {len(codes)} {_KIND_NAMES[kind]} accounts.")


def assign_codes(accounts: list[BankAccount], kept: list[BankAccount]) -> list[BankAccount]:
    """The accounts, each without a code given the next free code of its kind; an account sent with its code keeps
    it. kept: the accounts rows still use, which can't be dropped."""
    for account in accounts:
        if account.code and account.code not in BANK_SLOTS[account.kind]:
            raise InvalidInput(f"{account.code} isn't a code for a {_KIND_NAMES[account.kind]} account.")
    taken = [a.code for a in accounts if a.code]
    if len(taken) != len(set(taken)):
        raise InvalidInput("Two bank accounts have the same code.")
    for account in kept:
        if account.code not in taken:
            raise InvalidInput(f"{account.name} has transactions: move them to another account first.")
    used, coded = set(taken), []
    for account in accounts:
        if not account.code:
            free = next(c for c in BANK_SLOTS[account.kind] if c not in used)   # check_client counted them
            used.add(free)
            account = account.model_copy(update={"code": free})
        coded.append(account)
    return coded


def _accounts_in_use(store: MongoStore, client: Client) -> list[BankAccount]:
    """The client's bank accounts that rows are on, or are coded to (a transfer). A row on no account of its own is on
    the default bank."""
    default = settings_for(client).bank_account
    used = {code for row in store.rows(client.id) for code in (row.tx.bank_account or default, row.tx.account_code)}
    return [a for a in client.bank_accounts if a.code in used]


def add_client(store: MongoStore, fields: ClientFields) -> Client:
    check_client(fields)
    return store.create_client(fields.model_copy(update={"bank_accounts": assign_codes(fields.bank_accounts, [])}))


def change_client(store: MongoStore, client_id: int, patch: ClientPatch) -> Client:
    """Edits, archives or restores a client; an edited client must still be complete, and keeps the bank accounts its
    rows are on. Once a year is closed, its year end, VAT registration and kind of business stay as they are."""
    client = store.get_client(client_id)
    sent = patch.model_dump(exclude_unset=True)
    changes = {k: v for k, v in sent.items() if k != "archived" and v is not None}
    if client.closed_years and changes.get("first_year_end", client.first_year_end) != client.first_year_end:
        raise InvalidInput("The year end can't change once a year is closed.")
    for field, what in (("vat_registered", "VAT registration"), ("business_type", "business type")):
        if client.closed_years and changes.get(field, getattr(client, field)) != getattr(client, field):
            raise YearClosed(f"Reopen the closed years to change the client's {what}: it changes how they were "
                             "booked.")
    if changes and not client.archived:
        merged = ClientFields(**{**client.model_dump(include=set(ClientFields.model_fields)), **changes})
        check_client(merged)
        if "bank_accounts" in changes:
            merged.bank_accounts = assign_codes(merged.bank_accounts, _accounts_in_use(store, client))
        changes = {k: getattr(merged, k) for k in changes}
    if sent.get("archived") is not None:
        changes["archived"] = sent["archived"]
    return store.update_client(client_id, changes)


def needs_review(row: Transaction) -> bool:
    return any(found.severity in ("error", "warning") for found in row.issues)


# Each store's clients: (revision, rows, rows to review), as the clients page last worked them out.
_counted: WeakKeyDictionary[MongoStore, dict[int, tuple[int, int, int]]] = WeakKeyDictionary()


def _books():
    """books.py, which builds on this module's settings_for, so it is imported when first used."""
    from . import books
    return books


def _year_of(store: MongoStore, client: Client, row: Optional[StoredRow] = None,
             upload_id: Optional[int] = None) -> Optional[int]:
    """The year a change is made in: the row's year, or the year of an upload's first row, or the year it was added
    to; None when nothing says (the latest year)."""
    uploads = {u["id"]: u for u in store.uploads(client.id)}
    if row is None and upload_id is not None:
        row = next((r for r in store.rows(client.id) if r.upload_id == upload_id), None)
        if row is None:
            added = (uploads.get(upload_id) or {}).get("year_start")
            return year_index(client.first_year_end, added) or 0 if added else None
    if row is None:
        return None
    return _books().year_of_row(client, row, uploads) or 0   # a row before the first year shows in it


def summaries(store: MongoStore, archived: bool = False) -> list[ClientSummary]:
    """The clients page: each client with how many rows it has and how many need a look. A client's ledger is
    built again only once its work has changed (its revision moved)."""
    counted = _counted.setdefault(store, {})
    found = []
    for client in store.list_clients(archived):
        revision = store.revision(client.id)   # read before the rows, so a change made meanwhile shows next time
        known = counted.get(client.id)
        if known is None or known[0] != revision:
            rows = _books().all_rows(store, client.id)
            known = counted[client.id] = (revision, len(rows), sum(needs_review(r) for r in rows))
        found.append(ClientSummary(**client.model_dump(), rows=known[1], to_review=known[2]))
    return found


# A field sent as null that can't be left empty, and what to tell the person.
_NEEDED = {"direction": "Choose money in or out.", "gross": "Enter the amount.", "account_code": "Choose an account.",
           "document_type": "Choose a document type.", "include": "Tick or untick Include."}


def _check_edit(patch: RowPatch, sent: set[str], tx: Transaction, client: Client) -> None:
    for field, message in _NEEDED.items():
        if field in sent and getattr(patch, field) is None:
            raise InvalidInput(message)
    if "description" in sent and not patch.description:
        raise InvalidInput("Enter a description.")
    if "link" in sent and (patch.document_type if "document_type" in sent else tx.document_type) not in (
            "statement", "agent_statement"):
        raise InvalidInput("Only a bank line, or an item of an agent's statement, can be linked to documents.")
    if "account_code" in sent and patch.account_code not in _choosable_codes(client):
        raise InvalidInput(f"This account can't be chosen for {client.name}.")
    gross = patch.gross if "gross" in sent else tx.gross
    vat = patch.vat if "vat" in sent else tx.vat
    if gross <= 0:
        raise InvalidInput("The amount must be above zero.")
    if vat is not None and vat < 0:
        raise InvalidInput("VAT can't be negative.")
    if vat is not None and vat >= gross:
        raise InvalidInput("VAT must be below the amount.")


def change_row(store: MongoStore, client_id: int, row_id: int, patch: RowPatch) -> Ledger:
    """Saves a person's change to a row once it makes sense, and returns the ledger worked out again."""
    client = store.get_client(client_id)
    require_active(client)
    rows = store.rows(client_id)
    row = next((r for r in rows if r.id == row_id), None)
    if row is None:
        raise NotFound("This row was not found.")
    if "apart_from" in patch.model_fields_set and (
            patch.apart_from is None or not set(patch.apart_from) <= {r.id for r in rows} - {row_id}):
        raise InvalidInput("Not the same can only name other rows of this client.")
    # A revert, or a change to the whole document, changes the other rows of its document too.
    whole = patch.revert or bool(patch.model_fields_set & WHOLE_DOCUMENT)
    changed = [r for r in rows if r.id == row_id or (whole and row.tx.document_ref
                                                    and r.tx.document_ref == row.tx.document_ref)]
    days = _days(store, client, changed) + [dt.date.fromisoformat(r.original["date"]) for r in changed
                                             if patch.revert and r.original and r.original.get("date")]
    ensure_open(client, days + ([patch.date] if patch.date else []))
    if patch.revert:
        store.patch_row(client_id, row_id, {}, revert=True)
    else:
        sent = patch.model_fields_set - {"revert"}
        _check_edit(patch, sent, row.tx, client)
        store.patch_row(client_id, row_id, patch.model_dump(mode="json", include=sent))
    return _books().ledger_of(store, client_id, _year_of(store, client, row))


def add_manual(store: MongoStore, client_id: int, upload: ManualUpload) -> Ledger:
    client = store.get_client(client_id)
    require_active(client)
    if not upload.transactions:
        raise InvalidInput("Add at least one row.")
    # As in an edit: only an account of the chart this kind of business uses, so a row can always be booked.
    if any(tx.account_code not in _choosable_codes(client) for tx in upload.transactions):
        raise InvalidInput(f"This account can't be chosen for {client.name}.")
    if any(tx.bank_account is not None and tx.bank_account not in bank_codes(client) for tx in upload.transactions):
        raise InvalidInput(f"Choose one of {client.name}'s bank accounts.")
    ensure_open(client, [tx.date for tx in upload.transactions], dt.date.today())   # undated: today's year
    added = store.add_upload(client_id, upload.name.strip() or "Manual entry", upload.kind, upload.transactions)
    return _books().ledger_of(store, client_id, _year_of(store, client, upload_id=added))


def _ensure_upload_open(store: MongoStore, client: Client, upload_id: int) -> None:
    """Refuses a change to an upload with rows in a closed year."""
    ensure_open(client, _days(store, client, (r for r in store.rows(client.id) if r.upload_id == upload_id)))


def set_upload_bank(store: MongoStore, client_id: int, upload_id: int, bank_account: str) -> Ledger:
    """Puts an upload's statement on another of the client's bank accounts or cards, with its lines."""
    client = store.get_client(client_id)
    require_active(client)
    check_bank_account(client, bank_account)
    _ensure_upload_open(store, client, upload_id)
    store.set_upload_bank(client_id, upload_id, bank_account)
    return _books().ledger_of(store, client_id, _year_of(store, client, upload_id=upload_id))


def remove_upload(store: MongoStore, client_id: int, upload_id: int) -> Ledger:
    client = store.get_client(client_id)
    require_active(client)
    _ensure_upload_open(store, client, upload_id)
    year = _year_of(store, client, upload_id=upload_id)
    store.delete_upload(client_id, upload_id)
    return _books().ledger_of(store, client_id, year)


def remove_row(store: MongoStore, client_id: int, row_id: int) -> Ledger:
    """Removes one row a person no longer wants, and returns its year's ledger worked out again without it."""
    client = store.get_client(client_id)
    require_active(client)
    row = next((r for r in store.rows(client_id) if r.id == row_id), None)
    if row is None:
        raise NotFound("This row was not found.")
    ensure_open(client, _days(store, client, [row]))
    store.delete_row(client_id, row_id)
    return _books().ledger_of(store, client_id, _year_of(store, client, row))

