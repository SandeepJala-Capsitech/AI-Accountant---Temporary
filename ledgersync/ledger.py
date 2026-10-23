"""A client's ledger as the pages show it (design of 2026-10-06): the saved rows booked and matched with the
client's own settings, each bank statement checked against its balances; the client summaries; and a
person's changes, checked before the store saves them."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Optional
from weakref import WeakKeyDictionary

from .accounts import BANK_SLOTS, choosable
from .checks import normalise
from .errors import ClientArchived, InvalidInput, InvalidTransactions, NotFound
from .export import file_name, workbook
from .file_dates import check_file_date
from .matching import match
from .money import to_money
from .models import (BankAccount, BusinessSettings, Client, ClientFields, ClientPatch, ClientSummary, Ledger,
                     ManualUpload, RowPatch, SavedRow, StatementCheck, Transaction, TrialBalance, Upload)
from .posting import trial_balance as post
from .statements import check_statement
from .rows import StoredRow
from .store_mongo import MongoStore


def bank_codes(client: Client) -> tuple[str, ...]:
    """The codes of the client's bank accounts and cards."""
    return tuple(a.code for a in client.bank_accounts)


def settings_for(client: Client) -> BusinessSettings:
    """How a client's rows are booked: its name (whose books), its VAT registration and kind of business, and its
    bank accounts and cards. Its first current account is the default bank (its first account if it has none)."""
    codes = bank_codes(client)
    default = next((a.code for a in client.bank_accounts if a.kind == "current"), codes[0])
    return BusinessSettings(business_name=client.name, vat_registered=client.vat_registered,
                            business_type=client.business_type, bank_accounts=codes, bank_account=default)


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


# How a message names each kind of bank account.
_KIND_NAMES = {"current": "current", "savings": "savings", "credit_card": "credit card", "petty_cash": "petty cash"}


