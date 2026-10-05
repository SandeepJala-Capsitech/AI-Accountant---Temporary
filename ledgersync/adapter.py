"""Temporary (until Phase 4): maps the model's rows onto ledger Transactions. The model picks the
account from the chart and says whether money went in or out; a negative amount still means
money out, whatever the model said."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .accounts import BY_CODE, SUSPENSE
from .checks import issue, normalise
from .models import BusinessSettings, Direction, Transaction
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


def _documents(rows: list[dict]) -> dict:
    """The rows of each receipt or invoice, keyed by its printed total (statement rows have none)."""
    documents: dict = {}
    for row in rows:
        total = to_money(row.get("document_total"))
        if total and to_money(row.get("amount")):
            documents.setdefault(abs(total), []).append(row)
    return documents


def _keep_unsplittable_whole(rows: list[dict]) -> list[dict]:
    """A receipt or invoice split by account whose rows do not carry the VAT printed on it is put back
    together as one row: one VAT total cannot be divided without guessing, so the document stays whole
    (its total and VAT as printed, on the biggest row's account) and is flagged as mixed items."""
    merged: dict = {}
    for total, parts in _documents(rows).items():
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
    sums = {total: sum(abs(to_money(row.get("amount"))) for row in parts)
            for total, parts in _documents(rows).items()}
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        total = to_money(row.get("document_total"))
        if total and sums.get(abs(total)) != abs(total):
            issues.append(issue("total_mismatch", f"The rows from this document add up to £{sums[abs(total)]} but "
                                                  f"its total is £{abs(total)}; check the amounts against the document."))
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
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=Direction.IN if said_in and amount > 0 else Direction.OUT,
                         gross=abs(amount), vat=abs(vat) if vat is not None else None,   # sign: direction
                         account_code=code, currency=row.get("currency"), source=source,
                         method="llm", issues=issues)
        result.append(normalise(tx, settings))
    return result
