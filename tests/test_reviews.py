"""The background review: which changes ask for one, the wait after the last change, the worker's tick and the run
itself, on a scripted chat model (tests/agent_fakes.py), never a real one."""
import datetime as dt
from types import SimpleNamespace

import pytest
from agent_fakes import ScriptedChatModel, call, say
from langchain.agents.middleware import model_retry

from ledgersync import books, ledger_book, reviews, upkeep
from ledgersync.agent.chat import Chat
from ledgersync.agent.tools import _to_look_at
from ledgersync.blobs import GridFSBlobStore
from ledgersync.config import Settings
from ledgersync.models import ChangeDraft
from ledgersync.records import AgentRecords
from test_agent import YEAR, keep_the_receipt, scripted, STAPLES

pytestmark = pytest.mark.mongo

T = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)   # "now" of the tests, which are all within a day


def secs(n: float) -> dt.datetime:
    return T + dt.timedelta(seconds=n)


def draft(client_id: int, **fields) -> ChangeDraft:
    return ChangeDraft(**{**dict(client_id=client_id, year_start=YEAR, actor="user", kind="upload_read",
                                 summary="statement.csv read: 2 rows"), **fields})


@pytest.fixture
def records(mongo_store):
    return AgentRecords(mongo_store.db)


@pytest.fixture
def staples(mongo_store, records) -> int:
    blobs = GridFSBlobStore(mongo_store.db)
    client_id = scripted._client(STAPLES, mongo_store, records)
    for path in scripted._documents(STAPLES):
        scripted._add_document(mongo_store, blobs, client_id, YEAR, path)
    return client_id


@pytest.fixture
def make(mongo_store, monkeypatch):
    """A worker over a Chat whose model answers `replies`; `.chat`, `.model`, `.records`, `.busy` (a list: busy while
    it isn't empty) and `.change(client_id, at, **fields)`, which a person makes at that time."""
    monkeypatch.setattr(model_retry, "time", SimpleNamespace(sleep=lambda seconds: None))   # retry at once

    def _make(*replies, then=None, **settings):
        model = ScriptedChatModel(replies=list(replies), then=then)
        settings = Settings(warmup=False, **settings)
        chat = Chat(settings, mongo_store, GridFSBlobStore(mongo_store.db), read=None, model=model)
        busy: list = []
        worker = reviews.Worker(chat, settings, busy=lambda client_id: bool(busy))

        def change(client_id: int, at: dt.datetime, **fields):
            upkeep.after_change(mongo_store, chat.records, chat.locks, draft(client_id, **fields), now=at,
                                wait=settings.review_wait_seconds)

        return SimpleNamespace(chat=chat, model=model, records=chat.records, worker=worker, busy=busy,
                               change=change, settings=settings)

    return _make


def test_triggers_ignores_the_agent_chat_reads_closing_and_description_only_edits():
    def t(**fields):
        return reviews.triggers(draft(1, **fields))

    assert t() and t(kind="row_edited", after={"account": "7400"}, actor="import")
    assert not t(actor="agent:qwen/qwen3.8-27b") and not t(in_chat=True)
    assert not t(actor="rules", kind="decision")
    for kind in ("year_closed", "decision_applied", "question_closed"):
        assert not t(kind=kind)
    assert not t(kind="row_edited", after={"description": "Taxi"})
    assert t(kind="row_edited", after={"description": "Taxi", "account": "7400"})
    for field in ("name", "contact_name", "folder", "ai_review"):
        assert not t(kind="client_changed", after={field: "x"})
    for field in ("vat_registered", "business_type", "first_year_end", "bank_accounts"):
        assert t(kind="client_changed", after={field: "x"})


