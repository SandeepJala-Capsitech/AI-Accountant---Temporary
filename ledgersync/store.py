"""The local database (design of 2026-10-06): clients, their uploads, and each upload's rows, in one SQLite
file through the standard library. One connection per call, so background jobs and requests use it side by
side. Rows keep their inputs only; statuses, matching and warnings are worked out again on every read."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ClientArchived, NotFound, StorageError
from .models import Client, ClientFields

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
