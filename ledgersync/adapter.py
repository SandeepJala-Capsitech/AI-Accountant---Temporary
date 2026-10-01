"""Temporary (until Phase 4): maps the model's rows onto ledger Transactions. The model picks the
account from the chart and says whether money went in or out; a negative amount still means
money out, whatever the model said."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .accounts import BY_CODE, SUSPENSE
from .checks import normalise
from .models import BusinessSettings, Direction, Issue, Transaction
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


def to_transactions(rows: list[dict], source: str, settings: BusinessSettings) -> list[Transaction]:
    result = []
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        said_in = row.get("direction") == "in"
        if said_in and amount < 0:
            issues.append(Issue(code="direction_conflict", severity="warning",
                                message="The model said money in but the amount was negative; recorded as money out."))
        code = str(row.get("account") or "")[:4]
        if code not in BY_CODE or code == settings.bank_account:
            issues.append(Issue(code="account_not_recognised", severity="warning",
                                message=f"'{row.get('account')}' is not an account to post to; "
                                        "it went to Suspense for review."))
            code = SUSPENSE
        elif code == SUSPENSE:
            issues.append(Issue(code="account_not_recognised", severity="warning",
                                message="The model was not sure which account this is; it went to Suspense for review."))
        vat = to_money(row.get("vat"))
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=Direction.IN if said_in and amount > 0 else Direction.OUT,
                         gross=abs(amount), vat=abs(vat) if vat is not None else None,   # sign: direction
                         account_code=code, currency=row.get("currency") or "GBP", source=source,
                         method="llm", issues=issues)
        result.append(normalise(tx, settings))
    return result
