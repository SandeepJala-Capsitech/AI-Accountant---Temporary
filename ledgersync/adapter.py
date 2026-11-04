"""Temporary (until Phase 4): maps the model's rows onto ledger Transactions. The model picks the
account from the chart and says whether money went in or out; a negative amount still means
money out, whatever the model said."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Optional

from .accounts import BY_CODE, SUSPENSE
from .checks import OWED_TO, issue, normalise
from .models import DOCUMENT_TYPES, BusinessSettings, Direction, Transaction
from .money import VatTreatment, to_money, vat_in_gross


def parse_date(text) -> Optional[dt.date]:
    """ISO or UK day-first dates; None when absent or unreadable."""
    if not text:
        return None
    raw = str(text).strip()
    for candidate, fmt in ((raw[:10], "%Y-%m-%d"), (raw, "%d/%m/%Y"), (raw, "%d/%m/%y"), (raw, "%d-%m-%Y")):
        try:
            return dt.datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _document_type(row: dict) -> str:
    """The model's document type; one the ledger does not know is left for a person to check."""
    kind = str(row.get("document_type") or "receipt").strip().lower()
    return kind if kind in DOCUMENT_TYPES else "other"


def _document_key(row: dict) -> Optional[tuple]:
    """Which document a row belongs to. Rows printed with the same total belong together when they come
    from the same kind of document, with the same counterparty and, except for a claim (whose lines have
    their own dates), the same date. The items of an agent's statement belong together whoever each was
    with and whatever they net to. None for a bank line or a row without a printed total."""
    total, kind = to_money(row.get("document_total")), _document_type(row)
    if not to_money(row.get("amount")) or kind == "statement":
        return None
    if kind == "agent_statement":
        return kind, str(row.get("agent") or "").strip().lower(), row.get("date")
    if not total:
        return None
    who = str(row.get("counterparty") or "").strip().lower()
    return abs(total), kind, who, None if kind == "expense_claim" else row.get("date")


def _documents(rows: list[dict]) -> dict:
    """The rows of each receipt, invoice or claim, keyed by _document_key."""
    documents: dict = {}
    for row in rows:
        key = _document_key(row)
        if key:
            documents.setdefault(key, []).append(row)
    return documents


def _vat_of(part: dict) -> Decimal:
    return abs(to_money(part.get("vat")) or 0)


_RATES = (VatTreatment.STANDARD, VatTreatment.REDUCED)
_ROUNDING = Decimal("0.02")   # invoices round VAT line by line


def _rates_of(part: dict) -> set[VatTreatment]:
    """The UK rates a part's own VAT fits within a penny or two of rounding: none for a part whose VAT fits no rate."""
    amount = abs(to_money(part.get("amount")))
    return {rate for rate in _RATES if abs(_vat_of(part) - vat_in_gross(amount, rate)) <= _ROUNDING}


def _vat_is_mixed(parts: list[dict]) -> bool:
    """Whether the VAT of one receipt's parts is not divided by account, so the parts can't be told apart by it: a
    part with VAT beside a part with none (the VAT is on only some of the items), or parts whose own VAT fits no
    one rate between them (20% on one, 5% on another). Each part is judged by its own rate, so the order of the
    lines makes no difference. VAT at one rate on each part is per item and divides."""
    bearing = [part for part in parts if _vat_of(part)]
    if not bearing:
        return False
    if len(bearing) < len(parts):
        return True
    fitted = [rates for rates in map(_rates_of, bearing) if rates]   # a part that fits no rate says nothing
    return bool(fitted) and not set.intersection(*fitted)


def _keep_unsplittable_whole(rows: list[dict]) -> list[dict]:
    """A receipt or invoice split by account whose VAT can't be divided between the parts is put back together as
    one row, flagged as mixed items, for the agent to split by the evidence. That is when the parts do not carry the
    VAT printed on it (one VAT total can't be divided without guessing), and when its items are at different VAT
    rates (zero-rated food beside standard-rated goods), whatever the model made of the VAT: it must not divide one
    rate's VAT between accounts. The row has the document's total and VAT as printed, on the account of its biggest
    part with VAT, else its biggest part."""
    merged: dict = {}
    for (total, *_), parts in _documents(rows).items():
        kind = _document_type(parts[0])
        if kind == "agent_statement" or len(parts) < 2:   # each item of a statement carries its own VAT
            continue
        printed_vat = to_money(parts[0].get("document_vat"))
        carried = sum(_vat_of(part) for part in parts)
        adds_up = sum(abs(to_money(part.get("amount"))) for part in parts) == total   # else they are separate receipts
        if adds_up and ((printed_vat and carried != abs(printed_vat))
                        or (kind != "expense_claim" and _vat_is_mixed(parts))):
            bearing = [part for part in parts if _vat_of(part)] or parts
            biggest = max(bearing, key=lambda part: abs(to_money(part.get("amount"))))
            vat = abs(printed_vat) if printed_vat else carried
            merged[id(parts[0])] = dict(biggest, amount=float(total), vat=float(vat), mixed_items=True)
            merged.update({id(part): None for part in parts[1:]})
    return [kept for kept in (merged.get(id(row), row) for row in rows) if kept is not None]


