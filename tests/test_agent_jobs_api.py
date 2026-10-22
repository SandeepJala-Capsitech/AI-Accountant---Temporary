"""The agent job API, run on stub steps: starting a job, the row lock while it runs, cancel, retry, and
carrying on after a restart."""
import datetime as dt
import re
import threading
import time

import pytest
from fakes import ROW, FakeModel, transactions_json
from fastapi.testclient import TestClient
from langgraph.checkpoint.mongodb import MongoDBSaver

from ledgersync.agent import Nodes, build_graph
from ledgersync.agent.llm import NOT_CONFIGURED as AGENT_NOT_CONFIGURED
from ledgersync.config import Settings
from ledgersync.errors import JobRunning
from ledgersync.models import ClientFields
from ledgersync.records.jobs import JobRecords
from ledgersync.store import Store
from server import create_app
from test_clients_api import BILL, CUBE, LINE, add, manual

mongo = pytest.mark.mongo

YEAR = {"period_start": "2025-04-01", "period_end": "2026-03-31"}
DATES = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
WAIT = 20.0                     # seconds before a stuck job or analysis fails the test instead of hanging it
EVERY_STEP = ["ingest", "classify", "gate", "requests", "snapshot"]
ROWS_LOCKED = "Business Cube Ltd's job is running. Rows can be changed once it finishes."
NOT_CONFIGURED = "Agent jobs need MongoDB and LEDGERSYNC_AGENT_MODEL. Set both in .env."


def stubs(calls=None, held=None, failing=None) -> Nodes:
    """Stub steps that note their names in `calls`. A step named in `held` waits for its event before it
    finishes, and one named in `failing` crashes once. The gate always passes."""
    calls = [] if calls is None else calls
    held = {} if held is None else held
    failing = set() if failing is None else failing

    def stub(name, result=lambda state: {}):
        def node(state):
            calls.append(name)
            if name in held and not held[name].wait(WAIT):
                raise TimeoutError(f"The {name} step was held for too long.")
            if name in failing:
                failing.discard(name)
                raise RuntimeError(f"The {name} step crashed.")
            return result(state)
        return node

    return Nodes(ingest=stub("ingest"), classify=stub("classify"),
                 gate=stub("gate", lambda state: {"gate_passed": True}),
                 fix=stub("fix", lambda state: {"fix_rounds": state["fix_rounds"] + 1}),
                 requests=stub("requests"), snapshot=stub("snapshot"))


class HeldModel(FakeModel):
    """A FakeModel that answers only while `go` is set, so a test can catch documents being read."""

    def __init__(self, replies=()):
        super().__init__(replies)
        self.go = threading.Event()
        self.go.set()

    def chat_json(self, messages, schema, images=None):
        if not self.go.wait(WAIT):
            raise TimeoutError("The model was held for too long.")
        return super().chat_json(messages, schema, images)


def poll(read, done, what: str):
    """Reads until `done` says yes, and fails the test after WAIT seconds."""
    deadline = time.monotonic() + WAIT
    while True:
        value = read()
        if done(value):
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out waiting for {what}.")
        time.sleep(0.05)


def wait_for(api, job_id: str) -> dict:
    """The job once it has stopped preparing."""
    return poll(lambda: api.get(f"/api/agent-jobs/{job_id}").json(), lambda job: job["status"] != "preparing",
                f"job {job_id} to finish")


def reached(calls: list, step: str) -> None:
    poll(lambda: calls, lambda seen: step in seen, f"the {step} step")


def app_for(store, calls=None, held=None, failing=None, *, model=None):
    return create_app(Settings(warmup=False), model_client=model or FakeModel([]), store=store,
                      agent_nodes=stubs(calls, held, failing))


def start(api, client_id: int) -> dict:
    resp = api.post(f"/api/clients/{client_id}/agent-jobs", json=YEAR)
    assert resp.status_code == 202, resp.text
    return resp.json()


def first_state(job_id: str, client_id: int) -> dict:
    return {"job_id": job_id, "client_id": client_id, "period_start": "2025-04-01", "period_end": "2026-03-31",
            "fix_rounds": 0, "gate_passed": False}


@pytest.fixture
def calls():
    return []


@pytest.fixture
def held():
    """Steps that wait for their event before finishing, by name. The api fixture lets them all go at the end."""
    return {}