def test_a_batch_of_uploads_causes_one_run_after_the_last_is_read(make, staples):
    h = make(say("Looked: nothing to do."))
    for n in range(3):
        h.change(staples, secs(5 * n))
    h.busy.append("reading")
    assert h.worker.tick(secs(11)) == []   # not due, and busy
    assert h.worker.tick(secs(25)) == []   # the last file was read at 10: due at 30
    h.busy.clear()
    assert h.worker.tick(secs(29)) == []
    h.busy.append("reading")
    assert h.worker.tick(secs(31)) == [] and h.model.seen == []   # due, but a file is still being read
    h.busy.clear()
    [run] = h.worker.tick(secs(32))
    assert run.status == "done" and run.trigger_summary == "after 3 files were added"
    assert h.worker.tick(secs(60)) == []
    assert len(h.model.seen) == 1


def test_a_run_applies_what_the_evidence_supports_and_records_it_as_background(make, mongo_store, staples):
    args = keep_the_receipt(mongo_store, staples)
    h = make(call("resolve_issue", **args), say("Kept the receipt and dropped the sheet line."))
    h.change(staples, T)

    [run] = h.worker.tick(secs(21))

    [decision] = h.records.decisions(staples)
    assert (decision.status, decision.trigger, decision.review_id) == ("applied", "background", run.id)
    assert run.status == "done" and run.decision_ids == [decision.id] and run.year_start == YEAR
    assert run.summary == "Kept the receipt and dropped the sheet line."
    assert next(r for r in mongo_store.rows(staples) if r.id == args["resolution"]["drop_row_ids"][0]).tx.excluded
    assert ledger_book.unposted_changes(mongo_store, h.records, staples, YEAR) == 0   # the books were settled
    state = h.records.review_state(staples)
    assert (state.running, state.due_at, state.retry_at) == (False, None, None)
    assert state.last_seq == next(c.seq for c in h.records.changes(staples) if c.actor == "user")


def test_suggest_only_records_suggestions_and_changes_nothing(make, mongo_store, staples):
    mongo_store.update_client(staples, {"ai_review": "suggest"})
    args = keep_the_receipt(mongo_store, staples)
    h = make(call("resolve_issue", **args), say("Suggested keeping the receipt."))
    h.change(staples, T)

    [run] = h.worker.tick(secs(21))

    [decision] = h.records.decisions(staples)
    assert (decision.status, decision.trigger, decision.review_id) == ("suggested", "background", run.id)
    assert run.status == "done" and run.decision_ids == [decision.id]
    assert not any(r.tx.excluded for r in mongo_store.rows(staples))


def test_off_records_one_quiet_run_a_day_and_moves_the_mark(make, mongo_store, staples):
    mongo_store.update_client(staples, {"ai_review": "off"})
    h = make()
    h.change(staples, T)
    [run] = h.worker.tick(secs(21))
    assert (run.status, run.summary) == ("off", "")
    first = h.records.review_state(staples)
    assert first.last_seq == max(c.seq for c in h.records.changes(staples)) and first.due_at is None

    h.change(staples, secs(100))
    assert h.worker.tick(secs(121)) == []   # the same day: no second note
    assert len(h.records.reviews(staples)) == 1 and h.model.seen == []
    assert h.records.review_state(staples).last_seq > first.last_seq


def test_the_daily_limit_pauses_with_the_exact_note(make, staples):
    h = make(say("Done."), review_daily_limit=1)
    h.change(staples, T)
    [first] = h.worker.tick(secs(21))
    assert first.status == "done"

    h.change(staples, secs(100))
    [paused] = h.worker.tick(secs(121))
    assert (paused.status, paused.summary) == ("limited", "AI review paused for today: limit reached")
    state = h.records.review_state(staples)
    assert state.due_at is not None and state.retry_at is not None   # it waits for tomorrow

    h.change(staples, secs(200))
    assert h.worker.tick(secs(221)) == []
    assert [r.status for r in h.records.reviews(staples)] == ["limited", "done"]
    assert len(h.model.seen) == 1


