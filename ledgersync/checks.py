"""Makes a transaction ready to post (VAT split, bank account) and lists what a bookkeeper
should look at. Issues never change amounts; error-level issues block the trial balance."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .accounts import BY_CODE, CAPITAL, CREDITORS, DEBTORS, DIRECTORS_LOAN, DRAWINGS, STAFF_EXPENSES, AccountType
from .models import NOT_TRANSACTIONS, BusinessSettings, Direction, Issue, Transaction
from .money import PENNY, ZERO, VatTreatment, vat_in_gross

_NO_VAT = {AccountType.LIABILITY, AccountType.EQUITY}
# Issues matching.match adds; listed here so validating again replaces them instead of adding copies.
MATCHING_ISSUES = {"choose_payment", "part_payment", "overpayment", "possible_payment", "stale_link",
                   "mixed_link"}
# Issues statements.check_statement adds to bank lines; derived too, so checking again replaces them.
STATEMENT_ISSUES = {"statement_gap", "statement_total"}
_DERIVED = {"unknown_account", "same_account", "non_gbp_currency", "vat_not_applicable", "vat_estimated",
            "vat_arithmetic", "vat_rate_mismatch", "date_missing", "date_out_of_period", "unusual_direction",
            "not_booked", "vat_blocked", "director_loan"} | MATCHING_ISSUES | STATEMENT_ISSUES


def issue(code: str, message: str, severity: str = "warning") -> Issue:
    return Issue(code=code, message=message, severity=severity)


def is_derived(found: Issue) -> bool:
    """An issue the ledger works out afresh on every pass; such issues are never saved."""
    return found.code in _DERIVED


_NOT_TRANSACTION_NAMES = {
    "quote": "a quote", "pro_forma": "a pro forma invoice", "purchase_order": "a purchase order",
    "remittance_advice": "a remittance advice", "supplier_statement": "a supplier's statement of account",
    "other": "a document that is not a transaction",
}


def booked(tx: Transaction) -> bool:
    """A quote, a pro forma or another document that is not a transaction is booked only when a person
    ticks Include."""
    return tx.document_type not in NOT_TRANSACTIONS or tx.include


def other_side(tx: Transaction, settings: BusinessSettings) -> str:
    """The other side of a posting. A receipt or a bank line moved money through the bank. An invoice,
    a claim or an included document is owed until a bank line pays it: a sale to the customer's account
    (Debtors), anything else to the supplier's (Creditors), and a claim to the employee."""
    if tx.document_type == "expense_claim":
        return STAFF_EXPENSES
    if tx.document_type == "invoice" or tx.document_type in NOT_TRANSACTIONS:
        account = BY_CODE.get(tx.account_code)
        return DEBTORS if account is not None and account.type == AccountType.INCOME else CREDITORS
    return settings.bank_account


def _most_vat(gross: Decimal) -> Decimal:
    """The most VAT a VAT-inclusive amount can hold: a sixth (20% of the net), plus a little
    because invoices round VAT line by line."""
    return vat_in_gross(gross, VatTreatment.STANDARD) + Decimal("0.05") + (gross / 500).quantize(PENNY)


