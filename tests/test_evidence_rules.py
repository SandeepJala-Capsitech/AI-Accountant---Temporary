"""The document rules: which rows book from what they have, which wait in Suspense for a receipt or invoice, and
when a receipt's VAT is reclaimed."""
from decimal import Decimal as D

import pytest

from helpers import SETTINGS, normalise_and_match, row
from ledgersync.evidence_rules import (NO_DOCUMENT_LIMIT, SIMPLIFIED_LIMIT, DocumentRule, document_rule,
                                       journal_with_rule)
from ledgersync.models import Transaction


def ready(**overrides) -> Transaction:
    """The row after normalise and match, as the ledger hands it to the rules."""
    return normalise_and_match([row(**overrides)])[0]


def statement_line(gross: str, account: str = "7500", direction: str = "out") -> Transaction:
    """A bank line with no document behind it."""
    return ready(document_type="statement", gross=D(gross), account_code=account, direction=direction)


def receipt(gross: str, vat: str = None, vat_no: str = None, account: str = "7400", **overrides) -> Transaction:
    """A receipt, with the VAT it shows and the supplier's VAT number if printed."""
    return ready(document_type="receipt", gross=D(gross), vat=D(vat) if vat else None, account_code=account,
                 supplier_vat_number=vat_no, **overrides)


def verdict(rule: DocumentRule) -> tuple:
    return rule.action, rule.reclaim_vat, rule.needs


def test_the_limits_are_those_the_accountant_chose():
    assert (NO_DOCUMENT_LIMIT, SIMPLIFIED_LIMIT) == (D("50.00"), D("250.00"))


@pytest.mark.parametrize("kind,gross,vat_no,expected", [
    ("statement", "49.99", None, ("book", False, None)),
    ("statement", "50.00", None, ("hold", False, "receipt_or_invoice")),
    ("expense_claim", "84.20", None, ("hold", False, "receipt_or_invoice")),
    ("expense_claim", "20.00", None, ("book", False, None)),
    ("receipt", "120.00", "GB123456789", ("book", True, None)),
    ("receipt", "120.00", None, ("book", False, None)),
    ("receipt", "250.00", "GB123456789", ("book", True, None)),
    ("receipt", "250.01", "GB123456789", ("hold", False, "vat_invoice")),
    ("invoice", "300.00", None, ("book", True, None)),
])
def test_document_rule(kind, gross, vat_no, expected):
    # Every row shows VAT of a fifth of its gross, as a receipt or invoice would for a standard-rate purchase.
    vat = str((D(gross) / 6).quantize(D("0.01"))) if kind in ("receipt", "invoice") else None
    tx = ready(document_type=kind, gross=D(gross), vat=D(vat) if vat else None, account_code="7400",
               supplier_vat_number=vat_no)
    assert verdict(document_rule(tx, SETTINGS)) == expected


def test_a_receipt_showing_no_vat_reclaims_none():
    assert document_rule(receipt("120.00", vat=None, vat_no="GB123456789"), SETTINGS).reclaim_vat is False


def test_a_document_that_is_not_a_vat_invoice_reclaims_nothing():
    rule = document_rule(receipt("120.00", vat="20.00", vat_no="GB123456789", not_vat_invoice=True), SETTINGS)
    assert verdict(rule) == ("book", False, None)
    invoice = ready(document_type="invoice", gross=D("300.00"), vat=D("50.00"), not_vat_invoice=True)
    assert document_rule(invoice, SETTINGS).reclaim_vat is False


@pytest.mark.parametrize("gross,needs", [("250.00", "receipt_or_invoice"), ("250.01", "vat_invoice"),
                                         ("300.00", "vat_invoice")])
def test_a_bank_line_over_250_with_no_document_needs_a_vat_invoice(gross, needs):
    assert verdict(document_rule(statement_line(gross), SETTINGS)) == ("hold", False, needs)


def test_an_asset_purchase_is_covered():
    assert document_rule(statement_line("500.00", "0030"), SETTINGS).action == "hold"


@pytest.mark.parametrize("account", ["0030", "0040", "0050", "0055"])   # equipment, furniture, cars, vans
def test_a_fixed_asset_bought_without_a_document_is_held(account):
    assert verdict(document_rule(statement_line("100.00", account), SETTINGS)) == ("hold", False, "receipt_or_invoice")


@pytest.mark.parametrize("account", ["1100", "1103", "0031"])   # debtors, prepayments, depreciation
def test_other_asset_accounts_need_no_document(account):
    assert verdict(document_rule(statement_line("100.00", account), SETTINGS)) == ("book", True, None)


