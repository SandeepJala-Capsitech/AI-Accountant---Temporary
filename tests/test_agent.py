"""The accountant agent run on a scripted chat model (tests/agent_fakes.py), never a real one: its tools take the client
and the thread from the run's context, a tool error goes back to the model and the run carries on, a model that fails
or loops ends the turn with an error event, and no tool can set a date."""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import openrouter
import pytest
from agent_fakes import ScriptedChatModel, call, say
from langchain.agents.middleware import model_retry
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

import run_scripted_resolutions as scripted
from ledgersync.agent import AgentDeps, ChatContext, ChatEvent, build_agent, stream_turn
from ledgersync.agent.llm import chat_model
from ledgersync.agent.tools import make_tools
from ledgersync.blobs import GridFSBlobStore
from ledgersync.config import Settings
from ledgersync.errors import ModelUnavailable
from ledgersync.models import ClientFields
from ledgersync.records import AgentRecords

ROOT = Path(__file__).parent.parent
STAPLES = ROOT / "eval" / "agent_cases" / "duplicate-receipt-spreadsheet"
YEAR = dt.date(2026, 4, 1)   # the case's first year
SETTINGS = Settings()


def context(client_id: int = 1) -> ChatContext:
    return ChatContext(client_id=client_id, thread_id=uuid4().hex, year_start=YEAR)


def offline_agent(model, **deps):
    """The agent on the scripted model, with its checkpoints in memory and no database behind its tools."""
    return build_agent(SETTINGS, AgentDeps(store=None, records=None, blobs=None, **deps), model=model,
                       checkpointer=InMemorySaver())


def answered(model: ScriptedChatModel, n: int) -> list[ToolMessage]:
    """The tool results model call n was given after the last call's tool calls."""
    messages = model.seen[n]
    return [m for m in messages[next(k for k in range(len(messages) - 1, -1, -1)
                                     if messages[k].type == "ai") + 1:] if isinstance(m, ToolMessage)]


def kinds(events) -> list[str]:
    return [e.type for e in events]


@pytest.fixture
def deps(mongo_store):
    return AgentDeps(store=mongo_store, records=AgentRecords(mongo_store.db), blobs=GridFSBlobStore(mongo_store.db))


@pytest.fixture
def staples(mongo_store, deps) -> int:
    """The duplicate-receipt case's client, with its files added as the chat adds them: a Staples receipt, the
    expenses sheet line for it and the bank line that paid it."""
    client_id = scripted._client(STAPLES, mongo_store, deps.records)
    for path in scripted._documents(STAPLES):
        scripted._add_document(mongo_store, deps.blobs, client_id, YEAR, path)
    return client_id


def keep_the_receipt(store, client_id: int) -> dict:
    """resolve_issue's arguments for the case's own resolution: keep the receipt, drop the sheet line."""
    step = json.loads((STAPLES / "expected_decisions.json").read_text())["resolutions"][0]
    by_name = {u["name"]: u["id"] for u in store.uploads(client_id)}

    def row_id(name: dict) -> int:
        return scripted._row_id(store, client_id, name)

    return {"row_id": row_id(step["row"]),
            "resolution": {"action": "keep_one", **scripted._ARGUMENTS["keep_one"](step["arguments"], row_id)},
            "evidence": [{"upload_id": by_name[e["file"]], "quote": e["quote"]} for e in step["evidence"]],
            "reason": step["reason"]}


# ─── Tool errors go back to the model ──────────────────────────────────────────

def test_malformed_tool_args_become_error_message_and_run_continues():
    model = ScriptedChatModel(replies=[call("get_row", row_id="the taxi"), say("Row ids are numbers. Done.")])

    events = list(stream_turn(offline_agent(model), context(), "Look at the taxi."))

    [result] = answered(model, 1)
    assert result.status == "error" and "row_id" in result.content
    assert kinds(events) == ["token"] and events[0].data == {"text": "Row ids are numbers. Done."}


def test_unknown_tool_reported_back():
    model = ScriptedChatModel(replies=[call("set_date", row_id=1, date="2026-10-14"), say("I can't date it.")])

    events = list(stream_turn(offline_agent(model), context(), "Date the taxi 14 Oct."))

    [result] = answered(model, 1)
    assert result.status == "error" and "There is no tool called set_date" in result.content
    assert kinds(events) == ["token"]


