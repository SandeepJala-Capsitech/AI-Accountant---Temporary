"""The chat API (design of 2026-11-02) on a scripted chat model (tests/agent_fakes.py) and a fake model client that
reads the files, never real ones: threads on a client's open year, a message streamed back as steps, the agent's
reply and the summary, answers to questions, and one run at a time on each thread."""
import datetime as dt
import json
import threading
import time
from pathlib import Path

import pytest
from agent_fakes import ScriptedChatModel, call, say
from fakes import FakeModel, transactions_json
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.mongodb import MongoDBSaver

from ledgersync import ledger, ledger_book
from ledgersync.agent import AgentDeps, build_agent
from ledgersync.agent import chat as chat_module
from ledgersync.blobs import MemoryBlobStore
from ledgersync.config import Settings
from ledgersync.models import ClientFields
from ledgersync.records import AgentRecords
from server import create_app

pytestmark = pytest.mark.mongo

CLIENT = Path(__file__).parent.parent / "eval" / "agent_cases" / "no-invoice-over-50-suspense" / "client.json"
YEAR = "2026-04-01"   # the client's first year, which holds today

# The files sent, and what the model client reads from each, as the model answers.
STATEMENT = ("barclays-october.csv", "Date,Amount,Memo\n22/10/2026,-240.00,BRIGHTLINE PRINT\n")
PAYMENT = {"description": "BRIGHTLINE PRINT", "date": "2026-10-22", "amount": 240.0, "direction": "out",
           "account": "7500 Printing", "vat": None, "currency": "GBP", "document_type": "statement",
           "counterparty": "Brightline Print"}
TAXI_RECEIPT = ("taxi.txt", "Leeds City Cabs. Leeds Station to Kirkstall. Fare £18.00")
TAXI = {"description": "Leeds City Cabs taxi", "date": None, "amount": 18.0, "direction": "out",
        "account": "7400 Travel", "vat": None, "currency": "GBP", "document_type": "receipt",
        "counterparty": "Leeds City Cabs"}
INVOICE_FILE = ("brightline-invoice-BP-2210.txt", "Brightline Print Ltd. Invoice BP-2210. £200.00 + VAT £40.00")
INVOICE = {"description": "Brightline: business cards and A5 brochures", "date": "2026-10-19", "amount": 240.0,
           "vat": 40.0, "direction": "out", "account": "7500 Printing", "currency": "GBP",
           "document_type": "invoice", "counterparty": "Brightline Print Ltd", "document_number": "BP-2210",
           "document_total": 240.0, "document_net": 200.0, "document_vat": 40.0,
           "supplier_vat_number": "GB999225861"}

def other_invoice(who: str, total: float, number: str) -> dict:
    """An invoice that isn't the £240.00 Brightline payment's: another payee, or another total."""
    return {**INVOICE, "description": f"{who}: leaflets", "amount": total, "vat": None, "counterparty": who,
            "document_number": number, "document_total": total, "document_net": None, "document_vat": None,
            "supplier_vat_number": None}


@pytest.fixture
def client_id(mongo_store) -> int:
    """Northbridge as the agent cases add it: a VAT registered company whose first year ends 31 Mar 2027, with one
    current account (1200)."""
    return ledger.add_client(mongo_store, ClientFields.model_validate(json.loads(CLIENT.read_text()))).id


@pytest.fixture
def make(mongo_store):
    """Starts the API on the test database, with the chat model answering `replies` in order and the model client
    reading each file as `reads` says, and returns a client for it (`.model` is the chat model). Each call is a new
    app on the same database, as after a restart; they share the original files."""
    blobs, opened = MemoryBlobStore(), []

    def _make(*replies, reads=(), header=True, **settings):
        model = ScriptedChatModel(replies=list(replies))
        app = create_app(Settings(warmup=False, **settings), model_client=FakeModel(list(reads)), store=mongo_store,
                         blobs=blobs, chat_model=model)
        api = TestClient(app, headers={"X-LedgerSync": "1"} if header else {})   # as the LedgerSync pages send
        api.__enter__()
        api.model = model
        opened.append(api)
        return api

    yield _make
    for api in opened:
        api.__exit__(None, None, None)


def new_thread(api, client_id: int) -> dict:
    created = api.post("/api/chat/threads", json={"client_id": client_id})
    assert created.status_code == 201, created.text
    return created.json()


