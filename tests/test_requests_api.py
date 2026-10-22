"""The client's upload link: what it shows, and a file sent through it. The file is read into a new upload for
the client (past the row lock), the request is marked received, its exception is resolved, and a run starts that
supersedes the job awaiting review, or the running job once it finishes (and never while other documents of the
client are still being read). One upload at a time can claim a request. Also the hourly reminder thread."""
import threading
from types import SimpleNamespace

import pymupdf
import pytest
from fakes import FakeModel, transactions_json
from fastapi.testclient import TestClient

import server
from ledgersync.agent.tools import ToolDeps
from ledgersync.config import Settings
from ledgersync.errors import ModelError
from ledgersync.notify import ConsoleNotifier, send_document_requests
from ledgersync.records.audit import AuditLog
from ledgersync.records.exceptions import DocumentRequests, Exceptions
from ledgersync.records.jobs import JobRecords
from ledgersync.store import Store
from server import create_app
from test_agent_jobs_api import DATES, WAIT, poll, start, stubs, wait_for
from test_clients_api import BILL, add, manual

mongo = pytest.mark.mongo

UNKNOWN_LINK = "This link isn't valid. Ask your accountant for a new one."
ACME = {"description": "Acme Ltd invoice 1043", "date": "2026-03-01", "amount": 1200.0, "direction": "out",
        "account": "5000 Purchases", "vat": None, "currency": "GBP"}


def pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


PDF = pdf_bytes("Acme Ltd invoice 1043. Total GBP 1,200.00")


class HeldModel(FakeModel):
    """A FakeModel that holds back reading any document whose text holds `marker` until `go` is set."""

    def __init__(self, replies):
        super().__init__(replies)
        self.marker = None
        self.go = threading.Event()

    def chat_json(self, messages, schema, images=None):
        if self.marker is not None and self.marker in str(messages) and not self.go.wait(WAIT):
            raise TimeoutError("The model was held for too long.")
        return super().chat_json(messages, schema, images)


@pytest.fixture
def held():
    return {}


@pytest.fixture
def notifier():
    return ConsoleNotifier()


@pytest.fixture
def records(mongo_store):
    return JobRecords(mongo_store.db)


@pytest.fixture
def exceptions(mongo_store):
    return Exceptions(mongo_store.db)


@pytest.fixture
def doc_requests(mongo_store):
    return DocumentRequests(mongo_store.db)


@pytest.fixture
def model():
    return HeldModel([transactions_json(ACME)] * 5)


@pytest.fixture
def api(mongo_store, records, held, notifier, model):
    app = create_app(Settings(warmup=False), model_client=model, store=mongo_store, agent_nodes=stubs(held=held),
                     notifier=notifier)
    with TestClient(app, headers={"X-LedgerSync": "1"}) as client:
        yield client
        model.go.set()
        for event in held.values():
            event.set()
        poll(records.unfinished, lambda jobs: not jobs, "every job to finish")   # nothing writes after the drop


@pytest.fixture
def cube_id(api):
    cid = add(api)["id"]
    manual(api, cid, BILL)
    return cid


@pytest.fixture
def finished_job(api, cube_id):
    job = wait_for(api, start(api, cube_id)["id"])
    assert job["status"] == "awaiting_review"
    return job["id"]


def ask(exceptions, doc_requests, client_id, job_id, token, document_type="Invoice from Acme Ltd"):
    exception = exceptions.log(client_id=client_id, job_id=job_id, stage="classify",
                               description=f"No {document_type} for the £1,200.00 paid to Acme",
                               assumption="Booked as 5000 Purchases", confidence=0.4, row_ids=[])
    made = doc_requests.create(client_id=client_id, job_id=job_id, exception_id=exception["id"],
                               document_type=document_type, period="March 2026",
                               reason="It explains the £1,200.00 paid to Acme.")
    if token:
        doc_requests.mark_sent([made["id"]], token=token, at=server.dt.datetime.now(server.dt.timezone.utc))
    return SimpleNamespace(token=token, request_id=made["id"], exception_id=exception["id"], job_id=job_id)


