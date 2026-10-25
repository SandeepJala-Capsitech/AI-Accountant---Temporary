"""A client's years (design of 2026-10-23): each year's ledger, opening balances and trial balance. Matching runs over
all of a client's rows, whatever their year, so a payment in one year clears an invoice of another, and each bank
statement is checked whole; a year's views then take the rows of that year. A year opens with the balances the year
before closed with, its profit carried into retained earnings or the owners' capital; the first year with the
balances typed in from the client's last accounts, and the invoices unpaid behind its Debtors and Creditors."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from . import years as yr
from .accounts import BY_CODE, CREDITORS, DEBTORS, AccountType, reserve_for
from .checks import normalise
from .errors import InvalidInput, InvalidTransactions, NotFound
from .export import file_name, workbook
from .file_dates import check_file_date
from .ledger import require_active, settings_for
from .matching import match_documents
from .models import (BusinessSettings, Client, Ledger, OpeningBalances, OpenItem, SavedRow, StatementCheck,
                     TrialBalance, Upload, Year)
from .money import ZERO, to_money
from .posting import trial_balance as post
from .rows import StoredRow
from .statements import check_statement
from .store_mongo import MongoStore

_PROFIT_AND_LOSS = (AccountType.INCOME.value, AccountType.EXPENSE.value)


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}" if amount >= 0 else f"-£{-amount:,.2f}"


def _year(client: Client, n: int) -> Year:
    start, end = yr.year_bounds(client.first_year_end, n)
    return Year(start=start, end=end, label=yr.label(end))


def year_of_row(client: Client, row: StoredRow, uploads_by_id: dict[int, dict]) -> Optional[int]:
    """The year a row belongs to: the year of its date; with no date, the year its upload was added to (for an upload
    saved without one, the year of the day it was saved). None before the first year starts."""
    day = row.tx.date
    if day is None:
        upload = uploads_by_id.get(row.upload_id) or {}
        day = upload.get("year_start") or (
            dt.date.fromisoformat(upload["created_at"][:10]) if upload.get("created_at") else None)
    return yr.year_index(client.first_year_end, day) if day else None


def _place(client: Client, row: StoredRow, uploads_by_id: dict[int, dict]) -> int:
    """The year a row is shown and booked in: its own, or the first year for a row dated before it, where the period
    check says so."""
    n = year_of_row(client, row, uploads_by_id)
    return 0 if n is None else n


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


def _typed(opening: OpeningBalances) -> dict[str, Decimal]:
    """The first year's typed opening balances by account, signed with debits positive."""
    balances: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in opening.lines:
        balances[line.code] += line.debit - line.credit
    return {code: balance for code, balance in balances.items() if balance != 0}


@dataclass
class _Books:
    """Everything a year's views are worked out from: the client, all its rows and uploads, the year each row is in,
    what people decided, its first year's opening balances, and its years."""
    client: Client
    stored: list[StoredRow]
    uploads: list[dict]
    placed: list[int]
    read: list[Optional[Decimal]]
    apart: list[set[int]]
    opening: OpeningBalances
    years: list[Year]

    @property
    def settings(self) -> BusinessSettings:
        return settings_for(self.client)

    def settings_in(self, n: int) -> BusinessSettings:
        """The client's settings with year n as the accounting period, so a row dated outside it says so."""
        return self.settings.model_copy(update={"period_start": self.years[n].start, "period_end": self.years[n].end})

    @property
    def unpaid(self) -> list[OpenItem]:
        """The invoices brought into the first year, dated the day before it starts, as matching dates them."""
        day = self.years[0].start - yr.DAY
        return [item.model_copy(update={"date": day}) for item in self.opening.unpaid]

    def index(self, year_start: dt.date) -> int:
        n = next((k for k, y in enumerate(self.years) if y.start == year_start), None)
        if n is None:
            raise NotFound("That year was not found.")
        return n

    def post(self, settings: BusinessSettings, keep: list[bool], opening: dict[str, Decimal]) -> TrialBalance:
        return post([row.tx for row in self.stored], settings, self.read, self.apart, keep=keep, opening=opening,
                    open_items=self.unpaid)