def events_of(body: str) -> list[tuple[str, dict]]:
    """The server-sent events of a stream, as (type, data)."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def send(api, thread_id: str, text: str, *files: tuple[str, str]) -> list[tuple[str, dict]]:
    """Sends a message with its files (name, text) and returns the events streamed back."""
    sent = api.post(f"/api/chat/threads/{thread_id}/messages", data={"text": text},
                    files=[("files", (name, body.encode(), "text/plain")) for name, body in files])
    assert sent.status_code == 200, sent.text
    assert sent.headers["content-type"].startswith("text/event-stream")
    return events_of(sent.text)


def summary_of(events) -> dict:
    [summary] = [data for kind, data in events if kind == "summary"]
    return summary


def net(summary: dict, code: str) -> str:
    """Debit less credit of the posted trial balance's line for the account, as pounds and pence."""
    line = next((l for l in summary["trial_balance"]["lines"] if l["code"] == code), None)
    return "0.00" if line is None else f"{float(line['debit']) - float(line['credit']):.2f}"


def held_payment(make, client_id: int, *more_reads: str):
    """A chat whose first message sent the statement with the £240.00 Brightline payment, which has no invoice:
    the API, the thread and the question asking for the invoice."""
    api = make(say("One payment needs its invoice."), reads=[transactions_json(PAYMENT), *more_reads])
    thread = new_thread(api, client_id)
    [question] = summary_of(send(api, thread["id"], "Review October.", STATEMENT))["questions"]
    return api, thread, question


# ─── Threads ───────────────────────────────────────────────────────────────────

def test_new_thread_belongs_to_client(make, mongo_store, client_id):
    api = make()
    other = ledger.add_client(mongo_store, ClientFields(name="Other Ltd", business_type="limited_company",
                                                        contact_name="Ann", first_year_end=dt.date(2027, 3, 31))).id

    thread = new_thread(api, client_id)

    assert (thread["client_id"], thread["year_start"], thread["title"]) == (client_id, YEAR, "New chat")
    assert [t["id"] for t in api.get(f"/api/chat/threads?client_id={client_id}").json()] == [thread["id"]]
    assert api.get(f"/api/chat/threads?client_id={other}").json() == []
    assert api.get(f"/api/chat/threads/{thread['id']}").json() == {"thread": thread, "messages": [], "summary": None}


