import json
from pathlib import Path

from ledgersync.accounts import BY_CODE, CHART, AccountType, choosable

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


def test_a_model_may_choose_any_account_but_the_bank_and_the_vat_control_accounts():
    # The bank is the other side of every posting, and the ledger splits VAT itself.
    assert {a.code for a in choosable()} == set(BY_CODE) - {"1200", "2200", "2201"}


def test_every_account_has_a_short_definition():
    # The model is told what belongs in each account; short keeps the prompt within the free plan.
    assert [a.code for a in CHART if not a.definition.strip() or len(a.definition) > 110] == []
