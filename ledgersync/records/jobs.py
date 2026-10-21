"""Agent jobs and their activity feed, kept in MongoDB so they survive a restart and the page can poll them.

A job is one run of the agent on a client's rows for one period. While it works it writes short
plain-English events ("Sorted 40 rows into accounts.") that the page shows as the activity feed.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Literal, Optional
from uuid import uuid4

import pymongo
from pydantic import BaseModel, TypeAdapter, ValidationError
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import OperationFailure

from ledgersync.errors import InvalidInput, NotFound

logger = logging.getLogger(__name__)

JobStatus = Literal["preparing", "awaiting_review", "failed", "cancelled", "superseded"]
JobStage = Literal["ingest", "classify", "gate", "fix", "requests", "snapshot", "done"]


class JobRecord(BaseModel):
    """One job as callers see it. The period is a pair of dates; the two times are ISO-8601 UTC strings."""

    id: str                          # 32 hex characters
    client_id: int
    period_start: dt.date
    period_end: dt.date
    status: JobStatus
    stage: JobStage
    fix_rounds: int = 0
    gate: Optional[dict] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    new_documents: bool = False
    supersedes: Optional[str] = None  # the id of the job this one replaced
    created_at: str
    updated_at: str


class JobEvent(BaseModel):
    """One line of a job's activity feed. `seq` counts from 1 within the job."""

    job_id: str
    seq: int
    at: str
    stage: str
    message: str


# The fields `update` may change: where the job has got to. Who it is for, its period, what it
# replaced and its times are fixed when it is created (`updated_at` is set by `update` itself).
_UPDATABLE = ("status", "stage", "fix_rounds", "gate", "error", "cancel_requested", "new_documents")
_FIELD_TYPES = {name: TypeAdapter(JobRecord.model_fields[name].annotation) for name in _UPDATABLE}

# Databases whose indexes were already set up (or refused) in this process, by name. The database
# user the app runs as may not be allowed to create indexes, so they are asked for once per
# process, not on every JobRecords, which is built for every request. Filled under _INDEX_LOCK.
_INDEXED_DATABASES: set[str] = set()
_INDEX_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _job(doc: dict) -> JobRecord:
    """A stored job as a record: MongoDB's `_id` is the job's id, and the event counter stays inside."""
    return JobRecord.model_validate({**doc, "id": doc["_id"]})


def _not_found() -> NotFound:
    return NotFound("This job was not found.")


class JobRecords:
    """Creates, changes and finds jobs, and keeps each job's activity feed."""

    def __init__(self, db: Database):
        self._jobs = db["jobs"]
        self._events = db["job_events"]
        with _INDEX_LOCK:
            if db.name not in _INDEXED_DATABASES:
                for collection, keys, unique in (
                        (self._events, [("job_id", pymongo.ASCENDING), ("seq", pymongo.ASCENDING)], True),
                        (self._jobs, [("client_id", pymongo.ASCENDING), ("status", pymongo.ASCENDING)], False)):
                    try:
                        collection.create_index(keys, unique=unique)
                    except OperationFailure as exc:
                        logger.error(
                            "Could not create the %s index on %s (%s). The database user needs the "
                            "createIndex privilege on %s; until someone creates the index, %s.",
                            "unique (job_id, seq)" if unique else "(client_id, status)", collection.name, exc,
                            collection.name,
                            "nothing in the database stops two events of a job sharing a seq" if unique
                            else "finding a client's jobs reads every job")
                _INDEXED_DATABASES.add(db.name)   # not reached for any other error, so that is tried again

    def create(self, client_id: int, period_start: dt.date, period_end: dt.date, *,
               supersedes: Optional[str] = None) -> JobRecord:
        """Starts a job record for the client's period: `preparing`, at the `ingest` stage."""
        if period_end < period_start:
            raise InvalidInput("The period must end after it starts.")
        now = _now()
        job = JobRecord(id=uuid4().hex, client_id=client_id, period_start=period_start, period_end=period_end,
                        status="preparing", stage="ingest", supersedes=supersedes, created_at=now, updated_at=now)
        document = job.model_dump(mode="json")   # the dates as ISO strings
        document["_id"] = document.pop("id")
        document["event_seq"] = 0                # the last seq given out in this job's feed
        self._jobs.insert_one(document)
        return job

    def get(self, job_id: str) -> JobRecord:
        doc = self._jobs.find_one({"_id": job_id})
        if doc is None:
            raise _not_found()
        return _job(doc)

    def update(self, job_id: str, **changes) -> JobRecord:
        """Changes the named fields (status, stage, fix_rounds, gate, error, cancel_requested,
        new_documents), sets `updated_at` and returns the job as it now is.

        Anything else, or a value the field can't hold, is refused and nothing is saved. A `gate`
        is saved as JSON, so a `Decimal` in it becomes an exact decimal string such as "12.50"."""
        unknown = sorted(set(changes) - set(_UPDATABLE))
        if unknown:
            raise InvalidInput(f"These job fields cannot be changed: {', '.join(unknown)}.")
        fields = {}
        for name, value in changes.items():
            try:
                checked = _FIELD_TYPES[name].validate_python(value, strict=True)
            except ValidationError:
                raise InvalidInput(f"The job's {name} cannot be set to {value!r}.") from None
            fields[name] = _FIELD_TYPES[name].dump_python(checked, mode="json")
        fields["updated_at"] = _now()
        doc = self._jobs.find_one_and_update({"_id": job_id}, {"$set": fields}, return_document=ReturnDocument.AFTER)
        if doc is None:
            raise _not_found()
        return _job(doc)

    def event(self, job_id: str, stage: str, message: str) -> JobEvent:
        """Adds a line to the job's activity feed. Its `seq` is the next number for this job: taken
        in one step on the job document, so two writers at once never get the same one."""
        counter = self._jobs.find_one_and_update(
            {"_id": job_id}, {"$inc": {"event_seq": 1}}, projection={"event_seq": 1},
            return_document=ReturnDocument.AFTER)
        if counter is None:
            raise _not_found()
        event = JobEvent(job_id=job_id, seq=counter["event_seq"], at=_now(), stage=stage, message=message)
        self._events.insert_one(event.model_dump())
        return event

    def events(self, job_id: str, after: int = 0) -> list[JobEvent]:
        """The job's events with a `seq` above `after`, oldest first. A page that remembers the last
        `seq` it showed passes it here to get only what is new."""
        docs = self._events.find({"job_id": job_id, "seq": {"$gt": after}}).sort("seq", pymongo.ASCENDING)
        return [JobEvent.model_validate(doc) for doc in docs]

    def active_for(self, client_id: int) -> Optional[JobRecord]:
        """The client's `preparing` job, or None. (There should be at most one; with more, the newest.)"""
        doc = self._jobs.find_one({"client_id": client_id, "status": "preparing"}, sort=[("created_at", pymongo.DESCENDING)])
        return _job(doc) if doc else None

    def latest_for(self, client_id: int) -> Optional[JobRecord]:
        """The client's most recently created job, whatever its status, or None."""
        doc = self._jobs.find_one({"client_id": client_id}, sort=[("created_at", pymongo.DESCENDING)])
        return _job(doc) if doc else None

    def unfinished(self) -> list[JobRecord]:
        """Every `preparing` job of every client, oldest first: the ones to look at after a restart."""
        docs = self._jobs.find({"status": "preparing"}).sort("created_at", pymongo.ASCENDING)
        return [_job(doc) for doc in docs]
