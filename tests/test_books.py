from datetime import date
from decimal import Decimal as D

import pytest
from helpers import (acme_two_years, acme_with_a_non_gbp_row, acme_with_statement_missing_a_line,  # noqa: F401 (fixtures)
                     acme_with_unpaid_bill_in_year_one, acme_with_year_one, add_rows, bill_row, change_stationery_receipt_to, first_row_id, net, row,
                     the_bills_payment_dated)

from ledgersync import books, ledger
from ledgersync.errors import InvalidInput, InvalidTransactions, NotFound, YearClosed
from ledgersync.models import BankAccount, ClientPatch, ManualUpload, OpeningBalances, OpeningLine, OpenItem, RowPatch

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


OPENING_BANK = OpeningBalances(lines=[OpeningLine(code="1200", debit=D("1000.00")),
                                      OpeningLine(code="3000", credit=D("1000.00"))])


def statement_across_the_first_year_start(store, acme, **march):
    """A statement with a £100.00 stationery line on 15 Mar 2025, before the first year, and a £50.00 one on 10 Apr."""
    line = dict(document_type="statement", account_code="7504", counterparty="Ryman")
    add_rows(store, acme, row(**line, date=date(2025, 3, 15), gross=D("100.00"), **march),
             row(**line, date=date(2025, 4, 10), gross=D("50.00")))


def test_a_statement_straddling_the_first_year_start_is_not_counted_twice(store, acme):
    books.set_opening(store, acme, OPENING_BANK)
    statement_across_the_first_year_start(store, acme)
    tb = books.trial_balance(store, acme, YEAR_1)
    # the £50.00 line has no document, so it sits in Suspense rather than on its cost account
    assert (net(tb, "1200"), net(tb, "7504"), net(tb, "9998")) == (D("950.00"), D("0.00"), D("50.00"))
    march, _ = books.ledger(store, acme, YEAR_1).transactions
    assert [(i.code, i.message) for i in march.issues] == [("before_first_year", (
        "Dated before the first year: already in the opening balances, so not booked. Tick Include to book it."))]


def test_ticking_include_books_a_row_dated_before_the_first_year(store, acme):
    books.set_opening(store, acme, OPENING_BANK)
    statement_across_the_first_year_start(store, acme, include=True)
    tb = books.trial_balance(store, acme, YEAR_1)
    # neither line has a document, so both sit in Suspense rather than on their cost account
    assert (net(tb, "1200"), net(tb, "7504"), net(tb, "9998")) == (D("850.00"), D("0.00"), D("150.00"))


def test_a_pre_year_invoice_doesnt_duplicate_its_brought_forward_item(store, acme):
    books.set_opening(store, acme, OpeningBalances(
        lines=[OpeningLine(code="1200", debit=D("500.00")), OpeningLine(code="2100", credit=D("500.00"))],
        unpaid=[OpenItem(id="1", direction="out", counterparty="Smith Solicitors Ltd", amount=D("500.00"))]))
    add_rows(store, acme, row(date=date(2025, 3, 1), gross=D("500.00"), document_type="invoice",
                              counterparty="Smith Solicitors Ltd", document_ref="bill"),
             row(date=date(2025, 5, 2), gross=D("500.00"), document_type="statement", counterparty="Smith Solicitors"))
    tb = books.trial_balance(store, acme, YEAR_1)
    assert (net(tb, "2100"), net(tb, "7600"), net(tb, "1200")) == (0, 0, 0)
    _, line = books.ledger(store, acme, YEAR_1).transactions
    assert [s.ref for s in line.pays] == ["ob-1"]


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


# ─── Closing and reopening a year (Task 8) ─────────────────────────────────────

def test_a_closed_year_refuses_changes(store, acme_with_year_one):
    books.close(store, acme_with_year_one, date(2025,4,1))
    with pytest.raises(YearClosed, match="Reopen it"):
        ledger.change_row(store, acme_with_year_one, first_row_id(store, acme_with_year_one), RowPatch(account_code="7502"))


def test_years_close_in_order_and_reopen_from_the_latest(store, acme_two_years):
    with pytest.raises(InvalidInput, match="Close the year to 31 Mar 2026 first"):
        books.close(store, acme_two_years, date(2026,4,1))
    books.close(store, acme_two_years, date(2025,4,1)); books.close(store, acme_two_years, date(2026,4,1))
    with pytest.raises(InvalidInput, match="Reopen the year to 31 Mar 2027 first"):
        books.reopen(store, acme_two_years, date(2025,4,1))


