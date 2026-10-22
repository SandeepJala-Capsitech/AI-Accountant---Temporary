"""The trial balance snapshots: each run saves its trial balance after posting and after the gate, as new versions that
are never updated, each with its own Excel workbook; the steps that save them; and the routes that list and download
them."""
import datetime as dt
import io
import logging
import re
import threading
from decimal import Decimal
from unittest import mock

import pytest
from fakes import FakeModel
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, OperationFailure
from test_agent_jobs_api import NOT_CONFIGURED, YEAR, poll, start, wait_for
from test_agent_nodes import RESULT, FakeAgent

from ledgersync import ledger as books
from ledgersync.agent import make_snapshot
from ledgersync.agent.nodes import make_classify
from ledgersync.agent.state import JobState
from ledgersync.agent.tools import ToolDeps
from ledgersync.blobs import GridFSBlobStore
from ledgersync.config import Settings
from ledgersync.errors import NotFound
from ledgersync.export import file_name
from ledgersync.jobs import JobCancelled
from ledgersync.models import ClientFields, Transaction, TrialBalance
from ledgersync.records.audit import AuditLog
from ledgersync.records.jobs import JobRecords
from ledgersync.records.snapshots import Snapshots
from ledgersync.store import Store
from server import create_app

pytestmark = pytest.mark.mongo

DAY = dt.date(2026, 10, 6)
PERIOD = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FIELDS = ["id", "job_id", "client_id", "version", "stage", "is_balanced", "lines", "totals", "problems",
          "excel_blob_id", "created_at"]
ZERO_TOTALS = {key: "0.00" for key in ("debits", "credits", "income", "expenses", "net_profit", "assets",
                                        "liabilities")}


def receipt(**changes) -> Transaction:
    """BT's broadband: £72.00 with £12.00 VAT, dated inside the period, which passes every check of the gate."""
    fields = dict(direction="out", gross="72.00", vat="12.00", account_code="7502", date=dt.date(2025, 9, 1),
                  description="BT Business, broadband", document_type="receipt", counterparty="BT Business")
    return Transaction(**{**fields, **changes})


def new_client(store, name: str, *rows: Transaction) -> int:
    client = store.create_client(ClientFields(**{**CUBE.model_dump(), "name": name}))
    store.add_upload(client.id, "BT bill.pdf", "pdf", list(rows))
    return client.id


def save(snapshots, ledger, balance, job_id="a", stage="after_gate"):
    return snapshots.save(job_id=job_id, client_id=1, stage=stage, ledger=ledger, balance=balance, day=DAY)


def sheet_text(blobs, snapshot: dict) -> list[list]:
    """The rows of the "Trial balance" sheet of a snapshot's workbook."""
    book = load_workbook(io.BytesIO(blobs.get(snapshot["excel_blob_id"]).data))
    return [[c.value for c in row] for row in book["Trial balance"].iter_rows()]


# --- fixtures -----------------------------------------------------------------------------------------------

@pytest.fixture
def client_id(mongo_store):
    """Business Cube Ltd with BT's £72.00 receipt."""
    return new_client(mongo_store, "Business Cube Ltd", receipt())


@pytest.fixture
def ledger(mongo_store, client_id):
    return books.build(mongo_store, client_id)


@pytest.fixture
def balance(mongo_store, client_id):
    return books.trial_balance(mongo_store, client_id)


@pytest.fixture
def blobs(mongo_db):
    return GridFSBlobStore(mongo_db)


@pytest.fixture
def snapshots(mongo_db, blobs):
    return Snapshots(mongo_db, blobs)


@pytest.fixture
def records(mongo_db):
    return JobRecords(mongo_db)


@pytest.fixture
def deps(mongo_store, records, mongo_db, blobs, snapshots):
    return ToolDeps(store=mongo_store, records=records, audit=AuditLog(mongo_db), blobs=blobs, snapshots=snapshots,
                    settings=Settings())


def state_for(records, client_id: int) -> JobState:
    job = records.create(client_id, *PERIOD)
    return JobState(job_id=job.id, client_id=client_id, period_start="2025-04-01", period_end="2026-03-31",
                    fix_rounds=0, gate_passed=False)


@pytest.fixture
def state(records, client_id) -> JobState:
    return state_for(records, client_id)


def messages(records, state) -> list[str]:
    return [e.message for e in records.events(state["job_id"])]


