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