@pytest.mark.parametrize("account", ["2210", "2202", "2250", "2300", "1240"])   # PAYE, VAT, DLA, loan, card pay-off
def test_exempt_accounts_need_no_document(account):
    rule = document_rule(statement_line("500.00", account), SETTINGS)
    assert verdict(rule) == ("book", True, None)


def test_a_transfer_between_the_clients_banks_needs_no_document():
    assert document_rule(statement_line("500.00", "1210"), SETTINGS).action == "book"


def test_money_in_needs_no_document():
    assert document_rule(statement_line("500.00", "4000", direction="in"), SETTINGS).action == "book"
    assert document_rule(statement_line("500.00", "7500", direction="in"), SETTINGS).action == "book"   # a refund


def test_a_payment_of_a_document_needs_no_further_document():
    paying = ready(document_type="statement", gross=D("500.00"), account_code="7400")
    paying = paying.model_copy(update={"paid_against": "2100"})
    assert document_rule(paying, SETTINGS).action == "book"


def test_reasons_are_one_plain_sentence_without_amounts():
    for tx in (statement_line("500.00"), statement_line("20.00"), receipt("300.00"), receipt("60.00"),
               receipt("60.00", vat_no="GB123456789")):
        reason = document_rule(tx, SETTINGS).reason
        assert reason.endswith(".") and "£" not in reason and "\n" not in reason


def test_held_row_posts_gross_to_suspense():
    lines = journal_with_rule(statement_line("240.00", "7500"), 0,
                              DocumentRule(action="hold", reclaim_vat=False, needs="receipt_or_invoice", reason="x"))
    assert [(l.code, l.debit, l.credit) for l in lines] == [("9998", D("240.00"), 0), ("1200", 0, D("240.00"))]


def test_a_held_row_that_has_vat_still_posts_no_vat_leg():
    tx = receipt("300.00", vat="50.00", vat_no="GB123456789")
    lines = journal_with_rule(tx, 3, document_rule(tx, SETTINGS))
    assert [(l.transaction, l.code, l.debit, l.credit) for l in lines] == [
        (3, "9998", D("300.00"), 0), (3, "1200", 0, D("300.00"))]


def test_a_held_refund_reverses_the_legs():
    tx = statement_line("240.00", "7500", direction="in")
    lines = journal_with_rule(tx, 0, DocumentRule(action="hold", reclaim_vat=False, needs="receipt_or_invoice",
                                                  reason="x"))
    assert [(l.code, l.debit, l.credit) for l in lines] == [("9998", 0, D("240.00")), ("1200", D("240.00"), 0)]


def test_reclaimed_vat_posts_as_the_row_does():
    tx = receipt("120.00", vat="20.00", vat_no="GB123456789")
    lines = journal_with_rule(tx, 0, document_rule(tx, SETTINGS))
    assert [(l.code, l.debit, l.credit) for l in lines] == [
        ("7400", D("100.00"), 0), ("2201", D("20.00"), 0), ("1200", 0, D("120.00"))]


def test_no_reclaim_puts_vat_in_cost():
    tx = receipt("60.00", vat="10.00", vat_no=None)
    assert tx.vat_posted == D("10.00")   # normalise would book it; the rule keeps it in the cost
    before = tx.model_copy(deep=True)
    lines = journal_with_rule(tx, 0, document_rule(tx, SETTINGS))
    assert {l.code for l in lines} == {"7400", "1200"} and sum(l.debit for l in lines) == D("60.00")
    assert [(l.code, l.debit, l.credit) for l in lines] == [("7400", D("60.00"), 0), ("1200", 0, D("60.00"))]
    assert tx == before   # the row itself is not changed


def test_no_reclaim_on_a_refund_credits_the_whole_gross_to_the_account():
    tx = receipt("60.00", vat="10.00", direction="in")
    lines = journal_with_rule(tx, 0, DocumentRule(action="book", reclaim_vat=False, reason="x"))
    assert [(l.code, l.debit, l.credit) for l in lines] == [("7400", 0, D("60.00")), ("1200", D("60.00"), 0)]


def test_every_journal_balances():
    rows = [statement_line("20.00"), statement_line("500.00"), receipt("60.00", vat="10.00"),
            receipt("300.00", vat="50.00", vat_no="GB123456789"), receipt("90.00", vat="15.00", vat_no="GB123456789")]
    for tx in rows:
        lines = journal_with_rule(tx, 0, document_rule(tx, SETTINGS))
        assert sum(l.debit for l in lines) == sum(l.credit for l in lines)
