"""The job's steps. Until the later tasks build them, each step is a stub that only says so in the feed.

`staged` wraps every step, stubs or real: a cancelled job stops before its next step, and otherwise the job
shows which stage it is at and its feed says the step started."""
from __future__ import annotations

from ledgersync.jobs import JobCancelled
from ledgersync.records.jobs import JobRecords

from .graph import STEPS, Node, Nodes
from .state import JobState

NOT_BUILT = "This step isn't built yet, so it did nothing."


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