def test_a_fault_in_a_tool_is_answered_so_the_thread_stays_usable():
    model = ScriptedChatModel(replies=[call("list_questions"), say("Something failed.")])
    agent = offline_agent(model)   # no records behind the tool, so it raises AttributeError
    ctx = context()

    events = list(stream_turn(agent, ctx, "What is open?"))

    [result] = answered(model, 1)
    assert result.status == "error" and "list_questions couldn't finish" in result.content
    assert "AttributeError" not in result.content
    assert [(e.type, e.data.get("state")) for e in events] == [("step", "running"), ("step", "failed"),
                                                               ("token", None)]
    assert events[1].data["text"] == "Checking the open questions"
    kept = agent.get_state({"configurable": {"thread_id": ctx.thread_id}}).values["messages"]
    assert [m.type for m in kept] == ["human", "ai", "tool", "ai"]   # every tool call answered


# ─── A model that fails, loops or is stopped ───────────────────────────────────

@pytest.mark.mongo
def test_model_exception_mid_run_emits_error_event_keeps_steps(monkeypatch, deps, acme):
    monkeypatch.setattr(model_retry, "time", SimpleNamespace(sleep=lambda seconds: None))   # retry at once
    model = ScriptedChatModel(replies=[call("list_questions")], then=ConnectionError("OpenRouter hung up"))
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    events = list(stream_turn(agent, context(acme), "Review the books."))

    assert len(model.seen) == 4   # the tool call, then a failing call and its two retries
    assert kinds(events) == ["step", "step", "error"]
    assert events[1].data["state"] == "done" and events[0].data["id"] == events[1].data["id"]
    assert events[2].data["code"] == "model_unavailable" and "OpenRouter hung up" in events[2].data["message"]


@pytest.mark.mongo
def test_model_call_limit_stops_with_message(deps, acme):
    model = ScriptedChatModel(then=lambda messages: call("list_questions"))
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    events = list(stream_turn(agent, context(acme), "Review the books."))

    assert len(model.seen) == 30
    assert events[-1].type == "error"
    assert events[-1].data == {"code": "loop_limit", "message": "Stopped after 30 steps, before finishing.",
                               "steps": 30}
    assert [e.type for e in events[:-1]] == ["step"] * 60   # each step running, then done


def test_a_stopped_chat_stops_before_its_next_model_call():
    model = ScriptedChatModel(replies=[say("Never said.")])
    agent = offline_agent(model, is_cancelled=lambda thread_id: True)

    events = list(stream_turn(agent, context(), "Review the books."))

    assert model.seen == []
    assert [(e.type, e.data) for e in events] == [("token", {"text": "I stopped there, as you asked."})]


# ─── The tools ─────────────────────────────────────────────────────────────────

