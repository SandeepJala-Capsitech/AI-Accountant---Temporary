from datetime import date
from decimal import Decimal as D

import pytest
from helpers import add_rows, net, row

from ledgersync import books
from ledgersync.errors import InvalidInput, InvalidTransactions, NotFound
from ledgersync.models import OpeningBalances, OpeningLine, OpenItem

pytestmark = pytest.mark.mongo

YEAR_1, YEAR_2 = date(2025, 4, 1), date(2026, 4, 1)


@pytest.fixture
def store(mongo_store):
    return mongo_store


def test_the_second_year_opens_with_the_first_years_closing_and_its_profit_in_retained_earnings(store, acme):
    # acme: limited company, first year end 31 Mar 2026; one sale of £1,200 (VAT £200) paid into the bank in year 1,
    # one £60 stationery receipt in year 2
    add_rows(store, acme, row(date=date(2025, 6, 1), direction="in", gross=D("1200.00"), vat=D("200.00"),
                              account_code="4000", counterparty="Bright Cafe"),
             row(date=date(2026, 5, 1), gross=D("60.00"), account_code="7504"))
    tb = books.trial_balance(store, acme, date(2026,4,1))
    opening = {l.code: l.debit - l.credit for l in tb.opening}
    assert opening == {"1200": D("1200.00"), "2200": D("-200.00"), "3200": D("-1000.00")}
    assert net(tb, "7504") == D("60.00") and net(tb, "1200") == D("1140.00")


def test_a_payment_in_year_one_clears_an_unpaid_bill_brought_forward(store, acme):
    books.set_opening(store, acme, OpeningBalances(
        lines=[OpeningLine(code="1200", debit=D("500.00")), OpeningLine(code="2100", credit=D("500.00"))],
        unpaid=[OpenItem(id="1", direction="out", counterparty="Smith Solicitors Ltd", amount=D("500.00"))]))
    add_rows(store, acme, row(date=date(2025,5,2), counterparty="Smith Solicitors", gross=D("500.00"), document_type="statement"))
    tb = books.trial_balance(store, acme, date(2025,4,1))
    assert net(tb, "2100") == 0 and net(tb, "7600") == 0 and net(tb, "1200") == 0


def test_opening_balances_must_balance_and_agree_with_the_unpaid_invoices(store, acme):
    with pytest.raises(InvalidInput, match="must be the same"):
        books.set_opening(store, acme, OpeningBalances(lines=[OpeningLine(code="1200", debit=D("10.00"))]))
    with pytest.raises(InvalidInput, match="Debtors is £300.00"):
        books.set_opening(store, acme, OpeningBalances(
            lines=[OpeningLine(code="1100", debit=D("300.00")), OpeningLine(code="3000", credit=D("300.00"))],
            unpaid=[OpenItem(id="1", direction="in", counterparty="Bright Cafe", amount=D("200.00"))]))


def test_a_statement_across_the_year_end_is_split_but_checked_whole(store, acme):
    # one statement upload: a line on 30 Mar 2026 and a line on 2 Apr 2026, opening and closing balances that add up
    line = dict(document_type="statement", counterparty="Shop")
    add_rows(store, acme, row(**line, date=date(2026, 3, 30), gross=D("10.00"), opening_balance=D("100.00"),
                              balance=D("90.00")),
             row(**line, date=date(2026, 4, 2), gross=D("20.00"), balance=D("70.00"), closing_balance=D("70.00")))
    year1, year2 = books.ledger(store, acme, date(2025,4,1)), books.ledger(store, acme, date(2026,4,1))
    assert len(year1.transactions) == len(year2.transactions) == 1
    assert year1.uploads[0].statement.status == year2.uploads[0].statement.status == "ok"


def test_an_undated_row_belongs_to_the_year_it_was_added_to(store, acme):
    add_rows(store, acme, row(date=None, gross=D("9.00")), year_start=date(2026,4,1))
    assert not books.ledger(store, acme, date(2025,4,1)).transactions
    [undated] = books.ledger(store, acme, date(2026,4,1)).transactions
    assert any(i.code == "date_missing" for i in undated.issues)


