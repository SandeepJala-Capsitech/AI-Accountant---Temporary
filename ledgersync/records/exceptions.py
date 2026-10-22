"""What the agent couldn't settle, and the documents it asked the client for, kept in MongoDB beside the jobs.

An exception says what couldn't be settled, the assumption the agent made instead and how sure it is; the job
carries on. A document request is linked to an exception: it waits as `pending` until the run ends, when the
system emails the client one link for all of the run's requests (`sent`). An upload through that link claims it
(`receiving`, one upload at a time) while the file is read, and it is `received` once the file is saved. When a
later run emails a request for the same document, the older one still waiting is closed (`replaced`), so the
client has one live request for each document.

Both are keyed by what they say, so a step that runs again after a restart and logs the same exception, or asks
for the same document, gets the one already saved instead of a second.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import threading
from typing import Literal, Optional

import pymongo
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from ledgersync.errors import NotFound

logger = logging.getLogger(__name__)

ExceptionStatus = Literal["open", "resolved"]
RequestStatus = Literal["pending", "sent", "receiving", "received", "replaced"]

FIRST_REMINDER = dt.timedelta(days=3)     # after the email, while the document hasn't arrived
SECOND_REMINDER = dt.timedelta(days=7)    # and the last one
CLAIM_EXPIRES = dt.timedelta(minutes=15)  # an upload's claim this old is taken as stalled (say, the server stopped)

# Databases whose indexes were already set up (or refused) in this process, by name, as in records/jobs.py: the
# database user the app runs as may not be allowed to create indexes, so they are asked for once per process.
_INDEXED_DATABASES: set[str] = set()
_INDEX_LOCK = threading.Lock()


def _ensure_indexes(db: Database) -> None:
    with _INDEX_LOCK:
        if db.name in _INDEXED_DATABASES:
            return
        for collection, keys, without in (
                ("exceptions", [("job_id", pymongo.ASCENDING), ("created_at", pymongo.ASCENDING)],
                 "finding a job's exceptions reads every exception"),
                ("document_requests", [("job_id", pymongo.ASCENDING), ("status", pymongo.ASCENDING)],
                 "finding a job's requests reads every request"),
                ("document_requests", [("token", pymongo.ASCENDING)],
                 "opening an upload link reads every request"),
                ("document_requests", [("status", pymongo.ASCENDING), ("sent_at", pymongo.ASCENDING)],
                 "looking for reminders reads every request")):
            try:
                db[collection].create_index(keys)
            except OperationFailure as exc:
                logger.error("Could not create the %s index on %s (%s). The database user needs the createIndex "
                             "privilege on %s; until someone creates the index, %s.",
                             ", ".join(k for k, _ in keys), collection, exc, collection, without)
        _INDEXED_DATABASES.add(db.name)   # not reached for any other error, so that is tried again


def _iso(at: dt.datetime) -> str:
    """A time as these records keep it: UTC ISO-8601 to the microsecond, so the text sorts as the time does
    (a time without a zone is taken as UTC)."""
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    return at.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def _now() -> str:
    return _iso(dt.datetime.now(dt.timezone.utc))


def _key(*parts) -> str:
    """A record's id made from what it says: the same parts always give the same 32 hex characters."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def _public(doc: dict) -> dict:
    """A stored record as callers see it: MongoDB's `_id` is its `id`."""
    return {"id": doc["_id"], **{k: v for k, v in doc.items() if k != "_id"}}


def document_key(document_type: str, period: str) -> str:
    """What makes two of a client's requests ask for the same document: its type and period, trimmed, with runs of
    spaces as one, and in lower case."""
    return f"{' '.join(document_type.split()).lower()}\x1f{' '.join(period.split()).lower()}"


def _waiting(now: dt.datetime) -> dict:
    """The query for requests still waiting for the client's file: sent, or claimed by an upload that stalled."""
    return {"$or": [{"status": "sent"},
                    {"status": "receiving", "receiving_since": {"$lte": _iso(now - CLAIM_EXPIRES)}}]}


def link_status(request: dict, now: dt.datetime) -> str:
    """A request as the client's page shows it: "pending" while it waits for the file (a stalled upload's claim
    counts as waiting), "replaced" once a newer request asks for the same document, otherwise "received" (from
    the moment an upload claims it)."""
    status = request["status"]
    if status == "replaced":
        return "replaced"
    stalled = status == "receiving" and request.get("receiving_since", "") <= _iso(now - CLAIM_EXPIRES)
    return "pending" if status in ("pending", "sent") or stalled else "received"


