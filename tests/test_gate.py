"""The quality gate: six checks in plain code, so the same ledger passes or fails the same way every time. These
tests build ledgers in memory, with no database."""
import datetime as dt
import itertools
from decimal import Decimal

import pytest

from ledgersync.checks import issue, normalise
from ledgersync.errors import InvalidTransactions
from ledgersync.gate import CHECK_CODES, GateResult, gbp, run_quality_gate
from ledgersync.ledger import settings_for
from ledgersync.matching import match
from ledgersync.models import Client, Ledger, SavedRow, StatementCheck, TrialBalance, Upload
from ledgersync.posting import trial_balance

YEAR = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
CLIENT = Client(id=1, name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke",
                created_at="2025-04-01T00:00:00+00:00", updated_at="2025-04-01T00:00:00+00:00")
SETTINGS = settings_for(CLIENT)
_ids = itertools.count(1)


def clean_row(**changes) -> SavedRow:
    """A receipt for stationery paid from the bank in June 2025, on upload 1: booked, with nothing wrong. Each row
    gets the next row ID unless it is given one."""
    fields = {"date": dt.date(2025, 6, 2), "description": "Paper and pens", "direction": "out", "gross": "24.00",
              "vat": "4.00", "account_code": "7504", "document_type": "receipt", "counterparty": "Rymans",
              "upload_id": 1, **changes}
    fields.setdefault("id", next(_ids))
    return SavedRow(**fields)


CLEAN_ROWS = [clean_row(),
              clean_row(date=dt.date(2025, 9, 15), description="Logo design", direction="in", gross="120.00",
                        vat="20.00", account_code="4000", counterparty="Acme Ltd")]
ROW_WITH_ERROR = clean_row(account_code="1234")              # not in the chart: the trial balance can't be built
SUSPENSE_ROW = clean_row(account_code="9998", vat=None)


def ledger_of(rows, statement=None, *, uploads=None) -> Ledger:
    """The rows as a client's ledger shows them: booked and matched with the client's settings, as ledger.build
    does. They are on one upload, bank.csv, whose statement check is `statement`, unless `uploads` are given."""
    ready = match([normalise(row, SETTINGS) for row in rows], SETTINGS)
    transactions = [SavedRow(**{**tx.model_dump(), "id": row.id, "upload_id": row.upload_id})
                    for row, tx in zip(rows, ready)]
    if uploads is None:
        uploads = [upload(1, "bank.csv", statement)]
    return Ledger(client=CLIENT, uploads=uploads, transactions=transactions)


def upload(upload_id: int, name: str, statement=None) -> Upload:
    return Upload(id=upload_id, name=name, kind="csv", created_at="2026-04-10T09:00:00+00:00", statement=statement)


def balance_of(rows):
    """The rows' trial balance, or the problems that stop it being built, as the gate is given them."""
    try:
        return trial_balance(list(rows), SETTINGS)
    except InvalidTransactions as exc:
        return exc.problems


def failing(result: GateResult) -> dict[str, str]:
    """The checks that failed, by code, with what each says."""
    return {c.code: c.detail for c in result.checks if not c.passed}


def check(result: GateResult, code: str):
    return next(c for c in result.checks if c.code == code)


def unbalanced(debits: str, credits: str) -> TrialBalance:
    return TrialBalance(lines=[], total_debits=Decimal(debits), total_credits=Decimal(credits), is_balanced=False,
                        journal=[])


# --- the brief's cases --------------------------------------------------------------------------------------

def test_a_clean_ledger_passes_all_six_checks():
    result = run_quality_gate(ledger_of(CLEAN_ROWS), balance_of(CLEAN_ROWS), *YEAR)
    assert result.passed and [c.code for c in result.checks] == list(CHECK_CODES)


def test_a_statement_gap_fails_only_statements_reconcile():
    result = run_quality_gate(ledger_of(CLEAN_ROWS, statement=StatementCheck(status="gap", difference=Decimal("6.15"))),
                              balance_of(CLEAN_ROWS), *YEAR)
    assert failing(result) == {"statements_reconcile": "bank.csv doesn't add up by £6.15."}


def test_an_error_issue_fails_no_errors_with_its_row():
    result = run_quality_gate(ledger_of([ROW_WITH_ERROR]), balance_of([ROW_WITH_ERROR]), *YEAR)
    assert check(result, "no_errors").row_ids == [ROW_WITH_ERROR.id]


def test_a_booked_row_in_suspense_fails_no_suspense():
    result = run_quality_gate(ledger_of([SUSPENSE_ROW]), balance_of([SUSPENSE_ROW]), *YEAR)
    assert failing(result) == {"no_suspense": "1 booked row is in 9998 Suspense."}