def test_a_failed_run_is_recorded_and_retried_later(make, staples):
    outage = ConnectionError("OpenRouter hung up")
    h = make(outage, outage, outage, say("Looked again."))
    h.change(staples, T)

    [failed] = h.worker.tick(secs(21))

    assert failed.status == "failed" and "OpenRouter hung up" in failed.error
    state = h.records.review_state(staples)
    assert state.last_seq == 0 and state.due_at is not None and state.running is False
    assert dt.datetime.fromisoformat(state.retry_at) == secs(21) + dt.timedelta(minutes=10)
    assert h.worker.tick(secs(300)) == []
    [again] = h.worker.tick(secs(21 + 601))
    assert again.status == "done" and again.summary == "Nothing to change."
    state = h.records.review_state(staples)
    assert (state.due_at, state.retry_at) == (None, None) and state.last_seq > 0


def test_a_restart_keeps_a_due_review_and_retries_a_running_one(make, staples):
    h = make(say("Looked."))
    h.change(staples, T)
    h.records.patch_review_state(staples, running=True)   # the process died during its run

    restarted = reviews.Worker(h.chat, h.settings, busy=lambda client_id: False)
    restarted.recover()
    state = h.records.review_state(staples)
    assert state.running is False and state.due_at is not None

    assert len(restarted.tick(secs(3600))) == 1
    assert restarted.tick(secs(3700)) == []

    # a review that was only due keeps waiting for its time
    h.change(staples, secs(200))
    after = reviews.Worker(h.chat, h.settings, busy=lambda client_id: False)
    after.recover()
    assert after.tick(secs(205)) == []


def test_the_agents_own_changes_cause_no_further_run(make, mongo_store, staples):
    args = keep_the_receipt(mongo_store, staples)
    h = make(call("resolve_issue", **args), say("Kept the receipt."))
    h.change(staples, T)
    assert len(h.worker.tick(secs(21))) == 1
    assert any(c.actor.startswith("agent:") for c in h.records.changes(staples))   # the decision was recorded

    assert h.worker.tick(secs(100)) == []
    assert h.records.review_state(staples).due_at is None
    assert len(h.records.reviews(staples)) == 1


def test_a_change_during_a_run_causes_exactly_one_more_run(make, staples):
    def mid_run(messages):
        h.change(staples, secs(30), kind="row_edited", row_ids=[1], after={"account": "7400"})
        return say("First look done.")

    h = make(mid_run, say("Second look done."))
    h.change(staples, T)

    [first] = h.worker.tick(secs(21))

    assert first.status == "done"
    state = h.records.review_state(staples)
    assert state.due_at is not None and state.running is False and state.last_seq < max(
        c.seq for c in h.records.changes(staples))
    [second] = h.worker.tick(secs(60))
    assert second.trigger_summary == "after 1 row was changed"
    assert h.worker.tick(secs(100)) == []
    assert h.records.review_state(staples).due_at is None


def test_a_name_change_causes_no_run_and_a_vat_change_does(make, staples):
    h = make(say("Looked."))
    h.change(staples, T, kind="client_changed", after={"name": "Staples Ltd"}, year_start=None)
    assert h.records.review_state(staples).due_at is None
    assert h.worker.tick(secs(100)) == []

    h.change(staples, secs(100), kind="client_changed", after={"vat_registered": False}, year_start=None)
    [run] = h.worker.tick(secs(121))
    assert run.trigger_summary == "after the client's details changed"


