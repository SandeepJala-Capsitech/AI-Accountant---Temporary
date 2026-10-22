"""The trial balance snapshots: each run of the agent saves the client's trial balance twice, after posting
(`after_posting`) and after the quality gate (`after_gate`), so the reviewer sees what each step produced.

A snapshot is a new document each time and is never updated. Its `version` counts from 1 for each job. It holds the
trial balance's lines and totals with every amount as an exact decimal string ("12.50", never a float), whether it
balances, and the Excel workbook of the same ledger, kept in the blob store (`excel_blob_id`). A trial balance that
can't be built (a booked row has an error) is saved with `is_balanced` false, no lines, zero totals and the
`problems` that stopped it.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from decimal import Decimal
from typing import Literal, Optional, Union
from uuid import uuid4

import pymongo
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from ledgersync.blobs import BlobStore
from ledgersync.errors import NotFound
from ledgersync.export import file_name, workbook
from ledgersync.models import Ledger, TrialBalance, TrialBalanceLine
from ledgersync.money import PENNY, ZERO

logger = logging.getLogger(__name__)

SnapshotStage = Literal["after_posting", "after_gate"]
TOTALS = ("debits", "credits", "income", "expenses", "net_profit", "assets", "liabilities")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Databases whose trial_balances index was already set up (or refused) in this process, by name, as in
# records/jobs.py: the database user the app runs as may not be allowed to create indexes, so it is asked for once
# per process, not on every Snapshots, which is built with the tools' dependencies.
_INDEXED_DATABASES: set[str] = set()
_INDEX_LOCK = threading.Lock()


def _ensure_index(db: Database) -> None:
    with _INDEX_LOCK:
        if db.name in _INDEXED_DATABASES:
            return
        try:
            db["trial_balances"].create_index([("job_id", pymongo.ASCENDING), ("version", pymongo.ASCENDING)],
                                              unique=True)
        except OperationFailure as exc:
            logger.error("Could not create the unique (job_id, version) index on trial_balances (%s). The database "
                         "user needs the createIndex privilege on trial_balances; until someone creates the index, "
                         "nothing in the database stops two snapshots of a job sharing a version.", exc)
        _INDEXED_DATABASES.add(db.name)   # not reached for any other error, so that is tried again


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _money(amount: Decimal) -> str:
    """An amount as an exact decimal string with its pennies: "72.00", "-60.00"."""
    return str(amount.quantize(PENNY))


def _line(line: TrialBalanceLine) -> dict:
    return {"code": line.code, "name": line.name, "type": line.type, "debit": _money(line.debit),
            "credit": _money(line.credit)}


def _public(doc: dict) -> dict:
    """A stored snapshot as callers see it: MongoDB's `_id` is its `id`, first."""
    return {"id": doc["_id"], **{key: value for key, value in doc.items() if key != "_id"}}


class Snapshots:
    """Saves each version of a job's trial balance with its workbook, and finds them again."""

    def __init__(self, db: Database, blobs: BlobStore):
        self._saved = db["trial_balances"]
        self._blobs = blobs
        _ensure_index(db)

    def save(self, *, job_id: str, client_id: int, stage: SnapshotStage, ledger: Ledger,
             balance: Union[TrialBalance, list[str]], day: dt.date) -> dict:
        """Saves the trial balance as the job's next version and returns the snapshot as saved. `balance` is the
        trial balance, or the problems that stop it being built (a list of them). The workbook (of `ledger` and
        `balance`, for `day`) is kept in the blob store first.

        The version is the job's highest plus one. If another save took that version meanwhile (a duplicate-key
        error from the unique index), the next one is tried once; a second collision is raised."""
        built = isinstance(balance, TrialBalance)
        excel_blob_id = self._blobs.put(workbook(ledger, balance, day), name=file_name(ledger.client.name, day),
                                        content_type=XLSX)
        doc = {"_id": uuid4().hex, "job_id": job_id, "client_id": client_id, "version": 0, "stage": stage,
               "is_balanced": balance.is_balanced if built else False,
               "lines": [_line(line) for line in balance.lines] if built else [],
               "totals": _totals(balance if built else None),
               "problems": [] if built else list(balance),
               "excel_blob_id": excel_blob_id, "created_at": _now()}
        doc["version"] = self._highest(job_id) + 1
        try:
            self._saved.insert_one(dict(doc))   # a copy, because insert_one adds `_id` to what it is given
        except DuplicateKeyError:
            doc["version"] = self._highest(job_id) + 1
            self._saved.insert_one(dict(doc))   # a second collision is raised
        return _public(doc)

    def _highest(self, job_id: str) -> int:
        """The job's highest version so far, 0 when it has none."""
        top = self._saved.find_one({"job_id": job_id}, {"version": 1}, sort=[("version", pymongo.DESCENDING)])
        return top["version"] if top else 0

    def latest(self, job_id: str) -> Optional[dict]:
        """The job's newest snapshot, or None when it has none."""
        doc = self._saved.find_one({"job_id": job_id}, sort=[("version", pymongo.DESCENDING)])
        return _public(doc) if doc else None

    def list(self, job_id: str) -> list[dict]:
        """The job's snapshots, oldest version first."""
        return [_public(doc) for doc in self._saved.find({"job_id": job_id}).sort("version", pymongo.ASCENDING)]

    def get(self, job_id: str, version: int) -> dict:
        """One of the job's snapshots by its version, or NotFound."""
        doc = self._saved.find_one({"job_id": job_id, "version": version})
        if doc is None:
            raise NotFound("This snapshot was not found.")
        return _public(doc)


def _totals(balance: Optional[TrialBalance]) -> dict:
    """The trial balance's totals as exact strings; all "0.00" when there is none (it couldn't be built)."""
    if balance is None:
        return {name: _money(ZERO) for name in TOTALS}
    amounts = (balance.total_debits, balance.total_credits, balance.total_income, balance.total_expenses,
               balance.net_profit, balance.total_assets, balance.total_liabilities)
    return {name: _money(amount) for name, amount in zip(TOTALS, amounts)}
