"""Makes a transaction ready to post (VAT split, bank account) and lists what a bookkeeper
should look at. Issues never change amounts; error-level issues block the trial balance."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .accounts import BY_CODE, AccountType
from .models import BusinessSettings, Direction, Issue, Transaction
from .money import PENNY, ZERO, VatTreatment, vat_in_gross

_NO_VAT = {AccountType.LIABILITY, AccountType.EQUITY}
_DERIVED = {"unknown_account", "same_account", "non_gbp_currency", "vat_not_applicable", "vat_estimated",
            "vat_arithmetic", "vat_rate_mismatch", "date_missing", "date_out_of_period", "unusual_direction"}


def _issue(code: str, message: str, severity: str = "warning") -> Issue:
    return Issue(code=code, message=message, severity=severity)


def _most_vat(gross: Decimal) -> Decimal:
    """The most VAT a VAT-inclusive amount can hold: a sixth (20% of the net), plus a little
    because invoices round VAT line by line."""
    return vat_in_gross(gross, VatTreatment.STANDARD) + Decimal("0.05") + (gross / 500).quantize(PENNY)


def normalise(tx: Transaction, settings: BusinessSettings) -> Transaction:
    kept = [i for i in tx.issues if i.code not in _DERIVED]   # re-validating must not duplicate
    issues: list[Issue] = []
    account = BY_CODE.get(tx.account_code)
    contra = tx.contra_account_code or settings.bank_account
    if account is None:
        issues.append(_issue("unknown_account", f"Account {tx.account_code} is not in the chart of accounts.", "error"))
    if contra not in BY_CODE:
        issues.append(_issue("unknown_account", f"Account {contra} is not in the chart of accounts.", "error"))
    if contra == tx.account_code:
        issues.append(_issue("same_account", "A transaction cannot post to and from the same account.", "error"))
    if tx.currency != "GBP":
        issues.append(_issue("non_gbp_currency", f"{tx.currency} amounts must be converted to GBP first.", "error"))

    # Works from the inputs every time (vat as shown, vat_treatment as chosen) and never writes
    # them back, so a re-coded or corrected row gets its VAT worked out afresh.
    treatment = tx.vat_treatment or (account.vat if account else VatTreatment.OUTSIDE_SCOPE)
    vat = tx.vat
    posted: Optional[Decimal] = vat
    if not settings.vat_registered:
        posted = ZERO
    elif account is not None and account.type in _NO_VAT:
        if vat:
            issues.append(_issue("vat_not_applicable", f"VAT does not apply to {account.name}; it was ignored."))
        posted = ZERO
    elif vat is None and tx.vat_treatment is None:
        posted = ZERO   # VAT is reclaimable only when charged: none shown, none booked
    elif vat is None:   # a person chose the rate: split the VAT out of the gross at that rate
        posted = vat_in_gross(tx.gross, treatment)
        if posted > 0:
            issues.append(_issue("vat_estimated", f"VAT of £{posted} estimated at the {treatment.value} rate; "
                                                  "check it against the invoice."))
    elif vat < 0 or vat >= tx.gross or vat > _most_vat(tx.gross):
        issues.append(_issue("vat_arithmetic", f"VAT £{vat} is not possible on £{tx.gross}: at the 20% rate it "
                                               f"would be £{vat_in_gross(tx.gross, VatTreatment.STANDARD)}.", "error"))
        posted = None
    elif (treatment in (VatTreatment.STANDARD, VatTreatment.REDUCED)
          and abs(vat - vat_in_gross(tx.gross, treatment)) > Decimal("0.02")):
        issues.append(_issue("vat_rate_mismatch", f"£{vat} is not {treatment.value}-rate VAT on £{tx.gross}; "
                                                  "mixed rates?", "info"))

    if tx.date is None:
        issues.append(_issue("date_missing", "No date found; add the transaction date."))
    elif ((settings.period_start and tx.date < settings.period_start)
          or (settings.period_end and tx.date > settings.period_end)):
        issues.append(_issue("date_out_of_period", f"{tx.date} is outside the accounting period."))
    if account is not None and tx.direction == Direction.IN and account.type == AccountType.EXPENSE:
        issues.append(_issue("unusual_direction", f"Money in on {account.name} is usually a refund; check it.", "info"))
    if account is not None and tx.direction == Direction.OUT and account.type == AccountType.INCOME:
        issues.append(_issue("unusual_direction", f"Money out on {account.name} is usually a customer refund; "
                                                  "check it.", "info"))
    net = tx.gross - posted if posted is not None else None
    return tx.model_copy(update={"vat_posted": posted, "net": net, "contra_account_code": contra,
                                 "issues": kept + issues})
