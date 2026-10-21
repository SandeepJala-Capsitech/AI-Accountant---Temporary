"""The job as a LangGraph graph: ingest, classify and the quality gate, a fix loop back to the gate while it
fails, then the document requests and the trial balance snapshot.

The checkpointer saves the state after every step, so a job that stopped carries on from its last finished
step when it is invoked again with no input."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from langgraph.graph import END, START, StateGraph

from .state import JobState

Node = Callable[[JobState], dict]

STEPS = ("ingest", "classify", "gate", "fix", "requests", "snapshot")


@dataclass(frozen=True)
class Nodes:
    """The job's six steps. Each takes the state and returns the fields it changes, or {}."""

    ingest: Node
    classify: Node
    gate: Node
    fix: Node
    requests: Node
    snapshot: Node


def build_graph(nodes: Nodes, checkpointer, *, max_fix_rounds: int = 3):
    """The compiled graph. After the gate it goes to fix and back to the gate while the gate fails and fewer
    than `max_fix_rounds` rounds are done; then on to requests and snapshot, whether the gate passed or not."""

    def after_gate(state: JobState) -> str:
        return "requests" if state["gate_passed"] or state["fix_rounds"] >= max_fix_rounds else "fix"

    graph = StateGraph(JobState)
    for step in STEPS:
        graph.add_node(step, getattr(nodes, step))
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "classify")
    graph.add_edge("classify", "gate")
    graph.add_conditional_edges("gate", after_gate, ["fix", "requests"])
    graph.add_edge("fix", "gate")
    graph.add_edge("requests", "snapshot")
    graph.add_edge("snapshot", END)
    return graph.compile(checkpointer=checkpointer)