def test_rename_and_delete_thread(make, mongo_store, client_id):
    api = make(say("Done."))
    thread = new_thread(api, client_id)
    send(api, thread["id"], "Anything to do?")
    checkpoints = mongo_store.client[f"{mongo_store.db_name}_checkpoints"].checkpoints

    renamed = api.patch(f"/api/chat/threads/{thread['id']}", json={"title": "October review"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "October review"
    assert checkpoints.count_documents({"thread_id": thread["id"]}) > 0

    assert api.delete(f"/api/chat/threads/{thread['id']}").status_code == 204
    assert api.get(f"/api/chat/threads/{thread['id']}").status_code == 404
    assert checkpoints.count_documents({"thread_id": thread["id"]}) == 0


# ─── A message ─────────────────────────────────────────────────────────────────

def test_message_streams_steps_then_summary_then_done(make, client_id):
    api = make(say("I read the statement."), reads=[transactions_json(PAYMENT)])
    thread = new_thread(api, client_id)

    events = send(api, thread["id"], "Review October.", STATEMENT, ("empty.txt", ""))

    kinds = [kind for kind, _ in events]
    assert kinds[0] == "status" and kinds[-2:] == ["summary", "done"]
    assert max(k for k, kind in enumerate(kinds) if kind == "step") < kinds.index("token") < kinds.index("summary")
    steps = [data for kind, data in events if kind == "step"]
    statement = [s for s in steps if s["file"] == "barclays-october.csv"]
    assert [(s["text"], s["state"]) for s in statement] == [("Reading barclays-october.csv…", "running"),
                                                            ("Read barclays-october.csv (1 row)", "done")]
    assert statement[0]["id"] == statement[1]["id"] and statement[1]["rows"] == 1
    empty = [s for s in steps if s["file"] == "empty.txt"]
    assert [(s["text"], s["state"]) for s in empty] == [("Reading empty.txt…", "running"),
                                                        ("Couldn't read empty.txt: 'empty.txt' is empty.", "failed")]
    assert empty[1]["reason"] == "'empty.txt' is empty."
    assert "".join(data["text"] for kind, data in events if kind == "token") == "I read the statement."
    assert api.get(f"/api/chat/threads/{thread['id']}").json()["thread"]["title"] == "Review October."


def test_summary_lists_held_payment_and_question(make, client_id):
    api = make(say("One payment needs its invoice."), reads=[transactions_json(PAYMENT)])
    thread = new_thread(api, client_id)

    summary = summary_of(send(api, thread["id"], "Review October.", STATEMENT))

    [held] = summary["trial_balance"]["suspense"]
    assert (held["gross"], held["needs"]) == ("240.00", "receipt_or_invoice")
    [question] = summary["questions"]
    assert (question["kind"], question["status"], question["row_ids"]) == ("receipt_or_invoice", "open",
                                                                          [held["row_id"]])
    assert question["text"] == "Can you upload the receipt or invoice?"
    assert net(summary, "9998") == "240.00" and net(summary, "1200") == "-240.00"
    assert summary["trial_balance"]["is_balanced"]
    assert summary["post"]["batch_id"] and summary["post"]["problems"] == []
    assert summary["sorted"] == []


def test_without_the_openrouter_key_a_turn_says_the_model_is_unavailable(mongo_store, client_id):
    app = create_app(Settings(warmup=False), model_client=FakeModel([transactions_json(PAYMENT)]), store=mongo_store,
                     blobs=MemoryBlobStore())
    with TestClient(app, headers={"X-LedgerSync": "1"}) as api:
        thread = new_thread(api, client_id)
        events = send(api, thread["id"], "Review October.", STATEMENT)

        detail = api.get(f"/api/chat/threads/{thread['id']}").json()

    [error] = [data for kind, data in events if kind == "error"]
    assert error["code"] == "model_unavailable" and "OPENROUTER_API_KEY" in error["message"]
    assert [kind for kind, _ in events][-2:] == ["summary", "done"]   # the file was read, and the year posted
    assert summary_of(events)["trial_balance"]["suspense"][0]["gross"] == "240.00"
    assert detail["messages"] == [{"role": "user", "text": "Review October."}]   # kept, for when the model is back


# ─── Answers ───────────────────────────────────────────────────────────────────

def test_answer_date_returns_updated_summary(make, client_id):
    api = make(say("The taxi has no date."), reads=[transactions_json(TAXI)])
    thread = new_thread(api, client_id)
    [question] = summary_of(send(api, thread["id"], "Review October.", TAXI_RECEIPT))["questions"]
    assert question["kind"] == "date"

    answered = api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/answer",
                        json={"kind": "date", "date": "2026-10-09"})

    assert answered.status_code == 200, answered.text
    summary = answered.json()
    assert summary["questions"] == []
    [decision] = summary["sorted"]
    [row_id] = question["row_ids"]
    assert decision["rule"] == "R1 Date from the person" and decision["after"] == {str(row_id): {"date": "2026-10-09"}}
    detail = api.get(f"/api/chat/threads/{thread['id']}").json()
    assert detail["messages"][-1] == {"role": "user",
                                      "text": "Answered: When was this Leeds City Cabs taxi? → 9 Oct 2026"}
    assert detail["summary"]["sorted"] == summary["sorted"]


def test_the_next_turn_sees_the_answer(make, client_id):
    api = make(say("The taxi has no date."), say("Noted."), reads=[transactions_json(TAXI)])
    thread = new_thread(api, client_id)
    [question] = summary_of(send(api, thread["id"], "Review October.", TAXI_RECEIPT))["questions"]
    api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/answer",
             json={"kind": "date", "date": "2026-10-09"})

    events = send(api, thread["id"], "Thanks.")

    assert len(api.model.seen) == 2   # one model call each turn
    assert [(m.type, m.text) for m in api.model.seen[1][-3:]] == [
        ("ai", "The taxi has no date."), ("human", "Answered: When was this Leeds City Cabs taxi? → 9 Oct 2026"),
        ("human", "Thanks.")]
    assert [data["text"] for kind, data in events if kind == "token"] == ["Noted."]


