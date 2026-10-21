"""The job's graph: which steps run in which order, the fix loop's limit, carrying on after a crash, and the
wrapper every step runs in."""
import datetime as dt
import itertools

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ledgersync.agent import JobState, Nodes, build_graph, staged
from ledgersync.jobs import JobCancelled
from ledgersync.records.jobs import JobRecords

# The job's first state, as the runner starts it (not LangGraph's START sentinel).
START: JobState = {"job_id": "j1", "client_id": 1, "period_start": "2025-04-01", "period_end": "2026-03-31",
                   "fix_rounds": 0, "gate_passed": False}
CONFIG = {"configurable": {"thread_id": "j1"}}


def stub_nodes(calls: list, gate=(), classify_fails_once: bool = False, fix_counts: bool = True) -> Nodes:
    """Nodes that note their names in `calls`. The gate passes or fails as `gate` says, in turn, and
    each fix round adds one to fix_rounds (or, with fix_counts=False, returns nothing)."""
    answers = iter(gate)
    crashes = [RuntimeError("classify crashed")] if classify_fails_once else []
    fixed = (lambda state: {"fix_rounds": state["fix_rounds"] + 1}) if fix_counts else (lambda state: {})

    def stub(name, result=lambda state: {}):
        def node(state):
            calls.append(name)
            if name == "classify" and crashes:
                raise crashes.pop()
            return result(state)
        return node

    return Nodes(ingest=stub("ingest"), classify=stub("classify"),
                 gate=stub("gate", lambda state: {"gate_passed": next(answers)}),
                 fix=stub("fix", fixed), requests=stub("requests"), snapshot=stub("snapshot"))


def prepare(gate, classify_fails_once=False, max_fix_rounds=3, fix_counts=True):
    """The graph on stub nodes and an in-memory checkpointer, with the list its nodes note their names in."""
    calls = []
    nodes = stub_nodes(calls, gate, classify_fails_once, fix_counts)
    return calls, build_graph(nodes, InMemorySaver(), max_fix_rounds=max_fix_rounds), CONFIG


def run(gate, **options) -> list:
    calls, graph, config = prepare(gate, **options)
    graph.invoke(START, config)
    return calls


def test_a_passing_gate_goes_straight_to_requests_and_snapshot():
    calls = run(gate=[True])
    assert calls == ["ingest", "classify", "gate", "requests", "snapshot"]


def test_a_failing_gate_gets_three_fix_rounds_then_carries_on():
    calls = run(gate=[False] * 4)
    assert calls == ["ingest", "classify", "gate", "fix", "gate", "fix", "gate", "fix", "gate", "requests", "snapshot"]


def test_a_gate_that_passes_after_a_fix_ends_the_loop():
    calls = run(gate=[False, True])
    assert calls == ["ingest", "classify", "gate", "fix", "gate", "requests", "snapshot"]


def test_the_number_of_fix_rounds_can_be_set():
    calls = run(gate=[False] * 2, max_fix_rounds=1)
    assert calls == ["ingest", "classify", "gate", "fix", "gate", "requests", "snapshot"]


FIX_THREE_TIMES = ["ingest", "classify", "gate", "fix", "gate", "fix", "gate", "fix", "gate", "requests", "snapshot"]


def test_the_graph_counts_fix_rounds_even_when_the_fix_step_doesnt():
    calls, graph, config = prepare(gate=itertools.repeat(False), fix_counts=False)
    assert graph.invoke(START, config)["fix_rounds"] == 3
    assert calls == FIX_THREE_TIMES


def test_more_than_three_fix_rounds_is_never_allowed():
    calls, graph, config = prepare(gate=itertools.repeat(False), max_fix_rounds=10)
    assert graph.invoke(START, config)["fix_rounds"] == 3
    assert calls == FIX_THREE_TIMES


def test_a_crash_resumes_from_the_last_finished_node():
    calls, graph, config = prepare(gate=[True], classify_fails_once=True)
    with pytest.raises(RuntimeError):
        graph.invoke(START, config)
    graph.invoke(None, config)
    assert calls == ["ingest", "classify", "classify", "gate", "requests", "snapshot"]


@pytest.mark.mongo
def test_each_step_sets_the_stage_and_says_it_started_unless_the_job_is_cancelled(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, dt.date(2025, 4, 1), dt.date(2026, 3, 31))
    calls = []
    classify = staged(stub_nodes(calls), records).classify
    state = {**START, "job_id": job.id, "fix_rounds": 2}
    assert classify(state) == {}
    saved = records.get(job.id)
    assert (saved.stage, saved.fix_rounds) == ("classify", 2)
    assert [(e.stage, e.message) for e in records.events(job.id)] == [("classify", "Started classify")]
    records.update(job.id, cancel_requested=True)
    with pytest.raises(JobCancelled):
        classify(state)
    assert calls == ["classify"]                       # the cancelled step didn't run
    assert len(records.events(job.id)) == 1
