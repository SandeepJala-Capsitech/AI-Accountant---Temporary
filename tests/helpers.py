"""Small helpers shared by tests: a row with sensible defaults, the figures a test asks about."""
import datetime as dt
from decimal import Decimal

from ledgersync.checks import normalise
from ledgersync.matching import match
from ledgersync.models import BusinessSettings, Transaction

SETTINGS = BusinessSettings(business_name="Acme Ltd", vat_registered=True, business_type="limited_company")


def row(**overrides) -> Transaction:
    """A receipt for £0.00 on 7600 dated 1 Jan 2026, paid out in GBP, with whatever is overridden."""
    fields = dict(date=dt.date(2026, 1, 1), description="x", direction="out", gross=Decimal("0.00"), vat=None,
                  account_code="7600", currency="GBP", document_type="receipt")
    return Transaction(**{**fields, **overrides})


def net(tb, code: str) -> Decimal:
    """Debit minus credit of the trial balance's line for the account; 0 if it has none."""
    return sum((line.debit - line.credit for line in tb.lines if line.code == code), Decimal(0))


def match_one(document: Transaction, line: Transaction) -> Transaction:
    """The bank line after matching it against the document."""
    return match([normalise(document, SETTINGS), normalise(line, SETTINGS)], SETTINGS)[1]


def only_issue(tx: Transaction, code: str):
    """The row's one issue with this code."""
    found = [i for i in tx.issues if i.code == code]
    assert len(found) == 1, f"expected one {code} issue, found {[i.code for i in tx.issues]}"
    return found[0]


def normalise_and_match(rows: list[Transaction], settings: BusinessSettings = SETTINGS) -> list[Transaction]:
    """The rows as the ledger shows them: each one normalised, then matched together."""
    return match([normalise(tx, settings) for tx in rows], settings)


def payroll_row(**overrides) -> dict:
    """One row of a payroll report as the model answers it: net pay out on 7000, dated the pay date."""
    fields = dict(description="Net pay", date="2026-03-28", amount=3000.0, direction="out", account="7000",
                  currency="GBP", document_type="payroll", owed_to="net_pay")
    return {**fields, **overrides}