@pytest.mark.mongo
def test_resolve_retried_records_once(mongo_store, deps, staples):
    args = keep_the_receipt(mongo_store, staples)
    model = ScriptedChatModel(replies=[call("resolve_issue", **args), call("resolve_issue", **args),
                                       say("Kept the receipt.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    events = list(stream_turn(agent, context(staples), "Review the books."))

    [decision] = deps.records.decisions(staples)
    assert decision.status == "applied" and decision.actor == "agent:openai/gpt-oss-120b"
    first, again = (json.loads(answered(model, n)[0].content) for n in (1, 2))
    assert first == again and first["decision_id"] == decision.id and first["status"] == "applied"
    assert [e.data["text"] for e in events if e.type == "step" and e.data["state"] == "done"] == [
        f"Row {args['row_id']}: R2 Duplicate: kept the receipt"] * 2
    dropped = args["resolution"]["drop_row_ids"][0]
    assert next(r for r in mongo_store.rows(staples) if r.id == dropped).tx.excluded


@pytest.mark.mongo
def test_a_step_names_the_document_opened(mongo_store, deps, staples):
    receipt = next(u for u in mongo_store.uploads(staples) if u["name"] == "staples-receipt.txt")
    model = ScriptedChatModel(replies=[call("read_document", upload_id=receipt["id"]), say("Read it.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    events = list(stream_turn(agent, context(staples), "Open the receipt."))

    assert [(e.data["text"], e.data["state"]) for e in events if e.type == "step"] == [
        ("Opening staples-receipt.txt…", "running"), ("Opened staples-receipt.txt", "done")]
    [result] = answered(model, 1)
    page = json.loads(result.content)
    assert (page["name"], page["page"], page["pages"]) == ("staples-receipt.txt", 1, 1)
    assert "STAPLES" in page["text"] and "64.80" in page["text"]


@pytest.mark.mongo
def test_list_issues_by_family(mongo_store, deps, staples):
    model = ScriptedChatModel(replies=[call("list_issues", family="duplicate"), call("list_issues", family="documents"),
                                       say("Listed.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    list(stream_turn(agent, context(staples), "What needs a look?"))

    names = {r.id: (r.tx.document_type, r.tx.description) for r in mongo_store.rows(staples)}
    duplicate, documents = (json.loads(answered(model, n)[0].content) for n in (1, 2))
    assert [names[r["row_id"]][0] for r in duplicate["rows"]] == ["statement"]
    assert duplicate["rows"][0]["issues"][-1]["code"] == "booked_twice"
    # The bank line is held for its receipt; the sheet line is booked from the receipt, so it isn't held.
    assert [(names[r["row_id"]][0], r["held"]["needs"]) for r in documents["rows"]] == [
        ("statement", "receipt_or_invoice")]


def test_tools_use_context_client_not_args():
    for tool in make_tools(AgentDeps(store=None, records=None, blobs=None), actor="agent:test"):
        schema = json.dumps(tool.tool_call_schema.model_json_schema())
        assert "client_id" not in schema and "thread_id" not in schema, tool.name


@pytest.mark.mongo
def test_a_row_of_another_client_is_not_found(mongo_store, deps, staples):
    other = mongo_store.create_client(ClientFields(name="Other Ltd", business_type="limited_company",
                                                   contact_name="O", first_year_end=dt.date(2027, 3, 31))).id
    row_id = mongo_store.rows(staples)[0].id
    model = ScriptedChatModel(replies=[call("get_row", row_id=row_id, client_id=staples), say("Not found.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    list(stream_turn(agent, context(other), "Look at it."))

    [result] = answered(model, 1)
    assert result.status == "error" and "not found" in result.content


def test_no_tool_can_set_a_date():
    def properties(schema) -> list[tuple[str, dict]]:
        """Every property of the schema and of the schemas it nests, by name."""
        found = []
        if isinstance(schema, dict):
            found += list((schema.get("properties") or {}).items())
            for value in schema.values():
                found += properties(value)
        elif isinstance(schema, list):
            for value in schema:
                found += properties(value)
        return found

    tools = make_tools(AgentDeps(store=None, records=None, blobs=None), actor="agent:test")
    assert {t.name for t in tools} == {"get_overview", "list_issues", "get_row", "list_documents", "read_document",
                                       "resolve_issue", "ask_user", "list_questions", "list_decisions",
                                       "get_trial_balance"}
    for tool in tools:
        for name, field in properties(tool.tool_call_schema.model_json_schema()):
            assert "date" not in name and field.get("format") not in ("date", "date-time"), (tool.name, name)


# ─── Threads ───────────────────────────────────────────────────────────────────

@pytest.mark.mongo
def test_two_threads_share_no_messages(deps, acme):
    model = ScriptedChatModel(replies=[say("Hello from the first chat."), say("Hello from the second chat.")])
    agent = build_agent(SETTINGS, deps, model=model)   # checkpoints in <db>_checkpoints, on the one client
    first, second = context(acme), context(acme)

    list(stream_turn(agent, first, "First chat: the Staples receipt."))
    list(stream_turn(agent, second, "Second chat: the taxi."))

    assert [m.content for m in model.seen[1] if isinstance(m, HumanMessage)] == ["Second chat: the taxi."]
    kept = agent.get_state({"configurable": {"thread_id": first.thread_id}}).values["messages"]
    assert [m.content for m in kept] == ["First chat: the Staples receipt.", "Hello from the first chat."]


# ─── The chat model ────────────────────────────────────────────────────────────

def test_chat_model_sends_openrouter_the_agreed_settings():
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "gen-1", "object": "chat.completion", "created": 1,
                                         "model": "openai/gpt-oss-120b", "system_fingerprint": None,
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "Hello."}}]})

    model = chat_model(Settings(openrouter_api_key="test-key"))
    model.client = openrouter.OpenRouter(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(answer)))

    assert model.invoke("Hi").content == "Hello."
    assert model.request_timeout == 60_000 and model.max_retries == 2   # milliseconds, as ChatOpenRouter takes them
    [body] = sent
    assert body["model"] == "openai/gpt-oss-120b" and body["reasoning"] == {"effort": "medium"}
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}


def test_chat_model_needs_the_openrouter_key():
    with pytest.raises(ModelUnavailable, match="OPENROUTER_API_KEY"):
        chat_model(Settings())


def test_an_event_is_sent_as_sse():
    event = ChatEvent("step", {"id": "call_1", "text": "Opened costco-receipt-21-10.txt", "state": "done"})
    assert event.sse() == ('event: step\ndata: {"id": "call_1", "text": "Opened costco-receipt-21-10.txt", '
                           '"state": "done"}\n\n')