@pytest.fixture
def failing():
    return set()


@pytest.fixture
def model():
    return HeldModel([transactions_json(ROW)])


@pytest.fixture
def records(mongo_store):
    return JobRecords(mongo_store.db)


@pytest.fixture
def api(mongo_store, records, model, calls, held, failing):
    client = TestClient(app_for(mongo_store, calls, held, failing, model=model), headers={"X-LedgerSync": "1"})
    with client:
        yield client
        model.go.set()
        for event in held.values():
            event.set()
        # No worker may write to the database after the test drops it.
        poll(records.unfinished, lambda jobs: not jobs, "every job to finish")


@pytest.fixture
def hold(held):
    """Keeps a started job in its classify step until the test sets this event (or ends)."""
    held["classify"] = threading.Event()
    return held["classify"]


@pytest.fixture
def busy(api):
    return api.app.state.busy


@pytest.fixture
def runner(api):
    return api.app.state.agent_runner


@pytest.fixture
def client_id(api):
    """A client with no uploads yet."""
    return add(api)["id"]


@pytest.fixture
def client_with_upload(api, client_id):
    manual(api, client_id, BILL, LINE)
    return client_id


@pytest.fixture
def new_client(mongo_store):
    return mongo_store.create_client(ClientFields(**CUBE)).id


@mongo
def test_a_client_with_no_uploads_cant_start_a_job(api, client_id, records, calls):
    resp = api.post(f"/api/clients/{client_id}/agent-jobs", json=YEAR)
    assert resp.status_code == 422
    assert resp.json()["detail"]["message"] == "Add this client's documents before starting a job."
    assert (records.latest_for(client_id), calls) == (None, [])


@mongo
def test_a_job_cant_start_while_documents_are_being_read(api, client_with_upload, busy):
    busy.begin(client_with_upload)
    resp = api.post(f"/api/clients/{client_with_upload}/agent-jobs", json=YEAR)
    assert resp.status_code == 409
    assert resp.json()["detail"]["message"] == "Documents are still being read. Start the job when they're done."
    busy.end(client_with_upload)
    start(api, client_with_upload)


@mongo
def test_a_second_job_and_row_edits_are_refused_while_one_runs(api, client_with_upload, hold, runner):
    cid = client_with_upload
    ledger = api.get(f"/api/clients/{cid}/ledger").json()
    first_row_id, upload_id = ledger["transactions"][0]["id"], ledger["uploads"][0]["id"]
    job = start(api, cid)
    again = api.post(f"/api/clients/{cid}/agent-jobs", json=YEAR)
    edit = api.patch(f"/api/clients/{cid}/rows/{first_row_id}", json={"include": True})
    assert again.status_code == edit.status_code == 409
    assert again.json()["detail"]["message"] == ("A job is already running for Business Cube Ltd. "
                                                 "Wait for it to finish or cancel it.")
    assert edit.json()["detail"]["message"].endswith("job is running. Rows can be changed once it finishes.")
    others = [api.delete(f"/api/clients/{cid}/rows/{first_row_id}"),
              api.post(f"/api/clients/{cid}/uploads", json={"transactions": [BILL]}),
              api.delete(f"/api/clients/{cid}/uploads/{upload_id}"),
              api.post("/api/analyze", data={"text": "BT broadband 72.00", "client_id": str(cid)})]
    assert [(r.status_code, r.json()["detail"]["message"]) for r in others] == [(409, ROWS_LOCKED)] * 4
    with pytest.raises(JobRunning, match=re.escape(ROWS_LOCKED)):   # an analysis that was queued before the job
        with runner.reading(cid):
            pass
    hold.set()
    assert wait_for(api, job["id"])["status"] == "awaiting_review"
    assert api.patch(f"/api/clients/{cid}/rows/{first_row_id}", json={"include": True}).status_code == 200


@mongo
def test_a_finished_job_is_awaiting_review_with_its_events(api, client_with_upload, calls):
    job = wait_for(api, api.post(f"/api/clients/{client_with_upload}/agent-jobs", json=YEAR).json()["id"])
    assert (job["status"], job["stage"]) == ("awaiting_review", "done")
    events = api.get(f"/api/agent-jobs/{job['id']}/events").json()
    assert events[0]["stage"] == "ingest"
    assert [e["message"] for e in events] == [f"Started {step}" for step in EVERY_STEP] + [
        "Finished. The trial balance is ready for review."]
    assert [e["seq"] for e in api.get(f"/api/agent-jobs/{job['id']}/events?after=4").json()] == [5, 6]
    assert calls == EVERY_STEP