def _insert_once(collection, doc: dict) -> tuple[dict, bool]:
    """Saves the record unless one with its id is already saved; gives the saved record and whether this
    call saved it."""
    try:
        collection.insert_one(dict(doc))
        return _public(doc), True
    except DuplicateKeyError:
        return _public(collection.find_one({"_id": doc["_id"]})), False


class Exceptions:
    """What the agent couldn't settle in a job: `open` until something resolves it."""

    def __init__(self, db: Database):
        self._exceptions = db["exceptions"]
        _ensure_indexes(db)

    def log(self, *, client_id: int, job_id: str, stage: str, description: str, assumption: str,
            confidence: float, row_ids: list[int]) -> dict:
        """Saves an open exception and returns it; the same job logging the same description for the same rows
        gets the exception already saved."""
        return self.log_once(client_id=client_id, job_id=job_id, stage=stage, description=description,
                             assumption=assumption, confidence=confidence, row_ids=row_ids)[0]

    def log_once(self, *, client_id: int, job_id: str, stage: str, description: str, assumption: str,
                 confidence: float, row_ids: list[int]) -> tuple[dict, bool]:
        """As `log`, and also whether this call saved it (False when it was already there)."""
        rows = sorted(set(row_ids))
        doc = {"_id": _key(job_id, description, rows), "client_id": client_id, "job_id": job_id, "stage": stage,
               "description": description, "assumption": assumption, "confidence": float(confidence),
               "row_ids": rows, "status": "open", "note": "", "created_at": _now(), "resolved_at": None}
        return _insert_once(self._exceptions, doc)

    def get(self, exception_id: str) -> dict:
        doc = self._exceptions.find_one({"_id": exception_id})
        if doc is None:
            raise NotFound("This exception was not found.")
        return _public(doc)

    def for_job(self, job_id: str) -> list[dict]:
        """The job's exceptions, in the order they were logged."""
        docs = self._exceptions.find({"job_id": job_id}).sort([("created_at", pymongo.ASCENDING), ("_id", 1)])
        return [_public(doc) for doc in docs]

    def resolve(self, exception_id: str, *, note: str) -> dict:
        """Marks the exception resolved with a note saying how, and returns it (one already resolved is returned
        as it is)."""
        return self.resolve_once(exception_id, note=note)[0]

    def resolve_once(self, exception_id: str, *, note: str) -> tuple[dict, bool]:
        """As `resolve`, and also whether this call resolved it: only an open exception is resolved, so two callers
        at once can't both resolve it."""
        doc = self._exceptions.find_one_and_update(
            {"_id": exception_id, "status": "open"},
            {"$set": {"status": "resolved", "note": note, "resolved_at": _now()}}, return_document=ReturnDocument.AFTER)
        if doc is not None:
            return _public(doc), True
        return self.get(exception_id), False   # NotFound when there is no such exception


