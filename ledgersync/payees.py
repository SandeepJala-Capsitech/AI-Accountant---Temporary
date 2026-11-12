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


# How a card statement words the money paid to the card from the bank.
_CARD_PAYMENT = re.compile(r"\b(payments?|thank ?you|direct debit|repayment|paid in full)\b")


def card_payment(*texts: Optional[str]) -> bool:
    """Whether a credit on a card statement is worded as a payment to the card ("PAYMENT RECEIVED - THANK YOU"),
    not a refund from a shop."""
    return any(_CARD_PAYMENT.search((text or "").lower()) for text in texts)


# How a bank line words money from a customer for work not yet invoiced.
_ON_ACCOUNT = re.compile(r"\b(on account|payment on a/?c|deposit|advance payment|in advance|retainer)\b")


def on_account(*texts: Optional[str]) -> bool:
    """Whether money in is worded as paid ahead of its invoice ("payment on account", "deposit")."""
    return any(_ON_ACCOUNT.search((text or "").lower()) for text in texts)


# How a bank line words a payment of part of a bill.
_PART_PAYMENT = re.compile(r"\b(deposit|part[- ]payment|instal?ments?|on account|payment on a/?c|balance (due|of|on)|"
                           r"final payment|stage payment)\b")


def part_payment(*texts: Optional[str]) -> bool:
    """Whether a payment's own words say it pays part of a bill ("deposit on van", "part payment on invoice 1001")."""
    return any(_PART_PAYMENT.search((text or "").lower()) for text in texts)


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


# The owner's own bills, paid from the business account. A company may have a mortgage or council tax of its own,
# so those two are the owner's only for a business that is not a company.
OWNERS_TAX = {"self assessment": "Self Assessment tax", "hmrc sa": "Self Assessment tax"}
OWNERS_HOME = {"mortgage": "A mortgage", "council tax": "Council tax", "tv licence": "A TV licence",
               "tv licensing": "A TV licence"}


def personal(*texts: Optional[str], business_type: Optional[str] = None) -> Optional[str]:
    """What the payee (or description) names that is the owner's own spending, as messages name it: a consumer
    service; the owner's Self Assessment tax; and, for a business that is not a company, a mortgage or council tax.
    None for anything else."""
    names = {**PERSONAL, **OWNERS_TAX}
    if business_type and business_type != "limited_company":
        names.update(OWNERS_HOME)
    return _found(names, *texts)