def normalise(tx: Transaction, settings: BusinessSettings) -> Transaction:
    kept = [i for i in tx.issues if i.code not in _DERIVED]   # re-validating must not duplicate
    issues: list[Issue] = []
    account = BY_CODE.get(tx.account_code)
    # The bank and the accounts for what is owed are worked out afresh on every pass, so a row whose
    # account or document type changes moves with it; any other account sent in (petty cash, say) is kept.
    worked_out = {None, settings.bank_account, DEBTORS, CREDITORS, STAFF_EXPENSES}
    contra = other_side(tx, settings) if tx.contra_account_code in worked_out else tx.contra_account_code
    if account is None:
        issues.append(issue("unknown_account", f"Account {tx.account_code} is not in the chart of accounts.", "error"))
    if contra not in BY_CODE:
        issues.append(issue("unknown_account", f"Account {contra} is not in the chart of accounts.", "error"))
    if contra == tx.account_code:
        issues.append(issue("same_account", "A transaction cannot post to and from the same account.", "error"))
    if tx.currency != "GBP":
        issues.append(issue("non_gbp_currency", f"{tx.currency} amounts must be converted to GBP first.", "error"))

    # Works from the inputs every time (vat as shown, vat_treatment as chosen) and never writes
    # them back, so a re-coded or corrected row gets its VAT worked out afresh.
    treatment = tx.vat_treatment or (account.vat if account else VatTreatment.OUTSIDE_SCOPE)
    vat = tx.vat
    posted: Optional[Decimal] = vat
    if not settings.vat_registered:
        posted = ZERO
    elif account is not None and account.type in _NO_VAT:
        if vat:
            issues.append(issue("vat_not_applicable", f"VAT does not apply to {account.name}; it was ignored."))
        posted = ZERO
    elif account is not None and not account.reclaim_vat and tx.direction == Direction.OUT:
        # Business entertainment, or a car: the VAT can't be reclaimed, so it stays in the cost.
        if vat:
            issues.append(issue("vat_blocked", f"VAT on {account.name} can't be reclaimed, so the £{vat} stays "
                                                "in the cost.", "info"))
        posted = ZERO
    elif vat is None and tx.vat_treatment is None:
        posted = ZERO   # VAT is reclaimable only when charged: none shown, none booked
    elif vat is None:   # a person chose the rate: split the VAT out of the gross at that rate
        posted = vat_in_gross(tx.gross, treatment)
        if posted > 0:
            issues.append(issue("vat_estimated", f"VAT of £{posted} estimated at the {treatment.value} rate; "
                                                  "check it against the invoice."))
    elif vat < 0 or vat >= tx.gross or vat > _most_vat(tx.gross):
        issues.append(issue("vat_arithmetic", f"VAT £{vat} is not possible on £{tx.gross}: at the 20% rate it "
                                               f"would be £{vat_in_gross(tx.gross, VatTreatment.STANDARD)}.", "error"))
        posted = None
    elif (treatment in (VatTreatment.STANDARD, VatTreatment.REDUCED)
          and abs(vat - vat_in_gross(tx.gross, treatment)) > Decimal("0.02")):
        issues.append(issue("vat_rate_mismatch", f"£{vat} is not {treatment.value}-rate VAT on £{tx.gross}; "
                                                  "mixed rates?", "info"))

    if tx.date is None:
        issues.append(issue("date_missing", "No date found; add the transaction date."))
    elif ((settings.period_start and tx.date < settings.period_start)
          or (settings.period_end and tx.date > settings.period_end)):
        issues.append(issue("date_out_of_period", f"{tx.date} is outside the accounting period."))
    if account is not None and tx.direction == Direction.IN and account.type == AccountType.EXPENSE:
        issues.append(issue("unusual_direction", f"Money in on {account.name} is usually a refund; check it.", "info"))
    if account is not None and tx.direction == Direction.OUT and account.type == AccountType.INCOME:
        issues.append(issue("unusual_direction", f"Money out on {account.name} is usually a customer refund; "
                                                  "check it.", "info"))
    if settings.business_type == "limited_company" and account is not None and tx.account_code in (CAPITAL, DRAWINGS):
        issues.append(issue("director_loan", f"For a limited company, use 2250 Director's Loan Account instead of "
                                             f"{account.name}."))
    elif settings.business_type not in (None, "limited_company") and tx.account_code == DIRECTORS_LOAN:
        issues.append(issue("director_loan", "Director's Loan Account is for limited companies; use 3260 Drawings "
                                             "or 3000 Capital Introduced."))
    if not booked(tx):
        issues.append(issue("not_booked", f"Looks like {_NOT_TRANSACTION_NAMES[tx.document_type]}: not booked. "
                                          "Tick Include to book it as an invoice.", "info"))
    net = tx.gross - posted if posted is not None else None
    return tx.model_copy(update={"vat_posted": posted, "net": net, "contra_account_code": contra,
                                 "issues": kept + issues})