@mongo
def test_an_unknown_job_is_not_found(api):
    unknown = "0" * 32
    for resp in (api.get(f"/api/agent-jobs/{unknown}"), api.get(f"/api/agent-jobs/{unknown}/events"),
                 api.post(f"/api/agent-jobs/{unknown}/cancel"), api.post(f"/api/agent-jobs/{unknown}/retry")):
        assert (resp.status_code, resp.json()["detail"]["message"]) == (404, "This job was not found.")


@mongo
def test_a_cancelled_job_stops_before_its_next_step(api, client_with_upload, hold, calls):
    job = start(api, client_with_upload)
    reached(calls, "classify")
    cancelling = api.post(f"/api/agent-jobs/{job['id']}/cancel").json()
    assert (cancelling["status"], cancelling["cancel_requested"]) == ("preparing", True)
    hold.set()
    assert wait_for(api, job["id"])["status"] == "cancelled"
    assert calls == ["ingest", "classify"]
    assert api.get(f"/api/agent-jobs/{job['id']}/events").json()[-1]["message"] == "Cancelled."


@mongo
def test_a_cancel_during_the_last_step_isnt_overwritten_by_the_finish(api, client_with_upload, held, calls):
    held["snapshot"] = threading.Event()
    job = start(api, client_with_upload)
    reached(calls, "snapshot")
    api.post(f"/api/agent-jobs/{job['id']}/cancel")
    held["snapshot"].set()
    assert wait_for(api, job["id"])["status"] == "cancelled"
    assert calls == EVERY_STEP


@mongo
def test_a_failed_job_is_retried_from_the_step_that_failed(api, client_with_upload, failing, calls):
    failing.add("gate")
    job = wait_for(api, start(api, client_with_upload)["id"])
    assert (job["status"], job["stage"]) == ("failed", "gate")
    assert job["error"] == "Something went wrong at the gate step. Details are in the server log."
    assert api.get(f"/api/agent-jobs/{job['id']}/events").json()[-1]["message"] == f"Stopped: {job['error']}"
    retried = api.post(f"/api/agent-jobs/{job['id']}/retry")
    assert (retried.status_code, retried.json()["status"], retried.json()["error"]) == (202, "preparing", None)
    assert wait_for(api, job["id"])["status"] == "awaiting_review"
    assert calls == ["ingest", "classify", "gate", "gate", "requests", "snapshot"]
    again = api.post(f"/api/agent-jobs/{job['id']}/retry")
    assert (again.status_code, again.json()["detail"]["message"]) == (422, "Only a failed job can be retried.")


@mongo
def test_two_quick_starts_make_one_job(api, client_with_upload, hold, runner):
    both_ready = threading.Barrier(2)
    outcomes = []

    def go():
        both_ready.wait()
        try:
            outcomes.append(runner.start(client_with_upload, *DATES).status)
        except JobRunning as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert sorted(outcomes) == ["job_running", "preparing"]


@mongo
def test_documents_being_read_hold_back_a_job_until_they_are_saved(api, client_with_upload, model, busy):
    cid = client_with_upload
    model.go.clear()
    analysis = api.post("/api/analyze", data={"text": "BT broadband 72.00", "client_id": str(cid)})
    assert analysis.status_code == 202, analysis.text
    poll(lambda: busy.busy(cid), bool, "the analysis to start")
    refused = api.post(f"/api/clients/{cid}/agent-jobs", json=YEAR)
    assert (refused.status_code, refused.json()["detail"]["message"]) == (
        409, "Documents are still being read. Start the job when they're done.")
    model.go.set()
    done = poll(lambda: api.get(f"/api/jobs/{analysis.json()['job_id']}").json(),
                lambda job: job["status"] not in ("queued", "running"), "the analysis to finish")
    assert (done["status"], busy.busy(cid)) == ("succeeded", False)
    assert len(api.get(f"/api/clients/{cid}/ledger").json()["uploads"]) == 2   # the analysis saved its rows
    start(api, cid)


