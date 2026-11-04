"""Taking back a decision a person doesn't want, and making one the background review only suggested. Neither changes
a stored decision: the log only grows. An undo is a decision of its own that reverses the first; an applied
suggestion is a new applied decision, and the change record says which suggestion it came from."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import TypeAdapter

from . import resolutions, upkeep
from .agent.locks import ClientLocks
from .errors import InvalidInput
from .models import ChangeDraft, Decision, DecisionDraft
from .records import AgentRecords
from .resolutions import Resolution, Texts
from .store_mongo import MongoStore

_NUMBERS = {"gross", "vat"}


def _same(field: str, now, wanted) -> bool:
    if field in _NUMBERS and now is not None and wanted is not None:
        try:
            return Decimal(str(now)) == Decimal(str(wanted))
        except InvalidOperation:
            pass
    return now == wanted


def _decision(records: AgentRecords, client_id: int, decision_id: str) -> Optional[Decision]:
    return next((d for d in records.decisions(client_id) if d.id == decision_id), None)


def undo_decision(store: MongoStore, records: AgentRecords, locks: ClientLocks, *, client_id: int,
                  decision_id: str) -> Decision:
    """Puts back what an applied decision changed and records the reversal. InvalidInput when the decision isn't the
    client's, isn't applied, was undone already, or a row it changed has changed since (the person changes it by
    hand then). The change is remembered, so neither the chat nor the review makes it again."""
    d = _decision(records, client_id, decision_id)
    if d is None:
        raise InvalidInput("This decision was not found.")
    if d.status != "applied" or d.action == "undo":
        raise InvalidInput("Only a change that was made can be undone.")
    if any(x.reverses == decision_id for x in records.decisions(client_id)):
        raise InvalidInput("This change was undone already.")
    key = records.decision_key(client_id, decision_id) or ""
    with locks.hold(client_id):
        rows = {r.id: r.tx for r in store.rows(client_id)}
        for n, wanted in d.after.items():
            tx = rows.get(int(n)) if n.isdigit() else None
            now = tx.model_dump(mode="json") if tx else {}
            if tx is None or not all(_same(f, now.get(f), v) for f, v in wanted.items()):
                raise InvalidInput(f"Row {n} has changed since; change it by hand instead.")
        touched = [int(n) for n in d.before]
        parts = [int(n) for n in d.after if n not in d.before]
        saved = store.row_data(client_id, touched)
        try:
            for n, values in d.before.items():
                store.patch_row(client_id, int(n), dict(values))
        except Exception:
            store.put_row_data(client_id, saved)
            raise
        for n in parts:
            store.delete_row(client_id, n)
        undone, _ = records.append_decision(DecisionDraft(
            client_id=client_id, year_start=d.year_start, action="undo", rule=f"Undo: {d.rule}", status="applied",
            row_ids=d.row_ids, reason=f"A person undid {d.rule}.", before=d.after, after=d.before, actor="user",
            reverses=decision_id), f"undo|{decision_id}")
        records.reject_key(client_id, key)
    upkeep.after_change(store, records, locks, ChangeDraft(
        client_id=client_id, year_start=d.year_start, actor="user", kind="decision_undone", row_ids=d.row_ids,
        before=d.after, after={"decision_id": decision_id}, summary=f"Undid {d.rule} (rows {', '.join(map(str, d.row_ids))})"))
    return undone


def apply_suggestion(store: MongoStore, records: AgentRecords, locks: ClientLocks, *, client_id: int,
                     decision_id: str, texts: Texts) -> Decision:
    """Makes a suggested decision as the person: the stored resolution and evidence go through resolve again, which
    checks them against the rows as they are now. Returns the new decision, which is refused if they no longer hold.
    InvalidInput when the decision isn't the client's suggestion, or was applied already."""
    d = _decision(records, client_id, decision_id)
    if d is None or d.status != "suggested" or d.resolution is None:
        raise InvalidInput("This is not a suggestion.")
    if decision_id in records.applied_suggestions(client_id):
        raise InvalidInput("This suggestion was applied already.")
    resolution = TypeAdapter(Resolution).validate_python(d.resolution)
    with locks.hold(client_id):
        made = resolutions.resolve(
            store, records, client_id=client_id, thread_id=d.thread_id, actor="user", row_id=d.row_ids[0],
            resolution=resolution, evidence=d.evidence, reason=d.reason, texts=texts, trigger="background",
            review_id=d.review_id, suggest=False)
    if made.status == "applied":
        upkeep.after_change(store, records, locks, ChangeDraft(
            client_id=client_id, year_start=made.year_start, actor="user", kind="decision_applied",
            row_ids=made.row_ids, after={"decision_id": decision_id, "applied_as": made.id},
            summary=f"Applied the suggestion: {made.rule} (rows {', '.join(map(str, made.row_ids))})"))
    return made
