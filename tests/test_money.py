from decimal import Decimal

import pytest

from ledgersync.money import VatTreatment as V, to_money, vat_in_gross


@pytest.mark.parametrize("gross, treatment, vat", [
    ("120.00", V.STANDARD, "20.00"),
    ("7.50", V.STANDARD, "1.25"),
    ("68.39", V.STANDARD, "11.40"),       # 11.398 rounds up
    ("21.00", V.REDUCED, "1.00"),
    ("12.50", V.ZERO, "0.00"),
    ("45.00", V.EXEMPT, "0.00"),
    ("1450.00", V.OUTSIDE_SCOPE, "0.00"),
    ("0.03", V.STANDARD, "0.01"),         # exactly half a penny: half up (banker's rounding gives 0.00)
])
def test_vat_contained_in_a_gross_amount(gross, treatment, vat):
    assert vat_in_gross(Decimal(gross), treatment) == Decimal(vat)


@pytest.mark.parametrize("raw, value", [
    ("£1,234.50", "1234.50"), (72.1, "72.10"), ("0.005", "0.01"), ("-72", "-72.00"),
    ("1,234,567.89", "1234567.89"), ("-£1,234.50", "-1234.50"),
    ("abc", None), ("", None), (None, None), ("nan", None), (True, None),
])
def test_to_money_reads_exact_pennies(raw, value):
    assert to_money(raw) == (Decimal(value) if value is not None else None)


@pytest.mark.parametrize("raw", ["72,00", "1,2,3", "12,34.50"])
def test_a_comma_that_does_not_group_thousands_is_not_an_amount(raw):
    # "72,00" is 72 in much of Europe; reading it as 7200.00 would post a hundred times too much.
    assert to_money(raw) is None


@pytest.mark.parametrize("raw", [1e26, "1e30", "12345678901234567890"])
def test_absurdly_large_numbers_are_not_amounts(raw):
    # Used to raise decimal.InvalidOperation (from 1e26), which became an HTTP 500.
    assert to_money(raw) is None