@mongo
def test_without_a_model_or_injected_steps_jobs_are_not_configured(mongo_store):
    with TestClient(create_app(Settings(warmup=False), model_client=FakeModel([]), store=mongo_store)) as api:
        resp = api.post("/api/clients/1/agent-jobs", json=YEAR)
    assert resp.status_code == 503
    assert resp.json()["detail"] == {"code": "agent_not_configured", "message": NOT_CONFIGURED}


def test_without_mongodb_jobs_are_not_configured(tmp_path):
    app = create_app(Settings(warmup=False), model_client=FakeModel([]), store=Store(tmp_path / "ledgersync.db"),
                     agent_nodes=stubs())
    with TestClient(app) as api:
        cid = add(api)["id"]
        manual(api, cid, BILL)
        responses = [api.post(f"/api/clients/{cid}/agent-jobs", json=YEAR), api.get(f"/api/agent-jobs/{'0' * 32}")]
        assert api.patch(f"/api/clients/{cid}/rows/1", json={"include": True}).status_code == 200   # no row lock
    assert [(r.status_code, r.json()["detail"]["message"]) for r in responses] == [(503, NOT_CONFIGURED)] * 2


@mongo
def test_with_a_model_set_the_built_in_steps_run_and_the_classify_agent_asks_for_the_models_key(mongo_store, records):
    settings = Settings(warmup=False, agent_model="some/model")   # no OPENROUTER_API_KEY
    with TestClient(create_app(settings, model_client=FakeModel([]), store=mongo_store)) as api:   # needs no model yet
        cid = add(api)["id"]
        manual(api, cid, BILL)
        job = wait_for(api, start(api, cid)["id"])
        messages = [e["message"] for e in api.get(f"/api/agent-jobs/{job['id']}/events").json()]
    assert (job["status"], job["stage"], job["error"]) == ("failed", "classify", AGENT_NOT_CONFIGURED)
    assert messages == ["Started ingest", "Found 1 document and 1 row. No rows need a look.", "Started classify",
                        f"Stopped: {AGENT_NOT_CONFIGURED}"]


@mongo
def test_an_unfinished_job_resumes_when_the_app_starts(mongo_store, records, new_client, calls):
    job = records.create(new_client, *DATES)          # left preparing, as after a crash
    with TestClient(app_for(mongo_store, calls)) as api:
        assert wait_for(api, job.id)["status"] == "awaiting_review"
    assert calls == EVERY_STEP


@mongo
def test_a_job_that_stopped_mid_way_carries_on_from_its_checkpoint(mongo_store, records, new_client, calls):
    job = records.create(new_client, *DATES)
    saver = MongoDBSaver(mongo_store.client, f"{mongo_store.db_name}_checkpoints")
    crashed = build_graph(stubs(failing={"classify"}), saver)
    with pytest.raises(RuntimeError):                 # the server stopped during classify
        crashed.invoke(first_state(job.id, new_client), {"configurable": {"thread_id": job.id}}, durability="sync")
    with TestClient(app_for(mongo_store, calls)) as api:
        assert wait_for(api, job.id)["status"] == "awaiting_review"
    assert calls == ["classify", "gate", "requests", "snapshot"]


@mongo
def test_a_job_that_cant_be_loaded_or_carried_on_doesnt_stop_the_app(mongo_store, mongo_db, records, new_client):
    mongo_db.jobs.insert_one({"_id": "f" * 32, "client_id": 999, "status": "preparing",
                              "created_at": "2026-01-01T00:00:00.000000+00:00"})   # corrupt: most fields missing
    job = records.create(new_client, *DATES)
    mongo_store.client[f"{mongo_store.db_name}_checkpoints"].checkpoints.insert_one(
        {"thread_id": job.id, "checkpoint_ns": "", "checkpoint_id": "1", "type": "msgpack",
         "checkpoint": b"not a checkpoint", "metadata": {}})
    with TestClient(app_for(mongo_store)) as api:
        failed = wait_for(api, job.id)
    assert (failed["status"], failed["error"]) == (
        "failed", "This job couldn't carry on from where it stopped. Details are in the server log.")
    assert mongo_db.jobs.find_one({"_id": "f" * 32})["status"] == "preparing"   # skipped and left alone
