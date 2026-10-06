import datetime as dt
from decimal import Decimal

import pytest

from ledgersync.adapter import parse_date, to_transactions
from ledgersync.models import BusinessSettings
from ledgersync.posting import trial_balance

ROW = {"description": "BT Business Broadband", "date": "2026-09-01", "amount": 72.0, "direction": "out",
       "account": "7502 Telephone and Internet", "vat": None, "currency": "GBP"}


def adapt(*rows):
    return to_transactions(list(rows), source="table", settings=BusinessSettings())


def issues_of(t):
    return [(i.code, i.severity) for i in t.issues]


def test_model_rows_become_ledger_transactions():
    [t] = adapt(ROW)
    assert (t.direction.value, t.gross, t.account_code, t.date) == ("out", Decimal("72.00"), "7502", dt.date(2026, 9, 1))
    assert (t.vat_posted, t.source, t.method) == (Decimal("0.00"), "table", "llm")   # no VAT shown, none booked


def test_rows_that_do_not_add_up_to_the_document_total_are_flagged():
    # The note said Breakfast £20 and Travelling charges £50, but "Total £60".
    rows = [dict(ROW, description="Breakfast", amount=20.0, account="7406 Subsistence", document_total=60.0),
            dict(ROW, description="Travelling charges", amount=50.0, account="7400 Travel", document_total=60.0)]
    for t in adapt(*rows):
        [mismatch] = [i for i in t.issues if i.code == "total_mismatch"]
        assert mismatch.severity == "warning" and "70.00" in mismatch.message and "60.00" in mismatch.message


def test_rows_that_add_up_to_the_document_total_are_not_flagged():
    rows = [dict(ROW, amount=20.0, document_total=70.0), dict(ROW, amount=50.0, document_total=70.0)]
    assert not [i for t in adapt(*rows) for i in t.issues if i.code == "total_mismatch"]


def test_statement_rows_without_a_document_total_are_not_checked():
    assert not [i for t in adapt(ROW, dict(ROW, amount=10.0)) for i in t.issues if i.code == "total_mismatch"]


def test_a_split_that_loses_the_printed_vat_is_put_back_together():
    # WHSmith: coffee 3.00 + notebook 9.00, one VAT total of 2.00. The model split it as 11.00 and
    # 1.00 with no VAT: the VAT could not be divided, so the receipt stays one row, as printed.
    doc = {"document_total": 12.0, "document_vat": 2.0, "date": "2026-09-15"}
    rows = [dict(ROW, **doc, description="Whsmith for A4 notebook", amount=11.0, account="7504 Office Stationery"),
            dict(ROW, **doc, description="Whsmith for Coffee to go", amount=1.0, vat=0.0, account="7406 Subsistence")]
    [t] = adapt(*rows)
    assert (t.gross, t.vat, t.account_code, t.description) == (
        Decimal("12.00"), Decimal("2.00"), "7504", "Whsmith for A4 notebook")
    assert ("mixed_items", "warning") in issues_of(t) and "total_mismatch" not in [c for c, _ in issues_of(t)]


def test_a_split_that_carries_the_printed_vat_stays_split():
    # Sainsbury's: groceries 3.75 (VAT 0.00) and cleaning supplies 4.20 (VAT 0.70); the VAT summary says 0.70.
    doc = {"document_total": 7.95, "document_vat": 0.70}
    rows = [dict(ROW, **doc, amount=3.75, vat=0.0, account="8205 Refreshments"),
            dict(ROW, **doc, amount=4.20, vat=0.70, account="7801 Cleaning")]
    assert [(t.gross, t.vat, t.account_code) for t in adapt(*rows)] == [
        (Decimal("3.75"), Decimal("0.00"), "8205"), (Decimal("4.20"), Decimal("0.70"), "7801")]


def test_a_split_of_a_document_without_vat_stays_split():
    doc = {"document_total": 50.0, "document_vat": None}
    rows = [dict(ROW, **doc, amount=18.0, account="7406 Subsistence"),
            dict(ROW, **doc, amount=32.0, account="7400 Travel")]
    assert [t.account_code for t in adapt(*rows)] == ["7406", "7400"]


def test_a_receipt_kept_whole_because_of_mixed_items_is_flagged():
    [t] = adapt(dict(ROW, document_total=72.0, mixed_items=True))
    assert ("mixed_items", "warning") in issues_of(t)


def test_vat_printed_on_the_document_is_booked_as_printed():
    [t] = adapt({**ROW, "vat": 10.0})
    assert (t.vat, t.vat_posted, t.net) == (Decimal("10.00"), Decimal("10.00"), Decimal("62.00"))
    assert "vat_estimated" not in [code for code, _ in issues_of(t)]


