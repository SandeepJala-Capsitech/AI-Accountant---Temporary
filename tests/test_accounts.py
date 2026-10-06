import json
from pathlib import Path

from ledgersync.accounts import BANK, BY_CODE, CHART, AccountType, choosable

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def test_codes_are_unique_four_digit_strings():
    codes = [a.code for a in CHART]
    assert len(codes) == len(set(codes)) and all(len(c) == 4 and c.isdigit() for c in codes)


def test_every_code_the_eval_expects_exists():
    used = {r["account_code"] for p in FIXTURES.glob("*/expected.json") for r in json.loads(p.read_text())["rows"]}
    assert used and used <= set(BY_CODE)


def test_posting_accounts_have_the_right_types():
    assert BY_CODE["1200"].type == AccountType.ASSET
    assert BY_CODE["2200"].type == BY_CODE["2201"].type == AccountType.LIABILITY
    assert BY_CODE["4000"].type == AccountType.INCOME and BY_CODE["7502"].type == AccountType.EXPENSE


def test_1200_is_the_bank_account():
    # Every payment's other side posts to 1200: renamed "Debtors", a rent payment showed as money owed to us.
    assert (BANK, BY_CODE["1200"].name, BY_CODE["1210"].name) == ("1200", "Bank Current Account", "Bank Deposit Account")
    assert BY_CODE["1210"].type == AccountType.ASSET


def test_debtors_and_creditors_have_their_own_codes():
    assert (BY_CODE["1100"].name, BY_CODE["1100"].type) == ("Debtors", AccountType.ASSET)
    assert (BY_CODE["2100"].name, BY_CODE["2100"].type) == ("Creditors", AccountType.LIABILITY)


def test_a_model_may_choose_any_account_but_the_bank_and_the_control_accounts():
    # The bank is the other side of every posting, and the ledger splits VAT itself. What is owed
    # (Debtors, Creditors, Expenses Owed to Staff) is picked by the ledger from the document type.
    assert {a.code for a in choosable()} == set(BY_CODE) - {"1200", "2200", "2201", "1100", "2100", "2110"}


def test_expenses_owed_to_staff_is_a_liability_of_its_own():
    # A claim is owed to the employee, not to a trade supplier, so it stays out of 2100 Creditors.
    assert (BY_CODE["2110"].name, BY_CODE["2110"].type) == ("Expenses Owed to Staff", AccountType.LIABILITY)


def test_every_account_has_a_short_definition():
    # The model is told what belongs in each account; short keeps the prompt within the free plan.
    assert [a.code for a in CHART if not a.definition.strip() or len(a.definition) > 110] == []