def _books(store: MongoStore, client_id: int, today: Optional[dt.date] = None) -> _Books:
    client = store.get_client(client_id)
    stored = store.rows(client_id)
    listed = store.uploads(client_id)
    by_id = {u["id"]: u for u in listed}
    placed = [_place(client, row, by_id) for row in stored]
    latest = max(placed + [yr.year_index(client.first_year_end, today or dt.date.today()) or 0])
    read, apart = _decisions(stored)
    return _Books(client=client, stored=stored, uploads=listed, placed=placed, read=read, apart=apart,
                  opening=store.opening(client_id), years=[_year(client, n) for n in range(latest + 1)])


def years_of(store: MongoStore, client_id: int, today: dt.date) -> list[Year]:
    """The client's years: the first through the latest that holds a row, or holds today, whichever is later."""
    return _books(store, client_id, today).years


def _saved_rows(b: _Books, n: Optional[int]) -> tuple[list[SavedRow], dict[int, Optional[StatementCheck]]]:
    """The rows of year n (every row when None), matched over all the client's rows and the invoices brought forward,
    then each upload's bank lines checked whole against the balances its statement prints, and its document's date
    against its file name's; and each upload's statement check."""
    settings = b.settings if n is None else b.settings_in(n)
    ready, _ = match_documents([normalise(row.tx, settings) for row in b.stored], settings, b.read, b.apart, b.unpaid)
    positions: dict[int, list[int]] = defaultdict(list)
    for k, row in enumerate(b.stored):
        positions[row.upload_id].append(k)
    names = {u["id"]: u["name"] for u in b.uploads}
    # A card's statement prints what is owed on it, so its balances move the other way.
    cards = {a.code for a in b.client.bank_accounts if a.kind == "credit_card"}
    on_card = {u["id"] for u in b.uploads if u["bank_account"] in cards}
    checks: dict[int, Optional[StatementCheck]] = {}
    for upload_id, ks in positions.items():
        checked, checks[upload_id] = check_statement([ready[k] for k in ks], credit_card=upload_id in on_card)
        checked = check_file_date(checked, names.get(upload_id, ""))
        for k, tx in zip(ks, checked):
            ready[k] = tx
    # The rows each issue is about, from their place in the rows matched to their row ids.
    rows = [SavedRow(**{**tx.model_dump(), "issues": [
                         {**i.model_dump(), "related": [b.stored[p].id for p in i.related]} for i in tx.issues]},
                     id=row.id, upload_id=row.upload_id, edited=row.original is not None, original=row.original)
            for k, (row, tx) in enumerate(zip(b.stored, ready)) if n is None or b.placed[k] == n]
    return rows, checks


def _ledger(b: _Books, n: int) -> Ledger:
    transactions, checks = _saved_rows(b, n)
    held = {row.upload_id for row in transactions}
    start = b.years[n].start
    uploads = [Upload(id=u["id"], name=u["name"], kind=u["kind"], sha256=u["sha256"], model=u["model"],
                      warnings=u["warnings"], created_at=u["created_at"], rows=u["row_count"],
                      statement=checks.get(u["id"]), bank_account=u["bank_account"], year_start=u["year_start"])
               for u in b.uploads if u["id"] in held or u["year_start"] == start]
    return Ledger(client=b.client, year=b.years[n], years=b.years, uploads=uploads, transactions=transactions)


def ledger(store: MongoStore, client_id: int, year_start: dt.date) -> Ledger:
    """The year's ledger: its rows, matched over all the client's rows, and the uploads holding any of them or added
    to the year. NotFound when year_start isn't the start of one of the client's years."""
    b = _books(store, client_id)
    return _ledger(b, b.index(year_start))


def ledger_of(store: MongoStore, client_id: int, n: Optional[int]) -> Ledger:
    """The ledger of the client's year n, as a change made in it returns; the latest year when n is None, or when a
    change left year n no longer one of the client's years (its last row removed)."""
    b = _books(store, client_id)
    last = len(b.years) - 1
    return _ledger(b, last if n is None else min(n, last))


def all_rows(store: MongoStore, client_id: int) -> list[SavedRow]:
    """Every row of the client, whatever its year, as the year ledgers show them (for the clients page)."""
    return _saved_rows(_books(store, client_id), None)[0]


