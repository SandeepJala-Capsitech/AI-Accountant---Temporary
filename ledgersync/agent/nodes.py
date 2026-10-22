"""The job's steps. Ingest, classify, the quality gate, fix and requests are built; until a later task builds the
snapshot, it is a stub that only says so in the feed.

`staged` wraps every step, stubs or real: a cancelled job stops before its next step, and otherwise the job
shows which stage it is at and its feed says the step started.

`make_agent_node` builds a step that an agent runs (classify, and each fix round): LangChain's agent with some of
the tools, a prompt, a result it returns when it finishes, and limits on its model and tool calls."""
from __future__ import annotations

import datetime as dt
import warnings
from dataclasses import dataclass
from typing import Callable

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from ledgersync import ledger as books
from ledgersync.config import MAX_FIX_ROUNDS
from ledgersync.gate import gate_for
from ledgersync.jobs import JobCancelled
from ledgersync.notify import no_email_note, send_document_requests
from ledgersync.records.jobs import JobRecords

from .graph import STEPS, Node, Nodes
from .prompts import AGENT_REQUEST, CLASSIFY_PROMPT, FIX_PROMPT
from .schemas import FixResult, PostingResult
from .state import JobState
from .tools import ToolDeps, exception_tools, fix_tools, row_tools

NOT_BUILT = "This step isn't built yet, so it did nothing."

# An agent keeps no checkpoints, so the durability make_agent_node gives it has nothing to do, as LangGraph warns on
# each run. That is intended (see make_agent_node), so the warning is left out.
warnings.filterwarnings("ignore", message="`durability` has no effect when no checkpointer is present",
                        category=UserWarning)


def staged(nodes: Nodes, records: JobRecords) -> Nodes:
    """The same steps, each run inside `_staged`."""
    return Nodes(**{step: _staged(step, getattr(nodes, step), records) for step in STEPS})


def _staged(stage: str, node: Node, records: JobRecords) -> Node:
    def run(state: JobState) -> dict:
        """Raises JobCancelled when the job has been asked to stop. Otherwise saves the job's stage and the
        fix rounds done so far, writes "Started <stage>" to its feed, and runs the step."""
        job_id = state["job_id"]
        if records.get(job_id).cancel_requested:
            raise JobCancelled()
        records.update(job_id, stage=stage, fix_rounds=state["fix_rounds"])
        records.event(job_id, stage, f"Started {stage}")
        return node(state)

    return run


def stub_nodes(records: JobRecords) -> Nodes:
    """Steps that only write NOT_BUILT to the feed. The gate passes, so a job goes straight on to review."""

    def stub(stage: str, changes=lambda state: {}) -> Node:
        def run(state: JobState) -> dict:
            records.event(state["job_id"], stage, NOT_BUILT)
            return changes(state)

        return run

    return Nodes(ingest=stub("ingest"), classify=stub("classify"),
                 gate=stub("gate", lambda state: {"gate_passed": True}),
                 fix=stub("fix", lambda state: {"fix_rounds": state["fix_rounds"] + 1}),
                 requests=stub("requests"), snapshot=stub("snapshot"))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def make_ingest(deps: ToolDeps) -> Node:
    """The ingest step: looks at the client's saved rows and says in the feed how many documents and rows it found
    and how many need a look. It changes nothing."""

    def ingest(state: JobState) -> dict:
        built = books.build(deps.store, state["client_id"])
        rows = built.transactions
        review = sum(books.needs_review(r) for r in rows)
        if review:
            look = f"{_count(review, 'row')} {'needs' if review == 1 else 'need'} a look."
        else:
            look = "No rows need a look."
        deps.records.event(state["job_id"], "ingest",
                           f"Found {_count(len(built.uploads), 'document')} and {_count(len(rows), 'row')}. {look}")
        return {}

    return ingest


def make_requests(deps: ToolDeps) -> Node:
    """The requests step: emails the client one link for every document the run asked for, and says in the feed
    how many it asked for (or that there was no address to send them to)."""

    def requests(state: JobState) -> dict:
        job_id, client_id = state["job_id"], state["client_id"]
        pending = len(deps.requests.pending_for(job_id))
        sent = send_document_requests(deps, client_id, job_id)
        if sent:
            line = f"Asked the client for {_count(sent, 'document')}."
        elif pending:
            line = no_email_note(deps.store.get_client(client_id).name, pending)
        else:
            line = "No documents to ask the client for."
        deps.records.event(job_id, "requests", line)
        return {}

    return requests


# --- steps an agent runs ------------------------------------------------------------------------------------

# Builds an agent from a model factory, its tools, its system prompt, the type of its result, and its limits on
# model calls and on tool calls.
AgentFactory = Callable[[Callable[[], BaseChatModel], list[BaseTool], str, type[BaseModel], int, int], Runnable]

