"""A client's years (design of 2026-10-23): each year's ledger, opening balances and trial balance. Matching runs over
all of a client's rows, whatever their year, so a payment in one year clears an invoice of another, and each bank
statement is checked whole; a year's views then take the rows of that year. A year opens with the balances the year
before closed with, its profit carried into retained earnings or the owners' capital; the first year with the
balances typed in from the client's last accounts, and the invoices unpaid behind its Debtors and Creditors. A year
the accountant closes keeps the balances it closed with, which the next year opens with, until it is reopened."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from . import years as yr
from .accounts import BY_CODE, CREDITORS, DEBTORS, AccountType, reserve_for
from .checks import booked, long_date, normalise
from .errors import InvalidInput, InvalidTransactions, NotFound
from .export import file_name, workbook
from .file_dates import check_file_date
from .ledger import ensure_open, needs_review, require_active, settings_for
from .matching import DocumentStatus, match_documents
from .models import (BankCheck, BusinessSettings, Client, ClosedYear, InvoiceSummary, Ledger, OpeningBalances, OpenItem,
                     SavedRow, StatementCheck, Transaction, TrialBalance, Upload, Year, YearSummary)
from .money import ZERO, to_money
from .posting import trial_balance as post
from .rows import StoredRow, now_iso
from .statements import check_statement
from .store_mongo import MongoStore

_PROFIT_AND_LOSS = (AccountType.INCOME.value, AccountType.EXPENSE.value)


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}" if amount >= 0 else f"-£{-amount:,.2f}"


def _year(client: Client, n: int) -> Year:
    start, end = yr.year_bounds(client.first_year_end, n)
    return Year(start=start, end=end, label=yr.label(end), closed=any(c.start == start for c in client.closed_years))


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


def _matched(b: _Books, settings: BusinessSettings) -> tuple[list[Transaction], dict[str, DocumentStatus]]:
    """All the client's rows matched together with the invoices brought forward, in the order of b.stored, and what
    is still owed on each document, by its ref."""
    return match_documents([normalise(row.tx, settings) for row in b.stored], settings, b.read, b.apart, b.unpaid)


def _saved_rows(b: _Books, n: Optional[int]) -> tuple[list[SavedRow], dict[int, Optional[StatementCheck]]]:
    """The rows of year n (every row when None), matched over all the client's rows and the invoices brought forward,
    then each upload's bank lines checked whole against the balances its statement prints, and its document's date
    against its file name's; and each upload's statement check."""
    settings = b.settings if n is None else b.settings_in(n)
    ready, _ = _matched(b, settings)
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


def _changed_since_closed(b: _Books, n: int) -> bool:
    """Whether closed year n's balances, worked out now, differ from the ones it closed with: say, a document
    added to a later year changed how one of its rows is matched. True when they can't be worked out."""
    saved = next(c.closing for c in b.client.closed_years if c.start == b.years[n].start)
    try:
        return _balances(_trial_balance(b, n)) != saved
    except InvalidTransactions:
        return True


def _year_now(b: _Books, n: int) -> Year:
    """Year n, a closed one saying whether its rows now work out to other balances than it closed with."""
    year = b.years[n]
    if year.closed:
        year = year.model_copy(update={"changed_since_closed": _changed_since_closed(b, n)})
    return year


def _ledger(b: _Books, n: int) -> Ledger:
    transactions, checks = _saved_rows(b, n)
    held = {row.upload_id for row in transactions}
    start = b.years[n].start
    uploads = [Upload(id=u["id"], name=u["name"], kind=u["kind"], sha256=u["sha256"], model=u["model"],
                      warnings=u["warnings"], created_at=u["created_at"], rows=u["row_count"],
                      statement=checks.get(u["id"]), bank_account=u["bank_account"], year_start=u["year_start"])
               for u in b.uploads if u["id"] in held or u["year_start"] == start]
    return Ledger(client=b.client, year=_year_now(b, n), years=b.years, uploads=uploads, transactions=transactions)


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


def _balances(tb: TrialBalance) -> dict[str, Decimal]:
    """A trial balance's balances by account, signed with debits positive."""
    return {line.code: line.debit - line.credit for line in tb.lines}


def _carried(b: _Books, balances: dict[str, Decimal]) -> dict[str, Decimal]:
    """Balances at a year's end as the next year opens with them: the income and costs in the reserve (retained
    earnings, or the owners' capital)."""
    reserve = reserve_for(b.client.business_type)
    carried: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for code, balance in balances.items():
        carried[reserve if BY_CODE[code].type.value in _PROFIT_AND_LOSS else code] += balance
    return {code: balance for code, balance in carried.items() if balance != 0}


