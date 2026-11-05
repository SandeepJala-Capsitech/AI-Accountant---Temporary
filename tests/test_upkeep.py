"""Settling a client's books after any change: the rules' questions, the document rules and the posting are brought
up to date for every open year, under the client's lock."""
import logging
from contextlib import contextmanager
from datetime import date

import pytest
from helpers import acme_two_years, acme_with_a_non_gbp_row, acme_with_year_one, add_rows  # noqa: F401
from test_questions import trainline_invoice, travel_line

from ledgersync import books, ledger_book, upkeep
from ledgersync.agent.locks import ClientLocks
from ledgersync.models import ChangeDraft
from ledgersync.records import AgentRecords

pytestmark = pytest.mark.mongo

YEAR_1 = date(2025, 4, 1)
YEAR_2 = date(2026, 4, 1)
TODAY = date(2026, 11, 4)


@pytest.fixture
def store(mongo_store):
    return mongo_store


@pytest.fixture
def records(mongo_db):
    return AgentRecords(mongo_db)


@pytest.fixture
def locks():
    return ClientLocks()


def change(client_id, **kw):
    return ChangeDraft(**{**dict(client_id=client_id, actor="user", kind="upload_read", summary="Read a file"), **kw})


def batches(records, client_id, year_start):
    return records.db.posting_batches.count_documents(
        {"client_id": client_id, "year_start": year_start.isoformat()})


def test_a_document_added_later_closes_its_question_and_clears_suspense(store, records, locks, acme):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    upkeep.after_change(store, records, locks, change(acme), today=TODAY)
    [q] = [x for x in records.questions(acme, YEAR_1) if x.status == "open"]
    assert q.kind == "receipt_or_invoice"
    assert ledger_book.posted_trial_balance(store, records, acme, YEAR_1).suspense[0].gross == 240

    add_rows(store, acme, trainline_invoice(), year_start=YEAR_1)
    upkeep.after_change(store, records, locks, change(acme), today=TODAY)
    done = records.question(q.id)
    assert (done.status, done.answer) == ("resolved", "Matched to rows.csv")
    posted = ledger_book.posted_trial_balance(store, records, acme, YEAR_1)
    assert posted.suspense == []
    assert ledger_book.unposted_changes(store, records, acme, YEAR_1) == 0
    assert books.trial_balance(store, acme, YEAR_1).lines == posted.lines


def test_after_change_records_the_change_before_settling(store, records, locks, acme_with_year_one, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(upkeep, "settle", boom)
    upkeep.after_change(store, records, locks, change(acme_with_year_one), today=TODAY)
    assert len(records.changes(acme_with_year_one)) == 1


def test_after_change_settles_every_open_year(store, records, locks, acme_two_years):
    out = upkeep.after_change(store, records, locks, change(acme_two_years), today=TODAY)
    assert set(out) == {YEAR_1, YEAR_2}
    assert batches(records, acme_two_years, YEAR_1) == 1 and batches(records, acme_two_years, YEAR_2) == 1


def test_after_change_skips_closed_years(store, records, locks, acme_two_years):
    upkeep.after_change(store, records, locks, change(acme_two_years), today=TODAY)
    books.close(store, acme_two_years, YEAR_1)
    before = batches(records, acme_two_years, YEAR_1)
    out = upkeep.after_change(store, records, locks, change(acme_two_years, year_start=YEAR_2), today=TODAY)
    assert YEAR_1 not in out
    assert batches(records, acme_two_years, YEAR_1) == before


def test_a_failing_settle_is_logged_not_raised(store, records, locks, acme_with_year_one, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(upkeep, "settle", boom)
    with caplog.at_level(logging.ERROR, logger="ledgersync.upkeep"):
        assert upkeep.after_change(store, records, locks, change(acme_with_year_one), today=TODAY) == {}
    assert any(r.name == "ledgersync.upkeep" and r.exc_info for r in caplog.records)


def test_settle_with_a_blocked_year_returns_problems(store, records, locks, acme_with_a_non_gbp_row):
    out = upkeep.after_change(store, records, locks, change(acme_with_a_non_gbp_row), today=TODAY)
    result = out[YEAR_1]
    assert result.problems and result.batch_id is None


def test_settle_runs_inside_the_clients_lock(store, records, acme_with_year_one, monkeypatch):
    seen = []

    class Spy:
        @contextmanager
        def hold(self, client_id):
            seen.append((client_id, "enter"))
            yield
            seen.append((client_id, "exit"))

    def fake(*a, **k):
        seen.append("settle")
        return None
    monkeypatch.setattr(upkeep, "settle", fake)
    upkeep.after_change(store, records, Spy(), change(acme_with_year_one), today=TODAY)
    c = acme_with_year_one
    assert seen == [(c, "enter"), "settle", (c, "exit")] * 2   # a year at a time (this one and the next)


def test_settling_again_with_nothing_new_writes_no_question(store, records, locks, acme, monkeypatch):
    add_rows(store, acme, travel_line(), year_start=YEAR_1)
    upkeep.after_change(store, records, locks, change(acme), today=TODAY)
    writes = []
    for name in ("upsert_question", "set_question"):
        original = getattr(records, name)
        monkeypatch.setattr(records, name, lambda *a, _o=original, _n=name, **k: writes.append(_n) or _o(*a, **k))

    upkeep.after_change(store, records, locks, change(acme), today=TODAY)

    assert writes == []
    assert [q.kind for q in records.questions(acme, YEAR_1)] == ["receipt_or_invoice"]


def test_a_year_closed_while_waiting_for_the_lock_is_not_settled(store, records, locks, acme_with_year_one, monkeypatch):
    upkeep.after_change(store, records, locks, change(acme_with_year_one), today=TODAY)
    books.close(store, acme_with_year_one, YEAR_1)
    seen = []
    monkeypatch.setattr(upkeep, "_open_years", lambda *a, **k: [YEAR_1])   # a list taken before the close
    monkeypatch.setattr(upkeep, "settle", lambda *a, **k: seen.append(a))
    assert upkeep.after_change(store, records, locks, change(acme_with_year_one), today=TODAY) == {}
    assert seen == []


def test_posting_to_a_closed_year_writes_nothing(store, records, acme_with_year_one):
    books.close(store, acme_with_year_one, YEAR_1)
    result = ledger_book.post(store, records, acme_with_year_one, YEAR_1)
    assert result.batch_id is None and result.problems == ["That year is closed."]
    assert batches(records, acme_with_year_one, YEAR_1) == 0


def test_a_change_makes_the_client_due_for_a_review_after_the_wait(store, records, locks, acme):
    import datetime as dt
    at = dt.datetime(2026, 11, 4, 10, 0, tzinfo=dt.timezone.utc)
    upkeep.after_change(store, records, locks, change(acme, in_chat=True), today=TODAY, now=at)
    assert records.review_state(acme).due_at is None   # a chat's own change needs no review
    upkeep.after_change(store, records, locks, change(acme), today=TODAY, now=at, wait=45)
    assert records.review_state(acme).due_at == "2026-11-04T10:00:45+00:00"