def test_paying_a_closed_years_invoice_leaves_that_year_alone(store, acme_with_unpaid_bill_in_year_one):
    before = books.trial_balance(store, acme_with_unpaid_bill_in_year_one, date(2025,4,1))
    books.close(store, acme_with_unpaid_bill_in_year_one, date(2025,4,1))
    add_rows(store, acme_with_unpaid_bill_in_year_one, the_bills_payment_dated(date(2026,5,10)))
    after = books.ledger(store, acme_with_unpaid_bill_in_year_one, date(2025,4,1))
    assert books.trial_balance(store, acme_with_unpaid_bill_in_year_one, date(2025,4,1)).lines == before.lines
    assert not after.year.changed_since_closed and bill_row(after).owed == 0


def test_reopen_edit_and_close_again_moves_the_next_years_opening(store, acme_two_years):
    # acme_two_years: year 1 holds a £60.00 stationery receipt paid from the bank (no VAT shown); year 2 one row
    def bank_opening():
        tb = books.trial_balance(store, acme_two_years, date(2026,4,1))
        return {l.code: l.debit - l.credit for l in tb.opening}["1200"]
    books.close(store, acme_two_years, date(2025,4,1))
    before = bank_opening()
    books.reopen(store, acme_two_years, date(2025,4,1))
    change_stationery_receipt_to(store, acme_two_years, D("80.00"))
    books.close(store, acme_two_years, date(2025,4,1))
    assert bank_opening() == before - D("20.00")


def test_closing_saves_the_years_balances_and_marks_it_closed(store, acme_with_year_one):
    client = books.close(store, acme_with_year_one, YEAR_1)
    [closed] = client.closed_years
    assert (closed.start, closed.end, closed.closing) == (YEAR_1, date(2026, 3, 31),
                                                          {"1200": D("-60.00"), "7504": D("60.00")})
    assert store.get_client(acme_with_year_one).closed_years == client.closed_years
    year1, year2 = books.ledger(store, acme_with_year_one, YEAR_1).years
    assert (year1.closed, year2.closed) == (True, False)
    assert books.reopen(store, acme_with_year_one, YEAR_1).closed_years == []
    assert not books.ledger(store, acme_with_year_one, YEAR_1).year.closed


def test_a_year_whose_trial_balance_cant_be_built_isnt_closed(store, acme):
    add_rows(store, acme, row(date=date(2025, 6, 1), gross=D("50.00"), account_code="9999"))
    with pytest.raises(InvalidInput, match="The trial balance must balance before the year is closed."):
        books.close(store, acme, YEAR_1)
    assert store.get_client(acme).closed_years == []


def test_a_year_is_closed_or_reopened_once(store, acme_with_year_one):
    with pytest.raises(InvalidInput, match="The year to 31 Mar 2026 is open."):
        books.reopen(store, acme_with_year_one, YEAR_1)
    books.close(store, acme_with_year_one, YEAR_1)
    with pytest.raises(InvalidInput, match="The year to 31 Mar 2026 is already closed."):
        books.close(store, acme_with_year_one, YEAR_1)


def test_the_next_year_opens_from_the_saved_closing(store, acme_two_years):
    books.close(store, acme_two_years, YEAR_1)
    assert books.opening(store, acme_two_years, YEAR_2) == {"1200": D("-60.00"), "3200": D("60.00")}
    # The saved closing stands even when the rows behind it now work out otherwise (written past the guard).
    change = next(r for r in store.rows(acme_two_years) if r.tx.account_code == "7504")
    store.patch_row(acme_two_years, change.id, {"gross": "70.00"})
    assert books.opening(store, acme_two_years, YEAR_2) == {"1200": D("-60.00"), "3200": D("60.00")}
    assert books.ledger(store, acme_two_years, YEAR_1).year.changed_since_closed
    assert not books.ledger(store, acme_two_years, YEAR_2).year.changed_since_closed