def _opening(b: _Books, n: int) -> dict[str, Decimal]:
    """Year n's opening balances by account, signed with debits positive: the first year's typed lines; a later
    year's, the balances of every row before it on top of those, with the income and costs of the years before it
    in the reserve (retained earnings, or the owners' capital)."""
    typed = _typed(b.opening)
    if n == 0:
        return typed
    try:
        before = b.post(b.settings, keep=[p < n for p in b.placed], opening=typed)
    except InvalidTransactions as exc:
        raise InvalidTransactions(f"Fix the earlier years first: {exc.message}", problems=exc.problems) from exc
    reserve = reserve_for(b.client.business_type)
    carried: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in before.lines:
        carried[reserve if line.type in _PROFIT_AND_LOSS else line.code] += line.debit - line.credit
    return {code: balance for code, balance in carried.items() if balance != 0}


def opening(store: MongoStore, client_id: int, year_start: dt.date) -> dict[str, Decimal]:
    """The year's opening balances by account, signed with debits positive."""
    b = _books(store, client_id)
    return _opening(b, b.index(year_start))


def trial_balance(store: MongoStore, client_id: int, year_start: dt.date) -> TrialBalance:
    """The year's trial balance: its opening balances plus its own rows, matched with what people decided as the
    ledger is, so it books the rows the ledger books; InvalidTransactions (422) while rows need fixing."""
    b = _books(store, client_id)
    n = b.index(year_start)
    return b.post(b.settings_in(n), keep=[p == n for p in b.placed], opening=_opening(b, n))


def export(store: MongoStore, client_id: int, year_start: dt.date, day: dt.date) -> tuple[bytes, str]:
    """The year's workbook and its file name; while rows need fixing, the trial balance sheet says what."""
    built = ledger(store, client_id, year_start)
    try:
        balance = trial_balance(store, client_id, year_start)
    except InvalidTransactions as exc:
        balance = exc.problems
    return workbook(built, balance, day), file_name(built.client.name, day)


def check_opening(opening: OpeningBalances) -> None:
    """Opening balances go on balance sheet accounts and balance, and the unpaid invoices make up Debtors and
    Creditors, so a payment of each can clear it."""
    for line in opening.lines:
        account = BY_CODE.get(line.code)
        if account is None:
            raise InvalidInput(f"{line.code} isn't an account in the chart.")
        if account.type.value in _PROFIT_AND_LOSS:
            raise InvalidInput(f"Opening balances take balance sheet accounts only: {account.code} {account.name} is "
                               "income or a cost.")
        if line.debit < 0 or line.credit < 0:
            raise InvalidInput("Opening amounts can't be negative.")
    debits = sum((line.debit for line in opening.lines), ZERO)
    credits = sum((line.credit for line in opening.lines), ZERO)
    if debits != credits:
        raise InvalidInput(f"Opening debits {_gbp(debits)} and credits {_gbp(credits)} must be the same.")
    ids: set[str] = set()
    for item in opening.unpaid:
        if not item.id or item.id in ids:
            raise InvalidInput("Give each unpaid invoice an id of its own.")
        ids.add(item.id)
        if not item.counterparty:
            raise InvalidInput("Name the customer or supplier of each unpaid invoice.")
        if item.amount <= 0:
            raise InvalidInput("Enter the amount still owed on each unpaid invoice.")
    balances = _typed(opening)
    for direction, code, sign, text in (("in", DEBTORS, 1, "The unpaid invoices from customers add up to {x}; "
                                                         "Debtors is {y}."),
                                        ("out", CREDITORS, -1, "The unpaid bills from suppliers add up to {x}; "
                                                               "Creditors is {y}.")):
        owed = sum((item.amount for item in opening.unpaid if item.direction.value == direction), ZERO)
        balance = sign * balances.get(code, ZERO)
        if owed != balance:
            raise InvalidInput(text.format(x=_gbp(owed), y=_gbp(balance)))


def set_opening(store: MongoStore, client_id: int, opening: OpeningBalances) -> Client:
    """Saves the client's first year's opening balances once they make sense."""
    client = store.get_client(client_id)
    require_active(client)
    check_opening(opening)
    store.set_opening(client_id, opening)
    return store.get_client(client_id)
