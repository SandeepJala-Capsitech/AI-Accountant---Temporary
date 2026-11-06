"""Payees a bookkeeper recognises by name: payment processors, whose payout is sales less their fees and refunds, and
consumer services that are usually personal spending. Kept here, in one place, so the reader's coding, the row checks
and the questions agree."""
from __future__ import annotations

import re
from typing import Optional

# Name as written in a payee or description, in lower case, against the name messages use.
PROCESSORS = {"stripe": "Stripe", "paypal": "PayPal", "square": "Square", "sumup": "SumUp", "zettle": "Zettle",
              "izettle": "Zettle", "shopify": "Shopify", "gocardless": "GoCardless", "amazon": "Amazon",
              "ebay": "eBay", "etsy": "Etsy"}
PERSONAL = {"netflix": "Netflix", "spotify": "Spotify", "disney plus": "Disney+", "disneyplus": "Disney+",
            "now tv": "NOW TV", "nowtv": "NOW TV", "audible": "Audible", "playstation": "PlayStation",
            "xbox": "Xbox", "nintendo": "Nintendo"}


def _padded(text: Optional[str]) -> str:
    """The text's words in lower case, one space apart and padded with one each side: " netflix com "."""
    return f" {' '.join(re.findall(r'[a-z0-9]+', (text or '').lower()))} "


def _found(names: dict[str, str], *texts: Optional[str]) -> Optional[str]:
    for text in texts:
        padded = _padded(text)
        for written, name in names.items():
            if f" {written} " in padded:
                return name
    return None


def processor(*texts: Optional[str]) -> Optional[str]:
    """The payment processor the payee (or description) names, as messages name it; None for anyone else."""
    return _found(PROCESSORS, *texts)


def personal(*texts: Optional[str]) -> Optional[str]:
    """The consumer service the payee (or description) names, as messages name it; None for anyone else."""
    return _found(PERSONAL, *texts)
