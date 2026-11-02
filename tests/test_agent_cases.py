"""The hand-worked agent cases (eval/agent_cases/) and the Northbridge demo (demo/northbridge/), run through the
services with no model: eval/run_scripted_resolutions.py applies each case's decisions and answers, and the posted
trial balance, the questions and the decisions are compared with what its notes.md worked out by hand. Also the
demo reset, which the Northbridge run starts from."""
import datetime as dt
import json
from pathlib import Path

import pytest

import run_scripted_resolutions as scripted
from job_scoring import compare
from ledgersync import demo, ledger_book
from ledgersync.records import AgentRecords

pytestmark = pytest.mark.mongo

ROOT = Path(__file__).parent.parent
NORTHBRIDGE = ROOT / "demo" / "northbridge"
YEAR = dt.date(2026, 4, 1)   # every case's first year, and the demo's
CASES = [*sorted(p for p in (ROOT / "eval" / "agent_cases").iterdir() if (p / "client.json").exists()), NORTHBRIDGE]


@pytest.fixture
def records(mongo_db):
    return AgentRecords(mongo_db)


@pytest.fixture
def checkpoints(mongo_client, mongo_db):
    """The database the chat's LangGraph checkpoints go in, beside the test database (conftest drops it)."""
    return mongo_client[f"{mongo_db.name}_checkpoints"]


def _read(case: Path, name: str):
    return json.loads((case / name).read_text())


def _sorted(questions: list[dict]) -> list[dict]:
    return sorted(questions, key=lambda q: (q["kind"], q["row"]["file"], q["row"]["description"]))


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.name)
def test_scripted_case_matches_expected(case, mongo_store, records):
    result = scripted.run(case, mongo_store, records)
    decisions, questions = _read(case, "expected_decisions.json"), _read(case, "expected_questions.json")

    assert [(d.status, d.rule) for d in result.resolutions] == [("applied", e["rule"]) for e in
                                                                 decisions["resolutions"]], \
        [d.reason for d in result.resolutions if d.status != "applied"]
    assert result.automatic == decisions["automatic"]
    assert result.from_answers == decisions["from_answers"]

    assert result.review.problems == [] and result.answered.problems == []
    if case == NORTHBRIDGE:
        assert compare(_read(case, "expected_trial_balance_before_answers.json"), result.review.trial_balance) == []
        assert compare(_read(case, "expected_trial_balance_after_answers.json"), result.answered.trial_balance) == []
    else:
        assert compare(_read(case, "expected_trial_balance.json"), result.answered.trial_balance) == []
    assert _sorted(result.review.questions) == _sorted(questions["before_answers"])
    assert _sorted(result.answered.questions) == _sorted(questions["after_answers"])


def _state(store, records, client_id: int):
    """Everything the reset puts back, without the ids it allocates afresh."""
    uploads = [(u["name"], u["kind"], u["bank_account"], u["year_start"], u["row_count"])
               for u in store.uploads(client_id)]
    return (uploads, [r.tx for r in store.rows(client_id)], store.opening(client_id),
            store.get_client(client_id).bank_accounts, records.decisions(client_id),
            [t.id for t in records.threads(client_id)])


def test_demo_reset_twice_leaves_the_same_rows(mongo_store, records, checkpoints):
    first = demo.reset(mongo_store, records, checkpoints)
    before = _state(mongo_store, records, first)
    scripted.run(NORTHBRIDGE, mongo_store, records)   # the demo, run through: decisions, questions, posts, splits
    thread = records.create_thread(first, YEAR, "Review October")
    checkpoints.checkpoints.insert_one({"thread_id": thread.id})
    mongo_store.update_client(first, {"archived": True})
    assert demo.reset(mongo_store, records, checkpoints) == first
    assert _state(mongo_store, records, first) == before and not mongo_store.get_client(first).archived
    assert len(before[0]) == 3 and len(before[1]) == 8 and before[4:] == ([], [])
    assert records.questions(first, YEAR, ("open", "waiting", "resolved")) == []
    assert ledger_book.posted_trial_balance(mongo_store, records, first, YEAR).journal == []
    assert checkpoints.checkpoints.count_documents({}) == 0


def test_demo_reset_command(monkeypatch, capsys, mongo_store, mongo_db):
    monkeypatch.setattr(demo.Settings, "from_env", lambda: demo.Settings(mongodb_uri="mongodb://test",
                                                                        mongodb_db_name=mongo_db.name))
    monkeypatch.setattr(demo, "MongoStore", lambda uri, db_name, max_pool: mongo_store)   # the tests' one client
    assert demo.main(["reset"]) == 0
    [client] = mongo_store.list_clients()
    assert capsys.readouterr().out == f"Northbridge Consulting Ltd is reset (client {client.id}).\n"
    assert client.name == "Northbridge Consulting Ltd" and len(mongo_store.rows(client.id)) == 8
