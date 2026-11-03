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
    assert agent_eval.score_questions(expected_questions["after_answers"], result.answered.questions) == []


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


def _every_file():
    cases = [p for p in sorted((ROOT / "eval" / "agent_cases").iterdir()) if (p / "client.json").exists()]
    return [(case, f) for case in [*cases, ROOT / "demo" / "northbridge"] for f in agent_eval.case_files(case)]


@pytest.mark.parametrize("case, path", _every_file(), ids=lambda v: v.name)
def test_prepared_rows_finds_every_file_as_the_server_reads_it(case, path):
    """The file as intake.load_upload gives it to pipeline.analyze maps back to its own .rows.json."""
    from ledgersync import intake
    rows = agent_eval.PreparedRows()
    rows.use(agent_eval.case_files(case))
    assert rows._find(intake.load_upload(path.name, path.read_bytes(), 1000)) == path
    assert path.with_name(f"{path.name}.rows.json").exists()


def test_run_case_goes_through_the_routes_with_prepared_rows(mongo_store, records):
    """The demo, on a chat model that decides nothing: the run completes, the rules' questions are answered through
    the answer and document routes, and the scores and problems are recorded (the scores fail: no decisions)."""
    from agent_fakes import ScriptedChatModel, say
    from fakes import FakeModel
    from fastapi.testclient import TestClient

    from ledgersync.config import Settings
    from server import create_app

    rows = agent_eval.PreparedRows()
    rows.install()
    try:
        app = create_app(Settings(warmup=False), model_client=FakeModel([]), store=mongo_store,
                         chat_model=ScriptedChatModel(then=say("Done.")))
        with TestClient(app, headers={"X-LedgerSync": "1"}) as api:
            result = agent_eval.run_case(api, mongo_store, records, ROOT / "demo" / "northbridge", rows, "scripted")
    finally:
        rows.uninstall()

    assert result["error_events"] == [] and result["failed_steps"] == []
    assert result["answers_not_applied"] == [] and result["steps"] >= 3
    assert result["trial_balance"]["passed"] is False or result["decisions"]["passed"] is False
    assert {"trial_balance", "decisions", "questions", "problems", "seconds", "refused_decisions"} <= set(result)
    assert agent_eval.passed(result) is False


def test_an_applied_decision_on_an_unknown_row_is_unexpected():
    odd = {"status": "applied", "row_ids": [99], "rule": "R9"}
    assert agent_eval.score_decisions({"resolutions": [], "automatic": [], "from_answers": []}, [odd], {}) == [
        {"unexpected": {"row_ids": [99], "rule": "R9"}}]
