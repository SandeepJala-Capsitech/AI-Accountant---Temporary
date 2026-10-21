import datetime as dt
import logging
import re
import threading
from decimal import Decimal
from unittest import mock

import pytest
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, OperationFailure

from ledgersync.errors import InvalidInput, NotFound
from ledgersync.records.jobs import JobEvent, JobRecord, JobRecords

pytestmark = pytest.mark.mongo

YEAR = (dt.date(2025, 4, 1), dt.date(2026, 3, 31))
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


def test_a_new_job_is_preparing_at_ingest(mongo_db):
    job = JobRecords(mongo_db).create(1, *YEAR)
    assert (job.status, job.stage, job.fix_rounds) == ("preparing", "ingest", 0)


def test_events_come_back_after_a_given_seq(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    for message in ("a", "b", "c"):
        records.event(job.id, "ingest", message)
    assert [e.message for e in records.events(job.id, after=1)] == ["b", "c"]


def test_active_for_finds_only_a_preparing_job(mongo_db):
    records = JobRecords(mongo_db)
    first = records.create(1, *YEAR)
    records.update(first.id, status="awaiting_review")
    second = records.create(1, *YEAR)
    assert records.active_for(1).id == second.id
    records.update(second.id, status="failed")
    assert records.active_for(1) is None


def test_an_unknown_job_is_not_found(mongo_db):
    with pytest.raises(NotFound, match="This job was not found."):
        JobRecords(mongo_db).get("0" * 32)


def test_a_period_that_ends_before_it_starts_is_refused(mongo_db):
    with pytest.raises(InvalidInput, match="The period must end after it starts."):
        JobRecords(mongo_db).create(1, YEAR[1], YEAR[0])
    assert mongo_db.jobs.count_documents({}) == 0


def test_a_one_day_period_is_allowed(mongo_db):
    day = dt.date(2026, 3, 31)
    job = JobRecords(mongo_db).create(1, day, day)
    assert job.period_start == job.period_end == day


# --- the record itself ---------------------------------------------------------------------------

def test_a_new_job_has_the_documented_fields_and_no_mongo_id(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(7, *YEAR)
    assert isinstance(job, JobRecord)
    assert re.fullmatch(r"[0-9a-f]{32}", job.id)
    assert (job.client_id, job.period_start, job.period_end) == (7, YEAR[0], YEAR[1])
    assert (job.gate, job.error, job.cancel_requested, job.new_documents, job.supersedes) == (None, None, False, False, None)
    assert ISO_UTC.match(job.created_at) and job.updated_at == job.created_at
    assert set(job.model_dump()) == set(JobRecord.model_fields)
    assert records.get(job.id) == job


def test_two_jobs_get_different_ids(mongo_db):
    records = JobRecords(mongo_db)
    assert records.create(1, *YEAR).id != records.create(1, *YEAR).id


def test_a_job_that_supersedes_another_remembers_which(mongo_db):
    records = JobRecords(mongo_db)
    old = records.create(1, *YEAR)
    new = records.create(1, *YEAR, supersedes=old.id)
    assert records.get(new.id).supersedes == old.id
    assert records.get(old.id).supersedes is None


def test_the_period_is_saved_as_iso_strings_and_comes_back_as_dates(mongo_db):
    job = JobRecords(mongo_db).create(1, *YEAR)
    raw = mongo_db.jobs.find_one({})
    assert (raw["period_start"], raw["period_end"]) == ("2025-04-01", "2026-03-31")
    assert JobRecords(mongo_db).get(job.id).period_start == YEAR[0]


# --- update --------------------------------------------------------------------------------------

def test_update_changes_the_fields_given_and_returns_the_new_record(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    updated = records.update(job.id, stage="classify", fix_rounds=2, new_documents=True, cancel_requested=True,
                             error="x", gate={"passed": False, "problems": ["a"]})
    assert (updated.stage, updated.fix_rounds, updated.new_documents, updated.cancel_requested) == ("classify", 2, True, True)
    assert updated.error == "x" and updated.gate == {"passed": False, "problems": ["a"]}
    assert updated.status == "preparing"   # untouched
    assert records.get(job.id) == updated


def test_update_moves_updated_at_forward_and_leaves_created_at(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    updated = records.update(job.id, stage="gate")
    assert ISO_UTC.match(updated.updated_at)
    assert updated.updated_at > job.updated_at and updated.created_at == job.created_at


def test_update_can_clear_a_field_to_none(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    records.update(job.id, error="boom", gate={"passed": True})
    cleared = records.update(job.id, error=None, gate=None)
    assert cleared.error is None and cleared.gate is None


def test_update_refuses_an_unknown_field_and_saves_nothing(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    with pytest.raises(InvalidInput, match="colour"):
        records.update(job.id, stage="gate", colour="red")
    assert records.get(job.id) == job
    assert "colour" not in mongo_db.jobs.find_one({})


@pytest.mark.parametrize("field", ["id", "_id", "client_id", "period_start", "period_end", "supersedes",
                                   "created_at", "updated_at", "event_seq"])
def test_update_refuses_the_fields_that_are_not_the_jobs_to_change(mongo_db, field):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    with pytest.raises(InvalidInput):
        records.update(job.id, **{field: "x"})
    assert records.get(job.id) == job


@pytest.mark.parametrize("changes", [{"status": "done"}, {"stage": "finished"}, {"fix_rounds": "many"},
                                     {"cancel_requested": "yes"}, {"gate": "passed"}])
def test_update_refuses_a_value_the_field_cannot_hold(mongo_db, changes):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    with pytest.raises(InvalidInput):
        records.update(job.id, **changes)
    assert records.get(job.id) == job


def test_update_of_an_unknown_job_is_not_found_and_creates_nothing(mongo_db):
    with pytest.raises(NotFound, match="This job was not found."):
        JobRecords(mongo_db).update("0" * 32, stage="gate")
    assert mongo_db.jobs.count_documents({}) == 0


def test_a_gate_holding_money_is_saved_as_an_exact_decimal_string(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    updated = records.update(job.id, gate={"difference": Decimal("12.50")})
    assert updated.gate == {"difference": "12.50"}
    assert mongo_db.jobs.find_one({})["gate"] == {"difference": "12.50"}


# --- the activity feed ---------------------------------------------------------------------------

def test_an_event_has_the_documented_fields_and_no_mongo_id(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    saved = records.event(job.id, "classify", "Sorted 40 rows into accounts.")
    assert isinstance(saved, JobEvent)
    assert (saved.job_id, saved.seq, saved.stage, saved.message) == (job.id, 1, "classify", "Sorted 40 rows into accounts.")
    assert ISO_UTC.match(saved.at)
    assert set(saved.model_dump()) == {"job_id", "seq", "at", "stage", "message"}
    assert records.events(job.id) == [saved]


def test_seqs_start_at_one_for_every_job(mongo_db):
    records = JobRecords(mongo_db)
    first, second = records.create(1, *YEAR), records.create(2, *YEAR)
    assert [records.event(first.id, "ingest", "a").seq, records.event(first.id, "ingest", "b").seq] == [1, 2]
    assert records.event(second.id, "ingest", "c").seq == 1
    assert [e.message for e in records.events(second.id)] == ["c"]


def test_events_without_after_come_back_in_seq_order_and_only_for_that_job(mongo_db):
    records = JobRecords(mongo_db)
    one, other = records.create(1, *YEAR), records.create(1, *YEAR)
    for n in range(5):
        records.event(one.id, "ingest", f"one-{n}")
        records.event(other.id, "ingest", f"other-{n}")
    events = records.events(one.id)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert [e.message for e in events] == [f"one-{n}" for n in range(5)]
    assert records.events(one.id, after=5) == []
    assert records.events("0" * 32) == []


def test_an_event_for_an_unknown_job_is_refused_and_saved_nowhere(mongo_db):
    with pytest.raises(NotFound, match="This job was not found."):
        JobRecords(mongo_db).event("0" * 32, "ingest", "hello")
    assert mongo_db.job_events.count_documents({}) == 0


def test_the_job_record_does_not_show_its_event_counter(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    records.event(job.id, "ingest", "a")
    assert "event_seq" not in records.get(job.id).model_dump()


def test_events_from_parallel_writers_never_share_a_seq(mongo_db):
    job = JobRecords(mongo_db).create(1, *YEAR)
    errors = []

    def work(worker):
        try:
            records = JobRecords(mongo_db)
            for n in range(5):
                records.event(job.id, "ingest", f"{worker}-{n}")
        except Exception as exc:   # surfaced below, not lost in the thread
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [e.seq for e in JobRecords(mongo_db).events(job.id)] == list(range(1, 21))


# --- finding jobs --------------------------------------------------------------------------------

def test_active_for_is_none_when_the_client_has_no_job(mongo_db):
    records = JobRecords(mongo_db)
    records.create(2, *YEAR)
    assert records.active_for(1) is None and records.latest_for(1) is None


def test_active_for_ignores_other_clients(mongo_db):
    records = JobRecords(mongo_db)
    records.create(2, *YEAR)
    mine = records.create(1, *YEAR)
    assert records.active_for(1).id == mine.id


def test_active_for_returns_the_newest_when_several_are_preparing(mongo_db):
    records = JobRecords(mongo_db)
    records.create(1, *YEAR)
    newest = records.create(1, *YEAR)
    assert records.active_for(1).id == newest.id


def test_latest_for_is_the_newest_job_whatever_its_status(mongo_db):
    records = JobRecords(mongo_db)
    older = records.create(1, *YEAR)
    newer = records.create(1, *YEAR)
    records.update(newer.id, status="cancelled")
    records.update(older.id, status="preparing")   # touching the older job must not make it "latest"
    assert records.latest_for(1).id == newer.id


def test_unfinished_is_every_preparing_job_of_every_client(mongo_db):
    records = JobRecords(mongo_db)
    a = records.create(1, *YEAR)
    b = records.create(2, *YEAR)
    for status in ("awaiting_review", "failed", "cancelled", "superseded"):
        records.update(records.create(3, *YEAR).id, status=status)
    assert {j.id for j in records.unfinished()} == {a.id, b.id}
    assert JobRecords(mongo_db).unfinished()[0].id == a.id   # oldest first


def test_unfinished_is_empty_with_no_jobs(mongo_db):
    assert JobRecords(mongo_db).unfinished() == []


# --- indexes -------------------------------------------------------------------------------------

def test_the_indexes_are_created_and_event_seqs_are_unique_per_job(mongo_db):
    records = JobRecords(mongo_db)
    job = records.create(1, *YEAR)
    records.event(job.id, "ingest", "a")
    event_indexes = mongo_db.job_events.index_information()
    assert any(info.get("unique") and info["key"] == [("job_id", 1), ("seq", 1)] for info in event_indexes.values())
    assert any(info["key"] == [("client_id", 1), ("status", 1)] for info in mongo_db.jobs.index_information().values())
    with pytest.raises(DuplicateKeyError):
        mongo_db.job_events.insert_one({"job_id": job.id, "seq": 1, "at": "x", "stage": "ingest", "message": "dup"})


def test_a_second_records_object_on_the_same_database_makes_no_index_call(mongo_db):
    JobRecords(mongo_db)   # the first one creates the indexes
    with mock.patch.object(Collection, "create_index") as create_index:
        records = JobRecords(mongo_db)
        records.event(records.create(1, *YEAR).id, "ingest", "a")
    create_index.assert_not_called()


def test_a_refused_index_is_logged_and_does_not_stop_the_records(mongo_db, caplog):
    refused = OperationFailure("not authorized on db to execute command createIndexes", code=13)
    with caplog.at_level(logging.ERROR, logger="ledgersync.records.jobs"):
        with mock.patch.object(Collection, "create_index", side_effect=refused) as create_index:
            records = JobRecords(mongo_db)    # must not raise
            JobRecords(mongo_db)              # already tried for this database, so no more calls or messages
    assert create_index.call_count == 2       # one per index, both tried once
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(messages) == 2
    assert any("job_events" in m for m in messages) and any("jobs" in m for m in messages)
    assert all("createIndex" in m for m in messages)
    job = records.create(1, *YEAR)
    assert records.event(job.id, "ingest", "a").seq == 1


def test_any_other_index_error_still_raises_and_is_tried_again_next_time(mongo_db):
    with mock.patch.object(Collection, "create_index", side_effect=RuntimeError("network down")):
        with pytest.raises(RuntimeError, match="network down"):
            JobRecords(mongo_db)
    JobRecords(mongo_db)   # not remembered as done, so this one creates the indexes for real
    assert "job_id_1_seq_1" in mongo_db.job_events.index_information()
    assert "client_id_1_status_1" in mongo_db.jobs.index_information()
