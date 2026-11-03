"""The agent eval runner's scoring (eval/run_agent_eval.py) agrees with the hand-worked figures: a case worked by
eval/run_scripted_resolutions.py with no model passes every score, and a wrong posted figure fails the trial balance
with its account in the difference."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

import run_agent_eval as agent_eval
import run_scripted_resolutions as scripted
from ledgersync.records import AgentRecords

pytestmark = pytest.mark.mongo

ROOT = Path(__file__).parent.parent
CASES = [ROOT / "eval" / "agent_cases" / "conflict-invoice-vs-expense-report", ROOT / "demo" / "northbridge"]


@pytest.fixture
def records(mongo_db):
    return AgentRecords(mongo_db)


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.name)
def test_scripted_case_passes_every_score(case, mongo_store, records):
    result = scripted.run(case, mongo_store, records)
    expected_decisions = json.loads((case / "expected_decisions.json").read_text())
    expected_questions = json.loads((case / "expected_questions.json").read_text())
    before, after = agent_eval.expected_trial_balances(case)
    decisions = [d.model_dump(mode="json") for d in records.decisions(result.client_id, result.year_start)]

    assert agent_eval.score_trial_balance(after, result.answered.trial_balance) == []
    if before is not None:
        assert agent_eval.score_trial_balance(before, result.review.trial_balance) == []
    assert agent_eval.score_decisions(expected_decisions, decisions, scripted._names(mongo_store, result.client_id)) == []
    assert agent_eval.score_questions(expected_questions["before_answers"], result.review.questions) == []


def test_a_wrong_posted_figure_fails_the_trial_balance(mongo_store, records):
    case = ROOT / "eval" / "agent_cases" / "clean-month"
    _, expected = agent_eval.expected_trial_balances(case)
    posted = scripted.run(case, mongo_store, records).answered.trial_balance
    account = sorted(posted)[0]
    posted[account] += Decimal("0.01")

    [difference] = agent_eval.score_trial_balance(expected, posted)
    assert difference["code"] == account and difference["difference"] == "0.01"


def test_missing_and_unexpected_questions_and_decisions_fail():
    row = {"file": "a.csv", "description": "X"}
    assert agent_eval.score_questions([{"kind": "date", "row": row}], []) == [
        {"missing": ("date", "a.csv", "X")}]
    assert agent_eval.score_questions([], [{"kind": "date", "row": row}]) == [
        {"unexpected": ("date", "a.csv", "X")}]
    expected = {"resolutions": [], "automatic": [{"rule": "R1", "row": row}], "from_answers": []}
    assert agent_eval.score_decisions(expected, [], {}) == [{"missing": ("a.csv", "X", "R1")}]