def test_upload_answer_moves_out_of_suspense(make, client_id):
    api, thread, question = held_payment(make, client_id, transactions_json(INVOICE))

    uploaded = api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/document",
                        files={"file": (INVOICE_FILE[0], INVOICE_FILE[1].encode(), "text/plain")})

    assert uploaded.status_code == 200, uploaded.text
    summary = uploaded.json()
    assert summary["questions"] == [] and summary["trial_balance"]["suspense"] == []
    assert net(summary, "9998") == "0.00"
    assert (net(summary, "7500"), net(summary, "2201"), net(summary, "1200")) == ("200.00", "40.00", "-240.00")


@pytest.mark.parametrize("who, total, file", [
    ("Acme Print Ltd", 240.0, "acme-invoice-A-17.txt"),           # another payee sharing a trade word
    ("Acme Print Ltd", 200.0, "acme-invoice-A-17.txt"),           # and another total
    ("Brightline Print Ltd", 200.0, "brightline-invoice-BP-2199.txt"),   # the payee, but another total
])
def test_upload_that_does_not_fit_says_why(make, client_id, who, total, file):
    api, thread, question = held_payment(make, client_id, transactions_json(other_invoice(who, total, "A-17")))

    uploaded = api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/document",
                        files={"file": (file, b"An invoice.", "text/plain")})

    assert uploaded.status_code == 200, uploaded.text
    [still] = uploaded.json()["questions"]
    assert (still["id"], still["status"]) == (question["id"], "open")
    assert still["why"] == f"Read {file}, but it doesn't match: {who}, £{total:.2f} against Brightline Print, £240.00."
    assert uploaded.json()["trial_balance"]["suspense"][0]["gross"] == "240.00"
    assert net(uploaded.json(), "9998") == "240.00"


def test_questions_survive_restart(make, client_id):
    # The Brightline invoice has no date, so it can't be the payment's yet: the £240.00 is held, and both ask.
    api = make(say("Two things need you."),
               reads=[transactions_json(PAYMENT), transactions_json({**INVOICE, "date": None})],
               max_parallel_jobs=1)   # the files are read in turn, so each gets its own reading
    thread = new_thread(api, client_id)
    asked = summary_of(send(api, thread["id"], "Review October.", STATEMENT, INVOICE_FILE))
    assert sorted(q["kind"] for q in asked["questions"]) == ["date", "receipt_or_invoice"]
    assert net(asked, "9998") == "240.00"
    [dating] = [q for q in asked["questions"] if q["kind"] == "date"]

    restarted = make()   # a new app on the same database
    listed = restarted.get(f"/api/clients/{client_id}/questions").json()
    assert sorted(q["id"] for q in listed) == sorted(q["id"] for q in asked["questions"])
    answered = restarted.post(f"/api/chat/threads/{thread['id']}/questions/{dating['id']}/answer",
                              json={"kind": "date", "date": "2026-10-19"})

    assert answered.status_code == 200, answered.text
    summary = answered.json()   # dated, the invoice is the payment's: both questions go, and so does the Suspense
    assert summary["questions"] == [] and summary["trial_balance"]["suspense"] == []
    assert (net(summary, "9998"), net(summary, "7500"), net(summary, "2201")) == ("0.00", "200.00", "40.00")
    assert summary["post"]["batch_id"] and summary["post"]["reversals_added"] > 0


def test_thread_history_survives_restart(make, client_id):
    api = make(say("Nothing to sort this time."))
    thread = new_thread(api, client_id)
    send(api, thread["id"], "Anything to do?")

    detail = make().get(f"/api/chat/threads/{thread['id']}").json()

    assert detail["messages"] == [{"role": "user", "text": "Anything to do?"},
                                  {"role": "assistant", "text": "Nothing to sort this time."}]
    assert detail["summary"]["questions"] == [] and detail["summary"]["trial_balance"]["is_balanced"]


# ─── One run at a time ─────────────────────────────────────────────────────────