def test_a_row_dated_after_the_period_fails_dates_in_period():
    late = clean_row(date=dt.date(2026, 4, 2))
    assert failing(run_quality_gate(ledger_of([late]), balance_of([late]), *YEAR)) == {
        "dates_in_period": "1 booked row is dated outside 1 April 2025 to 31 March 2026, or has no date."}


def test_problems_building_the_trial_balance_fail_trial_balance_builds():
    result = run_quality_gate(ledger_of(CLEAN_ROWS), ["Row 3: unknown account 1234"], *YEAR)
    assert check(result, "trial_balance_builds").detail == "Row 3: unknown account 1234"


# --- each check's detail and rows ---------------------------------------------------------------------------

def test_every_result_has_all_six_checks_in_order_and_passes_only_when_all_do():
    result = run_quality_gate(ledger_of([ROW_WITH_ERROR, SUSPENSE_ROW]), ["#1: This account isn't in the chart."],
                              *YEAR)
    assert [c.code for c in result.checks] == list(CHECK_CODES) and not result.passed
    assert CHECK_CODES == ("trial_balance_builds", "balanced", "statements_reconcile", "no_errors", "no_suspense",
                           "dates_in_period")


def test_the_problems_are_joined_with_semicolons():
    problems = ["#1: This account isn't in the chart.", "#4: Convert USD to GBP."]
    result = run_quality_gate(ledger_of(CLEAN_ROWS), problems, *YEAR)
    assert check(result, "trial_balance_builds").detail == ("#1: This account isn't in the chart.; "
                                                             "#4: Convert USD to GBP.")


def test_without_a_trial_balance_balanced_fails_too():
    result = run_quality_gate(ledger_of(CLEAN_ROWS), ["Row 3: unknown account 1234"], *YEAR)
    assert failing(result)["balanced"] == ("The trial balance can't be built, so its debits and credits can't be "
                                           "compared.")


def test_debits_and_credits_that_differ_fail_balanced_with_the_difference():
    result = run_quality_gate(ledger_of(CLEAN_ROWS), unbalanced("1200.00", "1193.85"), *YEAR)
    assert failing(result) == {"balanced": "Debits £1,200.00 and credits £1,193.85 differ by £6.15."}
    assert check(result, "balanced").row_ids == []


def test_the_difference_is_the_same_whichever_side_is_bigger():
    result = run_quality_gate(ledger_of(CLEAN_ROWS), unbalanced("10.00", "1010.50"), *YEAR)
    assert failing(result) == {"balanced": "Debits £10.00 and credits £1,010.50 differ by £1,000.50."}


def test_each_statement_that_doesnt_add_up_is_named_with_its_gap_rows():
    # The bank lines as check_statement leaves them: a gap on a line of the first statement, and on the last line
    # of the third. (Built as they are, since normalise would take off issues the ledger works out.)
    first = clean_row(upload_id=1, document_type="statement", vat=None,
                      issues=[issue("statement_gap", "Balance should be £80.00; the statement shows £73.85.")])
    fine = clean_row(upload_id=2, document_type="statement", gross="30.00", vat=None)
    last = clean_row(upload_id=3, document_type="statement", gross="12.00", vat=None,
                     issues=[issue("statement_total", "Opening £0.00 plus these rows is £12.00; the statement "
                                                      "closes at £1,212.00.")])
    rows = [first, fine, last]
    ledger = Ledger(client=CLIENT, transactions=rows, uploads=[
        upload(1, "march.csv", StatementCheck(status="gap", difference=Decimal("6.15"))),
        upload(2, "april.csv", StatementCheck(status="ok")),
        upload(3, "may.csv", StatementCheck(status="gap", difference=Decimal("1200.00"))),
        upload(4, "receipt.jpg", None)])
    result = run_quality_gate(ledger, balance_of(rows), *YEAR)
    assert failing(result) == {"statements_reconcile": "march.csv doesn't add up by £6.15. "
                                                       "may.csv doesn't add up by £1,200.00."}
    assert check(result, "statements_reconcile").row_ids == [first.id, last.id]


def test_a_statement_with_no_balances_to_compare_passes():
    result = run_quality_gate(ledger_of(CLEAN_ROWS, statement=StatementCheck(status="none")), balance_of(CLEAN_ROWS),
                              *YEAR)
    assert result.passed