def test_every_change_to_a_closed_years_rows_is_refused(store, acme_two_years):
    books.close(store, acme_two_years, YEAR_1)
    receipt = next(r for r in store.rows(acme_two_years) if r.tx.account_code == "7504")
    phone = next(r for r in store.rows(acme_two_years) if r.tx.account_code == "7502")
    refusals = [
        lambda: ledger.remove_row(store, acme_two_years, receipt.id),
        lambda: ledger.remove_upload(store, acme_two_years, receipt.upload_id),
        lambda: ledger.set_upload_bank(store, acme_two_years, receipt.upload_id, "1200"),
        lambda: ledger.add_manual(store, acme_two_years, ManualUpload(transactions=[row(date=date(2025, 7, 1),
                                                                                         gross=D("5.00"))])),
        lambda: ledger.add_manual(store, acme_two_years, ManualUpload(transactions=[row(date=date(2025, 3, 1),
                                                                                         gross=D("5.00"))])),
        # moving a row of an open year into the closed one
        lambda: ledger.change_row(store, acme_two_years, phone.id, RowPatch(date=date(2026, 3, 1))),
        lambda: books.set_opening(store, acme_two_years, OPENING_BANK),
    ]
    for refused in refusals:
        with pytest.raises(YearClosed, match=r"The year to 31 Mar 2026 is closed\. Reopen it to change it\."):
            refused()
    assert len(store.rows(acme_two_years)) == 2 and store.opening(acme_two_years) == OpeningBalances()
    # the open year still takes changes
    ledger.change_row(store, acme_two_years, phone.id, RowPatch(gross=D("30.00")))


def test_the_year_end_cant_change_once_a_year_is_closed(store, acme_with_year_one):
    books.close(store, acme_with_year_one, YEAR_1)
    with pytest.raises(InvalidInput, match="The year end can't change once a year is closed."):
        ledger.change_client(store, acme_with_year_one, ClientPatch(first_year_end=date(2026, 12, 31)))
    assert ledger.change_client(store, acme_with_year_one, ClientPatch(name="Acme Trading Ltd")).name == (
        "Acme Trading Ltd")


def test_vat_registration_and_business_type_cant_change_once_a_year_is_closed(store, acme_with_year_one):
    books.close(store, acme_with_year_one, YEAR_1)
    with pytest.raises(YearClosed, match="Reopen the closed years to change the client's VAT registration: it "
                                         "changes how they were booked."):
        ledger.change_client(store, acme_with_year_one, ClientPatch(vat_registered=False))
    with pytest.raises(YearClosed, match="Reopen the closed years to change the client's business type: it "
                                         "changes how they were booked."):
        ledger.change_client(store, acme_with_year_one, ClientPatch(business_type="sole_trader"))
    same = ledger.change_client(store, acme_with_year_one, ClientPatch(vat_registered=True,
                                                                       business_type="limited_company"))
    assert (same.vat_registered, same.business_type) == (True, "limited_company")
    books.reopen(store, acme_with_year_one, YEAR_1)
    changed = ledger.change_client(store, acme_with_year_one, ClientPatch(vat_registered=False,
                                                                          business_type="sole_trader"))
    assert (changed.vat_registered, changed.business_type) == (False, "sole_trader")


def test_a_bank_account_can_still_be_added_after_closing(store, acme_with_year_one):
    books.close(store, acme_with_year_one, YEAR_1)
    [current] = store.get_client(acme_with_year_one).bank_accounts
    changed = ledger.change_client(store, acme_with_year_one, ClientPatch(bank_accounts=[
        current.model_copy(update={"name": "Barclays current"}), BankAccount(name="Amex card", kind="credit_card")]))
    assert [(a.name, a.kind) for a in changed.bank_accounts] == [("Barclays current", "current"),
                                                                 ("Amex card", "credit_card")]


# ─── Invoices, bank checks and the year's summary (Task 9) ────────────────────

def test_an_invoice_paid_next_year_shows_as_owed_at_the_year_end_and_paid_now(store, acme_with_unpaid_bill_in_year_one):
    add_rows(store, acme_with_unpaid_bill_in_year_one, the_bills_payment_dated(date(2026,5,10)))
    [bill] = books.invoices(store, acme_with_unpaid_bill_in_year_one, date(2026,4,1))
    assert bill.kind == "purchase" and bill.owed == 0 and bill.paid_by[0].date == date(2026,5,10)


def test_the_bank_check_compares_the_books_with_the_statement(store, acme_with_statement_missing_a_line):
    [check] = books.bank_checks(store, acme_with_statement_missing_a_line, date(2025,4,1))
    assert check.difference == D("-25.00") and check.statement_date == date(2025,9,30)


def test_a_blocked_trial_balance_shows_in_the_summary(store, acme_with_a_non_gbp_row):
    s = books.summary(store, acme_with_a_non_gbp_row, date(2025,4,1))
    assert s.trial_balance == "blocked" and "Convert" in s.problems[0]


