"""The chat API (design of 2026-11-02) on a scripted chat model (tests/agent_fakes.py) and a fake model client that
reads the files, never real ones: threads on a client's open year, a message streamed back as steps, the agent's
reply and the summary, answers to questions, and one run at a time on each thread."""
import datetime as dt
import json
import threading
from pathlib import Path

import pytest
from agent_fakes import ScriptedChatModel, call, say
from fakes import FakeModel, transactions_json
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.mongodb import MongoDBSaver

from ledgersync import ledger
from ledgersync.agent import AgentDeps, build_agent
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
# Another supplier's invoice for the same amount, which isn't for this payment.
OTHER_INVOICE_FILE = ("harbour-signs-invoice-HS-17.txt", "Harbour Signs Ltd. Invoice HS-17. £240.00")
OTHER_INVOICE = {**INVOICE, "description": "Harbour Signs: shop sign", "vat": None,
                 "counterparty": "Harbour Signs Ltd", "document_number": "HS-17", "document_net": None,
                 "document_vat": None, "supplier_vat_number": None}


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

    [error] = [data for kind, data in events if kind == "error"]
    assert error["code"] == "model_unavailable" and "OPENROUTER_API_KEY" in error["message"]
    assert [kind for kind, _ in events][-2:] == ["summary", "done"]   # the file was read, and the year posted
    assert summary_of(events)["trial_balance"]["suspense"][0]["gross"] == "240.00"


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


def test_upload_answer_moves_out_of_suspense(make, client_id):
    api, thread, question = held_payment(make, client_id, transactions_json(INVOICE))

    uploaded = api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/document",
                        files={"file": (INVOICE_FILE[0], INVOICE_FILE[1].encode(), "text/plain")})

    assert uploaded.status_code == 200, uploaded.text
    summary = uploaded.json()
    assert summary["questions"] == [] and summary["trial_balance"]["suspense"] == []
    assert net(summary, "9998") == "0.00"
    assert (net(summary, "7500"), net(summary, "2201"), net(summary, "1200")) == ("200.00", "40.00", "-240.00")


def test_upload_that_does_not_fit_says_why(make, client_id):
    api, thread, question = held_payment(make, client_id, transactions_json(OTHER_INVOICE))

    uploaded = api.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/document",
                        files={"file": (OTHER_INVOICE_FILE[0], OTHER_INVOICE_FILE[1].encode(), "text/plain")})

    assert uploaded.status_code == 200, uploaded.text
    [still] = uploaded.json()["questions"]
    assert (still["id"], still["status"]) == (question["id"], "open")
    assert still["why"] == ("Read harbour-signs-invoice-HS-17.txt, but it doesn't match: Harbour Signs Ltd, £240.00 "
                            "against Brightline Print, £240.00.")
    assert uploaded.json()["trial_balance"]["suspense"][0]["gross"] == "240.00"


def test_questions_survive_restart(make, client_id):
    _, thread, question = held_payment(make, client_id)

    restarted = make()   # a new app on the same database
    assert [q["id"] for q in restarted.get(f"/api/clients/{client_id}/questions").json()] == [question["id"]]
    answered = restarted.post(f"/api/chat/threads/{thread['id']}/questions/{question['id']}/answer",
                              json={"kind": "no_document"})

    assert answered.status_code == 200, answered.text
    [waiting] = answered.json()["questions"]
    assert (waiting["id"], waiting["status"], waiting["answer"]) == (question["id"], "waiting", "I don't have it")
    assert answered.json()["trial_balance"]["suspense"][0]["needs"] == "receipt_or_invoice"   # still held


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
