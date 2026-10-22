"""The emails to the client: one per run with every document the run asked for, reminders after 3 and 7 days,
an exception instead when there is no address to send to, and the requests step's line in the feed."""
import datetime as dt
import smtplib
from types import SimpleNamespace

import pytest

from ledgersync.agent.nodes import make_requests
from ledgersync.agent.tools import ToolDeps
from ledgersync.config import Settings
from ledgersync.errors import EmailNotSent
from ledgersync.models import ClientFields, Transaction
from ledgersync.notify import ConsoleNotifier, SmtpNotifier, notifier_for, send_document_requests, send_reminders
from ledgersync.records.audit import AuditLog
from ledgersync.records.jobs import JobRecords

mongo = pytest.mark.mongo

PUBLIC_URL = "https://accounts.example.test"
T = dt.datetime(2026, 4, 10, 9, 0, tzinfo=dt.timezone.utc)
YEAR = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
CUBE = ClientFields(name="Business Cube Ltd", business_type="limited_company", contact_name="Jenny Clarke",
                    contact_email="jenny@businesscube.co.uk")
ACME = Transaction(date=dt.date(2026, 3, 2), description="ACME LTD", direction="out", gross="1200.00",
                   account_code="9998", document_type="statement", counterparty="Acme Ltd", document_ref="l1")


@pytest.fixture
def notifier():
    return ConsoleNotifier()


@pytest.fixture
def deps(mongo_store, mongo_db, notifier):
    return ToolDeps(store=mongo_store, records=JobRecords(mongo_db), audit=AuditLog(mongo_db), notifier=notifier,
                    settings=Settings(public_url=PUBLIC_URL))


def client_with_rows(store, **changes) -> int:
    client = store.create_client(CUBE.model_copy(update=changes))
    store.add_upload(client.id, "Bank.csv", "table", [ACME])
    return client.id


def asked(deps, client_id, job_id, *documents) -> list[str]:
    """Logs an exception and makes one pending request for each (document type, period, reason) given."""
    exception = deps.exceptions.log(client_id=client_id, job_id=job_id, stage="classify",
                                    description="No invoice for the £1,200.00 paid to Acme",
                                    assumption="Booked as 5000 Purchases", confidence=0.4, row_ids=[])
    return [deps.requests.create(client_id=client_id, job_id=job_id, exception_id=exception["id"],
                                 document_type=kind, period=period, reason=reason)["id"]
            for kind, period, reason in documents]


INVOICE = ("Invoice from Acme Ltd", "March 2026", "It explains the £1,200.00 paid to Acme.")
STATEMENTS = ("Bank statements", "February 2026", "The statements for February are missing.")


@pytest.fixture
def cube_id(mongo_store):
    return client_with_rows(mongo_store)


@pytest.fixture
def job(deps, cube_id):
    return deps.records.create(cube_id, *YEAR)


@pytest.fixture
def two_pending_requests(deps, cube_id, job):
    return asked(deps, cube_id, job.id, INVOICE, STATEMENTS)


@pytest.fixture
def client_without_email(deps, mongo_store):
    client_id = client_with_rows(mongo_store, contact_email="")
    job = deps.records.create(client_id, *YEAR)
    asked(deps, client_id, job.id, INVOICE)
    return SimpleNamespace(client_id=client_id, job_id=job.id)


@pytest.fixture
def request_sent_at_t(deps, cube_id, job):
    [request_id] = asked(deps, cube_id, job.id, INVOICE)
    deps.requests.mark_sent([request_id], token="tok", at=T)
    return request_id


def state(job) -> dict:
    return {"job_id": job.id, "client_id": job.client_id, "period_start": "2025-04-01", "period_end": "2026-03-31",
            "fix_rounds": 0, "gate_passed": True}


# --- the one email per run ----------------------------------------------------------------------------------

@mongo
def test_requests_from_one_run_go_in_one_email(deps, notifier, cube_id, job, two_pending_requests):
    assert send_document_requests(deps, cube_id, job.id) == 2
    (email,) = notifier.sent
    assert email.subject == "Documents needed for Business Cube Ltd's accounts"
    assert email.body.count("\n- ") == 2 and f"{PUBLIC_URL}/requests/" in email.body
    assert deps.requests.pending_for(job.id) == []


@mongo
def test_the_email_is_the_fixed_template_with_one_link_for_the_run(deps, notifier, cube_id, job, two_pending_requests):
    send_document_requests(deps, cube_id, job.id)
    (email,) = notifier.sent
    sent = deps.requests.by_token(email.body.split(f"{PUBLIC_URL}/requests/")[1].split("\n")[0])
    assert [r["id"] for r in sent] == two_pending_requests and {r["status"] for r in sent} == {"sent"}
    token = sent[0]["token"]
    assert len(token) == 32                                    # secrets.token_urlsafe(24)
    assert email.to == "jenny@businesscube.co.uk"
    assert email.body == (
        "Hello Jenny Clarke,\n\n"
        "To finish Business Cube Ltd's accounts for 1 April 2025 to 31 March 2026, we need:\n\n"
        "- Invoice from Acme Ltd (March 2026): It explains the £1,200.00 paid to Acme.\n"
        "- Bank statements (February 2026): The statements for February are missing.\n\n"
        f"Upload them here: {PUBLIC_URL}/requests/{token}\n\n"
        "Thank you.")


