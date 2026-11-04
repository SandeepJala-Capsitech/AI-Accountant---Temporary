"""What the accountant chat keeps beyond the books: the decisions log, the questions it asks, the chat threads and
the texts read from documents. They live in their own collections of the database it is given, next to the clients."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from typing import Optional
from uuid import uuid4

import pymongo
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from .errors import NotFound
from .models import Change, ChangeDraft, ChatThread, Decision, DecisionDraft, Question, QuestionDraft
from .rows import now_iso

# What a document keeps besides its model: the idempotency key or question key it is found by, `seq`, the time in
# nanoseconds it was added, which orders records that were added in the same second, a thread's turns, and the last
# summary an older thread kept in their place.
_KEPT_APART = ("_id", "idempotency_key", "key", "seq", "turns", "summary")
# Leaves a thread's turns out of a read: they hold every result the chat gave, so only turns() reads them.
_WITHOUT_TURNS = {"turns": 0}


def _model(cls, doc: dict):
    return cls.model_validate({k: v for k, v in doc.items() if k not in _KEPT_APART})


def _hash(prev_hash: str, decision: Decision) -> str:
    """The SHA-256 of the previous hash and the decision's canonical JSON (sorted keys, no spaces, no hash)."""
    body = json.dumps(decision.model_dump(mode="json", exclude={"hash"}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


class AgentRecords:
    def __init__(self, db: Database):
        """The records on `db`, which is the one the app's client already opened: this never makes a client."""
        self.db = db
        db.decisions.create_index([("client_id", pymongo.ASCENDING), ("year_start", pymongo.ASCENDING)])
        try:   # an older database has a unique index on the key alone, which would stop two clients using one key
            db.decisions.drop_index("idempotency_key_1")
        except OperationFailure:
            pass
        db.decisions.create_index([("client_id", pymongo.ASCENDING), ("idempotency_key", pymongo.ASCENDING)],
                                  unique=True)
        db.questions.create_index([("client_id", pymongo.ASCENDING), ("key", pymongo.ASCENDING)], unique=True)
        db.chat_threads.create_index([("client_id", pymongo.ASCENDING), ("updated_at", pymongo.ASCENDING)])
        db.changes.create_index([("client_id", pymongo.ASCENDING), ("seq", pymongo.ASCENDING)])

    # ─── Decisions ─────────────────────────────────────────────────────────────

    def append_decision(self, d: DecisionDraft, idempotency_key: str,
                        id: Optional[str] = None) -> tuple[Decision, bool]:
        """Adds the decision to its client's chain and says it is new. Keys are per client: given a key the client already used, it adds nothing
        and returns the decision stored under it with False, so a retried tool call decides only once. Two
        appends racing for one client may both chain from the same previous decision; the log is not locked.
        id: the decision's id when its maker needed it before adding it (a split names the decision that
        replaced the row); a new one otherwise."""
        stored = self.decision_by_key(d.client_id, idempotency_key)
        if stored:
            return stored, False
        latest = self.db.decisions.find_one({"client_id": d.client_id}, sort=[("seq", pymongo.DESCENDING)])
        prev_hash = latest["hash"] if latest else ""
        decision = Decision(**d.model_dump(), id=id or uuid4().hex, created_at=now_iso(), prev_hash=prev_hash, hash="")
        decision.hash = _hash(prev_hash, decision)
        doc = {**decision.model_dump(mode="json"), "_id": decision.id, "idempotency_key": idempotency_key,
               "seq": time.time_ns()}
        try:
            self.db.decisions.insert_one(doc)
        except DuplicateKeyError:   # the same key was added meanwhile
            stored = self.decision_by_key(d.client_id, idempotency_key)
            if stored is None:   # not the key: the id is taken
                raise
            return stored, False
        self.add_change(ChangeDraft(
            client_id=decision.client_id, year_start=decision.year_start, actor=decision.actor, kind="decision",
            row_ids=decision.row_ids, after={"decision_id": decision.id}, summary=f"{decision.rule}: {decision.status}"))
        return decision, True

    def decision_by_key(self, client_id: int, idempotency_key: str) -> Optional[Decision]:
        """The client's decision stored under the key, if any."""
        doc = self.db.decisions.find_one({"client_id": client_id, "idempotency_key": idempotency_key})
        return _model(Decision, doc) if doc else None

    def decisions(self, client_id: int, year_start: Optional[dt.date] = None,
                  since: Optional[str] = None) -> list[Decision]:
        """The client's decisions, oldest first; of one year, and made at or after `since` (an ISO time), if given."""
        query: dict = {"client_id": client_id}
        if year_start:
            query["year_start"] = year_start.isoformat()
        if since:
            query["created_at"] = {"$gte": since}
        return [_model(Decision, doc) for doc in self.db.decisions.find(query).sort("seq", pymongo.ASCENDING)]

    # ─── Changes ───────────────────────────────────────────────────────────────

    def add_change(self, c: ChangeDraft) -> Change:
        """Adds the change to the client's record of changes, which is only ever added to."""
        change = Change(**c.model_dump(), id=uuid4().hex, seq=time.time_ns(), created_at=now_iso())
        self.db.changes.insert_one({**change.model_dump(mode="json"), "_id": change.id})
        return change

    def changes(self, client_id: int, *, year_start: Optional[dt.date] = None, since: Optional[str] = None,
                limit: int = 200) -> list[Change]:
        """The client's latest `limit` changes, oldest first; of one year and those with no year, and made after
        `since` (an ISO time), if given."""
        query: dict = {"client_id": client_id}
        if year_start:
            query["year_start"] = {"$in": [year_start.isoformat(), None]}
        if since:
            query["created_at"] = {"$gt": since}
        docs = list(self.db.changes.find(query).sort("seq", pymongo.DESCENDING).limit(limit))
        # a change keeps its own seq and summary, which _model would set apart
        return [Change.model_validate({k: v for k, v in doc.items() if k != "_id"}) for doc in reversed(docs)]

    # ─── Questions ─────────────────────────────────────────────────────────────

    def upsert_question(self, q: QuestionDraft, key: str) -> Question:
        """Asks the question, once per key for each client: asked again, it takes the new kind, year, text,
        reason and rows (a held row can change its year or its kind of document) and keeps its id, its status and any
        answer."""
        new_id = uuid4().hex
        fresh = {**q.model_dump(mode="json", exclude={"kind", "year_start", "text", "why", "row_ids"}), "_id": new_id, "id": new_id,
                 "status": "open", "answer": None, "created_at": now_iso(), "resolved_at": None,
                 "seq": time.time_ns()}
        update = {"$set": {"kind": q.kind, "year_start": q.year_start.isoformat(), "text": q.text, "why": q.why,
                           "row_ids": q.row_ids}, "$setOnInsert": fresh}
        where = {"client_id": q.client_id, "key": key}
        try:
            doc = self.db.questions.find_one_and_update(where, update, upsert=True,
                                                        return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:   # another asker inserted it between the find and the insert: update theirs
            doc = self.db.questions.find_one_and_update(where, update, return_document=ReturnDocument.AFTER)
        return _model(Question, doc)

    def question(self, id: str) -> Optional[Question]:
        """The question with this id, in any status, if there is one."""
        doc = self.db.questions.find_one({"id": id})
        return _model(Question, doc) if doc else None

    def questions(self, client_id: int, year_start: Optional[dt.date],
                  statuses: tuple[str, ...] = ("open", "waiting")) -> list[Question]:
        """The client's questions for the year (every year when None) in these statuses, oldest first."""
        query: dict = {"client_id": client_id, "status": {"$in": list(statuses)}}
        if year_start is not None:
            query["year_start"] = year_start.isoformat()
        return [_model(Question, doc) for doc in self.db.questions.find(query).sort("seq", pymongo.ASCENDING)]

    def set_question(self, id: str, status: str, answer: Optional[str] = None) -> Question:
        """Moves the question to the status, with the person's answer; resolved sets the time it was."""
        changes = {"status": status, "answer": answer, "resolved_at": now_iso() if status == "resolved" else None}
        doc = self.db.questions.find_one_and_update({"id": id}, {"$set": changes}, return_document=ReturnDocument.AFTER)
        if not doc:
            raise NotFound("This question was not found.")
        return _model(Question, doc)

    def set_question_why(self, id: str, why: str) -> Question:
        """Says why the question is still asked; the rules' next refresh puts their own reason back."""
        doc = self.db.questions.find_one_and_update({"id": id}, {"$set": {"why": why}},
                                                    return_document=ReturnDocument.AFTER)
        if not doc:
            raise NotFound("This question was not found.")
        return _model(Question, doc)

    # ─── Chat threads ──────────────────────────────────────────────────────────

    def create_thread(self, client_id: int, year_start: dt.date, title: str) -> ChatThread:
        now = now_iso()
        thread = ChatThread(id=uuid4().hex, client_id=client_id, year_start=year_start, title=title,
                            created_at=now, updated_at=now)
        self.db.chat_threads.insert_one({**thread.model_dump(mode="json"), "_id": thread.id})
        return thread

    def threads(self, client_id: Optional[int] = None) -> list[ChatThread]:
        """The threads, of one client if given, the most recently used first."""
        query = {} if client_id is None else {"client_id": client_id}
        return [_model(ChatThread, doc) for doc in self.db.chat_threads.find(query, _WITHOUT_TURNS).sort(
            [("updated_at", pymongo.DESCENDING), ("_id", pymongo.ASCENDING)])]

    def thread(self, id: str) -> Optional[ChatThread]:
        doc = self.db.chat_threads.find_one({"_id": id}, _WITHOUT_TURNS)
        return _model(ChatThread, doc) if doc else None

    def touch_thread(self, id: str, title: Optional[str] = None) -> ChatThread:
        """Marks the thread as used now, and retitles it if given a title."""
        changes = {"updated_at": now_iso(), **({"title": title} if title is not None else {})}
        doc = self.db.chat_threads.find_one_and_update({"_id": id}, {"$set": changes}, projection=_WITHOUT_TURNS,
                                                       return_document=ReturnDocument.AFTER)
        if not doc:
            raise NotFound("This chat was not found.")
        return _model(ChatThread, doc)

    def add_turn(self, thread_id: str, turn: dict) -> None:
        """Keeps the turn (as JSON) after the thread's earlier ones: the id of the person's message, the files it
        carried and the result its turn ended with."""
        self.db.chat_threads.update_one({"_id": thread_id}, {"$push": {"turns": turn}})

    def turns(self, thread_id: str) -> list[dict]:
        """The thread's turns as JSON, oldest first; none before its first (or for a chat saved before turns were
        kept)."""
        doc = self.db.chat_threads.find_one({"_id": thread_id}, {"turns": 1})
        return list(doc.get("turns") or []) if doc else []

    def delete_thread(self, id: str) -> None:
        """Removes the thread's record; what else was kept for it is removed by whoever kept it."""
        self.db.chat_threads.delete_one({"_id": id})

    # ─── Document texts ────────────────────────────────────────────────────────

    def document_text(self, sha256: str) -> Optional[list[str]]:
        """The pages of text read from the document with this hash, or None if it was not read before."""
        doc = self.db.document_texts.find_one({"_id": sha256})
        return list(doc["pages"]) if doc else None

    def save_document_text(self, sha256: str, pages: list[str], method: str, model: str) -> None:
        self.db.document_texts.update_one(
            {"_id": sha256}, {"$set": {"pages": list(pages), "method": method, "model": model,
                                       "created_at": now_iso()}}, upsert=True)