def test_money_in_stays_money_in():
    [t] = adapt({**ROW, "direction": "in", "account": "4000 Sales"})
    assert (t.direction.value, t.account_code) == ("in", "4000")


def test_negative_amounts_become_money_out():
    # Baseline: bank rows the model kept negative were dropped, so bank exports scored 20%.
    [t] = adapt({**ROW, "amount": -72.0, "direction": "in", "vat": -12.0})
    assert (t.direction.value, t.gross, t.vat) == ("out", Decimal("72.00"), Decimal("12.00"))
    assert "direction_conflict" in [code for code, _ in issues_of(t)]


@pytest.mark.parametrize("account", ["9999 Nonsense", "1200 Bank Current Account", "", None])
def test_accounts_that_cannot_be_posted_to_go_to_suspense(account):
    [t] = adapt({**ROW, "account": account})
    assert t.account_code == "9998" and ("account_not_recognised", "warning") in issues_of(t)
    assert not [i for i in t.issues if i.severity == "error"]


def test_a_suspense_choice_is_flagged_for_review():
    [t] = adapt({**ROW, "account": "9998 Suspense"})
    assert t.account_code == "9998" and ("account_not_recognised", "warning") in issues_of(t)


def test_zero_or_unreadable_amounts_are_skipped():
    assert adapt({**ROW, "amount": 0}, {**ROW, "amount": None}) == []


def test_uk_dates_are_read_day_first():
    assert parse_date("03/09/2026") == dt.date(2026, 9, 3)
    assert parse_date("2026-09-03 00:00:00") == dt.date(2026, 9, 3)
    assert parse_date("sometime") is None


def test_one_absurd_amount_does_not_sink_the_other_rows():
    # A model row of 1e26 used to raise inside to_money and fail the whole analysis job.
    assert [t.gross for t in adapt({**ROW, "amount": 1e26}, ROW)] == [Decimal("72.00")]


def test_negative_vat_printed_on_a_credit_note_is_still_vat():
    # The prompt asks for a positive amount but the VAT "as printed": a refund prints "VAT -2.00".
    [t] = adapt({**ROW, "amount": 12.0, "direction": "in", "vat": -2.0})
    assert (t.vat, t.vat_posted) == (Decimal("2.00"), Decimal("2.00"))
    assert not [i for i in t.issues if i.severity == "error"]
    assert trial_balance([t], BusinessSettings()).is_balanced


def test_rows_carry_the_document_type_and_counterparty():
    [t] = adapt(dict(ROW, document_type="invoice", counterparty=" BT plc "))
    assert (t.document_type, t.counterparty, t.contra_account_code) == ("invoice", "BT plc", "2100")


def test_an_unexpected_document_type_is_left_for_a_person():
    [t] = adapt(dict(ROW, document_type="delivery note"))
    assert t.document_type == "other" and ("not_booked", "info") in issues_of(t)


def test_rows_of_one_invoice_share_a_reference_and_bank_lines_do_not():
    doc = {"document_total": 60.0, "document_type": "invoice", "counterparty": "Hilton", "date": "2026-09-01"}
    bill = adapt(dict(ROW, **doc, amount=20.0, account="7406 Subsistence"),
                 dict(ROW, **doc, amount=40.0, account="7400 Travel"))
    lines = adapt(dict(ROW, document_type="statement"), dict(ROW, document_type="statement"))
    assert bill[0].document_ref == bill[1].document_ref and lines[0].document_ref != lines[1].document_ref


def test_two_invoices_with_the_same_total_on_different_dates_are_two_documents():
    doc = {"document_total": 12000.0, "document_type": "invoice", "counterparty": "Business Cube"}
    first, second = adapt(dict(ROW, **doc, amount=12000.0, date="2026-10-01"),
                          dict(ROW, **doc, amount=12000.0, date="2026-11-01"))
    assert first.document_ref != second.document_ref
    assert "total_mismatch" not in [code for t in (first, second) for code, _ in issues_of(t)]


def test_a_claims_lines_share_one_reference_whatever_their_dates():
    claim = {"document_total": 50.0, "document_type": "expense_claim", "counterparty": "Matt Barnes"}
    lines = adapt(dict(ROW, **claim, amount=18.0, date="2026-09-12"),
                  dict(ROW, **claim, amount=32.0, date="2026-09-14"))
    assert lines[0].document_ref is not None and lines[0].document_ref == lines[1].document_ref
