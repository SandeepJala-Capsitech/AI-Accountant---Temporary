"""The accountant agent run on a scripted chat model (tests/agent_fakes.py), never a real one: its tools take the client
and the thread from the run's context, a tool error goes back to the model and the run carries on, a model that fails
or loops ends the turn with an error event, and no tool can set a date."""
import datetime as dt
import json
from decimal import Decimal
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
from helpers import add_rows, row
from ledgersync import ledger_book
from ledgersync.agent import AgentDeps, ChatContext, ChatEvent, build_agent, stream_turn
from ledgersync.agent.llm import chat_model
from ledgersync.agent.prompt import SYSTEM_PROMPT
from ledgersync.agent.tools import make_tools
from ledgersync.blobs import GridFSBlobStore
from ledgersync.checks import issue
from ledgersync.config import Settings
from ledgersync.errors import ModelUnavailable
from ledgersync.model_client import ModelClient
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
    assert decision.status == "applied" and decision.actor == "agent:qwen/qwen3.8-27b"
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


def rows_of_the_year(mongo_store, deps, client_id: int, upload_id=None, **args) -> dict:
    """What list_rows answers when called on the client's first year."""
    model = ScriptedChatModel(replies=[call("list_rows", **({"upload_id": upload_id} if upload_id else {}), **args),
                                       say("Listed.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())
    list(stream_turn(agent, context(client_id), "Show the rows."))
    [result] = answered(model, 1)
    return json.loads(result.content)


def three_clean_rows(mongo_store, acme) -> tuple[int, int]:
    """Two clean receipts in one upload and one in another; the ids of the two uploads."""
    first = add_rows(mongo_store, acme,
                     row(date=dt.date(2026, 10, 1), gross=Decimal("12.00"), vat=Decimal("2.00"), account_code="7504"),
                     row(date=dt.date(2026, 10, 2), gross=Decimal("24.00"), vat=Decimal("4.00"), account_code="7504"),
                     year_start=YEAR)
    second = add_rows(mongo_store, acme,
                      row(date=dt.date(2026, 10, 3), gross=Decimal("36.00"), vat=Decimal("6.00"), account_code="7504"),
                      year_start=YEAR)
    return first, second


@pytest.mark.mongo
def test_list_rows_lists_rows_that_have_no_issues(mongo_store, deps, acme):
    three_clean_rows(mongo_store, acme)

    listed = rows_of_the_year(mongo_store, deps, acme)

    assert [r["gross"] for r in listed["rows"]] == ["12.00", "24.00", "36.00"]
    assert all(r["kind"] == "receipt" and r["booked"] is True and "issues" not in r for r in listed["rows"])
    assert listed["rows"][0]["vat"] == "2.00" and "next_after_row_id" not in listed
    ids = [r["row_id"] for r in listed["rows"]]
    assert ids == sorted(ids)
    model = ScriptedChatModel(replies=[call("get_row", row_id=ids[1]), say("Looked.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())
    list(stream_turn(agent, context(acme), "Look."))
    [result] = answered(model, 1)
    assert result.status != "error" and json.loads(result.content)["row_id"] == ids[1]


@pytest.mark.mongo
def test_list_rows_by_document_lists_only_its_rows(mongo_store, deps, acme):
    first, second = three_clean_rows(mongo_store, acme)

    listed = rows_of_the_year(mongo_store, deps, acme, upload_id=second)

    assert [(r["upload_id"], r["gross"]) for r in listed["rows"]] == [(second, "36.00")]
    assert len(rows_of_the_year(mongo_store, deps, acme, upload_id=first)["rows"]) == 2


@pytest.mark.mongo
def test_list_rows_leaves_out_excluded_and_replaced_rows(mongo_store, deps, acme):
    three_clean_rows(mongo_store, acme)
    first, second, third = (r.id for r in sorted(mongo_store.rows(acme), key=lambda r: r.id))
    mongo_store.patch_row(acme, first, {"excluded": True})
    mongo_store.patch_row(acme, second, {"superseded_by": "d1"})

    listed = rows_of_the_year(mongo_store, deps, acme)

    assert [r["row_id"] for r in listed["rows"]] == [third]


@pytest.mark.mongo
def test_list_rows_pages(mongo_store, deps, acme):
    three_clean_rows(mongo_store, acme)

    page = rows_of_the_year(mongo_store, deps, acme, limit=1)
    after = rows_of_the_year(mongo_store, deps, acme, limit=2, after_row_id=page["next_after_row_id"])

    assert len(page["rows"]) == 1 and page["next_after_row_id"] == page["rows"][0]["row_id"]
    assert [r["gross"] for r in after["rows"]] == ["24.00", "36.00"] and "next_after_row_id" not in after


@pytest.mark.mongo
def test_an_empty_trial_balance_says_nothing_is_posted_yet(mongo_store, deps, acme):
    three_clean_rows(mongo_store, acme)

    def trial_balance() -> dict:
        model = ScriptedChatModel(replies=[call("get_trial_balance"), say("Looked.")])
        agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())
        list(stream_turn(agent, context(acme), "Is it posted?"))
        return json.loads(answered(model, 1)[0].content)

    before = trial_balance()
    assert "list_rows" in before["note"] and "Nothing is posted yet" in before["note"]

    ledger_book.post(mongo_store, deps.records, acme, YEAR)
    after = trial_balance()
    assert "note" not in after and after["entries_posted"] > 0


@pytest.mark.mongo
def test_a_bank_line_its_receipt_records_is_not_listed_as_needing_a_look(mongo_store, deps, acme):
    add_rows(mongo_store, acme,
             row(date=dt.date(2026, 10, 19), gross=Decimal("64.00"), account_code="7504", counterparty="Costco",
                 document_ref="r", document_total=Decimal("64.00")),
             row(date=dt.date(2026, 10, 19), gross=Decimal("64.00"), account_code="9998", counterparty="COSTCO",
                 document_ref="card", document_type="statement",
                 issues=[issue("account_not_recognised", "Account unclear: put in Suspense. Choose one.")]),
             year_start=dt.date(2026, 4, 1))
    model = ScriptedChatModel(replies=[call("list_issues"), call("get_overview"), say("Looked.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    list(stream_turn(agent, context(acme), "What needs a look?"))

    listed, overview = (json.loads(answered(model, n)[0].content) for n in (1, 2))
    assert listed.get("rows", []) == [] and overview["need_a_look"] == {}


@pytest.mark.mongo
def test_get_overview_lists_the_accounts_a_row_can_be_coded_to(mongo_store, deps, acme):
    model = ScriptedChatModel(replies=[call("get_overview"), say("Looked.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())

    list(stream_turn(agent, context(acme), "Look over the books."))

    [result] = answered(model, 1)
    accounts = json.loads(result.content)["accounts"]
    assert {"code": "8205", "name": next(a["name"] for a in accounts if a["code"] == "8205")} in accounts
    assert all(set(a) == {"code", "name"} for a in accounts)


def ask_about(deps, client_id: int, *row_ids: int) -> tuple[dict, list]:
    """What ask_user answers when called on the rows in the client's first year, and the agent's questions then."""
    model = ScriptedChatModel(replies=[call("ask_user", row_ids=list(row_ids), question="When was it?",
                                            why="No date."), say("Asked.")])
    agent = build_agent(SETTINGS, deps, model=model, checkpointer=InMemorySaver())
    list(stream_turn(agent, ChatContext(client_id=client_id, thread_id=uuid4().hex, year_start=dt.date(2025, 4, 1)),
                     "Ask."))
    [result] = answered(model, 1)
    mine = [q for q in deps.records.questions(client_id, dt.date(2025, 4, 1)) if q.source == "agent"]
    return json.loads(result.content), mine


@pytest.mark.mongo
def test_ask_user_leaves_out_what_the_books_already_ask(mongo_store, deps, acme):
    add_rows(mongo_store, acme, row(date=None, gross=Decimal("12.00"), account_code="7400", description="taxi"),
             row(date=dt.date(2025, 6, 1), gross=Decimal("12.00"), account_code="7400", description="bus",
                 document_type="statement"), year_start=dt.date(2025, 4, 1))
    taxi, bus = (next(r.id for r in mongo_store.rows(acme) if r.tx.description == d) for d in ("taxi", "bus"))

    result, mine = ask_about(deps, acme, taxi)
    assert result["asked"] is False and f"row {taxi} (a date)" in result["message"] and mine == []

    result, mine = ask_about(deps, acme, taxi, bus)
    assert [q.row_ids for q in mine] == [[bus]] and f"row {taxi} (a date)" in result["note"]


@pytest.mark.mongo
def test_ask_user_asks_about_a_row_the_books_do_not_ask_about(mongo_store, deps, acme):
    add_rows(mongo_store, acme, row(date=dt.date(2025, 6, 1), gross=Decimal("12.00"), account_code="7400",
                                    description="bus", document_type="statement"), year_start=dt.date(2025, 4, 1))
    bus = mongo_store.rows(acme)[0].id

    result, mine = ask_about(deps, acme, bus)

    assert result["status"] == "open" and [q.row_ids for q in mine] == [[bus]]


def the_tool(name: str):
    return next(t for t in make_tools(AgentDeps(store=None, records=None, blobs=None), actor="agent:test")
                if t.name == name)


def ask_user_args(question: str, why: str) -> dict:
    return {"row_ids": [1], "question": question, "why": why}


def test_ask_user_takes_the_longer_questions_the_model_writes():
    schema = the_tool("ask_user").tool_call_schema
    schema.model_validate(ask_user_args("q" * 290, "w" * 390))
    schema.model_validate(ask_user_args("q" * 300, "w" * 400))
    for question, why in (("q" * 301, "why"), ("q", "w" * 401)):
        with pytest.raises(ValueError):
            schema.model_validate(ask_user_args(question, why))


def test_resolve_issue_takes_a_reason_of_up_to_600_characters():
    schema = the_tool("resolve_issue").tool_call_schema
    args = {"row_id": 1, "resolution": {"action": "keep_one", "keep_row_id": 1, "drop_row_ids": [2]},
            "evidence": [{"upload_id": 5, "quote": "q"}]}
    schema.model_validate({**args, "reason": "r" * 600})
    with pytest.raises(ValueError):
        schema.model_validate({**args, "reason": "r" * 601})


def evidence_of(value):
    args = {"row_id": 1, "resolution": {"action": "keep_one", "keep_row_id": 1, "drop_row_ids": [2]}, "reason": "Same purchase.",
            "evidence": value}
    return the_tool("resolve_issue").tool_call_schema.model_validate(args).evidence


def test_resolve_issue_takes_evidence_sent_as_a_json_string():
    from ledgersync.models import Evidence
    want = [Evidence(upload_id=516, quote="of which VAT @ 20%")]
    assert evidence_of('[{"upload_id": 516, "quote": "of which VAT @ 20%"}]') == want
    # The one slip live runs made: a stray quote after the upload id.
    assert evidence_of('[{"upload_id": 516", "quote": "of which VAT @ 20%"}]') == want
    assert evidence_of(want) == want                                  # a list passes unchanged
    assert evidence_of([{"upload_id": 516, "quote": "of which VAT @ 20%"}]) == want


@pytest.mark.parametrize("broken", [
    '[{"upload_id": 516, "quote": "of which VAT @ 20%"',            # cut off
    '[{"upload_id": "516", "quote": "x"}, {"upload_id": 7", "quote": x}]',   # a second slip the repair doesn't cover
    '[{"upload_id": 516, "quote": "an "odd" quote"}]',              # a stray quote elsewhere is not repaired
    "not json",
])
def test_resolve_issue_still_refuses_evidence_broken_any_other_way(broken):
    with pytest.raises(ValueError):
        evidence_of(broken)


def test_resolve_issue_still_tells_the_model_evidence_is_a_list():
    schema = the_tool("resolve_issue").tool_call_schema.model_json_schema()
    evidence = schema["properties"]["evidence"]
    assert evidence["type"] == "array" and "anyOf" not in evidence and "oneOf" not in evidence
    item = schema["$defs"][evidence["items"]["$ref"].rsplit("/", 1)[-1]]
    assert item["properties"].keys() >= {"upload_id", "quote"}


def test_the_prompt_names_the_rules_the_live_runs_broke():
    for rule in ("Never quote a row's description", "Use only row ids that a tool gave you", "list_rows",
                 "the books ask the person about those automatically", "from the accounts get_overview lists",
                 "Use the invoice over an expenses-sheet line (use_primary): the service checks that a bank line pays it, "
                 "and says to ask when none does."):
        assert rule in " ".join(SYSTEM_PROMPT.split())


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
    assert {t.name for t in tools} == {"get_overview", "list_issues", "list_rows", "get_row", "list_documents", "read_document",
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
                                         "model": "qwen/qwen3.8-27b", "system_fingerprint": None,
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "Hello."}}]})

    model = chat_model(Settings(openrouter_api_key="test-key"))
    model.client = openrouter.OpenRouter(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(answer)))

    assert model.invoke("Hi").content == "Hello."
    assert model.request_timeout == 60_000 and model.max_retries == 2   # milliseconds, as ChatOpenRouter takes them
    [body] = sent
    assert body["max_tokens"] == 8192   # a degenerate generation is cut off, not left to run
    assert body["model"] == "qwen/qwen3.8-27b" and body["reasoning"] == {"effort": "medium"}
    # The reader's hosts, a full-precision one first and never a 4-bit one (tests/test_model_client.py), and none that
    # keeps the data.
    assert body["provider"] == {"order": ["deepinfra/bf16"], "quantizations": ["bf16", "fp16", "fp32", "fp8"],
                                "require_parameters": True, "data_collection": "deny"}


@pytest.mark.parametrize("env, route", [
    ({}, {"order": ["deepinfra/bf16"], "quantizations": ["bf16", "fp16", "fp32", "fp8"]}),
    ({"OPENROUTER_PROVIDERS": "parasail/fp8, wafer", "OPENROUTER_QUANTIZATIONS": "bf16"},
     {"order": ["parasail/fp8", "wafer"], "quantizations": ["bf16"]}),
    ({"OPENROUTER_PROVIDERS": "", "OPENROUTER_QUANTIZATIONS": ""}, {}),   # blank: left to OpenRouter, as for the reader
], ids=["defaults", "set", "blank"])
def test_the_agent_goes_to_the_hosts_the_reader_does(env, route):
    # Left to choose, OpenRouter sent reads of this one model to a 4-bit host that dropped lines: the agent must not
    # land there either, and follows the same two settings.
    settings = Settings.from_env({"OPENROUTER_API_KEY": "test-key", **env})
    assert chat_model(settings).openrouter_provider == {"require_parameters": True, "data_collection": "deny", **route}


def connected(model, answer):
    """The chat model with its OpenRouter client answering every request through `answer`, a function of the request."""
    model.client = openrouter.OpenRouter(api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(answer)))
    return model


def sse(*chunks) -> bytes:
    """What OpenRouter streams back: a chunk for each (delta, finish_reason) given."""
    return b"".join(
        b"data: " + json.dumps({"id": "gen-1", "object": "chat.completion.chunk", "created": 1,
                                "model": "qwen/qwen3.8-27b",
                                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}).encode() + b"\n\n"
        for delta, finish in chunks) + b"data: [DONE]\n\n"


def test_the_agent_runs_on_the_model_that_reads_the_files():
    settings = Settings.from_env({"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "some/model",
                                  "LEDGERSYNC_AGENT_MODEL": "x/y"})   # the old variable changes nothing
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "gen-1", "object": "chat.completion", "created": 1,
                                         "model": "some/model", "system_fingerprint": None,
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "Hello."}}]})

    connected(chat_model(settings), answer).invoke("Hi")

    assert sent[0]["model"] == ModelClient(settings).model == "some/model"


