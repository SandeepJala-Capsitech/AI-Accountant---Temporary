"""The background AI review: once a client has been quiet for a while after a person changed its books, the agent
reviews what changed on its own. A change that needs a review makes the client due (note); the Worker runs the due
clients one at a time, and run() does one client's review: per open year, the agent is pointed at what changed and at
the issues no earlier review has looked at, the books are settled, and the run is recorded with what it decided and
asked. The watermark is the change `seq`; a change that arrives during a run leaves the client due, so one more run
follows."""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import TYPE_CHECKING, Callable, Optional

from . import books, upkeep
from .agent.run import stream_turn
from .agent.tools import ChatContext, _to_look_at
from .errors import NotFound
from .models import Change, ChangeDraft, ReviewRun
from .records import AgentRecords

if TYPE_CHECKING:   # chat.py imports upkeep, which imports this module
    from .agent.chat import Chat
    from .config import Settings

logger = logging.getLogger(__name__)

RETRY_AFTER = dt.timedelta(minutes=10)
TICK_SECONDS = 2.0
LIMITED_NOTE = "AI review paused for today: limit reached"
NOTHING = "Nothing to change."
MAX_UNREVIEWED = 100   # rows a run's message names as not yet reviewed
_DETAILS = ("vat_registered", "business_type", "first_year_end", "bank_accounts")
_NO_REVIEW = ("year_closed", "decision_applied", "question_closed")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(when: dt.datetime) -> str:
    return when.isoformat(timespec="seconds")


def _when(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def triggers(change: ChangeDraft) -> bool:
    """Whether the change needs a review. Not the agent's own changes, those a chat turn made (it says so itself),
    the rules' own, closing a year or a question, applying a decision, a row's description alone, or a client's
    name, contact, folder or review setting."""
    if change.actor.startswith("agent:") or change.actor == "rules" or change.in_chat:
        return False
    if change.kind in _NO_REVIEW:
        return False
    if change.kind == "row_edited" and all(k == "description" for k in change.after):
        return False
    if change.kind == "client_changed" and not any(k in _DETAILS for k in change.after):
        return False
    return True


def note(records: AgentRecords, change: ChangeDraft, *, now: dt.datetime, wait: int) -> None:
    """When the change needs a review, makes its client due `wait` seconds from now (a retry already waiting stays)."""
    if triggers(change):
        records.patch_review_state(change.client_id, due_at=_iso(now + dt.timedelta(seconds=wait)))


# ─── What the agent is told ────────────────────────────────────────────────────

_ROW_KINDS = ("row_edited", "row_added", "row_removed", "row_linked", "row_included", "rows_apart", "row_reverted")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def trigger_summary(changes: list[Change]) -> str:
    """"after 3 files were added and 2 rows were changed": one phrase for each group of kinds."""
    def count(kinds) -> list[Change]:
        return [c for c in changes if c.kind in kinds]

    phrases = []
    if found := count(("upload_read",)):
        phrases.append(f"{_plural(len(found), 'file was', 'files were')} added")
    if found := count(("upload_removed",)):
        phrases.append(f"{_plural(len(found), 'file was', 'files were')} removed")
    if found := count(("upload_bank_changed",)):
        phrases.append(f"the bank account of {_plural(len(found), 'file', 'files')} changed")
    if found := count(_ROW_KINDS):
        n = len({r for c in found for r in c.row_ids}) or len(found)
        phrases.append(f"{_plural(n, 'row was', 'rows were')} changed")
    if count(("client_changed",)):
        phrases.append("the client's details changed")
    if count(("opening_balances_changed",)):
        phrases.append("the opening balances changed")
    if found := count(("question_answered",)):
        phrases.append(f"{_plural(len(found), 'question was', 'questions were')} answered")
    if found := count(("decision_undone",)):
        phrases.append(f"{_plural(len(found), 'change was', 'changes were')} undone")
    if count(("year_reopened",)):
        phrases.append("the year was reopened")
    if found := count(("decision",)):
        phrases.append(f"{_plural(len(found), 'decision was', 'decisions were')} made")
    return "after " + " and ".join(phrases) if phrases else "after a change"


def _message(changes: list[Change], unreviewed: list[int], first: bool, summary: str) -> str:
    lines = [f"Background review {summary}. These changes were made by people or imports since the last review:"]
    for c in changes:
        lines.append(f"- {c.summary}" + (f" (rows {', '.join(map(str, c.row_ids))})" if c.row_ids else ""))
    if unreviewed:
        lines.append("Not yet reviewed: rows " + ", ".join(map(str, unreviewed)))
    if first:
        lines.append("First review of this year: look at everything.")
    lines.append("Resolve what the evidence supports, ask the person about what it doesn't, and finish with one line "
                 "saying what you did.")
    return "\n".join(lines)


# ─── One client's review ───────────────────────────────────────────────────────

def _noted_today(records: AgentRecords, client_id: int, status: str, day: dt.date) -> bool:
    return any(r.status == status and r.started_at.startswith(day.isoformat())
               for r in records.reviews(client_id, limit=50))


def _midnight_after(now: dt.datetime) -> dt.datetime:
    return dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(), tzinfo=dt.timezone.utc)


