"""The audit log: every write the agent makes, with its reasoning, in a chain that shows tampering.

Each entry carries the hash of the entry before it, so changing or deleting an entry breaks the
chain from that point on and `verify` finds where. The class can only add and read entries; the
database role the app connects with refuses updates and deletes as well.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
import threading
from typing import Optional

import pymongo
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64   # the "previous hash" of the very first entry

# One lock for the whole process, not one per AuditLog: a log is built for every job, and two of
# them appending at once must still produce one chain with no two entries on the same previous hash.
_APPEND_LOCK = threading.Lock()

# Databases whose audit_log indexes were already set up (or refused) in this process, by name. The
# database user the app runs as may only insert and find, so the index is asked for once per
# process, not on every AuditLog, which is built for every job. Filled under _APPEND_LOCK.
_INDEXED_DATABASES: set[str] = set()


def _entry_hash(entry: dict) -> str:
    """SHA-256 of the entry's fields (not its `hash`, and not MongoDB's own `_id`) as JSON with
    sorted keys and no spaces, so the same entry always gives the same hash."""
    body = {key: value for key, value in entry.items() if key not in ("hash", "_id")}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public(doc: dict) -> dict:
    """A stored entry as callers see it, without MongoDB's `_id`."""
    return {key: value for key, value in doc.items() if key != "_id"}


class AuditLog:
    """An append-only, hash-chained log of what the agent (or the system, or a person) changed."""

    def __init__(self, db: Database):
        self._entries = db["audit_log"]
        self._counters = db["counters"]
        with _APPEND_LOCK:
            if db.name not in _INDEXED_DATABASES:
                try:
                    self._entries.create_index([("seq", pymongo.ASCENDING)], unique=True)
                except OperationFailure as exc:
                    logger.error(
                        "Could not create the unique seq index on audit_log (%s). The database user "
                        "needs the createIndex privilege on audit_log; until someone creates the index, "
                        "nothing in the database stops two entries sharing a seq.", exc)
                _INDEXED_DATABASES.add(db.name)   # not reached for any other error, so that is tried again

    def append(self, *, actor: str, action: str, client_id: int, job_id: Optional[str], target: dict,
               before: Optional[dict], after: Optional[dict], note: str = "") -> dict:
        """Adds one entry to the end of the chain and returns it as saved.

        `actor` is "agent", "system" or a user's email. `before` and `after` are the values the
        action changed (None when there was nothing before or after)."""
        with _APPEND_LOCK:
            seq = self._counters.find_one_and_update(
                {"_id": "audit_seq"}, {"$inc": {"seq": 1}}, upsert=True,
                return_document=ReturnDocument.AFTER)["seq"]
            last = self._entries.find_one({}, {"hash": 1}, sort=[("seq", pymongo.DESCENDING)])
            entry = {
                "seq": seq,
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                "actor": actor,
                "action": action,
                "client_id": client_id,
                "job_id": job_id,
                "target": copy.deepcopy(target),
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
                "note": note,
                "prev_hash": last["hash"] if last else GENESIS_HASH,
            }
            entry["hash"] = _entry_hash(entry)
            self._entries.insert_one(dict(entry))   # a copy, because insert_one adds `_id` to what it is given
        return entry

    def entries(self, *, client_id: int, job_id: Optional[str] = None) -> list[dict]:
        """The client's entries in the order they were made; with `job_id`, only that job's."""
        query: dict = {"client_id": client_id}
        if job_id is not None:
            query["job_id"] = job_id
        return [_public(doc) for doc in self._entries.find(query).sort("seq", pymongo.ASCENDING)]

    def verify(self) -> Optional[int]:
        """None when the whole chain holds, or the `seq` of the first entry that doesn't: its stored
        hash isn't the hash of its fields, or its previous hash isn't the hash of the entry before it."""
        expected_prev = GENESIS_HASH
        for doc in self._entries.find().sort("seq", pymongo.ASCENDING):
            if doc.get("hash") != _entry_hash(doc) or doc.get("prev_hash") != expected_prev:
                seq = doc.get("seq")
                return seq if isinstance(seq, int) else 0   # a document with no usable seq must not read as "verified"
            expected_prev = doc["hash"]
        return None