def test_a_later_run_is_pointed_at_issues_no_run_has_looked_at(make, mongo_store, staples):
    flagged = [row.id for row, _, _ in _to_look_at(books.ledger(mongo_store, staples, YEAR))]
    assert flagged
    earlier = AgentRecords(mongo_store.db).start_review(staples, YEAR, from_seq=0, to_seq=1, trigger_summary="x")
    AgentRecords(mongo_store.db).finish_review(earlier.id, status="done", summary="Looked.")   # looked at no row
    h = make(say("Looked."), say("Looked again."))
    h.change(staples, T)

    [run] = h.worker.tick(secs(21))

    text = h.model.seen[0][-1].content
    assert f"Not yet reviewed: rows {', '.join(map(str, flagged))}" in text and "First review of this year" not in text
    assert run.looked_at == flagged and h.records.looked_at(staples, YEAR) == set(flagged)

    h.change(staples, secs(100), kind="row_edited", row_ids=[flagged[0]], after={"account": "7400"})
    [second] = h.worker.tick(secs(121))
    text = h.model.seen[1][-1].content
    assert "Not yet reviewed" not in text and f"(rows {flagged[0]})" in text
    assert second.looked_at == [flagged[0]]


def test_the_first_review_of_a_year_says_to_look_at_everything(make, staples):
    h = make(say("Looked."))
    h.change(staples, T)
    h.worker.tick(secs(21))
    assert "First review of this year: look at everything." in h.model.seen[0][-1].content


def test_a_run_with_nothing_to_do_says_so(make, staples):
    h = make(say("All in order."))
    h.change(staples, T)
    [run] = h.worker.tick(secs(21))
    assert (run.status, run.summary, run.decision_ids, run.question_ids) == ("done", "Nothing to change.", [], [])


def first_row(h, client_id: int) -> int:
    return h.chat.store.rows(client_id)[0].id


def test_a_run_lists_the_questions_it_asked(make, staples):
    h = make(lambda m: call("ask_user", row_ids=[first_row(h, staples)], question="Is it personal?", why="Unclear."),
             say("Asked about the row."))
    h.change(staples, T)
    [run] = h.worker.tick(secs(21))
    [asked] = [q for q in h.records.questions(staples, YEAR) if q.source == "agent"]
    assert run.question_ids == [asked.id] and run.summary == "Asked about the row."


def test_a_change_in_a_year_that_is_not_open_causes_no_run(make, staples):
    h = make(say("Looked."))
    h.change(staples, T, year_start=YEAR - dt.timedelta(days=365))
    assert h.worker.tick(secs(21)) == []
    assert h.records.review_state(staples).due_at is None


def test_two_workers_ticking_the_same_due_client_run_it_once(make, staples):
    from_other = []

    def while_running(messages):
        # the other worker, which read the client as due and idle before this run began, ticks now
        from_other.extend(other.tick(secs(21)))
        return say("Looked.")

    h = make(while_running, say("A second run."))
    other = reviews.Worker(h.chat, h.settings, busy=lambda client_id: False)
    h.change(staples, T)
    stale = h.records.due_review_states()
    h.records.due_review_states = lambda: stale

    first = h.worker.tick(secs(21))

    assert len(first) == 1 and from_other == [] and len(h.model.seen) == 1


def test_claiming_a_running_review_fails(records):
    records.patch_review_state(5, running=False)
    assert records.claim_review(5) is True
    assert records.claim_review(5) is False
    records.patch_review_state(5, running=False)
    assert records.claim_review(5) is True


def test_a_client_that_is_gone_or_archived_is_not_retried(make, mongo_store, staples):
    h = make(say("Looked."))
    h.change(staples, T)
    mongo_store.update_client(staples, {"archived": True})
    assert h.worker.tick(secs(21)) == []
    state = h.records.review_state(staples)
    assert (state.due_at, state.retry_at, state.running) == (None, None, False)

    h.records.patch_review_state(9999, due_at=reviews._iso(T))   # a client that does not exist
    assert h.worker.tick(secs(21)) == []
    assert h.records.review_state(9999).due_at is None and h.records.review_state(9999).retry_at is None


def test_one_bad_state_does_not_stop_the_tick(make, staples):
    h = make(say("Looked."))
    h.records.patch_review_state(4242, due_at="not a time")
    h.change(staples, T)
    assert len(h.worker.tick(secs(21))) == 1