def test_the_years_run_to_the_latest_row_or_today(store, acme):
    assert [y.label for y in books.years_of(store, acme, date(2025, 6, 1))] == ["Year to 31 Mar 2026"]
    assert [y.start for y in books.years_of(store, acme, date(2026, 10, 25))] == [YEAR_1, YEAR_2]
    add_rows(store, acme, row(date=date(2028, 1, 5), gross=D("5.00")))
    assert len(books.years_of(store, acme, date(2026, 10, 25))) == 3


def test_an_unknown_year_is_not_found(store, acme):
    with pytest.raises(NotFound, match="That year was not found."):
        books.ledger(store, acme, date(2025, 4, 2))


def test_a_row_dated_before_the_first_year_shows_in_it_and_says_so(store, acme):
    add_rows(store, acme, row(date=date(2025, 1, 10), gross=D("12.00")))
    [early] = books.ledger(store, acme, YEAR_1).transactions
    assert [i.code for i in early.issues] == ["date_out_of_period"]
    assert net(books.trial_balance(store, acme, YEAR_1), "7600") == D("12.00")


def test_an_invoice_unpaid_at_the_year_end_is_owed_in_its_year_and_paid_in_the_next(store, acme):
    add_rows(store, acme, row(date=date(2026, 3, 1), gross=D("300.00"), document_type="invoice",
                              counterparty="Smith Solicitors", document_ref="bill"),
             row(date=date(2026, 6, 5), gross=D("300.00"), document_type="statement", counterparty="SMITH SOLICITORS"))
    year1, year2 = books.trial_balance(store, acme, YEAR_1), books.trial_balance(store, acme, YEAR_2)
    assert (net(year1, "2100"), net(year1, "7600")) == (D("-300.00"), D("300.00"))
    assert {l.code: l.debit - l.credit for l in year2.opening} == {"2100": D("-300.00"), "3200": D("300.00")}
    assert (net(year2, "2100"), net(year2, "1200"), net(year2, "7600")) == (0, D("-300.00"), 0)
    [line] = books.ledger(store, acme, YEAR_2).transactions
    assert [s.ref for s in line.pays] == ["bill"]


def test_a_sole_traders_profit_is_carried_into_the_owners_capital(store, acme):
    store.update_client(acme, {"business_type": "sole_trader"})
    add_rows(store, acme, row(date=date(2025, 6, 1), gross=D("50.00")))
    opening = books.opening(store, acme, YEAR_2)
    assert opening == {"1200": D("-50.00"), "3100": D("50.00")}


def test_rows_to_fix_in_an_earlier_year_hold_up_the_next_years_trial_balance(store, acme):
    add_rows(store, acme, row(date=date(2025, 6, 1), gross=D("50.00"), account_code="9999"))
    with pytest.raises(InvalidTransactions, match="Fix the earlier years first"):
        books.trial_balance(store, acme, YEAR_2)


@pytest.mark.parametrize("line, message", [
    (OpeningLine(code="4000", credit=D("1.00")),
     "Opening balances take balance sheet accounts only: 4000 Sales is income or a cost."),
    (OpeningLine(code="9999", debit=D("1.00")), "9999 isn't an account in the chart."),
])
def test_opening_balances_take_balance_sheet_accounts_only(store, acme, line, message):
    with pytest.raises(InvalidInput, match=message):
        books.set_opening(store, acme, OpeningBalances(lines=[line]))


def test_the_first_year_opens_with_last_years_retained_earnings(store, acme):
    # Ruling R4: the reserves are balance sheet accounts too.
    opening = OpeningBalances(lines=[OpeningLine(code="1200", debit=D("800.00")),
                                     OpeningLine(code="3200", credit=D("800.00"))])
    books.set_opening(store, acme, opening)
    assert store.opening(acme) == opening
    tb = books.trial_balance(store, acme, YEAR_1)
    assert [(l.code, l.debit, l.credit) for l in tb.opening] == [("1200", D("800.00"), 0), ("3200", 0, D("800.00"))]
    assert tb.is_balanced


def test_unpaid_bills_must_make_up_creditors(store, acme):
    with pytest.raises(InvalidInput, match=r"The unpaid bills from suppliers add up to £0\.00; Creditors is £500\.00\."):
        books.set_opening(store, acme, OpeningBalances(lines=[OpeningLine(code="1200", debit=D("500.00")),
                                                              OpeningLine(code="2100", credit=D("500.00"))]))