class DocumentRequests:
    """The documents asked of the client: `pending` until the run's email goes, `sent` with the email's link (its
    token) until an upload claims it (`receiving`), then `received`; or `replaced` by a later run's request for the
    same document."""

    def __init__(self, db: Database):
        self._requests = db["document_requests"]
        _ensure_indexes(db)

    def create(self, *, client_id: int, job_id: str, exception_id: str, document_type: str, period: str,
               reason: str) -> dict:
        """Saves a pending request and returns it. A job asks for each document once: asking again in the same
        job for the same document (see `document_key`), for whatever exception, gets the request already saved."""
        return self.create_once(client_id=client_id, job_id=job_id, exception_id=exception_id,
                                document_type=document_type, period=period, reason=reason)[0]

    def create_once(self, *, client_id: int, job_id: str, exception_id: str, document_type: str, period: str,
                    reason: str) -> tuple[dict, bool]:
        """As `create`, and also whether this call saved it."""
        key = document_key(document_type, period)
        doc = {"_id": _key(job_id, client_id, key), "client_id": client_id, "job_id": job_id,
               "exception_id": exception_id, "document_type": document_type, "period": period, "reason": reason,
               "document_key": key, "status": "pending", "token": None, "sent_at": None, "reminders": 0,
               "reminded_at": None, "received_at": None, "upload_id": None, "replaced_by": None,
               "created_at": _now()}
        return _insert_once(self._requests, doc)

    def get(self, request_id: str) -> dict:
        doc = self._requests.find_one({"_id": request_id})
        if doc is None:
            raise NotFound("This document request was not found.")
        return _public(doc)

    def for_job(self, job_id: str) -> list[dict]:
        """Every request the job made, in the order it made them."""
        return self._find({"job_id": job_id})

    def pending_for(self, job_id: str) -> list[dict]:
        """The job's requests not yet emailed, in the order they were made."""
        return self._find({"job_id": job_id, "status": "pending"})

    def mark_sent(self, ids: list[str], *, token: str, at: dt.datetime) -> None:
        """Marks pending requests as emailed with the link's token."""
        self._requests.update_many({"_id": {"$in": list(ids)}, "status": "pending"},
                                   {"$set": {"status": "sent", "token": token, "sent_at": _iso(at)}})

    def by_token(self, token: str) -> list[dict]:
        """The requests one email's link is for, in the order they were made; none for an unknown token."""
        return self._find({"token": token}) if token else []

    def claim(self, request_id: str, *, token: str, now: dt.datetime) -> Optional[dict]:
        """Marks a request waiting on this link as being received by one upload, and returns it. None when it
        isn't waiting on this link: received, replaced, or claimed by another upload in the last 15 minutes."""
        doc = self._requests.find_one_and_update(
            {"_id": request_id, "token": token, **_waiting(now)},
            {"$set": {"status": "receiving", "receiving_since": _iso(now)}}, return_document=ReturnDocument.AFTER)
        return _public(doc) if doc is not None else None

    def release(self, claimed: dict) -> None:
        """Puts a claimed request back to sent when its upload failed. Only the claim given is released: one that
        stalled and was claimed again by a later upload is left to that upload."""
        self._requests.update_one(
            {"_id": claimed["id"], "status": "receiving", "receiving_since": claimed["receiving_since"]},
            {"$set": {"status": "sent"}, "$unset": {"receiving_since": ""}})

    def mark_received(self, request_id: str, *, upload_id: int, at: dt.datetime) -> Optional[dict]:
        """Marks a request being received as received, with the upload the client's file was saved as, and
        returns it. None when it wasn't being received (say, received already)."""
        doc = self._requests.find_one_and_update(
            {"_id": request_id, "status": "receiving"},
            {"$set": {"status": "received", "upload_id": upload_id, "received_at": _iso(at)}},
            return_document=ReturnDocument.AFTER)
        return _public(doc) if doc is not None else None

    def replace_older(self, request: dict, *, now: dt.datetime) -> list[dict]:
        """Closes the client's older requests for the same document from other jobs that are still waiting
        (pending, sent, or claimed by an upload that stalled) as replaced by this one, and returns them as they
        were. A request an upload is reading right now is left to finish."""
        closable = {"$or": [{"status": {"$in": ["pending", "sent"]}},
                            {"status": "receiving", "receiving_since": {"$lte": _iso(now - CLAIM_EXPIRES)}}]}
        older = self._requests.find({"client_id": request["client_id"], "document_key": request["document_key"],
                                     "job_id": {"$ne": request["job_id"]},
                                     "created_at": {"$lt": request["created_at"]}, **closable}, {"_id": 1})
        closed = []
        for doc in older:
            was = self._requests.find_one_and_update(
                {"_id": doc["_id"], **closable},
                {"$set": {"status": "replaced", "replaced_by": request["id"], "replaced_at": _iso(now)},
                 "$unset": {"receiving_since": ""}}, return_document=ReturnDocument.BEFORE)
            if was is not None:   # not claimed by an upload in the meantime
                closed.append(_public(was))
        return closed

    def due_reminders(self, now: dt.datetime) -> list[dict]:
        """Requests still waiting for the client's file that are due a reminder: the first from 3 days after the
        email, the second from 7 days after, and never a third. A stalled upload's claim counts as waiting; a
        request being received or replaced gets none."""
        first, second = _iso(now - FIRST_REMINDER), _iso(now - SECOND_REMINDER)
        return self._find({"$and": [_waiting(now), {"$or": [{"reminders": 0, "sent_at": {"$lte": first}},
                                                            {"reminders": 1, "sent_at": {"$lte": second}}]}]})

    def mark_reminded(self, request: dict, *, now: dt.datetime) -> bool:
        """Counts a reminder for a request from `due_reminders`, and says whether this call did (False when
        another got there first). One reminder late enough for both counts as both, so a server that was off
        for a week sends one reminder, not two an hour apart."""
        both_due = request["sent_at"] <= _iso(now - SECOND_REMINDER)
        saved = self._requests.update_one(
            {"_id": request["id"], "reminders": request["reminders"], **_waiting(now)},
            {"$set": {"reminders": 2 if both_due else request["reminders"] + 1, "reminded_at": _iso(now)}})
        return saved.modified_count == 1

    def _find(self, query: dict) -> list[dict]:
        docs = self._requests.find(query).sort([("created_at", pymongo.ASCENDING), ("_id", 1)])
        return [_public(doc) for doc in docs]