def check_client(fields: ClientFields) -> None:
    """A client needs a company name and a responsible person, and an email needs an @. It has at least one bank
    account, each with a name of its own, and no more of a kind than the chart has codes for."""
    if not fields.name:
        raise InvalidInput("Enter the company name.")
    if not fields.contact_name:
        raise InvalidInput("Enter the responsible person.")
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
    rows are on."""
    client = store.get_client(client_id)
    sent = patch.model_dump(exclude_unset=True)
    changes = {k: v for k, v in sent.items() if k != "archived" and v is not None}
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


def _decisions(stored: list[StoredRow]) -> tuple[list[Optional[Decimal]], list[set[int]]]:
    """What people decided about a client's rows, for matching them: each row's amount as it was read before a person
    changed it (None if unchanged), and, for each row, the rows they said are not the same as it, both ways, from row
    ids to places in the list. The ledger and the trial balance both match with this, so they book the same rows."""
    read = [to_money(row.original.get("gross")) if row.original else None for row in stored]
    place = {row.id: n for n, row in enumerate(stored)}
    apart: list[set[int]] = [set() for _ in stored]
    for n, row in enumerate(stored):
        for other in (place[i] for i in row.tx.apart_from if i in place):
            apart[n].add(other)
            apart[other].add(n)
    return read, apart


def build(store: MongoStore, client_id: int) -> Ledger:
    """The client's ledger: every saved row booked and matched with the client's settings, then each upload's
    bank lines checked against the balances its statement prints, and its document's date against its file name's."""
    client = store.get_client(client_id)
    stored = store.rows(client_id)
    settings = settings_for(client)
    read, apart = _decisions(stored)
    ready = match([normalise(row.tx, settings) for row in stored], settings, read=read, apart=apart)
    positions: dict[int, list[int]] = defaultdict(list)
    for n, row in enumerate(stored):
        positions[row.upload_id].append(n)
    listed = store.uploads(client_id)
    names = {u["id"]: u["name"] for u in listed}
    # A card's statement prints what is owed on it, so its balances move the other way.
    cards = {a.code for a in client.bank_accounts if a.kind == "credit_card"}
    on_card = {u["id"] for u in listed if u["bank_account"] in cards}
    checks: dict[int, Optional[StatementCheck]] = {}
    for upload_id, ns in positions.items():
        checked, checks[upload_id] = check_statement([ready[n] for n in ns], credit_card=upload_id in on_card)
        checked = check_file_date(checked, names.get(upload_id, ""))
        for n, tx in zip(ns, checked):
            ready[n] = tx
    # The rows each issue is about, from their place in the rows matched to their row ids.
    transactions = [SavedRow(**{**tx.model_dump(), "issues": [
                                 {**i.model_dump(), "related": [stored[p].id for p in i.related]} for i in tx.issues]},
                             id=row.id,
                             upload_id=row.upload_id,
                             edited=row.original is not None, original=row.original)
                    for row, tx in zip(stored, ready)]
    uploads = [Upload(id=u["id"], name=u["name"], kind=u["kind"], sha256=u["sha256"], model=u["model"],
                      warnings=u["warnings"], created_at=u["created_at"], rows=u["row_count"],
                      statement=checks.get(u["id"]), bank_account=u["bank_account"]) for u in listed]
    return Ledger(client=client, uploads=uploads, transactions=transactions)


# Each store's clients: (revision, rows, rows to review), as the clients page last worked them out.
_counted: WeakKeyDictionary[MongoStore, dict[int, tuple[int, int, int]]] = WeakKeyDictionary()


def summaries(store: MongoStore, archived: bool = False) -> list[ClientSummary]:
    """The clients page: each client with how many rows it has and how many need a look. A client's ledger is
    built again only once its work has changed (its revision moved)."""
    counted = _counted.setdefault(store, {})
    found = []
    for client in store.list_clients(archived):
        revision = store.revision(client.id)   # read before the rows, so a change made meanwhile shows next time
        known = counted.get(client.id)
        if known is None or known[0] != revision:
            rows = build(store, client.id).transactions
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
    if patch.revert:
        store.patch_row(client_id, row_id, {}, revert=True)
    else:
        sent = patch.model_fields_set - {"revert"}
        _check_edit(patch, sent, row.tx, client)
        store.patch_row(client_id, row_id, patch.model_dump(mode="json", include=sent))
    return build(store, client_id)


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
    store.add_upload(client_id, upload.name.strip() or "Manual entry", upload.kind, upload.transactions)
    return build(store, client_id)


def set_upload_bank(store: MongoStore, client_id: int, upload_id: int, bank_account: str) -> Ledger:
    """Puts an upload's statement on another of the client's bank accounts or cards, with its lines."""
    client = store.get_client(client_id)
    require_active(client)
    check_bank_account(client, bank_account)
    store.set_upload_bank(client_id, upload_id, bank_account)
    return build(store, client_id)


def remove_upload(store: MongoStore, client_id: int, upload_id: int) -> Ledger:
    require_active(store.get_client(client_id))
    store.delete_upload(client_id, upload_id)
    return build(store, client_id)


def remove_row(store: MongoStore, client_id: int, row_id: int) -> Ledger:
    """Removes one row a person no longer wants, and returns the ledger worked out again without it."""
    require_active(store.get_client(client_id))
    store.delete_row(client_id, row_id)
    return build(store, client_id)


def trial_balance(store: MongoStore, client_id: int) -> TrialBalance:
    """The client's trial balance from its saved rows, matched with what people decided as the ledger is, so it books
    the rows the ledger books; InvalidTransactions (422) while rows need fixing."""
    client = store.get_client(client_id)
    stored = store.rows(client_id)
    read, apart = _decisions(stored)
    return post([row.tx for row in stored], settings_for(client), read=read, apart=apart)


def export(store: MongoStore, client_id: int, day: dt.date) -> tuple[bytes, str]:
    """The client's workbook and its file name; while rows need fixing, the trial balance sheet says what."""
    built = build(store, client_id)
    try:
        balance = trial_balance(store, client_id)
    except InvalidTransactions as exc:
        balance = exc.problems
    return workbook(built, balance, day), file_name(built.client.name, day)