def _opening(b: _Books, n: int) -> dict[str, Decimal]:
    """Year n's opening balances by account, signed with debits positive: the first year's typed lines; a later
    year's, the balances the latest closed year before it closed with (the typed lines when none is), and the
    balances of every row of the years between, carried into the year."""
    typed = _typed(b.opening)
    if n == 0:
        return typed
    closed = {c.start: c.closing for c in b.client.closed_years}
    last = next((k for k in range(n - 1, -1, -1) if b.years[k].start in closed), None)
    if last == n - 1:
        return _carried(b, closed[b.years[last].start])
    since, base = (-1, typed) if last is None else (last, _carried(b, closed[b.years[last].start]))
    try:
        before = b.post(b.settings, keep=[since < p < n for p in b.placed], opening=base)
    except InvalidTransactions as exc:
        raise InvalidTransactions(f"Fix the earlier years first: {exc.message}", problems=exc.problems) from exc
    return _carried(b, _balances(before))


def opening(store: MongoStore, client_id: int, year_start: dt.date) -> dict[str, Decimal]:
    """The year's opening balances by account, signed with debits positive."""
    b = _books(store, client_id)
    return _opening(b, b.index(year_start))


def trial_balance(store: MongoStore, client_id: int, year_start: dt.date) -> TrialBalance:
    """The year's trial balance: its opening balances plus its own rows, matched with what people decided as the
    ledger is, so it books the rows the ledger books; InvalidTransactions (422) while rows need fixing."""
    b = _books(store, client_id)
    return _trial_balance(b, b.index(year_start))


def _trial_balance(b: _Books, n: int) -> TrialBalance:
    return b.post(b.settings_in(n), keep=[p == n for p in b.placed], opening=_opening(b, n))


def _total_and_date(rows: list[Transaction]) -> tuple[Decimal, Optional[dt.date]]:
    """A document's total (its rows' gross, those the other way taken off) and its date, its latest row's."""
    total = sum((tx.gross if tx.direction == rows[0].direction else -tx.gross for tx in rows), ZERO)
    days = [tx.date for tx in rows if tx.date]
    return abs(total), max(days, default=None)


def _invoices(b: _Books, n: int) -> list[InvoiceSummary]:
    ready, statuses = _matched(b, b.settings_in(n))
    start = b.years[n].start
    refs: dict[str, list[int]] = defaultdict(list)
    for k, tx in enumerate(ready):
        if tx.document_type == "invoice" and booked(tx) and tx.claimed_in is None:
            refs[tx.document_ref or f"row-{k}"].append(k)

    def summary(ref: str, sale: bool, who: Optional[str], number: Optional[str], day: Optional[dt.date],
                total: Decimal, here: bool) -> Optional[InvoiceSummary]:
        """The invoice if it is dated in the year (here), or was still owed when the year started."""
        status = statuses[ref]
        if not here and total - sum((p.amount for p in status.paid_by if p.date and p.date < start), ZERO) <= 0:
            return None
        return InvoiceSummary(ref=ref, kind="sale" if sale else "purchase", counterparty=who, number=number, date=day,
                              total=total, paid=total - status.open, owed=status.open, paid_by=status.paid_by,
                              brought_forward=not here)

    found = []
    for ref, ks in refs.items():
        if ref not in statuses:
            continue
        rows = [ready[k] for k in ks]
        total, day = _total_and_date(rows)
        latest = next((k for k in ks if ready[k].date == day), ks[0])
        if b.placed[latest] <= n:
            found.append(summary(ref, rows[0].contra_account_code == DEBTORS,
                                 next((tx.counterparty for tx in rows if tx.counterparty), None),
                                 next((tx.document_number for tx in rows if tx.document_number), None), day, total,
                                 b.placed[latest] == n))
    for item in b.opening.unpaid:
        found.append(summary(f"ob-{item.id}", item.direction.value == "in", item.counterparty, item.number, item.date,
                             item.amount, False))
    return sorted((i for i in found if i), key=lambda i: (i.date is None, i.date or dt.date.min, i.ref))


def invoices(store: MongoStore, client_id: int, year_start: dt.date) -> list[InvoiceSummary]:
    """The year's booked invoices, and the earlier ones and open items still owed when it started, oldest first. Each
    says what has been paid and what is still owed after every payment, in any year."""
    b = _books(store, client_id)
    return _invoices(b, b.index(year_start))


