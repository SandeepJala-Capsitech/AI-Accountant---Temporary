"""The exceptions the agent logs when it can't settle something, and the documents it asks the client for: the two
tools, what they refuse, that neither ever raises, and that each new write is audited once."""
import datetime as dt

import pytest

from ledgersync.agent.tools import ToolDeps, exception_tools
from ledgersync.errors import LedgerSyncError, NotFound
from ledgersync.models import ClientFields, Transaction
from ledgersync.notify import ConsoleNotifier
from ledgersync.records.audit import AuditLog
from ledgersync.records.jobs import JobRecords

pytestmark = pytest.mark.mongo

YEAR = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke",
                    contact_email="jenny@businesscube.co.uk")
OTHER = ClientFields(name="Other Traders", business_type="sole_trader", contact_name="Sam Other")
ACME = Transaction(date=dt.date(2026, 3, 2), description="ACME LTD", direction="out", gross="1200.00",
                   account_code="9998", document_type="statement", counterparty="Acme Ltd", document_ref="l1")
REFUSED_REASON = "Keep the reason under 200 characters, as plain text without links or email addresses."
T = dt.datetime(2026, 4, 10, 9, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def deps(mongo_store, mongo_db):
    return ToolDeps(store=mongo_store, records=JobRecords(mongo_db), audit=AuditLog(mongo_db),
                    notifier=ConsoleNotifier())


@pytest.fixture
def cube_id(mongo_store):
    client = mongo_store.create_client(CUBE)
    mongo_store.add_upload(client.id, "Bank.csv", "table", [ACME])
    return client.id


@pytest.fixture
def row_id(mongo_store, cube_id):
    """The £1,200.00 paid to Acme, with no invoice behind it."""
    return mongo_store.rows(cube_id)[0].id


@pytest.fixture
def job(deps, cube_id):
    return deps.records.create(cube_id, *YEAR)


@pytest.fixture
def tools(deps, cube_id, job):
    return exception_tools(deps, cube_id, job.id, "classify")


@pytest.fixture
def exception_id(tools, row_id):
    return log(tools, [row_id])["exception_id"]


def log(tools, row_ids, **changes):
    args = {"description": "No invoice for the £1,200.00 paid to Acme", "assumption": "Booked as 5000 Purchases",
            "confidence": 0.4, "row_ids": row_ids}
    return tools["log_exception"].invoke({**args, **changes})


def ask(tools, exception_id, **changes):
    args = {"document_type": "Invoice", "period": "March 2026", "reason": "It explains the £1,200.00 paid to Acme.",
            "exception_id": exception_id}
    return tools["request_client_document"].invoke({**args, **changes})


# --- log_exception ------------------------------------------------------------------------------------------

def test_the_agent_gets_two_exception_tools_by_name(tools):
    assert set(tools) == {"log_exception", "request_client_document"}
    assert all(t.name == name and t.description for name, t in tools.items())


def test_the_same_exception_twice_is_logged_once(tools, deps, job, row_id):
    args = {"description": "No invoice for the £1,200.00 paid to Acme", "assumption": "Booked as 5000 Purchases",
            "confidence": 0.4, "row_ids": [row_id]}
    assert tools["log_exception"].invoke(args) == tools["log_exception"].invoke(args)
    assert len(deps.exceptions.for_job(job.id)) == 1


def test_a_logged_exception_is_open_with_its_assumption_and_rows(tools, deps, cube_id, job, row_id):
    result = log(tools, [row_id])
    [logged] = deps.exceptions.for_job(job.id)
    assert result == {"ok": True, "exception_id": logged["id"]}
    assert {k: logged[k] for k in ("client_id", "job_id", "stage", "description", "assumption", "confidence",
                                   "row_ids", "status")} == {
        "client_id": cube_id, "job_id": job.id, "stage": "classify",
        "description": "No invoice for the £1,200.00 paid to Acme", "assumption": "Booked as 5000 Purchases",
        "confidence": 0.4, "row_ids": [row_id], "status": "open"}


def test_other_rows_or_another_description_are_another_exception(tools, deps, job, row_id):
    first = log(tools, [row_id])["exception_id"]
    assert log(tools, [])["exception_id"] != first
    assert log(tools, [row_id], description="No receipt for the train fare")["exception_id"] != first
    assert len(deps.exceptions.for_job(job.id)) == 3


def test_a_new_exception_is_audited_once_and_a_repeat_not_at_all(tools, deps, cube_id, job, row_id):
    exception_id = log(tools, [row_id])["exception_id"]
    log(tools, [row_id])
    [entry] = deps.audit.entries(client_id=cube_id, job_id=job.id)
    assert (entry["actor"], entry["action"], entry["target"], entry["before"]) == (
        "agent", "log_exception", {"exception_id": exception_id}, None)
    assert entry["after"]["description"] == "No invoice for the £1,200.00 paid to Acme"
    assert deps.audit.verify() is None


@pytest.mark.parametrize("changes, error", [
    ({"description": " "}, "Describe what couldn't be settled in 1 to 500 characters."),
    ({"assumption": "x" * 501}, "Give the assumption you made in 1 to 500 characters."),
    ({"confidence": 1.5}, "Confidence must be from 0 to 1."),
])
def test_log_exception_checks_what_it_is_given(tools, deps, job, changes, error):
    assert log(tools, [], **changes) == {"ok": False, "error": error}
    assert deps.exceptions.for_job(job.id) == []


def test_an_exception_can_only_be_about_this_clients_rows(tools, mongo_store, deps, job, row_id):
    other = mongo_store.create_client(OTHER)
    mongo_store.add_upload(other.id, "Theirs.csv", "table", [ACME])
    theirs = mongo_store.rows(other.id)[0].id
    assert log(tools, [row_id, theirs, 999]) == {"ok": False, "error": f"These rows weren't found: {theirs}, 999."}
    assert deps.exceptions.for_job(job.id) == []


def test_log_exception_never_raises(tools, deps, monkeypatch):
    def broken(**kwargs):
        raise LedgerSyncError("The database could not be reached.")

    monkeypatch.setattr(deps.exceptions, "log_once", broken)
    assert log(tools, []) == {"ok": False, "error": "The database could not be reached."}


# --- request_client_document --------------------------------------------------------------------------------

def test_a_reason_with_a_link_is_refused(tools, exception_id):
    result = tools["request_client_document"].invoke({"document_type": "Invoice", "period": "March 2026",
                                                      "reason": "See http://example.com", "exception_id": exception_id})
    assert result == {"ok": False, "error": "Keep the reason under 200 characters, as plain text without links or email addresses."}


@pytest.mark.parametrize("reason", ["x" * 201, "Look at WWW.example.com", "Ask jenny@example.com", "HTTPS://x.test", "",
                                    "Upload it at acme-invoices.co/pay", "See ACME.COM"])
def test_a_long_reason_a_link_or_an_email_address_is_refused(tools, deps, job, exception_id, reason):
    assert ask(tools, exception_id, reason=reason) == {"ok": False, "error": REFUSED_REASON}
    assert deps.requests.pending_for(job.id) == []


@pytest.mark.parametrize("changes, error", [
    ({"document_type": "x" * 61}, "Name the document in 1 to 60 characters, as plain text without links or email addresses."),
    ({"document_type": "Invoice at www.acme.test"}, "Name the document in 1 to 60 characters, as plain text without links or email addresses."),
    ({"period": "x" * 41}, "Give the period in 1 to 40 characters, as plain text without links or email addresses."),
    ({"period": "@acme"}, "Give the period in 1 to 40 characters, as plain text without links or email addresses."),
    ({"document_type": "Invoice from acme.co"}, "Name the document in 1 to 60 characters, as plain text without links or email addresses."),
    ({"period": "see acme-invoices.org"}, "Give the period in 1 to 40 characters, as plain text without links or email addresses."),
])
def test_a_long_document_type_or_period_is_refused(tools, exception_id, changes, error):
    assert ask(tools, exception_id, **changes) == {"ok": False, "error": error}


@pytest.mark.parametrize("reason", ["£1,200.00 paid to Acme Ltd", "e.g. the March statement", "Invoice No. 1043 for Acme Ltd."])
def test_ordinary_text_with_full_stops_is_allowed(tools, exception_id, reason):
    assert ask(tools, exception_id, reason=reason)["ok"] is True


def test_a_request_must_be_for_an_exception_of_this_job(deps, cube_id, tools, row_id):
    elsewhere = deps.records.create(cube_id, *YEAR)
    theirs = log(exception_tools(deps, cube_id, elsewhere.id, "classify"), [row_id])["exception_id"]
    expected = {"ok": False, "error": "Give the exception_id that log_exception gave you for this job."}
    assert ask(tools, theirs) == expected
    assert ask(tools, "0" * 32) == expected


def test_a_request_is_pending_until_the_run_ends_and_is_audited_once(tools, deps, cube_id, job, exception_id):
    first = ask(tools, exception_id)
    assert ask(tools, exception_id) == first                       # replayed after a restart: the same request
    [pending] = deps.requests.pending_for(job.id)
    assert first == {"ok": True, "request_id": pending["id"]}
    assert {k: pending[k] for k in ("client_id", "job_id", "exception_id", "document_type", "period", "reason",
                                    "status", "token")} == {
        "client_id": cube_id, "job_id": job.id, "exception_id": exception_id, "document_type": "Invoice",
        "period": "March 2026", "reason": "It explains the £1,200.00 paid to Acme.", "status": "pending",
        "token": None}
    entries = [e for e in deps.audit.entries(client_id=cube_id, job_id=job.id) if e["action"] == "request_client_document"]
    assert [(e["actor"], e["target"]) for e in entries] == [("agent", {"request_id": pending["id"]})]


def test_asking_again_for_the_same_document_in_a_job_gives_the_same_request(tools, deps, cube_id, job, exception_id):
    another = log(tools, [], description="No statement for March")["exception_id"]
    first = ask(tools, exception_id)
    again = ask(tools, another, document_type="  INVOICE ", period="march  2026", reason="Another reason.")
    assert again == first and len(deps.requests.pending_for(job.id)) == 1
    entries = [e for e in deps.audit.entries(client_id=cube_id, job_id=job.id) if e["action"] == "request_client_document"]
    assert len(entries) == 1
    assert ask(tools, exception_id, period="February 2026")["request_id"] != first["request_id"]


def test_line_breaks_in_what_the_client_reads_are_flattened(tools, deps, job, exception_id):
    ask(tools, exception_id, reason="It explains\n\nthe payment.", document_type=" Invoice\r\nfrom Acme ")
    [pending] = deps.requests.pending_for(job.id)
    assert (pending["document_type"], pending["reason"]) == ("Invoice from Acme", "It explains the payment.")


# --- the records themselves ---------------------------------------------------------------------------------

def test_resolving_an_exception_keeps_the_note(deps, job, exception_id):
    resolved = deps.exceptions.resolve(exception_id, note="The client sent the document.")
    assert (resolved["status"], resolved["note"]) == ("resolved", "The client sent the document.")
    assert deps.exceptions.for_job(job.id) == [resolved]
    with pytest.raises(NotFound):
        deps.exceptions.resolve("0" * 32, note="x")


def test_a_request_is_sent_found_by_its_token_and_received(tools, deps, job, exception_id):
    request_id = ask(tools, exception_id)["request_id"]
    deps.requests.mark_sent([request_id], token="tok", at=T)
    assert deps.requests.pending_for(job.id) == []
    [sent] = deps.requests.by_token("tok")
    assert (sent["id"], sent["status"], sent["sent_at"]) == (request_id, "sent", "2026-04-10T09:00:00.000000+00:00")
    claimed = deps.requests.claim(request_id, token="tok", now=T + dt.timedelta(days=1))
    assert (claimed["status"], claimed["receiving_since"]) == ("receiving", "2026-04-11T09:00:00.000000+00:00")
    received = deps.requests.mark_received(request_id, upload_id=7, at=T + dt.timedelta(days=1))
    assert (received["status"], received["upload_id"]) == ("received", 7)
    assert deps.requests.by_token("tok") == [received] and deps.requests.by_token("other") == []
    assert deps.requests.claim(request_id, token="tok", now=T + dt.timedelta(days=30)) is None


def sent(tools, deps, exception_id) -> str:
    request_id = ask(tools, exception_id)["request_id"]
    deps.requests.mark_sent([request_id], token="tok", at=T)
    return request_id


def test_only_a_request_being_received_becomes_received(tools, deps, exception_id):
    request_id = sent(tools, deps, exception_id)
    assert deps.requests.mark_received(request_id, upload_id=7, at=T) is None
    assert deps.requests.get(request_id)["status"] == "sent"


def test_one_upload_at_a_time_claims_a_request_on_its_own_link(tools, deps, exception_id):
    request_id = sent(tools, deps, exception_id)
    assert deps.requests.claim(request_id, token="other", now=T) is None
    assert deps.requests.claim(request_id, token="tok", now=T)["status"] == "receiving"
    assert deps.requests.claim(request_id, token="tok", now=T + dt.timedelta(minutes=14)) is None


def test_a_claim_older_than_15_minutes_can_be_claimed_again(tools, deps, exception_id):
    request_id = sent(tools, deps, exception_id)
    stalled = deps.requests.claim(request_id, token="tok", now=T)          # say, the server stopped mid-read
    again = deps.requests.claim(request_id, token="tok", now=T + dt.timedelta(minutes=16))
    assert again["receiving_since"] == "2026-04-10T09:16:00.000000+00:00"
    deps.requests.release(stalled)                                       # the stalled upload's late failure
    assert deps.requests.get(request_id)["status"] == "receiving"        # leaves the new claim alone
    deps.requests.release(again)
    released = deps.requests.get(request_id)
    assert released["status"] == "sent" and "receiving_since" not in released


def test_resolving_twice_resolves_once(deps, exception_id):
    first = deps.exceptions.resolve_once(exception_id, note="The client sent the document.")
    assert (first[0]["status"], first[1]) == ("resolved", True)
    assert deps.exceptions.resolve_once(exception_id, note="Again.") == (first[0], False)