CLASSIFY_TOOLS = ("list_rows_to_review", "get_row", "lookup_account", "update_row", "log_exception",
                  "request_client_document")
FIX_TOOLS = ("get_gate_results", "get_trial_balance", "get_statement_checks", "list_rows", "get_row", "lookup_account",
             "update_row", "revert_row", "log_exception", "request_client_document")
LIMIT_ASSUMPTION = "Whatever it hadn't reached is left as it was, for the quality gate and the reviewer."
GATE_ASSUMPTION = "Left for the reviewer to decide."


@dataclass(frozen=True)
class AgentContext:
    """What one agent run is given besides its message: `cancelled()` says whether its job has been asked to stop."""

    cancelled: Callable[[], bool]


class StopWhenCancelled(AgentMiddleware):
    """Raises JobCancelled before each of the agent's model calls once its job has been asked to stop, so a cancel
    stops the agent at its next call instead of waiting for it to finish."""

    def before_model(self, state, runtime) -> None:
        if runtime.context is not None and runtime.context.cancelled():
            raise JobCancelled()
        return None


def default_agent_factory(model_factory: Callable[[], BaseChatModel], tools: list[BaseTool], prompt: str,
                          result_type: type[BaseModel], model_calls: int, tool_calls: int) -> Runnable:
    """LangChain's agent on the model `model_factory` builds (called here, inside the step, so a job needs a model
    only once it gets this far), with the tools and the prompt as its system prompt.

    - It returns `result_type` by calling a tool of that name (ToolStrategy), as not every OpenRouter model offers
      structured output of its own; a result that doesn't fit the type goes back to the model to try again.
    - It stops with JobCancelled before a model call once its job is cancelled (StopWhenCancelled).
    - It ends, without raising and without a result, before a model call past `model_calls`, or when a model call
      asks for tool calls past `tool_calls` (both limits per run; "end" is the exit_behavior that neither raises
      nor lets the model carry on).
    - A tool call whose arguments don't fit the tool goes back to the model as an error: the ToolNode's default.
    - It keeps no checkpoints of its own (`checkpointer=False`; it would otherwise save every step into the job's
      checkpoints). The job's graph checkpoints its steps, and a step that stopped part-way (a retry after an
      error, or a restart) runs again from the start in a fresh conversation. Its row changes are idempotent:
      update_row writes only what differs, and a row it changed no longer needs a look. The exceptions and
      document requests it made are not, as a new conversation could word them differently, so get_row and
      list_rows_to_review show the agent the exceptions this job logged on each row, with the document asked for
      about each, and the prompt tells it that such a row needs no new exception, and that a document it asks
      for about the row goes on that exception (asking again for the same document in the same job gets the
      request already made)."""
    return create_agent(
        model_factory(), tools, system_prompt=prompt, response_format=ToolStrategy(result_type),
        middleware=[StopWhenCancelled(),
                    ModelCallLimitMiddleware(run_limit=model_calls, exit_behavior="end"),
                    ToolCallLimitMiddleware(run_limit=tool_calls, exit_behavior="end")],
        context_schema=AgentContext, checkpointer=False)


def make_agent_node(deps: ToolDeps, *, stage: str, prompt: str, tool_names: tuple[str, ...],
                    result_type: type[BaseModel], model_factory: Callable[[], BaseChatModel], model_calls: int,
                    tool_calls: int, agent_factory: AgentFactory = default_agent_factory) -> Node:
    """A step that an agent runs. Each time it runs it builds the tools named in `tool_names` (from the row tools,
    the exception tools and the fix tools) for this job's client, job and stage, and the agent from
    `agent_factory`, with `prompt` formatted with the client's name and the job's period. It gives the agent one
    message naming the client and the period. The agent's result (a `result_type`, which has a `summary`) goes in
    the job's feed as its summary. An agent that stopped at a call limit has no result: an exception says so, once
    per job and stage, and the feed says so. Returns {}."""

    def run(state: JobState) -> dict:
        job_id, client_id = state["job_id"], state["client_id"]
        available = {**row_tools(deps, client_id, job_id), **exception_tools(deps, client_id, job_id, stage),
                     **fix_tools(deps, client_id, job_id)}
        about = {"client_name": deps.store.get_client(client_id).name, "period_start": state["period_start"],
                 "period_end": state["period_end"]}
        agent = agent_factory(model_factory, [available[name] for name in tool_names], prompt.format(**about),
                              result_type, model_calls, tool_calls)
        # The agent runs inside the job's graph and would take on its durability, "sync", under which LangGraph
        # waits after each step for a checkpoint save that a graph without a checkpointer never starts (an
        # AttributeError in langgraph 1.2.16). "exit" saves only at the end, which for the agent is nothing.
        result = agent.invoke({"messages": [HumanMessage(AGENT_REQUEST.format(**about))]},
                              context=AgentContext(cancelled=lambda: deps.records.get(job_id).cancel_requested),
                              durability="exit")
        finished = result.get("structured_response")
        if finished is None:
            _stopped_at_limit(deps, state, stage)
        else:
            deps.records.event(job_id, stage, finished.summary)
        return {}

    return run