def test_second_message_while_running_409(make, client_id):
    started, release = threading.Event(), threading.Event()

    def slow(messages):
        started.set()
        release.wait(30)
        return say("Done.")

    api = make(slow, say("Done again."))
    thread = new_thread(api, client_id)
    first = threading.Thread(target=send, args=(api, thread["id"], "Review October."))
    first.start()
    try:
        assert started.wait(30)
        second = api.post(f"/api/chat/threads/{thread['id']}/messages", data={"text": "And again?"})
        stopped = api.post(f"/api/chat/threads/{thread['id']}/stop")
    finally:
        release.set()
        first.join(30)

    assert second.status_code == 409
    assert second.json()["detail"]["message"] == "Still working on the last message."
    assert stopped.status_code == 200 and stopped.json() == {"stopping": True}
    assert [kind for kind, _ in send(api, thread["id"], "And again?")][-1] == "done"   # the run was let go


def test_an_answer_waits_for_the_message_running_on_its_chat(make, client_id):
    started, release = threading.Event(), threading.Event()

    def slow(messages):
        started.set()
        release.wait(30)
        return say("Done.")

    api = make(say("One payment needs its invoice."), slow, reads=[transactions_json(PAYMENT)])
    thread = new_thread(api, client_id)
    [question] = summary_of(send(api, thread["id"], "Review October.", STATEMENT))["questions"]
    running = threading.Thread(target=send, args=(api, thread["id"], "Anything else?"))
    answered = []
    answering = threading.Thread(target=lambda: answered.append(api.post(
        f"/api/chat/threads/{thread['id']}/questions/{question['id']}/answer", json={"kind": "no_document"})))
    running.start()
    try:
        assert started.wait(30)
        answering.start()
        answering.join(1)
        assert answering.is_alive()   # waiting for the run, not refused
    finally:
        release.set()
        running.join(30)
        answering.join(60)

    assert answered[0].status_code == 200, answered[0].text
    assert answered[0].json()["questions"][0]["status"] == "waiting"


