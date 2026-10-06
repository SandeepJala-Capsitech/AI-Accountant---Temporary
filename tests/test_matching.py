import pytest

from ledgersync.matching import names_match


@pytest.mark.parametrize("a, b", [
    ("M BARNES EXPENSES", "Matt Barnes"),
    ("BUS CUBE MGMT", "Business Cube Management Solutions Limited"),
    ("FIN: CURRYS ONLINE", "Currys Ltd"),
    ("BUS MGMT SOL", "Business Management Solutions"),   # only abbreviations in common
])
def test_names_that_match(a, b):
    assert names_match(a, b) and names_match(b, a)


@pytest.mark.parametrize("a, b", [
    ("ACME CONSULTING SERVICES LTD", "Northbridge Services Ltd"),   # only generic words in common
    ("HMRC PAYE", "Business Cube Management"),
    ("", "Business Cube"),
    (None, "Business Cube"),
    ("BT", "BT"),                                                   # too short to tell
])
def test_names_that_do_not_match(a, b):
    assert not names_match(a, b)


import datetime as dt
from decimal import Decimal

from ledgersync.checks import normalise
from ledgersync.matching import match
from ledgersync.models import BusinessSettings, Transaction

SETTINGS = BusinessSettings()


def row(kind, direction, gross, date, counterparty, ref, account="7100", **kw):
    return Transaction(document_type=kind, direction=direction, gross=gross, date=dt.date.fromisoformat(date),
                       counterparty=counterparty, document_ref=ref, account_code=account,
                       description=kw.pop("description", f"{counterparty} {kind}"), **kw)


def bill(gross="12000.00", date="2026-10-01", who="Business Cube Management Solutions", ref="bill-oct", **kw):
    return row("invoice", "out", gross, date, who, ref, **kw)


def bank(gross="12000.00", date="2026-10-03", who="BUSINESS CUBE MGMT", ref="line-1", direction="out", **kw):
    return row("statement", direction, gross, date, who, ref, **kw)


def matched(*rows):
    return match([normalise(t, SETTINGS) for t in rows], SETTINGS)


def codes(t):
    return [(i.code, i.severity) for i in t.issues]


def test_a_bank_line_pays_the_one_bill_it_matches():
    paid_bill, line = matched(bill(), bank())
    assert [(p.ref, p.amount) for p in line.pays] == [("bill-oct", Decimal("12000.00"))]
    assert (line.paid_against, line.paid_against_name) == ("2100", "Creditors")
    assert (line.vat_posted, line.net) == (Decimal("0.00"), Decimal("12000.00"))
    assert paid_bill.owed == Decimal("0.00") and [p.ref for p in paid_bill.paid_by] == ["line-1"]
    assert codes(line) == []


def test_an_unpaid_bill_is_still_owed():
    [open_bill] = matched(bill())
    assert open_bill.owed == Decimal("12000.00") and open_bill.paid_by == []


def test_two_bills_that_fit_ask_a_person_to_choose():
    october, later, line = matched(bill(), bill(date="2026-10-10", ref="bill-later"), bank(date="2026-10-20"))
    assert codes(line) == [("choose_payment", "error")] and line.pays == []
    assert [[c.ref for c in option] for option in line.candidates] == [["bill-oct"], ["bill-later"]]
    assert october.owed == later.owed == Decimal("12000.00")


@pytest.mark.parametrize("paid_on", ["2026-09-30", "2026-11-02"])
def test_a_payment_before_the_bill_or_more_than_31_days_after_is_not_matched(paid_on):
    open_bill, line = matched(bill(), bank(date=paid_on))
    assert line.pays == [] and open_bill.owed == Decimal("12000.00")


def test_a_payment_on_the_31st_day_is_matched():
    _, line = matched(bill(), bank(date="2026-11-01"))
    assert [p.ref for p in line.pays] == ["bill-oct"]


def test_undated_rows_are_never_matched():
    _, line = matched(bill(), bank().model_copy(update={"date": None}))
    assert line.pays == [] and line.candidates == []