@pytest.fixture
def sent_request(exceptions, doc_requests, cube_id, finished_job):
    """What the finished job asked the client for, emailed with the link token "tok-1"."""
    return ask(exceptions, doc_requests, cube_id, finished_job, "tok-1")


def send(api, sent, data=PDF, name="acme.pdf"):
    return api.post(f"/api/requests/{sent.token}/{sent.request_id}/file", files={"file": (name, data)})


def finished(api, analysis_job_id: str) -> dict:
    """The analysis once it has finished, and all that follows it."""
    return poll(lambda: api.get(f"/api/jobs/{analysis_job_id}").json(),
                lambda job: job["status"] not in ("queued", "running"), "the client's file to be read")


def sent_and_read(api, sent) -> dict:
    """Sends the PDF through the link and waits for its analysis (and all that follows it) to finish."""
    resp = send(api, sent)
    assert resp.status_code == 202, resp.text
    done = finished(api, resp.json()["job_id"])
    assert done["status"] == "succeeded", done
    return done["result"]


@mongo
def test_an_unknown_token_is_not_found(api):
    resp = api.get("/api/requests/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"]["message"] == "This link isn't valid. Ask your accountant for a new one."


@mongo
def test_a_link_shows_the_client_only_the_documents_it_asks_for(api, sent_request, exceptions, doc_requests, cube_id,
                                                                 finished_job):
    ask(exceptions, doc_requests, cube_id, finished_job, "tok-2", "Bank statements")   # another email's
    ask(exceptions, doc_requests, cube_id, finished_job, None, "Receipts")             # not emailed yet
    assert api.get("/api/requests/tok-1").json() == {"client_name": "Business Cube Ltd", "documents": [
        {"id": sent_request.request_id, "document_type": "Invoice from Acme Ltd", "period": "March 2026",
         "reason": "It explains the £1,200.00 paid to Acme.", "status": "pending"}]}


@mongo
def test_a_received_file_is_analysed_and_resolves_its_exception(api, sent_request, exceptions, cube_id):
    token, job_id = sent_request.token, sent_request.job_id
    result = sent_and_read(api, sent_request)
    assert api.get(f"/api/requests/{token}").json()["documents"][0]["status"] == "received"
    assert exceptions.for_job(job_id)[0]["status"] == "resolved"
    assert exceptions.for_job(job_id)[0]["note"] == "The client sent the document."
    ledger = api.get(f"/api/clients/{cube_id}/ledger").json()
    assert [u["name"] for u in ledger["uploads"]][0] == "acme.pdf" and result["upload_id"] == ledger["uploads"][0]["id"]
    original = api.get(f"/api/clients/{cube_id}/uploads/{result['upload_id']}/file")
    assert (original.status_code, original.content) == (200, PDF)


@mongo
def test_receiving_and_resolving_are_audited_as_the_system(api, sent_request, mongo_store, cube_id):
    sent_and_read(api, sent_request)
    entries = AuditLog(mongo_store.db).entries(client_id=cube_id, job_id=sent_request.job_id)
    assert [(e["actor"], e["action"], e["target"]) for e in entries] == [
        ("system", "receive_document", {"request_id": sent_request.request_id}),
        ("system", "resolve_exception", {"exception_id": sent_request.exception_id})]


@mongo
def test_a_file_for_a_job_awaiting_review_starts_a_run_that_supersedes_it(api, sent_request, finished_job, records,
                                                                          cube_id):
    sent_and_read(api, sent_request)
    assert records.get(finished_job).status == "superseded"
    assert records.latest_for(cube_id).supersedes == finished_job
    assert wait_for(api, records.latest_for(cube_id).id)["status"] == "awaiting_review"