def _open_years(chat: "Chat", client_id: int, today: dt.date) -> list[dt.date]:
    return [y.start for y in books.years_of(chat.store, client_id, today) if not y.closed]


def _quiet_run(records: AgentRecords, client_id: int, year: dt.date, seq: int, status: str, summary: str) -> ReviewRun:
    run = records.start_review(client_id, year, from_seq=seq, to_seq=seq, trigger_summary="")
    return records.finish_review(run.id, status=status, summary=summary)


def run(chat: "Chat", client_id: int, *, now: dt.datetime) -> list[ReviewRun]:
    """Reviews the client: one run for each open year with changes that need one since the last review. Returns the
    runs recorded. See the module's note for what happens to the client's mark, due time and retry."""
    records, store, settings = chat.records, chat.store, chat.settings
    state = records.review_state(client_id)
    new = records.changes_after(client_id, state.last_seq)
    to_seq = max((c.seq for c in new), default=state.last_seq)
    today = now.date()
    years = _open_years(chat, client_id, today)
    mode = store.get_client(client_id).ai_review
    runs: list[ReviewRun] = []

    if mode == "off" or records.runs_on(client_id, today) >= settings.review_daily_limit:
        quiet, summary = ("off", "") if mode == "off" else ("limited", LIMITED_NOTE)
        if years and not _noted_today(records, client_id, quiet, today):
            runs.append(_quiet_run(records, client_id, years[-1], to_seq, quiet, summary))
        if mode == "off":
            records.patch_review_state(client_id, last_seq=to_seq, due_at=None, running=False, retry_at=None)
        else:   # the changes wait for the next day
            records.patch_review_state(client_id, running=False, retry_at=_iso(_midnight_after(now)))
        return runs

    needing = [c for c in new if triggers(c)]
    failed = False
    for year in years:
        changes = [c for c in needing if c.year_start in (year, None)]
        if not changes:
            continue
        runs.append(_review_year(chat, client_id, year, changes, state.last_seq, to_seq, mode, now))
        if runs[-1].status == "failed":
            failed = True
            break

    if failed:
        records.patch_review_state(client_id, running=False, retry_at=_iso(now + RETRY_AFTER))
        return runs
    records.patch_review_state(client_id, last_seq=to_seq, running=False, retry_at=None)
    if not any(triggers(c) for c in records.changes_after(client_id, to_seq)):
        records.patch_review_state(client_id, due_at=None)
        # a change recorded between that look and the write above still leaves the client due
        if any(triggers(c) for c in records.changes_after(client_id, to_seq)):
            records.patch_review_state(client_id, due_at=_iso(now))
    return runs


