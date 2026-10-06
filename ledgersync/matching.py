"""Matches bank lines to the open invoices and claims they pay (design of 2026-10-06). Pure functions:
the same rows give the same matches, whatever order they arrived in."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import combinations
from typing import Optional

from .accounts import DEBTORS, STAFF_EXPENSES
from .checks import MATCHING_ISSUES, booked, issue
from .models import BusinessSettings, Direction, Issue, Settlement, Transaction
from .money import ZERO

WINDOW = dt.timedelta(days=31)   # a payment pays a document dated at most 31 days before it
MAX_SET = 5                      # one payment clears at most five documents from one counterparty

# Words that say nothing about who a business is: legal forms and small linking words, bank-statement
# noise (payment types and their short codes), generic trade words, and place and national words.
_IGNORED = {"ltd", "limited", "plc", "llp", "co", "the", "and", "for", "of", "to", "at", "in", "on",
            "bank", "payment", "payments", "fin", "card", "dd", "so", "bacs", "fps", "ref", "direct", "debit",
            "credit", "faster", "standing", "order", "transfer", "tfr", "deb", "vis", "pos", "bgc", "chq", "atm",
            "int", "services", "solutions", "group", "online", "international", "holdings", "company", "trading",
            "uk", "gb", "london", "british", "national", "royal", "england", "scotland", "wales"}


def _words(name: Optional[str]) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (name or "").lower()) if w not in _IGNORED]


def _abbreviates(short: str, long: str) -> bool:
    """'mgmt' abbreviates 'management': three letters or more, the same first letter, the rest in order."""
    if not 3 <= len(short) < len(long) or short[0] != long[0]:
        return False
    letters = iter(long)
    return all(ch in letters for ch in short)


def names_match(a: Optional[str], b: Optional[str]) -> bool:
    """True when two names share a word of four or more letters, or a word of one abbreviates a word of
    the other, after legal forms, bank words, generic trade words and numbers are set aside."""
    wa, wb = _words(a), _words(b)
    return (any(w in wb for w in wa if len(w) >= 4)
            or any(_abbreviates(x, y) or _abbreviates(y, x) for x in wa for y in wb))


@dataclass
class _Document:
    """An open invoice, claim or included document: the rows that share its document_ref."""
    ref: str
    direction: str
    account: Optional[str]        # where what is owed is held: 1100, 2100 or 2110
    counterparty: Optional[str]
    description: str
    rows: list[int] = field(default_factory=list)
    total: Decimal = ZERO
    date: Optional[dt.date] = None
    open: Decimal = ZERO
    paid_by: list[Settlement] = field(default_factory=list)

    def settlement(self, amount: Decimal) -> Settlement:
        return Settlement(ref=self.ref, amount=amount, date=self.date, description=self.description)

    def label(self) -> str:
        when = f", {self.date:%d %b %Y}" if self.date else ""
        return f"{self.description} ({_gbp(self.open)}{when})"


def _gbp(amount: Decimal) -> str:
    return f"£{amount:,.2f}"


def match(rows: list[Transaction], settings: BusinessSettings) -> list[Transaction]:
    """Which documents each bank line pays and what is still owed on each document, for transactions
    checks.normalise has already prepared. Lines a person linked go first, the rest oldest first, so the
    result does not depend on the order the rows arrived in."""
    rows = [_cleared(tx) for tx in rows]
    documents = _documents(rows)
    changes: list[dict] = [{} for _ in rows]
    lines = sorted((n for n, tx in enumerate(rows) if tx.document_type == "statement"),
                   key=lambda n: (rows[n].link is None, rows[n].date or dt.date.max, n))
    for n in lines:
        _settle(n, rows[n], documents, changes[n])
    for doc in documents.values():
        for n in doc.rows:
            changes[n].update(owed=doc.open, paid_by=list(doc.paid_by))
    return [tx.model_copy(update=change) if change else tx for tx, change in zip(rows, changes)]


def _cleared(tx: Transaction) -> Transaction:
    """The row without the outputs of an earlier match."""
    return tx.model_copy(update={"paid_against": None, "pays": [], "candidates": [], "owed": None, "paid_by": [],
                                 "issues": [i for i in tx.issues if i.code not in MATCHING_ISSUES]})


def _documents(rows: list[Transaction]) -> dict[str, _Document]:
    """The open documents: booked invoices, claims and included documents, grouped by document_ref.
    A claim is dated by its latest dated line."""
    documents: dict[str, _Document] = {}
    for n, tx in enumerate(rows):
        if tx.document_type in ("receipt", "statement") or not booked(tx):
            continue
        ref = tx.document_ref or f"row-{n}"
        doc = documents.setdefault(ref, _Document(ref=ref, direction=tx.direction.value,
                                                  account=tx.contra_account_code,
                                                  counterparty=tx.counterparty, description=tx.description))
        doc.rows.append(n)
        doc.total += tx.gross if tx.direction.value == doc.direction else -tx.gross
        if tx.date and (doc.date is None or tx.date > doc.date):
            doc.date = tx.date
        doc.counterparty = doc.counterparty or tx.counterparty
    for doc in documents.values():
        doc.open = doc.total
    return documents


def _could_pay(line: Transaction, doc: _Document) -> bool:
    """Still open, the same direction, and the payment dated on or after the document and within 31 days."""
    return (doc.open > ZERO and doc.direction == line.direction.value and doc.date is not None
            and line.date is not None and doc.date <= line.date <= doc.date + WINDOW)


def _settle(n: int, line: Transaction, documents: dict[str, _Document], change: dict) -> None:
    found: list[Issue] = []
    if line.link == []:                                   # Unlink: an ordinary bank line
        return
    if line.link:
        linked = [documents[ref] for ref in line.link if ref in documents]
        if len(linked) == len(line.link):
            _pay(n, line, linked, change, found)          # the person's choice, as given
            _note(change, line, found)
            return
        found.append(issue("stale_link", "A document this line was linked to is no longer in the table, "
                                         "so it was matched automatically."))
    kind, options = _decide(line, [d for d in documents.values() if _could_pay(line, d)])
    if kind == "pay":
        _pay(n, line, options[0], change, found)
    elif kind == "choose":
        found.append(issue("choose_payment", f"Could pay: {_choices(options)}. Choose one.", "error"))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
    elif kind == "suggest":
        found.append(issue("possible_payment", f"May pay {_choices(options)}: the same amount, but the names do "
                                               "not match. Link it if it does."))
        change["candidates"] = [[d.settlement(d.open) for d in option] for option in options]
    _note(change, line, found)


def _decide(line: Transaction, docs: list[_Document]) -> tuple[str, list[list[_Document]]]:
    """What to do with a bank line, given the open documents it could pay (same direction, within 31 days):
    pay them, ask a person to choose, suggest them, or nothing. Only documents from the same counterparty
    are ever paid automatically."""
    named = [d for d in docs if line.counterparty and names_match(line.counterparty, d.counterparty)]
    exact = [d for d in named if d.open == line.gross]
    if exact:
        return ("pay", [exact]) if len(exact) == 1 else ("choose", [[d] for d in exact])
    sets = _sets_adding_up_to(line.gross, named)
    if sets:
        return ("pay", sets) if len(sets) == 1 else ("choose", sets)
    if named:      # a part payment or an overpayment, when only one document can be meant
        return ("pay", [named]) if len(named) == 1 else ("choose", [[d] for d in named])
    same_amount = [d for d in docs if d.open == line.gross]
    return ("suggest", [[d] for d in same_amount]) if same_amount else ("none", [])


def _sets_adding_up_to(amount: Decimal, docs: list[_Document]) -> list[list[_Document]]:
    """Sets of two to five documents whose open amounts add up to the payment, oldest documents first;
    stops once it has found more than five, since a person has to choose anyway."""
    oldest = sorted(docs, key=lambda d: (d.date, d.ref))[:20]
    found: list[list[_Document]] = []
    for size in range(2, MAX_SET + 1):
        for chosen in combinations(oldest, size):
            if sum((d.open for d in chosen), ZERO) == amount:
                found.append(list(chosen))
                if len(found) > 5:
                    return found
    return found


def _pay(n: int, line: Transaction, docs: list[_Document], change: dict, found: list[Issue]) -> None:
    """Applies the bank line to documents, oldest first: each takes what is open on it and the last takes
    the rest, so a part payment or an overpayment shows on it. The line then posts against the account
    holding what was owed, with no VAT: the VAT was booked with the document."""
    docs = sorted(docs, key=lambda d: (d.date or dt.date.max, d.ref))
    owed_before, left, pays = sum((d.open for d in docs), ZERO), line.gross, []
    for k, doc in enumerate(docs):
        take = left if k == len(docs) - 1 else min(doc.open, left)
        doc.open, left = doc.open - take, left - take
        pays.append(doc.settlement(take))
        doc.paid_by.append(Settlement(ref=line.document_ref or f"row-{n}", amount=take, date=line.date,
                                      description=line.description))
    still_owed = owed_before - line.gross
    if still_owed > ZERO:
        found.append(issue("part_payment", f"Paid {_gbp(line.gross)} of {_gbp(owed_before)}. "
                                           f"{_gbp(still_owed)} still owed."))
    elif still_owed < ZERO:
        found.append(issue("overpayment", _overpaid(line, docs[-1], -still_owed)))
    change.update(paid_against=docs[0].account, pays=pays, vat_posted=ZERO, net=line.gross)


def _overpaid(line: Transaction, doc: _Document, excess: Decimal) -> str:
    whom = {DEBTORS: "the customer", STAFF_EXPENSES: "the employee"}.get(doc.account, "the supplier")
    if line.direction == Direction.IN:
        return f"Received {_gbp(excess)} more than owed. You now owe {whom} {_gbp(excess)}."
    return f"Paid {_gbp(excess)} more than owed. {whom[0].upper()}{whom[1:]} now owes you {_gbp(excess)}."


def _choices(options: list[list[_Document]]) -> str:
    return " or ".join(" + ".join(d.label() for d in option) for option in options)


def _note(change: dict, line: Transaction, found: list[Issue]) -> None:
    if found:
        change["issues"] = line.issues + found
