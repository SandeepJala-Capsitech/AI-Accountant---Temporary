"""Money and VAT arithmetic: exact pennies, rounded half up, VAT taken out of gross amounts."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Optional

PENNY = Decimal("0.01")
ZERO = Decimal("0.00")
LIMIT = Decimal("1000000000000")   # £1 trillion: anything bigger is a misread, not an amount
_GROUPED = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")   # 1,234,567.89


class VatTreatment(str, Enum):
    STANDARD = "standard"            # 20%
    REDUCED = "reduced"              # 5%
    ZERO = "zero"                    # 0%: VAT-able, at a nil rate
    EXEMPT = "exempt"
    OUTSIDE_SCOPE = "outside_scope"


_RATES = {VatTreatment.STANDARD: Decimal("0.20"), VatTreatment.REDUCED: Decimal("0.05")}


def to_money(value) -> Optional[Decimal]:
    """Pennies from a number or text such as '£1,234.50'; None when it is not an amount.
    Floats go through str() so 72.1 stays 72.10, and 0.1 + 0.2 becomes 0.30. A comma must
    group thousands: "72,00" (72 in much of Europe) is refused, not read as 7200.00."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).replace("£", "").strip()
    if "," in text:
        if not _GROUPED.fullmatch(text):
            return None
        text = text.replace(",", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite() or abs(amount) >= LIMIT:
        return None
    return amount.quantize(PENNY, rounding=ROUND_HALF_UP)


def vat_in_gross(gross: Decimal, treatment: VatTreatment) -> Decimal:
    """VAT inside a VAT-inclusive amount: gross/6 at 20%, gross/21 at 5%, otherwise none."""
    rate = _RATES.get(treatment)
    if rate is None:
        return ZERO
    return (gross * rate / (1 + rate)).quantize(PENNY, rounding=ROUND_HALF_UP)
