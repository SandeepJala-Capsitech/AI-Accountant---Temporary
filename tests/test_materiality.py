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


# Presentation questions: asked from the client's trivial level, left below it with a note saying so.
from ledgersync.materiality import Levels, decide  # noqa: E402

LV = Levels(overall=D("1500"), working=D("750"), trivial=D("45"), benchmark="profit", basis="test")
YEAR_END = BusinessSettings(period_start=dt.date(2026, 5, 2), period_end=dt.date(2027, 5, 1))


def bill(gross, account, **kw):
    return normalise(Transaction(date=kw.pop("date", dt.date(2026, 9, 1)), gross=gross, account_code=account,
                                 direction="out", document_type="receipt", description=kw.pop("description", "x"),
                                 **kw), YEAR_END)


def decided(*rows, levels=LV):
    return decide(list(rows), [1] * len(rows), {1: levels})


def codes(t):
    return [(i.code, i.severity) for i in t.issues]


def test_sundry_under_the_trivial_level_is_left_and_says_so():
    [small] = decided(bill("6.99", "8207"))
    assert codes(small) == [("left_small", "info")]
    assert small.issues[0].message == ("£6.99 is under this client's trivial level of £45.00: left on Sundry Expenses "
                                       "as coded.")
    [big] = decided(bill("60.00", "8207"))
    assert codes(big) == [("sundry_check", "warning")]


def test_with_no_levels_everything_is_asked():
    [t] = decided(bill("1.00", "8207"), levels=None)
    assert codes(t) == [("sundry_check", "warning")]


def test_a_small_asset_is_asked_about_only_under_the_trivial_level():
    assert codes(decided(bill("30.00", "0030"))[0]) == [("small_asset", "warning")]
    assert codes(decided(bill("200.00", "0030"))[0]) == []


def test_equipment_on_a_cost_account_is_asked_about_from_the_working_level():
    assert ("asset_check", "warning") in codes(decided(bill("800.00", "7504"))[0])
    assert codes(decided(bill("700.00", "7504"))[0]) == [("left_small", "info")]


def test_an_annual_bill_asks_about_the_part_after_the_year_end():
    [big] = decided(bill("1200.00", "8204", date=dt.date(2026, 11, 1)))
    assert codes(big) == [("prepayment", "warning")] and "£600.00" in big.issues[0].message
    [small] = decided(bill("60.00", "8204", date=dt.date(2026, 11, 1)))
    assert codes(small) == [("left_small", "info")]


def test_small_items_left_on_one_account_are_asked_about_together_when_they_add_up():
    rows = decided(*(bill("40.00", "8207") for _ in range(19)), bill("39.00", "8207"))
    together = [i for t in rows for i in t.issues if i.code == "small_items_add_up"]
    assert len(together) == 1 and "£799.00" in together[0].message and len(together[0].related) == 19


def test_an_accepted_question_is_neither_asked_nor_left():
    [t] = decided(bill("6.99", "8207", accepted=["sundry_check"]))
    assert codes(t) == []