def test_rows_with_an_error_are_counted_in_the_plural():
    other = clean_row(currency="USD", gross="36.00", vat="6.00")
    rows = [ROW_WITH_ERROR, other]
    result = run_quality_gate(ledger_of(rows), balance_of(rows), *YEAR)
    assert failing(result)["no_errors"] == "2 rows have an error."
    assert check(result, "no_errors").row_ids == [ROW_WITH_ERROR.id, other.id]


def test_one_row_with_an_error_is_counted_in_the_singular():
    result = run_quality_gate(ledger_of([ROW_WITH_ERROR]), balance_of([ROW_WITH_ERROR]), *YEAR)
    assert failing(result)["no_errors"] == "1 row has an error."


def test_a_warning_is_not_an_error():
    warned = clean_row(issues=[issue("account_not_recognised", "Account unclear. Choose one.")])
    result = run_quality_gate(ledger_of([warned]), balance_of([warned]), *YEAR)
    assert result.passed


def test_booked_rows_in_suspense_are_counted_with_their_rows():
    rows = [SUSPENSE_ROW, clean_row(account_code="9998", vat=None, gross="5.00"), *CLEAN_ROWS]
    result = run_quality_gate(ledger_of(rows), balance_of(rows), *YEAR)
    assert failing(result) == {"no_suspense": "2 booked rows are in 9998 Suspense."}
    assert check(result, "no_suspense").row_ids == [rows[0].id, rows[1].id]


def test_a_bank_line_left_in_suspense_that_pays_a_bill_is_booked_against_the_bill_so_it_passes():
    bill = clean_row(document_type="invoice", document_ref="inv-1", gross="48.00", vat="8.00")
    paid = clean_row(document_type="statement", account_code="9998", vat=None, gross="48.00",
                     date=dt.date(2025, 6, 20), description="RYMANS")
    ledger = ledger_of([bill, paid])
    assert ledger.transactions[1].paid_against == "2100"                     # Creditors: what it pays off
    assert run_quality_gate(ledger, balance_of([bill, paid]), *YEAR).passed


def test_a_row_whose_other_side_is_suspense_fails_no_suspense():
    odd = clean_row(contra_account_code="9998")
    result = run_quality_gate(ledger_of([odd]), balance_of([odd]), *YEAR)
    assert failing(result) == {"no_suspense": "1 booked row is in 9998 Suspense."}


def test_a_row_that_isnt_booked_can_stay_in_suspense_or_outside_the_period():
    quote = clean_row(account_code="9998", vat=None, gross="300.00", document_type="quote", date=dt.date(2024, 1, 5))
    result = run_quality_gate(ledger_of([quote, *CLEAN_ROWS]), balance_of([quote, *CLEAN_ROWS]), *YEAR)
    assert result.passed


def test_a_quote_ticked_to_include_is_booked_and_checked():
    quote = clean_row(account_code="9998", vat=None, document_type="quote", include=True)
    result = run_quality_gate(ledger_of([quote]), balance_of([quote]), *YEAR)
    assert failing(result) == {"no_suspense": "1 booked row is in 9998 Suspense."}


def test_booked_rows_with_no_date_or_dated_outside_the_period_are_counted_with_their_rows():
    early, undated, late = (clean_row(date=dt.date(2025, 3, 31), gross="6.00", vat="1.00"),
                            clean_row(date=None, gross="12.00", vat="2.00"),
                            clean_row(date=dt.date(2026, 4, 1), gross="18.00", vat="3.00"))
    rows = [early, *CLEAN_ROWS, undated, late]
    result = run_quality_gate(ledger_of(rows), balance_of(rows), *YEAR)
    assert failing(result) == {
        "dates_in_period": "3 booked rows are dated outside 1 April 2025 to 31 March 2026, or have no date."}
    assert check(result, "dates_in_period").row_ids == [early.id, undated.id, late.id]


def test_the_first_and_last_days_are_in_the_period():
    rows = [clean_row(date=YEAR[0], gross="6.00", vat="1.00"), clean_row(date=YEAR[1], gross="12.00", vat="2.00")]
    assert run_quality_gate(ledger_of(rows), balance_of(rows), *YEAR).passed


def test_a_check_that_passes_has_no_rows():
    result = run_quality_gate(ledger_of(CLEAN_ROWS), balance_of(CLEAN_ROWS), *YEAR)
    assert all(c.passed and c.row_ids == [] for c in result.checks)


@pytest.mark.parametrize("amount, shown", [("6.15", "£6.15"), ("1200", "£1,200.00"), ("0", "£0.00"),
                                           ("-6.15", "-£6.15"), ("-1234567.8", "-£1,234,567.80")])
def test_money_is_written_in_pounds_with_pence_and_thousands(amount, shown):
    assert gbp(Decimal(amount)) == shown