@mongo
def test_a_file_sent_while_a_job_runs_is_saved_and_starts_a_new_run_when_it_finishes(api, sent_request, held,
                                                                                      records, cube_id):
    held["classify"] = threading.Event()
    running = start(api, cube_id)["id"]
    sent_and_read(api, sent_request)                       # read and saved past the row lock
    assert len(api.get(f"/api/clients/{cube_id}/ledger").json()["uploads"]) == 2
    job = records.get(running)
    assert (job.status, job.new_documents) == ("preparing", True)
    held["classify"].set()
    poll(lambda: records.get(running).status, lambda status: status != "preparing", "the running job to finish")
    assert records.get(running).status == "superseded"
    replacement = records.latest_for(cube_id)
    assert replacement.supersedes == running and wait_for(api, replacement.id)["status"] == "awaiting_review"


@mongo
def test_a_file_for_a_cancelled_job_starts_no_run(api, sent_request, held, records, cube_id):
    held["classify"] = threading.Event()
    job = start(api, cube_id)["id"]
    api.post(f"/api/agent-jobs/{job}/cancel")
    held["classify"].set()
    assert wait_for(api, job)["status"] == "cancelled"
    sent_and_read(api, sent_request)
    assert records.latest_for(cube_id).id == job and records.get(job).status == "cancelled"


@mongo
def test_a_document_already_received_is_refused(api, sent_request):
    sent_and_read(api, sent_request)
    again = send(api, sent_request)
    assert (again.status_code, again.json()["detail"]["message"]) == (422, "This document has already arrived. Thank you.")


@mongo
def test_a_document_asked_for_on_another_link_or_not_yet_asked_for_is_refused(api, sent_request, exceptions,
                                                                             doc_requests, cube_id, finished_job):
    theirs = ask(exceptions, doc_requests, cube_id, finished_job, "tok-2", "Bank statements")
    unsent = ask(exceptions, doc_requests, cube_id, finished_job, None, "Receipts")
    for request_id in (theirs.request_id, unsent.request_id, "0" * 32):
        resp = api.post(f"/api/requests/tok-1/{request_id}/file", files={"file": ("acme.pdf", PDF)})
        assert (resp.status_code, resp.json()["detail"]["message"]) == (404, "This link doesn't ask for that document.")
    resp = api.post(f"/api/requests/nope/{sent_request.request_id}/file", files={"file": ("acme.pdf", PDF)})
    assert (resp.status_code, resp.json()["detail"]["message"]) == (404, UNKNOWN_LINK)


@mongo
def test_an_upload_needs_the_ledgersync_header_and_a_supported_file(api, sent_request):
    bare = TestClient(api.app).post(f"/api/requests/tok-1/{sent_request.request_id}/file",
                                    files={"file": ("acme.pdf", PDF)})
    assert (bare.status_code, bare.json()["detail"]["code"]) == (403, "refused_request")
    odd = send(api, sent_request, data=b"PK\x03\x04 not a document", name="notes.docx")
    assert odd.status_code == 415
    assert api.get("/api/requests/tok-1").json()["documents"][0]["status"] == "pending"


@mongo
def test_a_file_with_no_transactions_leaves_the_request_waiting(api, model, sent_request):
    model.replies[:] = [transactions_json()]
    resp = send(api, sent_request)
    done = poll(lambda: api.get(f"/api/jobs/{resp.json()['job_id']}").json(),
                lambda job: job["status"] not in ("queued", "running"), "the client's file to be read")
    assert (done["status"], done["error"]["message"]) == (
        "failed", "No transactions were found in 'acme.pdf'. Check it's the document asked for, then send it again.")
    assert api.get("/api/requests/tok-1").json()["documents"][0]["status"] == "pending"


def test_without_mongodb_every_link_is_unknown(tmp_path):
    app = create_app(Settings(warmup=False), model_client=FakeModel([]), store=Store(tmp_path / "ledgersync.db"))
    with TestClient(app, headers={"X-LedgerSync": "1"}) as api:
        responses = [api.get("/api/requests/tok-1"),
                     api.post("/api/requests/tok-1/r1/file", files={"file": ("acme.pdf", PDF)})]
    assert [(r.status_code, r.json()["detail"]["message"]) for r in responses] == [(404, UNKNOWN_LINK)] * 2