def test_a_sale_part_paid_in_the_year_shows_what_is_paid_and_owed(store, acme):
    add_rows(store, acme, row(date=date(2025,6,1), direction="in", gross=D("1200.00"), vat=D("200.00"),
                              account_code="4000", document_type="invoice", counterparty="Bright Cafe",
                              document_number="INV-7", document_ref="sale"),
             row(date=date(2025,6,20), direction="in", gross=D("500.00"), document_type="statement",
                 counterparty="BRIGHT CAFE"))
    [sale] = books.invoices(store, acme, YEAR_1)
    assert (sale.kind, sale.counterparty, sale.number, sale.date) == ("sale", "Bright Cafe", "INV-7", date(2025,6,1))
    assert (sale.total, sale.paid, sale.owed, sale.brought_forward) == (D("1200.00"), D("500.00"), D("700.00"), False)
    assert books.summary(store, acme, YEAR_1).unpaid_sales == D("700.00")


def test_an_unpaid_item_brought_forward_is_listed_first_with_its_own_date_and_number(store, acme):
    books.set_opening(store, acme, OpeningBalances(
        lines=[OpeningLine(code="1200", debit=D("500.00")), OpeningLine(code="2100", credit=D("500.00"))],
        unpaid=[OpenItem(id="1", direction="out", counterparty="Smith Solicitors Ltd", amount=D("500.00"),
                         number="S-12", date=date(2025,2,14))]))
    add_rows(store, acme, row(date=date(2025,7,1), gross=D("40.00"), document_type="invoice", counterparty="BT",
                              document_ref="bt"))
    [old, new] = books.invoices(store, acme, YEAR_1)
    assert (old.kind, old.number, old.date, old.owed, old.brought_forward) == ("purchase", "S-12", date(2025,2,14),
                                                                              D("500.00"), True)
    assert (new.counterparty, new.brought_forward) == ("BT", False)


def test_a_bill_paid_before_the_year_started_is_not_listed_in_it(store, acme_with_unpaid_bill_in_year_one):
    add_rows(store, acme_with_unpaid_bill_in_year_one, the_bills_payment_dated(date(2026,3,20)))
    assert books.invoices(store, acme_with_unpaid_bill_in_year_one, YEAR_2) == []
    [bill] = books.invoices(store, acme_with_unpaid_bill_in_year_one, YEAR_1)
    assert (bill.owed, bill.paid) == (0, D("300.00"))


def test_a_bank_account_without_a_statement_shows_its_books_at_the_year_end(store, acme_with_year_one):
    [check] = books.bank_checks(store, acme_with_year_one, YEAR_1)
    assert (check.code, check.books, check.statement, check.statement_date, check.difference) == (
        "1200", D("-60.00"), None, None, None)


def test_a_credit_card_is_shown_as_what_is_owed_on_it(store, acme):
    ledger.change_client(store, acme, ClientPatch(bank_accounts=[
        *store.get_client(acme).bank_accounts, BankAccount(name="Amex card", kind="credit_card")]))
    card = store.get_client(acme).bank_accounts[1].code
    line = dict(document_type="statement", bank_account=card, account_code="7504", counterparty="Paper Shop")
    add_rows(store, acme, row(**line, date=date(2025,8,1), gross=D("80.00"), opening_balance=D("0.00"),
                              balance=D("80.00")),
             row(**line, date=date(2025,8,9), gross=D("20.00"), balance=D("100.00"), closing_balance=D("100.00")),
             bank_account=card)
    current, amex = books.bank_checks(store, acme, YEAR_1)
    assert (current.books, current.statement) == (0, None)
    assert (amex.books, amex.statement, amex.difference) == (D("100.00"), D("100.00"), D("0.00"))


def test_the_summary_counts_the_rows_to_review_and_checks_the_banks(store, acme_with_statement_missing_a_line):
    s = books.summary(store, acme_with_statement_missing_a_line, YEAR_1)
    assert (s.rows, s.trial_balance, s.problems) == (2, "balanced", [])
    assert s.to_review == 1 and s.bank[0].difference == D("-25.00")
    assert not s.year.closed


def test_a_blocked_year_has_no_bank_checks_and_the_bank_route_refuses(store, acme_with_a_non_gbp_row):
    assert books.summary(store, acme_with_a_non_gbp_row, YEAR_1).bank == []
    with pytest.raises(InvalidTransactions):
        books.bank_checks(store, acme_with_a_non_gbp_row, YEAR_1)


def test_an_unknown_year_is_not_found_for_each_view(store, acme):
    for view in (books.invoices, books.bank_checks, books.summary):
        with pytest.raises(NotFound, match="That year was not found."):
            view(store, acme, date(2025, 4, 2))