@mongo
def test_without_a_contact_name_the_email_greets_the_client_by_name(deps, notifier, mongo_store):
    client_id = client_with_rows(mongo_store, contact_name="")
    job = deps.records.create(client_id, *YEAR)
    asked(deps, client_id, job.id, INVOICE)
    send_document_requests(deps, client_id, job.id)
    assert notifier.sent[0].body.startswith("Hello Business Cube Ltd,\n\n")


@mongo
def test_without_a_contact_email_an_exception_is_logged_instead(deps, notifier, client_without_email):
    nobody = client_without_email
    assert send_document_requests(deps, nobody.client_id, nobody.job_id) == 0 and notifier.sent == []
    assert deps.exceptions.for_job(nobody.job_id)[-1]["description"] == "Business Cube Ltd has no contact email, so 1 document request wasn't sent."
    assert len(deps.requests.pending_for(nobody.job_id)) == 1


@mongo
def test_with_nothing_pending_nothing_is_sent(deps, notifier, cube_id, job):
    assert send_document_requests(deps, cube_id, job.id) == 0
    assert notifier.sent == [] and deps.exceptions.for_job(job.id) == []


# --- reminders ----------------------------------------------------------------------------------------------

@mongo
def test_reminders_go_after_3_and_7_days_once_each(deps, notifier, request_sent_at_t):
    sent = [send_reminders(deps, T + dt.timedelta(days=d)) for d in (2, 3, 4, 7, 30)]
    assert sent == [0, 1, 0, 1, 0]
    assert [e.subject for e in notifier.sent] == ["Reminder: documents needed for Business Cube Ltd's accounts"] * 2
    assert notifier.sent[0].body.endswith(f"Upload them here: {PUBLIC_URL}/requests/tok\n\nThank you.")


@mongo
def test_a_links_due_requests_go_in_one_reminder(deps, notifier, cube_id, job, two_pending_requests):
    deps.requests.mark_sent(two_pending_requests, token="tok", at=T)
    assert send_reminders(deps, T + dt.timedelta(days=3)) == 1
    assert notifier.sent[0].body.count("\n- ") == 2


@mongo
def test_a_received_document_gets_no_reminder(deps, notifier, request_sent_at_t):
    deps.requests.claim(request_sent_at_t, token="tok", now=T + dt.timedelta(days=1))
    deps.requests.mark_received(request_sent_at_t, upload_id=1, at=T + dt.timedelta(days=1))
    assert send_reminders(deps, T + dt.timedelta(days=3)) == 0 and notifier.sent == []


@mongo
@pytest.mark.parametrize("claimed_after, reminders", [
    (dt.timedelta(days=1), 1),                                  # a read that stalled two days ago: still waiting
    (dt.timedelta(days=3) - dt.timedelta(minutes=5), 0),        # being read right now: no reminder
])
def test_a_document_being_read_gets_no_reminder_unless_the_read_stalled(deps, notifier, request_sent_at_t,
                                                                         claimed_after, reminders):
    deps.requests.claim(request_sent_at_t, token="tok", now=T + claimed_after)
    assert send_reminders(deps, T + dt.timedelta(days=3)) == reminders


@mongo
def test_after_a_long_gap_one_reminder_covers_both(deps, notifier, request_sent_at_t):
    assert [send_reminders(deps, T + dt.timedelta(days=d)) for d in (10, 10.1, 30)] == [1, 0, 0]


# --- the requests step --------------------------------------------------------------------------------------

@mongo
@pytest.mark.parametrize("documents, line", [
    ((), "No documents to ask the client for."),
    ((INVOICE,), "Asked the client for 1 document."),
    ((INVOICE, STATEMENTS), "Asked the client for 2 documents."),
])
def test_the_requests_step_says_what_it_asked_for(deps, cube_id, job, documents, line):
    if documents:
        asked(deps, cube_id, job.id, *documents)
    assert make_requests(deps)(state(job)) == {}
    assert deps.records.events(job.id)[-1].message == line


@mongo
def test_the_requests_step_says_when_there_is_no_one_to_send_to(deps, client_without_email):
    job = deps.records.get(client_without_email.job_id)
    make_requests(deps)(state(job))
    assert deps.records.events(job.id)[-1].message == (
        "Business Cube Ltd has no contact email, so 1 document request wasn't sent.")


# --- the notifiers ------------------------------------------------------------------------------------------

class FakeSmtp:
    """Stands in for smtplib.SMTP and notes what it was asked to do."""

    seen: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        FakeSmtp.seen.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSmtp.seen.append(("starttls",))

    def login(self, username, password):
        FakeSmtp.seen.append(("login", username))

    def send_message(self, message):
        if FakeSmtp.fail:
            raise smtplib.SMTPRecipientsRefused({})
        FakeSmtp.seen.append(("send", message["From"], message["To"], message["Subject"], message.get_content()))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSmtp.seen, FakeSmtp.fail = [], False
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