def _stopped_at_limit(deps: ToolDeps, state: JobState, stage: str) -> None:
    """Logs the exception for an agent that stopped at a call limit (once per job and stage: its description names
    the stage), and says so in the feed."""
    description = f"The agent stopped at its call limit before finishing {stage}."
    _system_exception(deps, state, stage, description, LIMIT_ASSUMPTION, [])
    deps.records.event(state["job_id"], stage, description)


def _system_exception(deps: ToolDeps, state: JobState, stage: str, description: str, assumption: str,
                      row_ids: list[int]) -> None:
    """Logs an exception the system raises, with confidence 0, and audits it as the system, unless this job already
    has it (the same description for the same rows), as when a step runs again after a retry."""
    job_id, client_id = state["job_id"], state["client_id"]
    logged, new = deps.exceptions.log_once(client_id=client_id, job_id=job_id, stage=stage, description=description,
                                           assumption=assumption, confidence=0.0, row_ids=row_ids)
    if new:
        deps.audit.append(actor="system", action="log_exception", client_id=client_id, job_id=job_id,
                          target={"exception_id": logged["id"]}, before=None,
                          after={"stage": stage, "description": description, "assumption": assumption,
                                 "confidence": logged["confidence"], "row_ids": logged["row_ids"]})


def make_classify(deps: ToolDeps, model_factory: Callable[[], BaseChatModel],
                  agent_factory: AgentFactory = default_agent_factory) -> Node:
    """The classify step: an agent that works through every row that needs a look, with CLASSIFY_TOOLS, in at most
    60 model calls and 150 tool calls."""
    return make_agent_node(deps, stage="classify", prompt=CLASSIFY_PROMPT, tool_names=CLASSIFY_TOOLS,
                           result_type=PostingResult, model_factory=model_factory, model_calls=60, tool_calls=150,
                           agent_factory=agent_factory)


def make_fix(deps: ToolDeps, model_factory: Callable[[], BaseChatModel],
             agent_factory: AgentFactory = default_agent_factory) -> Node:
    """One fix round: an agent that works on the quality gate's failing checks with FIX_TOOLS, in at most 30 model
    calls and 80 tool calls. Returns the fix rounds done, counting this one (build_graph counts them too, the same
    way)."""
    agent = make_agent_node(deps, stage="fix", prompt=FIX_PROMPT, tool_names=FIX_TOOLS, result_type=FixResult,
                            model_factory=model_factory, model_calls=30, tool_calls=80, agent_factory=agent_factory)

    def fix(state: JobState) -> dict:
        agent(state)
        return {"fix_rounds": state["fix_rounds"] + 1}

    return fix


# --- the quality gate ---------------------------------------------------------------------------------------

def make_gate(deps: ToolDeps, max_fix_rounds: int) -> Node:
    """The gate step: runs the quality gate on the client's rows as they are now, saves its result as the job's
    gate, and says in the feed that it passed or which checks failed. Returns {"gate_passed": ...}.

    Once the fix rounds are used up (`max_fix_rounds`, never more than MAX_FIX_ROUNDS, as build_graph counts them),
    a failing gate is the last: each failing check becomes an exception for the reviewer, with its rows, logged and
    audited as the system once per job. The job then goes on to review."""
    last_round = min(max_fix_rounds, MAX_FIX_ROUNDS)

    def gate(state: JobState) -> dict:
        job_id, rounds = state["job_id"], state["fix_rounds"]
        result = gate_for(deps.store, state["client_id"], dt.date.fromisoformat(state["period_start"]),
                          dt.date.fromisoformat(state["period_end"]))
        deps.records.update(job_id, gate=result.model_dump(mode="json"))
        failed = [c for c in result.checks if not c.passed]
        if not failed:
            deps.records.event(job_id, "gate", "Quality gate passed.")
            return {"gate_passed": True}
        deps.records.event(job_id, "gate", f"Quality gate: {len(failed)} of {len(result.checks)} checks failed "
                                           f"({', '.join(c.code for c in failed)}).")
        if rounds >= last_round:
            for found in failed:
                _system_exception(deps, state, "gate",
                                  f"Quality gate check {found.code} still fails after {_count(rounds, 'fix round')}: "
                                  f"{found.detail}", GATE_ASSUMPTION, found.row_ids)
        return {"gate_passed": False}

    return gate