@mongo
def test_the_reminder_thread_survives_an_error_and_stops_with_the_app(mongo_store, monkeypatch):
    calls = []

    def broken(deps, now):
        calls.append(now)
        raise RuntimeError("The reminders failed.")

    monkeypatch.setattr(server, "send_reminders", broken)

    def reminder_threads():
        return [t for t in threading.enumerate() if t.name == "document-reminders"]

    with TestClient(create_app(Settings(warmup=False), model_client=FakeModel([]), store=mongo_store)) as api:
        poll(lambda: calls, bool, "the first reminder pass")
        assert [t.is_alive() for t in reminder_threads()] == [True]
        assert api.get("/api/health").status_code == 200
    assert reminder_threads() == []


@mongo
def test_a_second_upload_while_the_first_is_being_read_is_refused_and_leaves_one_upload(api, model, sent_request,
                                                                                       cube_id):
    model.marker = "Acme"                                  # holds the reading of the client's file
    first = send(api, sent_request)
    assert first.status_code == 202, first.text
    again = send(api, sent_request)                        # a second tab, a reload or the back button
    assert (again.status_code, again.json()["detail"]["message"]) == (422, "This document has already arrived. Thank you.")
    assert api.get("/api/requests/tok-1").json()["documents"][0]["status"] == "received"
    model.go.set()
    assert finished(api, first.json()["job_id"])["status"] == "succeeded"
    assert [u["name"] for u in api.get(f"/api/clients/{cube_id}/ledger").json()["uploads"]].count("acme.pdf") == 1


@mongo
def test_a_failed_reading_lets_go_of_the_request_so_the_file_can_be_sent_again(api, model, sent_request):
    model.replies[:] = [ModelError("The model's answer was cut off."), transactions_json(ACME)]
    failed = finished(api, send(api, sent_request).json()["job_id"])
    assert (failed["status"], failed["error"]["message"]) == ("failed", "The model's answer was cut off.")
    assert api.get("/api/requests/tok-1").json()["documents"][0]["status"] == "pending"
    sent_and_read(api, sent_request)
    assert api.get("/api/requests/tok-1").json()["documents"][0]["status"] == "received"


@mongo
def test_the_new_run_waits_for_documents_still_being_read_then_starts_once(api, model, sent_request, finished_job,
                                                                           records, cube_id, mongo_store):
    model.marker = "HOLD"                                  # holds the accountant's own analysis
    reading = api.post("/api/analyze", data={"text": "HOLD BT broadband 72.00", "client_id": str(cube_id)})
    assert reading.status_code == 202, reading.text
    poll(lambda: api.app.state.busy.busy(cube_id), bool, "the accountant's analysis to start")
    sent_and_read(api, sent_request)                       # the client's file is read and saved meanwhile
    assert records.latest_for(cube_id).id == finished_job
    assert records.get(finished_job).status == "awaiting_review"
    model.go.set()
    assert finished(api, reading.json()["job_id"])["status"] == "succeeded"
    replacement = poll(lambda: records.latest_for(cube_id), lambda job: job.id != finished_job, "the new run")
    assert replacement.supersedes == finished_job and records.get(finished_job).status == "superseded"
    assert wait_for(api, replacement.id)["status"] == "awaiting_review"
    assert mongo_store.db.jobs.count_documents({"client_id": cube_id}) == 2      # the finished job and one new run


@mongo
def test_a_request_a_newer_run_asked_again_shows_as_replaced_and_takes_no_upload(api, sent_request, exceptions,
                                                                                   doc_requests, records, cube_id,
                                                                                   finished_job, mongo_store, notifier):
    newer = records.create(cube_id, *DATES, supersedes=finished_job)
    records.update(newer.id, status="awaiting_review", stage="done")
    ask(exceptions, doc_requests, cube_id, newer.id, None)               # the same document, not emailed yet
    deps = ToolDeps(store=mongo_store, records=records, audit=AuditLog(mongo_store.db), notifier=notifier)
    assert send_document_requests(deps, cube_id, newer.id) == 1
    assert api.get("/api/requests/tok-1").json()["documents"][0]["status"] == "replaced"
    refused = send(api, sent_request)
    assert (refused.status_code, refused.json()["detail"]["message"]) == (422, "This document has already arrived. Thank you.")
