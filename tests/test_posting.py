import datetime as dt
import random

import pytest

from ledgersync import accounts
from ledgersync.checks import normalise
from ledgersync.errors import InvalidTransactions
from ledgersync.models import BusinessSettings, Transaction
from ledgersync.posting import journal_for, trial_balance

REGISTERED = BusinessSettings()


def tx(direction, gross, account, **kw):
    return Transaction(direction=direction, gross=gross, account_code=account, description="t",
                       date=dt.date(2026, 9, 1), **kw)


def lines(t, settings=REGISTERED):
    return [(l.code, str(l.debit), str(l.credit)) for l in journal_for(normalise(t, settings), 0)]


@pytest.mark.parametrize("name, t, expected", [
    ("expense with VAT", tx("out", "120.00", "7502"),
     [("7502", "100.00", "0.00"), ("2201", "20.00", "0.00"), ("1200", "0.00", "120.00")]),
    ("sale with document VAT", tx("in", "2400.00", "4000", vat="400.00"),
     [("4000", "0.00", "2000.00"), ("2200", "0.00", "400.00"), ("1200", "2400.00", "0.00")]),
    ("supplier refund", tx("in", "89.99", "0030"),
     [("0030", "0.00", "74.99"), ("2201", "0.00", "15.00"), ("1200", "89.99", "0.00")]),
    ("customer refund", tx("out", "120.00", "4000"),
     [("4000", "100.00", "0.00"), ("2200", "20.00", "0.00"), ("1200", "0.00", "120.00")]),
    ("HMRC VAT payment", tx("out", "1450.00", "2202"), [("2202", "1450.00", "0.00"), ("1200", "0.00", "1450.00")]),
    ("PAYE and NIC", tx("out", "2140.37", "2210"), [("2210", "2140.37", "0.00"), ("1200", "0.00", "2140.37")]),
    ("drawings", tx("out", "500.00", "3260"), [("3260", "500.00", "0.00"), ("1200", "0.00", "500.00")]),
    ("loan received", tx("in", "10000.00", "2300"), [("2300", "0.00", "10000.00"), ("1200", "10000.00", "0.00")]),
    ("transfer to savings", tx("out", "5000.00", "1210"), [("1210", "5000.00", "0.00"), ("1200", "0.00", "5000.00")]),
    ("mixed-VAT receipt", tx("out", "7.95", "8205", vat="0.70"),
     [("8205", "7.25", "0.00"), ("2201", "0.70", "0.00"), ("1200", "0.00", "7.95")]),
])
def test_postings(name, t, expected):
    assert lines(t) == expected


def test_not_vat_registered_posts_the_gross():
    assert lines(tx("out", "120.00", "7502"), BusinessSettings(vat_registered=False)) == [
        ("7502", "120.00", "0.00"), ("1200", "0.00", "120.00")]


def test_trial_balance_shows_net_balances_by_code():
    tb = trial_balance([tx("out", "120.00", "7502"), tx("in", "2400.00", "4000", vat="400.00")], REGISTERED)
    assert [(l.code, l.name, str(l.debit), str(l.credit)) for l in tb.lines] == [
        ("1200", "Bank Current Account", "2280.00", "0.00"),
        ("2200", "Sales VAT", "0.00", "400.00"),
        ("2201", "Purchase VAT", "20.00", "0.00"),
        ("4000", "Sales", "0.00", "2000.00"),
        ("7502", "Telephone and Internet", "100.00", "0.00"),
    ]
    assert (str(tb.total_debits), str(tb.total_credits), tb.is_balanced) == ("2400.00", "2400.00", True)


def test_cancelling_transactions_leave_no_zero_lines():
    tb = trial_balance([tx("out", "120.00", "7502"), tx("in", "120.00", "7502")], REGISTERED)
    assert tb.lines == [] and tb.is_balanced and len(tb.journal) == 6


def test_unknown_account_blocks_the_trial_balance():
    with pytest.raises(InvalidTransactions, match="#2") as info:
        trial_balance([tx("out", "10.00", "7502"), tx("out", "10.00", "9999")], REGISTERED)
    assert "9999" in info.value.message


def test_trial_balance_always_balances_for_random_ledgers():
    rng = random.Random(3)
    codes = [a.code for a in accounts.CHART if a.code != accounts.BANK]
    ledger = []
    for n in range(1000):
        gross = f"{rng.randrange(1, 10**6) / 100:.2f}"
        extra = {"vat": f"{float(gross) * rng.choice([0, 0.05, 0.1, 0.16]):.2f}"} if n % 4 == 0 else {}
        ledger.append(tx(rng.choice(["in", "out"]), gross, rng.choice(codes), **extra))
    tb = trial_balance(ledger, REGISTERED)
    assert tb.is_balanced and tb.total_debits == tb.total_credits
    for n in range(len(ledger)):
        mine = [l for l in tb.journal if l.transaction == n]
        assert sum(l.debit for l in mine) == sum(l.credit for l in mine)
