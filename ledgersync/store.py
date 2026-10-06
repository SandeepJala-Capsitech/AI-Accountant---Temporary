"""The local database (design of 2026-10-06): clients, their uploads, and each upload's rows, in one SQLite
file through the standard library. One connection per call, so background jobs and requests use it side by
side. Rows keep their inputs only; statuses, matching and warnings are worked out again on every read."""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

from .checks import is_derived
from .errors import ClientArchived, NotFound, StorageError
from .models import Client, ClientFields, Transaction

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    business_type TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    contact_email TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    vat_registered INTEGER NOT NULL DEFAULT 1,
    archived_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    warnings TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    upload_id INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rows_by_client ON rows(client_id, upload_id, position);
"""
_CLIENT_FIELDS = ("name", "business_type", "contact_name", "contact_email", "contact_phone", "vat_registered")
# What is kept of a row: the inputs a document or a person gave. Everything else is worked out on read.
INPUTS = {"date", "description", "direction", "gross", "vat", "vat_treatment", "account_code", "contra_account_code",
          "currency", "source", "method", "evidence", "document_type", "counterparty", "document_ref", "include",
          "link", "balance", "opening_balance", "closing_balance"}
# The fields a person can edit; a row's first edit keeps what they held as `original`.
EDITABLE = ("date", "description", "counterparty", "direction", "gross", "vat", "account_code", "document_type")
# Fields of a whole document: a change to one row changes every row with its document_ref.
WHOLE_DOCUMENT = {"counterparty", "document_type", "include"}


class StoredRow(NamedTuple):
    id: int
    upload_id: int
    tx: Transaction
    original: Optional[dict]   # what the editable fields held before a person's first edit; None if never edited


def saved_form(tx: Transaction) -> dict:
    """A row as the database keeps it: its inputs, the issues the ledger doesn't work out itself, and a
    document reference (a manual entry arrives without one)."""
    data = tx.model_dump(mode="json", include=INPUTS)
    data["issues"] = [found.model_dump() for found in tx.issues if not is_derived(found)]
    data["document_ref"] = data.get("document_ref") or uuid.uuid4().hex[:12]
    return data


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._ready = False
        self._lock = threading.Lock()

    # ─── Clients ───────────────────────────────────────────────────────────────

    def create_client(self, fields: ClientFields) -> Client:
        now = _now()
        with self._db(write=True) as db:
            cursor = db.execute(
                "INSERT INTO clients (name, business_type, contact_name, contact_email, contact_phone,"
                " vat_registered, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fields.name, fields.business_type, fields.contact_name, fields.contact_email, fields.contact_phone,
                 int(fields.vat_registered), now, now))
            return _client(db, cursor.lastrowid)

    def get_client(self, client_id: int) -> Client:
        with self._db() as db:
            return _client(db, client_id)

    def list_clients(self, archived: bool = False) -> list[Client]:
        """The active clients, or the archived ones, by name."""
        with self._db() as db:
            found = db.execute("SELECT * FROM clients WHERE (archived_at IS NOT NULL) = ? "
                               "ORDER BY name COLLATE NOCASE, id", (int(archived),)).fetchall()
        return [_client_from(row) for row in found]

    def update_client(self, client_id: int, changes: dict) -> Client:
        """Changes the fields given; `archived` True archives the client and False restores it. An archived
        client can only be restored."""
        with self._db(write=True) as db:
            client = _client(db, client_id)
            fields = {k: changes[k] for k in _CLIENT_FIELDS if changes.get(k) is not None}
            if client.archived and fields:
                raise ClientArchived(f"Restore {client.name} to change it.")
            now = _now()
            sets = {k: int(v) if isinstance(v, bool) else v for k, v in fields.items()}
            archived = changes.get("archived")
            if archived is not None and archived != client.archived:
                sets["archived_at"] = now if archived else None
            if sets:
                sets["updated_at"] = now
                assignments = ", ".join(f"{k} = ?" for k in sets)
                db.execute(f"UPDATE clients SET {assignments} WHERE id = ?", (*sets.values(), client_id))
            return _client(db, client_id)

    # ─── Uploads and rows ──────────────────────────────────────────────────────

    def add_upload(self, client_id: int, name: str, kind: str, rows: list[Transaction], *, sha256: str = "",
                   model: str = "", warnings=()) -> int:
        """Saves an upload and its rows in order, and returns the upload's id. Archived or not: a job that
        finishes after its client was archived is still kept."""
        now = _now()
        with self._db(write=True) as db:
            _client(db, client_id)
            upload_id = db.execute(
                "INSERT INTO uploads (client_id, name, kind, sha256, model, warnings, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (client_id, name, kind, sha256, model or "", json.dumps(list(warnings)), now)).lastrowid
            db.executemany("INSERT INTO rows (client_id, upload_id, position, data) VALUES (?, ?, ?, ?)",
                           [(client_id, upload_id, n, json.dumps(saved_form(tx))) for n, tx in enumerate(rows)])
            _touch(db, client_id, now)
            return upload_id

    def uploads(self, client_id: int) -> list[dict]:
        """The client's uploads, newest first, each with its row count."""
        with self._db() as db:
            _client(db, client_id)
            found = db.execute("SELECT u.*, (SELECT COUNT(*) FROM rows r WHERE r.upload_id = u.id) AS row_count"
                               " FROM uploads u WHERE u.client_id = ? ORDER BY u.id DESC", (client_id,)).fetchall()
        return [{**dict(row), "warnings": json.loads(row["warnings"])} for row in found]

    def delete_upload(self, client_id: int, upload_id: int) -> None:
        """Removes an upload and its rows for good."""
        with self._db(write=True) as db:
            if not db.execute("DELETE FROM uploads WHERE id = ? AND client_id = ?", (upload_id, client_id)).rowcount:
                raise NotFound("This upload was not found.")
            _touch(db, client_id, _now())

    def rows(self, client_id: int) -> list[StoredRow]:
        """The client's rows in table order: by upload, oldest first, then as the model gave them."""
        with self._db() as db:
            _client(db, client_id)
            found = db.execute("SELECT id, upload_id, data FROM rows WHERE client_id = ? ORDER BY upload_id, position",
                               (client_id,)).fetchall()
        return [_stored(row) for row in found]

    def patch_row(self, client_id: int, row_id: int, changes: dict, *, revert: bool = False) -> None:
        """A person's change to a row: Link, Include, an edit, or revert. Include, counterparty and document
        type change every row of its document (the same document_ref); the rest change that row. A row's first
        edit keeps what it held as `original`; revert puts back every edited row of the document."""
        with self._db(write=True) as db:
            saved = {row["id"]: json.loads(row["data"]) for row in
                     db.execute("SELECT id, data FROM rows WHERE client_id = ?", (client_id,))}
            if row_id not in saved:
                raise NotFound("This row was not found.")
            ref = saved[row_id].get("document_ref")
            for rid, data in saved.items():
                if rid != row_id and not (ref and data.get("document_ref") == ref):
                    continue
                if revert:
                    if "original" not in data:
                        continue
                    data.update(data.pop("original"))
                else:
                    change = changes if rid == row_id else {k: v for k, v in changes.items() if k in WHOLE_DOCUMENT}
                    if not change:
                        continue
                    if "original" not in data and any(k in EDITABLE and data.get(k) != v for k, v in change.items()):
                        data["original"] = {k: data.get(k) for k in EDITABLE}
                    data.update(change)
                db.execute("UPDATE rows SET data = ? WHERE id = ?", (json.dumps(data), rid))
            _touch(db, client_id, _now())

    # ─── The database ──────────────────────────────────────────────────────────

    def _prepare(self) -> None:
        """Creates the folder, the file and the tables on first use, so starting the API touches nothing."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as db:
                    db.execute("PRAGMA journal_mode = WAL")
                    db.executescript(_SCHEMA)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except (OSError, sqlite3.Error) as exc:
                raise self._failed(exc) from None
            self._ready = True

    @contextmanager
    def _db(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """One connection for one call, in one transaction. A write takes the write lock up front (waiting up
        to ten seconds for another writer), so a background job and a person never trip over each other."""
        self._prepare()
        try:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        except sqlite3.Error as exc:
            raise self._failed(exc) from None
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(db)
            raise self._failed(exc) from None
        except BaseException:
            _rollback(db)
            raise
        finally:
            db.close()

    def _failed(self, exc: Exception) -> StorageError:
        logger.error("The database at %s could not be used: %s", self.path, exc)
        return StorageError("The local database could not be read or written. Details are in the server log.")


def _rollback(db: sqlite3.Connection) -> None:
    if db.in_transaction:
        db.execute("ROLLBACK")


def _touch(db: sqlite3.Connection, client_id: int, now: str) -> None:
    db.execute("UPDATE clients SET updated_at = ? WHERE id = ?", (now, client_id))


def _stored(row: sqlite3.Row) -> StoredRow:
    data = json.loads(row["data"])
    original = data.pop("original", None)
    return StoredRow(row["id"], row["upload_id"], Transaction.model_validate(data), original)


def _client(db: sqlite3.Connection, client_id: int) -> Client:
    row = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        raise NotFound("This client was not found.")
    return _client_from(row)


def _client_from(row: sqlite3.Row) -> Client:
    return Client(id=row["id"], name=row["name"], business_type=row["business_type"],
                  contact_name=row["contact_name"], contact_email=row["contact_email"],
                  contact_phone=row["contact_phone"], vat_registered=bool(row["vat_registered"]),
                  archived=row["archived_at"] is not None, archived_at=row["archived_at"],
                  created_at=row["created_at"], updated_at=row["updated_at"])