def _review_year(chat: "Chat", client_id: int, year: dt.date, changes: list[Change], from_seq: int, to_seq: int,
                 mode: str, now: dt.datetime) -> ReviewRun:
    """One run over one year: start it, have the agent review, settle the books and finish the run, once."""
    records, store = chat.records, chat.store
    touched = sorted({r for c in changes for r in c.row_ids})
    first = not any(r.status in ("done", "failed") for r in records.reviews(client_id, year_start=year, limit=50))
    try:
        built = books.ledger(store, client_id, year)
        flagged = [row.id for row, _, _ in _to_look_at(built)]
    except Exception:
        logger.exception("Couldn't list the issues of client %s, year %s", client_id, year)
        flagged = []
    seen = records.looked_at(client_id, year)
    unreviewed = [n for n in flagged if n not in seen and n not in touched][:MAX_UNREVIEWED]
    summary = trigger_summary(changes)
    run = records.start_review(client_id, year, from_seq=from_seq, to_seq=to_seq, trigger_summary=summary,
                               looked_at=touched + unreviewed)
    error: Optional[str] = None
    said = ""
    try:
        ctx = ChatContext(client_id, f"review-{run.id}", year, trigger="background",
                          mode="suggest" if mode == "suggest" else "apply", review_id=run.id)
        for event in stream_turn(chat.agent, ctx, _message(changes, unreviewed, first, summary)):
            if event.type == "token":
                said += event.data.get("text", "")
            elif event.type == "error" and error is None:
                error = event.data.get("message") or event.data.get("code") or "The review failed."
    except Exception as exc:   # noqa: BLE001 - a run that raised is a failed run
        logger.exception("The background review of client %s failed", client_id)
        error = str(exc) or type(exc).__name__
    try:
        with chat.locks.hold(client_id):
            upkeep.settle_open_years(store, records, client_id, today=now.date())
    except Exception:
        logger.exception("Couldn't settle client %s after its review", client_id)

    decisions = [d.id for d in records.decisions(client_id, year) if d.review_id == run.id]
    asked = [q.id for q in records.questions(client_id, year, ("open", "waiting", "resolved"))
             if q.source == "agent" and q.created_at >= run.started_at]
    if error is not None:
        return records.finish_review(run.id, status="failed", decision_ids=decisions, question_ids=asked, error=error)
    lines = [ln.strip() for ln in said.splitlines() if ln.strip()]
    text = lines[-1] if lines and (decisions or asked) else NOTHING
    return records.finish_review(run.id, status="done", decision_ids=decisions, question_ids=asked, summary=text)


# ─── The worker ────────────────────────────────────────────────────────────────

class Worker:
    """Runs the due reviews, one client at a time, on a daemon thread that ticks every two seconds. busy(client_id):
    whether files are being read for the client, which a review waits for."""

    def __init__(self, chat: "Chat", settings: "Settings", *, busy: Callable[[int], bool],
                 clock: Callable[[], dt.datetime] = utcnow):
        self.chat, self.settings, self.busy, self.clock = chat, settings, busy, clock
        self._stop, self._thread = threading.Event(), None

    def recover(self) -> None:
        """After a restart: a review that was running is due at once."""
        records = self.chat.records
        for state in records.running_review_states():
            records.patch_review_state(state.client_id, running=False, due_at=_iso(self.clock()), retry_at=None)

    def tick(self, now: Optional[dt.datetime] = None) -> list[ReviewRun]:
        """Runs every client that is due: its time has come, it isn't running or waiting to retry, and no file is
        being read for it. Returns the runs."""
        now = now or self.clock()
        records, runs = self.chat.records, []
        for state in records.due_review_states():
            try:
                if state.running or _when(state.due_at) > now or self.busy(state.client_id):
                    continue
                if state.retry_at and _when(state.retry_at) > now:
                    continue
                try:
                    client = self.chat.store.get_client(state.client_id)
                except NotFound:
                    client = None
                if client is None or client.archived:   # gone or archived: there is nothing to review
                    records.patch_review_state(state.client_id, due_at=None, retry_at=None)
                    continue
                if not records.claim_review(state.client_id):   # another worker has it
                    continue
            except Exception:
                logger.exception("The review worker couldn't look at client %s", state.client_id)
                continue
            try:
                runs.extend(run(self.chat, state.client_id, now=now))
            except Exception:
                logger.exception("The background review of client %s couldn't run", state.client_id)
                records.patch_review_state(state.client_id, running=False, retry_at=_iso(now + RETRY_AFTER))
        return runs

    def start(self) -> None:
        self.recover()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="review-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                logger.exception("The review worker's tick failed")
