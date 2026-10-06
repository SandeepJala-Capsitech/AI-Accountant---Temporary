"""Temporary (until Phase 4): maps the model's rows onto ledger Transactions. The model picks the
account from the chart and says whether money went in or out; a negative amount still means
money out, whatever the model said."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from .accounts import BY_CODE, SUSPENSE
from .checks import issue, normalise
from .models import DOCUMENT_TYPES, BusinessSettings, Direction, Transaction
from .money import to_money


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
    their own dates), the same date. None for a bank line or a row without a printed total."""
    total, kind = to_money(row.get("document_total")), _document_type(row)
    if not total or not to_money(row.get("amount")) or kind == "statement":
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


def _keep_unsplittable_whole(rows: list[dict]) -> list[dict]:
    """A receipt or invoice split by account whose rows do not carry the VAT printed on it is put back
    together as one row: one VAT total cannot be divided without guessing, so the document stays whole
    (its total and VAT as printed, on the biggest row's account) and is flagged as mixed items."""
    merged: dict = {}
    for (total, *_), parts in _documents(rows).items():
        printed_vat = to_money(parts[0].get("document_vat"))
        carried = sum(abs(to_money(part.get("vat")) or 0) for part in parts)
        if len(parts) > 1 and printed_vat and carried != abs(printed_vat):
            biggest = max(parts, key=lambda part: abs(to_money(part.get("amount"))))
            merged[id(parts[0])] = dict(biggest, amount=float(total), vat=float(abs(printed_vat)), mixed_items=True)
            merged.update({id(part): None for part in parts[1:]})
    return [kept for kept in (merged.get(id(row), row) for row in rows) if kept is not None]


def to_transactions(rows: list[dict], source: str, settings: BusinessSettings) -> list[Transaction]:
    result = []
    rows = _keep_unsplittable_whole(rows)
    documents = _documents(rows)
    sums = {key: sum(abs(to_money(row.get("amount"))) for row in parts) for key, parts in documents.items()}
    refs = {key: uuid.uuid4().hex[:12] for key in documents}   # one reference per document
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        key = _document_key(row)
        if key and sums[key] != key[0]:
            issues.append(issue("total_mismatch", f"The rows from this document add up to £{sums[key]} but "
                                                  f"its total is £{key[0]}; check the amounts against the document."))
        if row.get("mixed_items"):
            issues.append(issue("mixed_items", "This document mixes items of different kinds but shows one VAT total, "
                                               "so it was kept as one row; split it by hand if each kind needs its own "
                                               "account."))
        said_in = row.get("direction") == "in"
        if said_in and amount < 0:
            issues.append(issue("direction_conflict",
                                "The model said money in but the amount was negative; recorded as money out."))
        code = str(row.get("account") or "")[:4]
        if code not in BY_CODE or code == settings.bank_account:
            issues.append(issue("account_not_recognised", f"'{row.get('account')}' is not an account to post to; "
                                                          "it went to Suspense for review."))
            code = SUSPENSE
        elif code == SUSPENSE:
            issues.append(issue("account_not_recognised",
                                "The model was not sure which account this is; it went to Suspense for review."))
        vat = to_money(row.get("vat"))
        statement = _document_type(row) == "statement"
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=Direction.IN if said_in and amount > 0 else Direction.OUT,
                         gross=abs(amount), vat=abs(vat) if vat is not None else None,   # sign: direction
                         account_code=code, currency=row.get("currency"), source=source,
                         method="llm", issues=issues, document_type=_document_type(row),
                         counterparty=str(row.get("counterparty") or "").strip() or None,
                         document_ref=refs[key] if key else uuid.uuid4().hex[:12],
                         balance=to_money(row.get("balance")) if statement else None,
                         opening_balance=to_money(row.get("opening_balance")) if statement else None,
                         closing_balance=to_money(row.get("closing_balance")) if statement else None)
        result.append(normalise(tx, settings))
    return result