def test_reasoning_sent_the_openrouter_way_is_kept_apart_from_the_reply():
    # OpenRouter streams a reasoning model's thinking in its own fields (`reasoning`, `reasoning_details`), apart from
    # the reply's content, whichever model it is: the person never gets it as a token, and the run carries on.
    thought = "A row id is a number, not a name."
    tool_call = {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_row", "arguments": '{"row_id": "the taxi"}'}}
    answers = iter([
        sse(({"role": "assistant", "reasoning": thought,
              "reasoning_details": [{"type": "reasoning.text", "text": thought, "index": 0, "format": "unknown"}]},
             None),
            ({"tool_calls": [tool_call]}, None), ({}, "tool_calls")),
        sse(({"role": "assistant", "reasoning": "The tool wants a number."}, None),
            ({"content": "Row ids are "}, None), ({"content": "numbers."}, None), ({}, "stop")),
    ])
    model = connected(chat_model(Settings(openrouter_api_key="test-key")),
                      lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                     content=next(answers)))
    agent, ctx = offline_agent(model), context()

    events = list(stream_turn(agent, ctx, "Look at the taxi."))

    assert set(kinds(events)) == {"token"} and "".join(e.data["text"] for e in events) == "Row ids are numbers."
    assert "number, not a name" not in "".join(e.sse() for e in events)
    kept = agent.get_state({"configurable": {"thread_id": ctx.thread_id}}).values["messages"]
    assert [m.type for m in kept] == ["human", "ai", "tool", "ai"] and kept[2].status == "error"
    assert kept[1].content == "" and kept[1].additional_kwargs["reasoning_content"] == thought   # kept, not said
    assert kept[3].text == "Row ids are numbers."


def test_chat_model_needs_the_openrouter_key():
    with pytest.raises(ModelUnavailable, match="OPENROUTER_API_KEY"):
        chat_model(Settings())


def test_an_event_is_sent_as_sse():
    event = ChatEvent("step", {"id": "call_1", "text": "Opened costco-receipt-21-10.txt", "state": "done"})
    assert event.sse() == ('event: step\ndata: {"id": "call_1", "text": "Opened costco-receipt-21-10.txt", '
                           '"state": "done"}\n\n')