def test_two_chats_on_one_client_run_together_and_post_in_turn(make, client_id, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(messages):
        started.set()
        release.wait(30)
        return say("Done.")

    writes, lock = [], threading.Lock()
    write = chat_module.Chat._write

    def timed(self, thread, since):   # the write steps, timed
        begun = time.monotonic()
        time.sleep(0.3)
        done = write(self, thread, since)
        with lock:
            writes.append((begun, time.monotonic()))
        return done

    monkeypatch.setattr(chat_module.Chat, "_write", timed)
    api = make(slow, say("Done too."))
    first, second = new_thread(api, client_id), new_thread(api, client_id)
    events = {}
    running = threading.Thread(target=lambda: events.update(first=send(api, first["id"], "Review October.")))
    running.start()
    try:
        assert started.wait(30)
        other = threading.Thread(target=lambda: events.update(second=send(api, second["id"], "Anything else?")))
        other.start()   # the second chat isn't refused while the first runs
        time.sleep(0.1)
    finally:
        release.set()
        running.join(30)
        other.join(30)

    assert [kind for kind, _ in events["first"]][-1] == [kind for kind, _ in events["second"]][-1] == "done"
    (a_start, a_end), (b_start, b_end) = sorted(writes)
    assert a_end <= b_start   # one chat's write steps after the other's, never at once


def test_stop_ends_the_run_before_its_next_model_call(make, client_id):
    started, release = threading.Event(), threading.Event()

    def looks(messages):
        started.set()
        release.wait(30)
        return call("list_questions")

    api = make(looks)
    thread = new_thread(api, client_id)
    events = []
    run = threading.Thread(target=lambda: events.extend(send(api, thread["id"], "Review October.")))
    run.start()
    try:
        assert started.wait(30)
        assert api.post(f"/api/chat/threads/{thread['id']}/stop").json() == {"stopping": True}
    finally:
        release.set()
        run.join(30)

    assert len(api.model.seen) == 1
    assert "".join(data["text"] for kind, data in events if kind == "token") == "I stopped there, as you asked."
    assert [kind for kind, _ in events][-2:] == ["summary", "done"]


def test_a_tool_call_left_by_a_crash_is_closed_before_the_next_turn(make, mongo_store, client_id):
    api = make(say("Carrying on."))
    thread = new_thread(api, client_id)
    # A server that died while a tool ran left the call with no result in the thread's checkpoints.
    crashed = build_agent(Settings(), AgentDeps(store=mongo_store, records=AgentRecords(mongo_store.db), blobs=None),
                          model=ScriptedChatModel(), checkpointer=MongoDBSaver(
                              mongo_store.client, db_name=f"{mongo_store.db_name}_checkpoints"))
    crashed.update_state({"configurable": {"thread_id": thread["id"]}}, {"messages": [
        HumanMessage("Look at the taxi."),
        AIMessage("", tool_calls=[{"name": "list_questions", "args": {}, "id": "call_1", "type": "tool_call"}])]},
        as_node="model")

    events = send(api, thread["id"], "Carry on.")

    seen = api.model.seen[0]
    assert [m.type for m in seen] == ["system", "human", "ai", "tool", "human"]
    assert (seen[3].tool_call_id, seen[3].content) == ("call_1", "Stopped before this finished.")
    assert [data["text"] for kind, data in events if kind == "token"] == ["Carrying on."]
    assert not [data for kind, data in events if kind == "error"]


# ─── Who may write ─────────────────────────────────────────────────────────────

def test_writes_need_header(make, client_id):
    api, thread, question = held_payment(make, client_id)
    bare = make(header=False)
    chat = f"/api/chat/threads/{thread['id']}"

    refused = [
        bare.post("/api/chat/threads", json={"client_id": client_id}),
        bare.patch(chat, json={"title": "Renamed"}),
        bare.delete(chat),
        bare.post(f"{chat}/messages", data={"text": "Hello"}),
        bare.post(f"{chat}/questions/{question['id']}/answer", json={"kind": "not_sure"}),
        bare.post(f"{chat}/questions/{question['id']}/document", files={"file": ("a.txt", b"x", "text/plain")}),
        bare.post(f"{chat}/stop"),
    ]

    assert [r.status_code for r in refused] == [403] * len(refused)
    assert bare.get(chat).status_code == 200   # reading needs no header
    assert api.get(f"/api/clients/{client_id}/questions").json()[0]["status"] == "open"


def test_demo_reset_hidden_unless_demo(make, mongo_store):
    plain = make()
    assert plain.get("/api/health").json()["demo"] is False
    assert plain.post("/api/demo/reset").status_code == 404

    demo = make(demo=True)
    assert demo.get("/api/health").json()["demo"] is True
    assert make(header=False, demo=True).post("/api/demo/reset").status_code == 403
    reset = demo.post("/api/demo/reset")
    assert reset.status_code == 200, reset.text
    assert mongo_store.get_client(reset.json()["client_id"]).name == "Northbridge Consulting Ltd"


# ─── The client's records ──────────────────────────────────────────────────────

def test_client_routes_list_questions_decisions_and_posted_trial_balance(make, client_id):
    api = make(say("The taxi has no date."), reads=[transactions_json(TAXI)])
    thread = new_thread(api, client_id)
    [question] = summary_of(send(api, thread["id"], "Review October.", TAXI_RECEIPT))["questions"]
    api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/answer",
             json={"kind": "date", "date": "2026-10-09"})

    assert api.get(f"/api/clients/{client_id}/questions").json() == []
    [decision] = api.get(f"/api/clients/{client_id}/decisions?year_start={YEAR}").json()
    assert decision["rule"] == "R1 Date from the person"
    posted = api.get(f"/api/clients/{client_id}/years/{YEAR}/posted-trial-balance").json()
    assert posted["is_balanced"] and net({"trial_balance": posted}, "7400") == "18.00"


def test_posted_trial_balance_reports_unposted_changes(make, mongo_store, mongo_db, client_id):
    api = make(say("The taxi has no date."), reads=[transactions_json(TAXI)])
    thread = new_thread(api, client_id)
    [question] = summary_of(send(api, thread["id"], "Review October.", TAXI_RECEIPT))["questions"]
    api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/answer",
             json={"kind": "date", "date": "2026-10-09"})
    route = f"/api/clients/{client_id}/years/{YEAR}/posted-trial-balance"
    assert api.get(route).json()["unposted_changes"] == 0   # answering posted the row

    [row_id] = question["row_ids"]
    mongo_store.patch_row(client_id, row_id, {"excluded": True})   # a person drops the row after it was posted
    assert api.get(route).json()["unposted_changes"] == 1

    ledger_book.post(mongo_store, AgentRecords(mongo_db), client_id, dt.date.fromisoformat(YEAR))
    assert api.get(route).json()["unposted_changes"] == 0