def _printed_total(row: dict) -> Optional[Decimal]:
    """The total printed on the row's document, which matching checks its rows against; an agent's statement's
    is the net it paid over, negative when the business owes the agent."""
    total = to_money(row.get("document_total"))
    return total if total is None or _document_type(row) == "agent_statement" else abs(total)


def _vat_number(row: dict) -> Optional[str]:
    """The supplier's VAT number without spaces, in upper case; None when none is printed."""
    return "".join(str(row.get("supplier_vat_number") or "").split()).upper() or None


_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _numbers_in(text: str) -> set[Decimal]:
    """Every whole number in the text, as written with or without a pound sign or thousands commas: "£1,234.50" is
    1234.50. A figure inside a longer one ("60.00" in "1160.00" or "60.005") is not a number of its own."""
    return {Decimal(found.replace(",", "")) for found in _NUMBER.findall(text)}


def _only_printed_totals(rows: list[dict], text: str) -> list[dict]:
    """A claim's document_total that its text never prints is the model's own sum of the lines, not evidence: it
    becomes None, so the lines are not grouped and matched by it."""
    printed = _numbers_in(text)
    return [dict(row, document_total=None) if _document_type(row) == "expense_claim"
            and to_money(row.get("document_total")) is not None and abs(to_money(row.get("document_total"))) not in printed
            else row for row in rows]


def to_transactions(rows: list[dict], source: str, settings: BusinessSettings, opening_balance=None,
                    closing_balance=None, agent=None, text: Optional[str] = None) -> list[Transaction]:
    """The model's rows as ledger rows; a bank statement's opening and closing balances, and an agent's statement's
    agent, which the model gives once, go on each of its rows. text is what the file prints, when it has a text layer
    (a spreadsheet, csv or text, a PDF with text): a claim's total it does not print is dropped. None for an image."""
    result = []
    if text is not None:
        rows = _only_printed_totals(rows, text)
    rows = [dict(row, agent=agent) if _document_type(row) == "agent_statement" else row
            for row in _keep_unsplittable_whole(rows)]
    refs = {key: uuid.uuid4().hex[:12] for key in _documents(rows)}   # one reference per document
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        key = _document_key(row)
        if row.get("mixed_items"):
            issues.append(issue("mixed_items", "Items at different VAT rates or of different kinds, with one VAT total: kept as one row."))
        said_in = row.get("direction") == "in"
        if said_in and amount < 0:
            issues.append(issue("direction_conflict",
                                "Marked money in, but the amount was negative: booked as money out."))
        code = str(row.get("account") or "")[:4]
        if code not in BY_CODE or code == settings.bank_account:
            issues.append(issue("account_not_recognised", f"'{row.get('account')}' isn't an account: put in "
                                                          "Suspense. Choose one."))
            code = SUSPENSE
        elif code == SUSPENSE:   # the model itself found no account: a purchase, which still needs its document
            issues.append(issue("account_unclear",
                                "Account unclear: put in Suspense. Choose one."))
        vat, net = to_money(row.get("vat")), to_money(row.get("document_net"))
        kind = _document_type(row)
        statement = kind == "statement"
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=Direction.IN if said_in and amount > 0 else Direction.OUT,
                         gross=abs(amount), vat=abs(vat) if vat is not None else None,   # sign: direction
                         account_code=code, currency=row.get("currency"), source=source,
                         method="llm", issues=issues, document_type=kind,
                         counterparty=str(row.get("counterparty") or "").strip() or None,
                         document_number=None if statement else str(row.get("document_number") or "").strip() or None,
                         supplier_vat_number=None if statement else _vat_number(row),
                         agent=str(row.get("agent") or "").strip() or None,
                         owed_to=row.get("owed_to") if kind == "payroll" and row.get("owed_to") in OWED_TO else None,
                         document_total=_printed_total(row) if key else None,
                         document_net=abs(net) if net is not None and not statement else None,
                         not_vat_invoice=not statement and bool(row.get("not_vat_invoice")),
                         document_ref=refs[key] if key else uuid.uuid4().hex[:12],
                         balance=to_money(row.get("balance")) if statement else None,
                         opening_balance=to_money(opening_balance) if statement else None,
                         closing_balance=to_money(closing_balance) if statement else None)
        result.append(normalise(tx, settings))
    return result