# --- saving a snapshot --------------------------------------------------------------------------------------

def test_versions_count_up_for_each_job(snapshots, ledger, balance):
    versions = [snapshots.save(job_id=j, client_id=1, stage="after_gate", ledger=ledger, balance=balance, day=DAY)["version"]
                for j in ("a", "a", "b")]
    assert versions == [1, 2, 1]


def test_money_is_kept_as_exact_strings(snapshots, ledger, balance, mongo_db):
    snapshots.save(job_id="a", client_id=1, stage="after_gate", ledger=ledger, balance=balance, day=DAY)
    assert mongo_db.trial_balances.find_one({"job_id": "a"})["totals"]["debits"] == "72.00"


def test_a_trial_balance_that_cant_be_built_is_saved_with_its_problems(snapshots, ledger):
    saved = snapshots.save(job_id="a", client_id=1, stage="after_gate", ledger=ledger,
                           balance=["Row 3: unknown account 1234"], day=DAY)
    assert (saved["is_balanced"], saved["lines"], saved["problems"]) == (False, [], ["Row 3: unknown account 1234"])


def test_a_snapshot_holds_the_trial_balance_with_all_money_as_strings(snapshots, ledger, balance, client_id):
    saved = snapshots.save(job_id="a", client_id=client_id, stage="after_posting", ledger=ledger, balance=balance,
                           day=DAY)
    assert list(saved) == FIELDS
    assert re.fullmatch(r"[0-9a-f]{32}", saved["id"])
    assert (saved["job_id"], saved["client_id"], saved["version"], saved["stage"], saved["is_balanced"],
            saved["problems"]) == ("a", client_id, 1, "after_posting", True, [])
    assert saved["lines"] == [
        {"code": "1200", "name": "Bank Current Account", "type": "asset", "debit": "0.00", "credit": "72.00"},
        {"code": "7502", "name": "Telephone and Internet", "type": "expense", "debit": "60.00", "credit": "0.00"},
        {"code": "2201", "name": "Purchase VAT", "type": "liability", "debit": "12.00", "credit": "0.00"}]
    assert saved["totals"] == {"debits": "72.00", "credits": "72.00", "income": "0.00", "expenses": "60.00",
                               "net_profit": "-60.00", "assets": "-72.00", "liabilities": "-12.00"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00", saved["created_at"])


def test_what_is_saved_is_what_is_returned_and_read_back(snapshots, ledger, balance, mongo_db):
    saved = save(snapshots, ledger, balance)
    stored = mongo_db.trial_balances.find_one({"job_id": "a"})
    assert stored.pop("_id") == saved["id"] and stored == {k: v for k, v in saved.items() if k != "id"}
    assert snapshots.get("a", 1) == saved


def test_a_trial_balance_that_cant_be_built_has_zero_totals_and_a_workbook_that_says_why(snapshots, ledger, blobs):
    saved = save(snapshots, ledger, ["#1: This account isn't in the chart.", "#2: This account isn't in the chart."])
    assert saved["totals"] == ZERO_TOTALS
    assert sheet_text(blobs, saved)[1:] == [["The trial balance can't be produced yet:"],
                                            ["#1: This account isn't in the chart."],
                                            ["#2: This account isn't in the chart."]]


def test_each_snapshot_keeps_its_own_workbook_named_for_the_client_and_the_day(snapshots, ledger, balance, blobs):
    saved = save(snapshots, ledger, balance)
    kept = blobs.get(saved["excel_blob_id"])
    assert (kept.name, kept.content_type) == (file_name("Business Cube Ltd", DAY), XLSX)
    assert kept.name == "Business Cube Ltd 2026-10-06.xlsx" and kept.data[:2] == b"PK"
    assert sheet_text(blobs, saved) == [
        ["Business Cube Ltd — trial balance — 6 Oct 2026", None, None, None],
        ["Code", "Account", "Debit", "Credit"], ["1200", "Bank Current Account", 0, 72],
        ["7502", "Telephone and Internet", 60, 0], ["2201", "Purchase VAT", 12, 0], [None, "Totals", 72, 72]]


def test_a_snapshot_is_never_updated_so_each_save_is_a_new_document(snapshots, ledger, balance, mongo_db):
    first = save(snapshots, ledger, balance, stage="after_posting")
    second = save(snapshots, ledger, balance, stage="after_gate")
    assert first["id"] != second["id"]
    assert [(d["version"], d["stage"]) for d in mongo_db.trial_balances.find({"job_id": "a"}).sort("version")] == [
        (1, "after_posting"), (2, "after_gate")]


# --- reading them -------------------------------------------------------------------------------------------

def test_list_and_latest_give_a_jobs_snapshots_by_version(snapshots, ledger, balance):
    first = save(snapshots, ledger, balance, stage="after_posting")
    second = save(snapshots, ledger, balance, stage="after_gate")
    save(snapshots, ledger, balance, job_id="b")
    assert snapshots.list("a") == [first, second]
    assert snapshots.latest("a") == second
    assert snapshots.get("a", 1) == first


def test_a_job_with_no_snapshots_has_none(snapshots):
    assert (snapshots.list("a"), snapshots.latest("a")) == ([], None)


@pytest.mark.parametrize("job_id, version", [("a", 2), ("a", 0), ("a", -1), ("b", 1)])
def test_an_unknown_snapshot_is_not_found(snapshots, ledger, balance, job_id, version):
    save(snapshots, ledger, balance)
    with pytest.raises(NotFound, match="This snapshot was not found."):
        snapshots.get(job_id, version)


# --- versions that collide ----------------------------------------------------------------------------------

def test_a_version_taken_between_reading_and_saving_is_retried_once(snapshots, ledger, balance, monkeypatch):
    save(snapshots, ledger, balance)                      # version 1
    real, reads = snapshots._highest, []

    def stale(job_id):
        """The highest version as another writer had left it before it saved version 1."""
        reads.append(job_id)
        return 0 if len(reads) == 1 else real(job_id)

    monkeypatch.setattr(snapshots, "_highest", stale)
    assert save(snapshots, ledger, balance)["version"] == 2
    assert len(reads) == 2
    assert [s["version"] for s in snapshots.list("a")] == [1, 2]


def test_a_second_collision_is_raised_not_retried_for_ever(snapshots, ledger, balance, monkeypatch):
    save(snapshots, ledger, balance)
    reads = []
    monkeypatch.setattr(snapshots, "_highest", lambda job_id: reads.append(job_id) or 0)
    with pytest.raises(DuplicateKeyError):
        save(snapshots, ledger, balance)
    assert len(reads) == 2
    assert [s["version"] for s in snapshots.list("a")] == [1]


def test_two_saves_at_once_get_different_versions(snapshots, ledger, balance):
    both_ready, versions = threading.Barrier(2), []

    def go():
        both_ready.wait()
        versions.append(save(snapshots, ledger, balance)["version"])

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(versions) == [1, 2]


# --- the index ----------------------------------------------------------------------------------------------

def test_a_jobs_versions_are_unique_in_the_database(snapshots, ledger, balance, mongo_db):
    save(snapshots, ledger, balance)
    indexes = mongo_db.trial_balances.index_information()
    assert any(info.get("unique") and info["key"] == [("job_id", 1), ("version", 1)] for info in indexes.values())
    with pytest.raises(DuplicateKeyError):
        mongo_db.trial_balances.insert_one({"job_id": "a", "version": 1})


def test_a_second_snapshots_object_on_the_same_database_makes_no_index_call(mongo_db, blobs):
    Snapshots(mongo_db, blobs)    # the first one creates the index
    with mock.patch.object(Collection, "create_index") as create_index:
        Snapshots(mongo_db, blobs)
    create_index.assert_not_called()


def test_a_refused_index_is_logged_once_and_does_not_stop_the_snapshots(mongo_db, blobs, ledger, balance, caplog):
    refused = OperationFailure("not authorized on db to execute command createIndexes", code=13)
    with caplog.at_level(logging.ERROR, logger="ledgersync.records.snapshots"):
        with mock.patch.object(Collection, "create_index", side_effect=refused) as create_index:
            snapshots = Snapshots(mongo_db, blobs)    # must not raise
            Snapshots(mongo_db, blobs)                # already tried for this database
    assert create_index.call_count == 1
    [message] = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "trial_balances" in message and "createIndex" in message
    assert save(snapshots, ledger, balance)["version"] == 1


def test_any_other_index_error_still_raises_and_is_tried_again_next_time(mongo_db, blobs):
    with mock.patch.object(Collection, "create_index", side_effect=RuntimeError("network down")):
        with pytest.raises(RuntimeError, match="network down"):
            Snapshots(mongo_db, blobs)
    Snapshots(mongo_db, blobs)   # not remembered as done, so this one creates the index for real
    assert "job_id_1_version_1" in mongo_db.trial_balances.index_information()


# --- the tools' dependencies --------------------------------------------------------------------------------

def test_the_tool_deps_make_snapshots_on_their_blob_store_unless_given_some(mongo_store, mongo_db, records, blobs):
    made = ToolDeps(store=mongo_store, records=records, audit=AuditLog(mongo_db), blobs=blobs)
    assert isinstance(made.snapshots, Snapshots)
    own = Snapshots(mongo_db, blobs)
    assert ToolDeps(store=mongo_store, records=records, audit=AuditLog(mongo_db), snapshots=own).snapshots is own


def test_tool_deps_built_with_no_blob_store_still_save_snapshots(mongo_store, mongo_db, records, ledger, balance):
    made = ToolDeps(store=mongo_store, records=records, audit=AuditLog(mongo_db))   # as the older tests build them
    assert made.snapshots.save(job_id="a", client_id=1, stage="after_gate", ledger=ledger, balance=balance,
                               day=DAY)["version"] == 1


# --- the classify step saves after_posting ------------------------------------------------------------------

class Cancelled:
    """An agent factory whose agent is stopped by a cancel while it works."""

    def __call__(self, model_factory, tools, prompt, result_type, model_calls, tool_calls):
        return self

    def invoke(self, input, config=None, **kwargs):
        raise JobCancelled()


def test_the_classify_step_saves_the_trial_balance_after_posting_without_a_line_in_the_feed(deps, records, state):
    node = make_classify(deps, model_factory=None, agent_factory=FakeAgent(result=RESULT))
    assert node(state) == {}
    [saved] = deps.snapshots.list(state["job_id"])
    assert (saved["stage"], saved["version"], saved["client_id"], saved["totals"]["debits"]) == (
        "after_posting", 1, state["client_id"], "72.00")
    assert messages(records, state) == ["Coded 1 row."]


def test_the_classify_step_saves_what_posting_produced_when_the_agent_stopped_at_its_call_limit(deps, state):
    make_classify(deps, model_factory=None, agent_factory=FakeAgent(result=None))(state)
    [saved] = deps.snapshots.list(state["job_id"])
    assert (saved["stage"], saved["version"]) == ("after_posting", 1)
    assert deps.exceptions.for_job(state["job_id"])[-1]["description"] == (
        "The agent stopped at its call limit before finishing classify.")


def test_a_cancelled_classify_step_saves_nothing(deps, state):
    with pytest.raises(JobCancelled):
        make_classify(deps, model_factory=None, agent_factory=Cancelled())(state)
    assert deps.snapshots.list(state["job_id"]) == []


def test_a_classify_step_that_runs_again_saves_another_version(deps, state):
    node = make_classify(deps, model_factory=None, agent_factory=FakeAgent(result=RESULT))
    node(state)
    node(state)                                           # run again, as after a crash
    assert [(s["version"], s["stage"]) for s in deps.snapshots.list(state["job_id"])] == [
        (1, "after_posting"), (2, "after_posting")]


def test_the_classify_step_saves_rows_that_cant_be_posted_with_their_problems(mongo_store, deps, records):
    cid = new_client(mongo_store, "Broken Ltd", receipt(account_code="9999"))
    state = state_for(records, cid)
    make_classify(deps, model_factory=None, agent_factory=FakeAgent(result=RESULT))(state)
    [saved] = deps.snapshots.list(state["job_id"])
    assert (saved["is_balanced"], saved["lines"], saved["problems"]) == (
        False, [], ["#1: This account isn't in the chart."])


# --- the snapshot step --------------------------------------------------------------------------------------

def test_the_snapshot_step_saves_after_gate_and_says_the_total(deps, records, state):
    assert make_snapshot(deps)(state) == {}
    [saved] = deps.snapshots.list(state["job_id"])
    assert (saved["stage"], saved["version"], saved["is_balanced"]) == ("after_gate", 1, True)
    [event] = records.events(state["job_id"])
    assert (event.stage, event.message) == (
        "snapshot", "Trial balance saved as version 1. Debits and credits are both £72.00.")


def test_the_snapshot_step_is_the_next_version_after_posting(deps, records, state):
    make_classify(deps, model_factory=None, agent_factory=FakeAgent(result=RESULT))(state)
    make_snapshot(deps)(state)
    assert [(s["version"], s["stage"]) for s in deps.snapshots.list(state["job_id"])] == [
        (1, "after_posting"), (2, "after_gate")]
    assert messages(records, state)[-1] == "Trial balance saved as version 2. Debits and credits are both £72.00."


def test_the_total_is_written_as_money_with_thousands_separated(mongo_store, deps, records):
    state = state_for(records, new_client(mongo_store, "Big Ltd", receipt(gross="1234.56", vat="0.00")))
    make_snapshot(deps)(state)
    assert messages(records, state) == ["Trial balance saved as version 1. Debits and credits are both £1,234.56."]


@pytest.mark.parametrize("rows, count", [(1, "1 problem"), (2, "2 problems")])
def test_a_trial_balance_that_cant_be_built_says_how_many_problems_the_reviewer_has(mongo_store, deps, records, rows,
                                                                                    count):
    state = state_for(records, new_client(mongo_store, "Broken Ltd", *[receipt(account_code="9999")] * rows))
    make_snapshot(deps)(state)
    [saved] = deps.snapshots.list(state["job_id"])
    assert (saved["lines"], len(saved["problems"]), saved["totals"]) == ([], rows, ZERO_TOTALS)
    assert messages(records, state) == [f"Trial balance saved as version 1, with {count} for the reviewer."]


def test_a_trial_balance_that_is_built_but_doesnt_balance_doesnt_say_debits_and_credits_are_both_the_same(
        deps, records, state, monkeypatch):
    unequal = TrialBalance(lines=[], total_debits=Decimal("10.00"), total_credits=Decimal("9.00"), is_balanced=False,
                           journal=[])
    monkeypatch.setattr("ledgersync.agent.nodes.balance_for", lambda store, client_id: unequal)
    make_snapshot(deps)(state)
    [saved] = deps.snapshots.list(state["job_id"])
    assert (saved["is_balanced"], saved["problems"], saved["totals"]["debits"], saved["totals"]["credits"]) == (
        False, [], "10.00", "9.00")
    assert messages(records, state) == ["Trial balance saved as version 1. Debits are £10.00 and credits are £9.00."]


# --- the routes ---------------------------------------------------------------------------------------------

@pytest.fixture
def api(mongo_store, mongo_db, monkeypatch):
    """The app with the built-in steps: only the classify agent stands aside (a FakeAgent that returns its result), as
    nothing may call a model. Ingest, the gate, requests and snapshot run for real."""
    monkeypatch.setattr("server.make_classify", lambda deps, model_factory: make_classify(
        deps, model_factory, agent_factory=FakeAgent(result=RESULT)))
    settings = Settings(warmup=False, agent_model="some/model")
    with TestClient(create_app(settings, model_client=FakeModel([]), store=mongo_store),
                    headers={"X-LedgerSync": "1"}) as client:
        yield client
        # No worker may write to the database after the test drops it.
        poll(JobRecords(mongo_db).unfinished, lambda jobs: not jobs, "every job to finish")


@pytest.fixture
def finished_job_id(api, client_id):
    job = wait_for(api, start(api, client_id)["id"])
    assert (job["status"], job["gate"]["passed"]) == ("awaiting_review", True)
    return job["id"]


def test_a_finished_job_has_both_snapshots_and_a_downloadable_workbook(api, finished_job_id):
    listed = api.get(f"/api/agent-jobs/{finished_job_id}/snapshots").json()
    assert [s["stage"] for s in listed] == ["after_posting", "after_gate"]
    resp = api.get(f"/api/agent-jobs/{finished_job_id}/snapshots/2/export.xlsx")
    assert resp.status_code == 200 and resp.content[:2] == b"PK"


def test_the_listed_snapshots_are_the_saved_documents(api, finished_job_id, client_id):
    listed = api.get(f"/api/agent-jobs/{finished_job_id}/snapshots").json()
    assert [(s["version"], s["job_id"], s["client_id"], s["is_balanced"], s["totals"]["debits"]) for s in listed] == [
        (1, finished_job_id, client_id, True, "72.00"), (2, finished_job_id, client_id, True, "72.00")]
    assert all(list(s) == FIELDS and s["problems"] == [] and len(s["lines"]) == 3 for s in listed)


def test_each_version_downloads_its_own_workbook(api, finished_job_id, mongo_db):
    for version in (1, 2):
        resp = api.get(f"/api/agent-jobs/{finished_job_id}/snapshots/{version}/export.xlsx")
        stored = mongo_db.trial_balances.find_one({"job_id": finished_job_id, "version": version})
        kept = GridFSBlobStore(mongo_db).get(stored["excel_blob_id"])
        assert resp.status_code == 200 and resp.content == kept.data
        assert resp.headers["content-type"] == XLSX
        assert re.fullmatch(r'attachment; filename="Business Cube Ltd \d{4}-\d{2}-\d{2}\.xlsx"; '
                            r"filename\*=UTF-8''Business%20Cube%20Ltd%20\d{4}-\d{2}-\d{2}\.xlsx",
                            resp.headers["content-disposition"])
        book = load_workbook(io.BytesIO(resp.content))
        assert book["Trial balance"]["B6"].value == "Totals"


def test_the_feed_says_what_the_snapshot_step_saved(api, finished_job_id):
    events = api.get(f"/api/agent-jobs/{finished_job_id}/events").json()
    assert [e["message"] for e in events if e["stage"] == "snapshot"] == [
        "Started snapshot", "Trial balance saved as version 2. Debits and credits are both £72.00."]
    assert not any("version 1" in e["message"] for e in events)   # saving after posting writes nothing to the feed


def test_an_unknown_job_is_not_found(api):
    unknown = "0" * 32
    for resp in (api.get(f"/api/agent-jobs/{unknown}/snapshots"),
                 api.get(f"/api/agent-jobs/{unknown}/snapshots/1/export.xlsx")):
        assert (resp.status_code, resp.json()["detail"]["message"]) == (404, "This job was not found.")


def test_an_unknown_version_is_not_found(api, finished_job_id):
    for version in (0, 3):
        resp = api.get(f"/api/agent-jobs/{finished_job_id}/snapshots/{version}/export.xlsx")
        assert (resp.status_code, resp.json()["detail"]["message"]) == (404, "This snapshot was not found.")


def test_a_job_with_no_snapshots_lists_none_and_has_nothing_to_download(api, mongo_db, client_id):
    job = JobRecords(mongo_db).create(client_id, *PERIOD)
    JobRecords(mongo_db).update(job.id, status="awaiting_review")     # not one the app runs
    assert api.get(f"/api/agent-jobs/{job.id}/snapshots").json() == []
    resp = api.get(f"/api/agent-jobs/{job.id}/snapshots/1/export.xlsx")
    assert (resp.status_code, resp.json()["detail"]["message"]) == (404, "This snapshot was not found.")


def test_another_jobs_version_is_not_found_under_this_job(api, finished_job_id, mongo_db, client_id):
    other = JobRecords(mongo_db).create(client_id, *PERIOD)
    JobRecords(mongo_db).update(other.id, status="awaiting_review")
    assert api.get(f"/api/agent-jobs/{other.id}/snapshots/1/export.xlsx").status_code == 404


def test_without_a_runner_the_routes_are_not_configured(mongo_store):
    with TestClient(create_app(Settings(warmup=False), model_client=FakeModel([]), store=mongo_store)) as api:
        responses = [api.get(f"/api/agent-jobs/{'0' * 32}/snapshots"),
                     api.get(f"/api/agent-jobs/{'0' * 32}/snapshots/1/export.xlsx")]
    assert [(r.status_code, r.json()["detail"]["message"]) for r in responses] == [(503, NOT_CONFIGURED)] * 2


def test_without_mongodb_the_routes_are_not_configured(tmp_path):
    with TestClient(create_app(Settings(warmup=False), model_client=FakeModel([]),
                               store=Store(tmp_path / "ledgersync.db"))) as api:
        responses = [api.get(f"/api/agent-jobs/{'0' * 32}/snapshots"),
                     api.get(f"/api/agent-jobs/{'0' * 32}/snapshots/1/export.xlsx")]
    assert [(r.status_code, r.json()["detail"]["message"]) for r in responses] == [(503, NOT_CONFIGURED)] * 2