def test_money_in_does_not_pay_a_bill():
    open_bill, line = matched(bill(), bank(direction="in"))
    assert line.pays == [] and open_bill.owed == Decimal("12000.00")


def test_a_sales_invoice_is_paid_by_money_in():
    invoice = row("invoice", "in", "2400.00", "2026-09-30", "Harbour & Lane Architects LLP", "inv-117", account="4000")
    paid, line = matched(invoice, bank("2400.00", "2026-10-14", "HARBOUR LANE ARCHITECTS", direction="in",
                                       account="4000"))
    assert line.paid_against == "1100" and paid.owed == Decimal("0.00")


def test_a_claim_is_paid_by_its_reimbursement():
    lines = [row("expense_claim", "out", gross, date, "Matt Barnes", "claim-matt", account=account)
             for gross, date, account in (("173.75", "2026-09-14", "7402"), ("12.88", "2026-09-14", "7406"),
                                          ("27.50", "2026-09-30", "7502"))]
    *claim, line = matched(*lines, bank("214.13", "2026-10-05", "M BARNES EXPENSES"))
    assert line.paid_against == "2110" and {t.owed for t in claim} == {Decimal("0.00")}


def test_a_claim_is_dated_by_its_latest_dated_line():
    first = row("expense_claim", "out", "100.00", "2026-09-01", "Jenny Hogg", "claim")
    undated = row("expense_claim", "out", "50.00", "2026-09-20", "Jenny Hogg", "claim").model_copy(update={"date": None})
    *_, late = matched(first, undated, bank("150.00", "2026-10-03", "J HOGG EXPENSES"))
    *_, in_time = matched(first, undated, bank("150.00", "2026-10-02", "J HOGG EXPENSES"))
    assert late.pays == [] and [p.ref for p in in_time.pays] == ["claim"]


def test_unlink_makes_an_ordinary_bank_line():
    open_bill, line = matched(bill(), bank(link=[]))
    assert line.pays == [] and line.paid_against is None and open_bill.owed == Decimal("12000.00")


def test_link_pays_the_document_a_person_chose():
    october, later, line = matched(bill(), bill(date="2026-10-10", ref="bill-later"),
                                   bank(date="2026-10-20", link=["bill-later"]))
    assert [p.ref for p in line.pays] == ["bill-later"] and codes(line) == []
    assert (october.owed, later.owed) == (Decimal("12000.00"), Decimal("0.00"))


def test_a_link_to_a_document_no_longer_in_the_table_is_dropped():
    _, line = matched(bill(), bank(link=["gone"]))
    assert ("stale_link", "warning") in codes(line) and [p.ref for p in line.pays] == ["bill-oct"]


def test_lines_a_person_linked_are_applied_before_automatic_matches():
    # The automatic line comes first by date, but the person said the later line pays the bill.
    _, first, second = matched(bill(), bank(date="2026-10-02", ref="early"),
                               bank(date="2026-10-05", ref="late", link=["bill-oct"]))
    assert first.pays == [] and [p.ref for p in second.pays] == ["bill-oct"]


def test_matching_does_not_depend_on_the_order_of_the_rows():
    rows = [bill(), bill(gross="500.00", ref="bill-small", who="Clearway Office Supplies"),
            bank(), bank("500.00", ref="line-2", who="CLEARWAY OFFICE")]
    forward = {t.document_ref: (t.owed, [p.ref for p in t.pays]) for t in matched(*rows)}
    backward = {t.document_ref: (t.owed, [p.ref for p in t.pays]) for t in matched(*reversed(rows))}
    assert forward == backward


def test_matching_again_gives_the_same_result_without_duplicate_issues():
    once = matched(bill(), bill(date="2026-10-10", ref="bill-later"), bank(date="2026-10-20"))
    again = match([normalise(Transaction.model_validate(t.model_dump(mode="json")), SETTINGS) for t in once],
                  SETTINGS)
    assert again == once


def test_receipts_are_neither_documents_nor_payments():
    receipt, line = matched(row("receipt", "out", "12000.00", "2026-10-01", "Business Cube", "r-1"), bank())
    assert line.pays == [] and receipt.owed is None