def test_a_years_summary_shows_what_was_owed_at_its_end(store, acme_with_unpaid_bill_in_year_one):
    add_rows(store, acme_with_unpaid_bill_in_year_one, the_bills_payment_dated(date(2026,5,10)))
    assert books.summary(store, acme_with_unpaid_bill_in_year_one, YEAR_1).unpaid_purchases == D("300.00")
    [bill] = books.invoices(store, acme_with_unpaid_bill_in_year_one, YEAR_2)
    assert bill.owed == 0
    assert books.summary(store, acme_with_unpaid_bill_in_year_one, YEAR_2).unpaid_purchases == 0


def test_an_undated_manual_row_goes_in_the_year_it_was_added_to(store, acme_two_years):
    # "today" is in year 2 (the latest year), but the row is added while viewing year 1
    ledger.add_manual(store, acme_two_years, ManualUpload(year_start=YEAR_1, transactions=[row(date=None, gross=D("7.00"))]))
    in_year_1 = books.ledger(store, acme_two_years, YEAR_1)
    in_year_2 = books.ledger(store, acme_two_years, YEAR_2)
    assert [t.gross for t in in_year_1.transactions if t.date is None] == [D("7.00")]
    assert not [t for t in in_year_2.transactions if t.date is None]


def test_an_undated_manual_row_is_allowed_in_an_open_year_and_refused_in_a_closed_one(store, acme_two_years):
    books.close(store, acme_two_years, YEAR_1)
    undated = [row(date=None, gross=D("7.00"))]
    with pytest.raises(YearClosed, match=r"The year to 31 Mar 2026 is closed"):
        ledger.add_manual(store, acme_two_years, ManualUpload(year_start=YEAR_1, transactions=undated))
    added = ledger.add_manual(store, acme_two_years, ManualUpload(year_start=YEAR_2, transactions=undated))
    assert added.year.start == YEAR_2 and [t.gross for t in added.transactions if t.date is None] == [D("7.00")]


def test_a_manual_row_for_an_unknown_year_is_not_found(store, acme_two_years):
    with pytest.raises(NotFound, match="That year was not found."):
        ledger.add_manual(store, acme_two_years, ManualUpload(year_start=date(2020, 4, 1), transactions=[row(gross=D("5.00"))]))


def test_a_purchase_of_50_or_more_without_its_document_is_held_in_suspense(mongo_store, acme_with_year_one):
    add_rows(mongo_store, acme_with_year_one, row(date=date(2025, 7, 1), gross=D("240.00"), account_code="7400",
             document_type="statement", counterparty="Trainline", description="TRAINLINE"), year_start=date(2025, 4, 1))
    tb = books.trial_balance(mongo_store, acme_with_year_one, date(2025, 4, 1))
    assert net(tb, "9998") == D("240.00") and net(tb, "7400") == 0


def test_a_purchase_under_50_without_a_document_is_a_cost_with_no_vat(mongo_store, acme_with_year_one):
    add_rows(mongo_store, acme_with_year_one, row(date=date(2025, 7, 1), gross=D("38.50"), vat=D("6.42"),
             account_code="7300", document_type="statement", description="SHELL"), year_start=date(2025, 4, 1))
    tb = books.trial_balance(mongo_store, acme_with_year_one, date(2025, 4, 1))
    assert net(tb, "7300") == D("38.50") and net(tb, "2201") == 0


def test_the_rows_trial_balance_equals_the_posted_one(mongo_store, mongo_db, acme_with_year_one):
    from ledgersync import ledger_book
    from ledgersync.records import AgentRecords
    add_rows(mongo_store, acme_with_year_one,
             row(date=date(2025, 7, 1), gross=D("240.00"), account_code="7400", document_type="statement",
                 counterparty="Trainline", description="TRAINLINE"),
             row(date=date(2025, 7, 2), gross=D("38.50"), vat=D("6.42"), account_code="7300",
                 document_type="statement", description="SHELL"), year_start=date(2025, 4, 1))
    records = AgentRecords(mongo_db)
    ledger_book.post(mongo_store, records, acme_with_year_one, date(2025, 4, 1))
    rows = books.trial_balance(mongo_store, acme_with_year_one, date(2025, 4, 1))
    posted = ledger_book.posted_trial_balance(mongo_store, records, acme_with_year_one, date(2025, 4, 1))
    lines = lambda tb: sorted((l.code, l.debit, l.credit) for l in tb.lines)
    assert lines(rows) and lines(rows) == lines(posted)