def _bank_checks(b: _Books, n: int, tb: TrialBalance) -> list[BankCheck]:
    # Each account's latest statement ending in the year: its last line's date and the closing balance it prints.
    latest: dict[str, tuple[dt.date, int, Decimal]] = {}
    for upload in b.uploads:
        lines = [k for k, row in enumerate(b.stored) if row.upload_id == upload["id"]
                 and row.tx.document_type == "statement"]
        dated = [k for k in lines if b.stored[k].tx.date]
        closings = [b.stored[k].tx.closing_balance for k in lines if b.stored[k].tx.closing_balance is not None]
        if not dated or not closings:
            continue
        last = max(dated, key=lambda k: (b.stored[k].tx.date, k))
        account = upload["bank_account"] or b.settings.bank_account
        day = b.stored[last].tx.date
        if b.placed[last] == n and (account not in latest or (day, upload["id"]) > latest[account][:2]):
            latest[account] = (day, upload["id"], closings[-1])

    def held(code: str, day: Optional[dt.date]) -> Decimal:
        """What the books hold in the account, from its opening and the year's rows up to day (all of them if None)."""
        moved = sum((line.debit - line.credit for line in tb.journal if line.code == code and (
            day is None or (b.stored[line.transaction].tx.date or dt.date.max) <= day)), ZERO)
        balance = moved + sum((line.debit - line.credit for line in tb.opening if line.code == code), ZERO)
        return -balance if next(a.kind for a in b.client.bank_accounts if a.code == code) == "credit_card" else balance

    checks = []
    for account in b.client.bank_accounts:
        day, _, printed = latest.get(account.code, (None, 0, None))
        books = held(account.code, day)
        checks.append(BankCheck(code=account.code, name=account.name, kind=account.kind, books=books, statement=printed,
                                statement_date=day, difference=None if printed is None else books - printed))
    return checks


def bank_checks(store: MongoStore, client_id: int, year_start: dt.date) -> list[BankCheck]:
    """One check for each of the client's bank accounts: its balance in the books, from the opening balances and the
    year's rows up to the date of its latest statement of the year (the year's end with none), against that
    statement's closing balance, both as the statement prints it (what a card owes is positive). InvalidTransactions
    (422) while rows need fixing."""
    b = _books(store, client_id)
    n = b.index(year_start)
    return _bank_checks(b, n, _trial_balance(b, n))


def summary(store: MongoStore, client_id: int, year_start: dt.date) -> YearSummary:
    """The year at a glance. While rows need fixing its trial balance is blocked, with the problems, and there are no
    bank checks, which are worked out from it. What is unpaid is what is still owed on the year's invoices."""
    b = _books(store, client_id)
    n = b.index(year_start)
    rows, _ = _saved_rows(b, n)
    owed = _invoices(b, n)
    problems: list[str] = []
    bank: list[BankCheck] = []
    try:
        tb = _trial_balance(b, n)
        bank = _bank_checks(b, n, tb)
        if not tb.is_balanced:
            problems = [f"The trial balance doesn't balance: debits {_gbp(tb.total_debits)}, credits "
                        f"{_gbp(tb.total_credits)}."]
    except InvalidTransactions as exc:
        problems = exc.problems
    return YearSummary(year=_year_now(b, n), rows=len(rows), to_review=sum(needs_review(r) for r in rows),
                       unpaid_sales=sum((i.owed for i in owed if i.kind == "sale"), ZERO),
                       unpaid_purchases=sum((i.owed for i in owed if i.kind == "purchase"), ZERO), bank=bank,
                       trial_balance="blocked" if problems else "balanced", problems=problems)


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
    """Saves the client's first year's opening balances once they make sense, while the first year is open."""
    client = store.get_client(client_id)
    require_active(client)
    ensure_open(client, [yr.year_bounds(client.first_year_end, 0)[0]])
    check_opening(opening)
    store.set_opening(client_id, opening)
    return store.get_client(client_id)


def close(store: MongoStore, client_id: int, year_start: dt.date) -> Client:
    """Closes the year, after the ones before it, once its trial balance balances: it is locked until reopened,
    and keeps the balances it closed with for the next year to open with."""
    b = _books(store, client_id)
    require_active(b.client)
    n = b.index(year_start)
    year = b.years[n]
    if year.closed:
        raise InvalidInput(f"The year to {long_date(year.end)} is already closed.")
    still_open = next((y for y in b.years[:n] if not y.closed), None)
    if still_open is not None:
        raise InvalidInput(f"Close the year to {long_date(still_open.end)} first.")
    try:
        tb = _trial_balance(b, n)
    except InvalidTransactions:
        tb = None
    if tb is None or not tb.is_balanced:
        raise InvalidInput("The trial balance must balance before the year is closed.")
    return store.close_year(client_id, ClosedYear(start=year.start, end=year.end, closed_at=now_iso(),
                                                  closing=_balances(tb)))


def reopen(store: MongoStore, client_id: int, year_start: dt.date) -> Client:
    """Reopens the latest closed year, so it can be changed; the next year opens from its rows again."""
    b = _books(store, client_id)
    require_active(b.client)
    year = b.years[b.index(year_start)]
    if not year.closed:
        raise InvalidInput(f"The year to {long_date(year.end)} is open.")
    latest = b.client.closed_years[-1]
    if latest.start != year_start:
        raise InvalidInput(f"Reopen the year to {long_date(latest.end)} first.")
    return store.reopen_year(client_id, year_start)