def test_smtp_sends_over_starttls_after_logging_in(fake_smtp):
    SmtpNotifier("smtp.example.test", 587, "user", "secret", "accounts@example.test").send(
        "jenny@businesscube.co.uk", "Documents needed", "Hello")
    assert fake_smtp.seen == [("connect", "smtp.example.test", 587), ("starttls",), ("login", "user"),
                              ("send", "accounts@example.test", "jenny@businesscube.co.uk", "Documents needed",
                               "Hello\n")]


def test_an_email_that_cant_be_sent_says_what_to_check(fake_smtp):
    fake_smtp.fail = True
    with pytest.raises(EmailNotSent, match="Check the LEDGERSYNC_SMTP settings"):
        SmtpNotifier("smtp.example.test", 587, "", "", "accounts@example.test").send("a@b.test", "s", "b")


def test_without_an_smtp_host_emails_go_to_the_log(caplog):
    assert isinstance(notifier_for(Settings()), ConsoleNotifier)
    assert isinstance(notifier_for(Settings(smtp_host="smtp.example.test")), SmtpNotifier)
    console = ConsoleNotifier()
    with caplog.at_level("INFO", logger="ledgersync.notify"):
        console.send("a@b.test", "Subject", "Body")
    assert [(e.to, e.subject, e.body) for e in console.sent] == [("a@b.test", "Subject", "Body")]
    assert "Subject" in caplog.text


# --- a newer request closes the older one -------------------------------------------------------------------

def newer_run(deps, cube_id, job, *documents):
    """A run that superseded `job`, with pending requests for the documents given."""
    newer = deps.records.create(cube_id, *YEAR, supersedes=job.id)
    return newer, asked(deps, cube_id, newer.id, *documents)


@mongo
def test_sending_a_newer_request_closes_the_older_one_for_the_same_document(deps, notifier, cube_id, job,
                                                                            request_sent_at_t):
    newer, [newer_id] = newer_run(deps, cube_id, job, ("invoice from ACME Ltd ", "march 2026", "Still needed."))
    assert send_document_requests(deps, cube_id, newer.id) == 1
    old = deps.requests.get(request_sent_at_t)
    assert (old["status"], old["replaced_by"]) == ("replaced", newer_id)
    [entry] = [e for e in deps.audit.entries(client_id=cube_id) if e["action"] == "replace_request"]
    assert (entry["actor"], entry["job_id"], entry["target"], entry["before"], entry["after"]) == (
        "system", newer.id, {"request_id": request_sent_at_t}, {"status": "sent"},
        {"status": "replaced", "replaced_by": newer_id})
    now = dt.datetime.now(dt.timezone.utc)
    assert deps.requests.claim(request_sent_at_t, token="tok", now=now) is None     # its upload is refused
    notifier.sent.clear()
    assert send_reminders(deps, now + dt.timedelta(days=3)) == 1                    # the newer one's only
    assert notifier.sent[0].body.count("\n- ") == 1 and f"{PUBLIC_URL}/requests/tok\n" not in notifier.sent[0].body


@mongo
def test_a_request_for_another_period_is_not_closed(deps, cube_id, job, request_sent_at_t):
    newer, _ = newer_run(deps, cube_id, job, ("Invoice from Acme Ltd", "February 2026", "Also needed."))
    send_document_requests(deps, cube_id, newer.id)
    assert deps.requests.get(request_sent_at_t)["status"] == "sent"


@mongo
def test_an_older_request_never_emailed_is_closed_too(deps, cube_id, job):
    [never_sent] = asked(deps, cube_id, job.id, INVOICE)                 # its run failed before the email
    newer, _ = newer_run(deps, cube_id, job, INVOICE)
    send_document_requests(deps, cube_id, newer.id)
    assert deps.requests.get(never_sent)["status"] == "replaced"


@mongo
@pytest.mark.parametrize("claimed_ago, status", [(dt.timedelta(minutes=5), "receiving"),     # uploading now
                                                 (dt.timedelta(minutes=16), "replaced")])    # a stalled upload
def test_a_request_being_uploaded_right_now_is_not_closed(deps, cube_id, job, request_sent_at_t, claimed_ago, status):
    deps.requests.claim(request_sent_at_t, token="tok", now=dt.datetime.now(dt.timezone.utc) - claimed_ago)
    newer, _ = newer_run(deps, cube_id, job, INVOICE)
    send_document_requests(deps, cube_id, newer.id)
    assert deps.requests.get(request_sent_at_t)["status"] == status


@mongo
def test_without_an_email_to_send_the_newer_request_the_older_one_stays_live(deps, mongo_store):
    client_id = client_with_rows(mongo_store, contact_email="")
    old_job = deps.records.create(client_id, *YEAR)
    [old] = asked(deps, client_id, old_job.id, INVOICE)
    deps.requests.mark_sent([old], token="tok", at=T)                   # sent before the address was removed
    newer, _ = newer_run(deps, client_id, old_job, INVOICE)
    assert send_document_requests(deps, client_id, newer.id) == 0
    assert deps.requests.get(old)["status"] == "sent"
