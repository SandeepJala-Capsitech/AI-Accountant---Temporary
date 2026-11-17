import datetime as dt
from decimal import Decimal

from ledgersync.checks import normalise
from ledgersync.materiality import levels, year_levels
from ledgersync.models import BusinessSettings, Transaction, Year

D = Decimal


def test_a_profitable_client_takes_5_percent_of_profit():
    got = levels(D("100000"), D("70000"), which="last year")
    assert (got.overall, got.working, got.trivial, got.benchmark) == (D("1500.00"), D("750.00"), D("45.00"), "profit")
    assert got.basis == "5% of profit before tax, £30,000.00, last year"


def test_a_loss_making_client_takes_1_percent_of_costs():
    got = levels(D("10000"), D("20000"), which="last year")
    assert (got.overall, got.benchmark) == (D("200.00"), "costs")


def test_no_figures_give_no_levels_so_everything_is_asked():
    assert levels(D("0"), D("0"), which="this year") is None


def test_part_of_a_year_is_scaled_to_twelve_months():
    got = levels(D("15000"), D("9000"), months=6, which="this year so far")
    assert got.overall == D("600.00") and "scaled to 12 months" in got.basis


def test_the_firms_figure_sets_the_levels():
    got = levels(D("100000"), D("70000"), which="last year", firm=D("2000"))
    assert (got.overall, got.working, got.trivial, got.benchmark) == (D("2000.00"), D("1000.00"), D("60.00"), "firm")
    assert got.basis == "set by the firm"


YEARS = [Year(start=dt.date(2025, 5, 2), end=dt.date(2026, 5, 1), label="2026"),
         Year(start=dt.date(2026, 5, 2), end=dt.date(2027, 5, 1), label="2027")]


def booked(day, gross, account, direction="out"):
    return normalise(Transaction(date=day, gross=gross, account_code=account, direction=direction,
                                 document_type="statement"), BusinessSettings())


def test_a_years_levels_come_from_the_year_before_when_it_has_figures():
    rows = [booked(dt.date(2025, 6, 1), "100000", "4000", "in"), booked(dt.date(2025, 7, 1), "70000", "7100"),
            booked(dt.date(2026, 6, 1), "5000", "4000", "in")]
    got = year_levels(rows, [0, 0, 1], YEARS, 1, None)
    assert got.overall == D("1500.00") and got.basis.endswith("last year")


def test_a_first_year_is_worked_out_from_itself_so_far():
    rows = [booked(dt.date(2026, 6, 1), "15000", "4000", "in"), booked(dt.date(2026, 10, 20), "9000", "7100")]
    got = year_levels(rows, [1, 1], YEARS, 1, None)
    assert got.overall == D("600.00") and "this year so far" in got.basis
