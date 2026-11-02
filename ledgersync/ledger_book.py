"""The posted ledger the accountant chat shows. The pages' trial balance is worked out from the rows on every read and
never stored; this one is stored, and only ever added to. Each posting compares what every row of a year should
post now, after the document rules, with what is already posted for that row, and writes the difference as one
batch: a row whose lines changed gets a reversal of each line it has posted and its lines afresh; a row no longer
booked gets the reversals alone. A batch is checked whole before anything is written. Money is kept as Decimal128.
It lives in the ledger_entries and posting_batches collections of the records' database."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional, Sequence
from uuid import uuid4

import pymongo
from bson import Decimal128
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from . import books
from .accounts import BY_CODE, PURCHASE_VAT, SALES_VAT, SUSPENSE
from .checks import _most_vat
from .errors import InvalidTransactions
from .evidence_rules import DocumentRule, document_rule, journal_with_rule
from .ledger import settings_for
from .models import BusinessSettings, JournalLine, PostedTrialBalance, PostResult, SuspenseItem, Transaction
from .money import PENNY, ZERO
from .posting import summarise
from .records import AgentRecords
from .rows import now_iso
from .store_mongo import MongoStore

Ruled = list[tuple[int, Transaction, DocumentRule]]


def _money(value) -> Decimal:
    """Pennies from a Decimal128 as stored, or a Decimal."""
    return (value.to_decimal() if isinstance(value, Decimal128) else Decimal(value)).quantize(PENNY)


def _stored(amount: Decimal) -> Decimal128:
    return Decimal128(str(amount.quantize(PENNY)))


def _ensure_indexes(db: Database) -> None:
    db.ledger_entries.create_index([("client_id", pymongo.ASCENDING), ("year_start", pymongo.ASCENDING)])
    db.ledger_entries.create_index([("batch_id", pymongo.ASCENDING)])
    db.posting_batches.create_index([("client_id", pymongo.ASCENDING), ("year_start", pymongo.ASCENDING),
                                     ("key", pymongo.ASCENDING)], unique=True)


def _ruled(store: MongoStore, client_id: int, year_start: dt.date) -> tuple[BusinessSettings, Ruled]:
    """The client's settings, and the year's booked rows by row id with the document rule each falls under;
    InvalidTransactions while rows need fixing."""
    settings = settings_for(store.get_client(client_id))
    return settings, [(row_id, tx, document_rule(tx, settings))
                      for row_id, tx in books.booked_rows(store, client_id, year_start)]


def wanted_journal(store: MongoStore, client_id: int, year_start: dt.date,
                   ruled: Optional[Ruled] = None) -> dict[int, list[JournalLine]]:
    """What each of the year's booked rows should post now, by row id: its journal lines after the document rules,
    with the row id as their transaction. ruled: the rows and their rules, when the caller has them already.
    InvalidTransactions while rows need fixing (their journal can't be worked out)."""
    if ruled is None:
        ruled = _ruled(store, client_id, year_start)[1]
    return {row_id: journal_with_rule(tx, row_id, rule) for row_id, tx, rule in ruled}


def _net(lines: Iterable[tuple[str, Decimal, Decimal]]) -> dict[str, Decimal]:
    """Debits less credits by account, leaving out the accounts that come to nothing."""
    net: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for code, debit, credit in lines:
        net[code] += debit - credit
    return {code: amount for code, amount in net.items() if amount != 0}


def _legs(lines: list[JournalLine]) -> list[tuple[str, Decimal, Decimal]]:
    return [(l.code, l.debit, l.credit) for l in lines]


@dataclass
class _Posted:
    """What is posted for a client's year: every entry, oldest first, with its money as Decimal; the lines of each
    row that are neither reversals nor reversed; each row's net by account; and the latest batch."""
    entries: list[dict]
    live: dict[int, list[dict]] = field(default_factory=dict)
    net: dict[int, dict[str, Decimal]] = field(default_factory=dict)
    last_batch: Optional[str] = None


def _posted(db: Database, client_id: int, year_start: dt.date) -> _Posted:
    where = {"client_id": client_id, "year_start": year_start.isoformat()}
    entries = [{**d, "debit": _money(d["debit"]), "credit": _money(d["credit"])}
               for d in db.ledger_entries.find(where).sort([("seq", pymongo.ASCENDING), ("_id", pymongo.ASCENDING)])]
    reversed_ = {d["reverses"] for d in entries if d["reverses"]}
    by_row: dict[int, list[dict]] = defaultdict(list)
    for d in entries:
        by_row[d["row_id"]].append(d)
    latest = db.posting_batches.find_one(where, sort=[("seq", pymongo.DESCENDING)])
    return _Posted(entries=entries,
                   live={r: [d for d in ds if not d["reverses"] and d["_id"] not in reversed_] for r, ds in by_row.items()},
                   net={r: n for r, ds in by_row.items() if (n := _net((d["code"], d["debit"], d["credit"]) for d in ds))},
                   last_batch=latest["_id"] if latest else None)


def _changed(wanted: dict[int, list[JournalLine]], posted: _Posted) -> list[int]:
    """The rows whose wanted lines come to something other than what is posted for them, by account: changed rows,
    new rows, and rows posted that are no longer booked."""
    return sorted(r for r in set(wanted) | set(posted.net) if _net(_legs(wanted.get(r, []))) != posted.net.get(r, {}))


def _new_lines(wanted: dict[int, list[JournalLine]], posted: _Posted) -> list[dict]:
    """For each changed row, the reversal of each line it has posted (debit and credit swapped), then its wanted
    lines."""
    lines = []
    for r in _changed(wanted, posted):
        lines += [dict(row_id=r, code=d["code"], description=d["description"], debit=d["credit"], credit=d["debit"],
                       reverses=d["_id"]) for d in posted.live.get(r, [])]
        lines += [dict(row_id=r, code=l.code, description=l.description, debit=l.debit.quantize(PENNY),
                       credit=l.credit.quantize(PENNY), reverses=None) for l in wanted.get(r, [])]
    return lines


def _problems(lines: list[dict], rows: dict[int, Transaction], vat_registered: bool) -> list[str]:
    """Why the batch can't be posted: a row's lines or the whole batch not balancing, an account not in the chart,
    or a new VAT leg that is not on Sales or Purchase VAT, is more than the row's gross can hold (a sixth, with the
    rounding allowance checks.normalise allows), or is for a client that isn't VAT registered. A VAT leg is a line
    on an account other than the row's own, its bank (or contra) and Suspense."""
    problems = []
    by_row: dict[int, list[dict]] = defaultdict(list)
    for line in lines:
        by_row[line["row_id"]].append(line)
    for r, ls in by_row.items():
        debits, credits = sum((l["debit"] for l in ls), ZERO), sum((l["credit"] for l in ls), ZERO)
        if debits != credits:
            problems.append(f"Row {r}: debits £{debits} and credits £{credits} do not balance.")
        problems += [f"Row {r}: {l['code']} isn't an account in the chart." for l in ls if l["code"] not in BY_CODE]
        fresh = [l for l in ls if l["reverses"] is None and l["code"] in BY_CODE]
        tx = rows.get(r)
        if tx is None:
            if fresh:
                problems.append(f"Row {r} isn't one of the year's booked rows.")
            continue
        own = {tx.paid_against or tx.account_code, tx.contra_account_code, SUSPENSE}
        for l in (l for l in fresh if l["code"] not in own):
            amount = abs(l["debit"] - l["credit"])
            if l["code"] not in (SALES_VAT, PURCHASE_VAT):
                problems.append(f"Row {r}: VAT goes only on 2200 or 2201, not {l['code']}.")
            elif not vat_registered:
                problems.append(f"Row {r}: the client isn't VAT registered, so no VAT goes on {l['code']}.")
            elif amount > _most_vat(tx.gross):
                problems.append(f"Row {r}: VAT of £{amount} is more than a sixth of its £{tx.gross}.")
    debits, credits = sum((l["debit"] for l in lines), ZERO), sum((l["credit"] for l in lines), ZERO)
    if debits != credits:
        problems.append(f"The batch's debits £{debits} and credits £{credits} do not balance.")
    return problems


def _key(lines: list[dict], after: Optional[str]) -> str:
    """The SHA-256 of the canonical JSON of the batch's lines and the batch it follows: a second posting of the same
    difference (a retry, or two at once) has the same key, which the unique index refuses. The batch it follows is
    in it so a row put back after being reversed can post the very lines it posted before."""
    body = {"after": after, "lines": [{"row_id": l["row_id"], "code": l["code"], "debit": str(l["debit"]),
                                       "credit": str(l["credit"]), "reverses": l["reverses"]} for l in lines]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _nothing(problems: Sequence[str] = ()) -> PostResult:
    return PostResult(batch_id=None, entries_added=0, reversals_added=0, total_debits=ZERO, total_credits=ZERO,
                      problems=list(problems))


def post(store: MongoStore, records: AgentRecords, client_id: int, year_start: dt.date, *,
         decision_ids: Sequence[str] = ()) -> PostResult:
    """Posts the year's changes since the last posting as one batch, recording the decisions behind it. Nothing is
    written when there is nothing to post, when the same batch was posted already, or when the batch fails its
    checks or rows need fixing (then with the problems). entries_added counts the new lines, reversals_added the
    reversals. Postings are not locked against each other: two at once with different differences both post."""
    db = records.db
    _ensure_indexes(db)
    try:
        settings, ruled = _ruled(store, client_id, year_start)
    except InvalidTransactions as exc:
        return _nothing(exc.problems)
    posted = _posted(db, client_id, year_start)
    lines = _new_lines(wanted_journal(store, client_id, year_start, ruled=ruled), posted)
    if not lines:
        return _nothing()
    problems = _problems(lines, {r: tx for r, tx, _ in ruled}, settings.vat_registered)
    if problems:
        return _nothing(problems)
    debits, credits = sum((l["debit"] for l in lines), ZERO), sum((l["credit"] for l in lines), ZERO)
    reversals = sum(1 for l in lines if l["reverses"])
    batch_id, now, seq, year = uuid4().hex, now_iso(), time.time_ns(), year_start.isoformat()
    try:
        db.posting_batches.insert_one({
            "_id": batch_id, "client_id": client_id, "year_start": year, "key": _key(lines, posted.last_batch),
            "after": posted.last_batch, "decision_ids": list(decision_ids), "entries": len(lines) - reversals,
            "reversals": reversals, "total_debits": _stored(debits), "total_credits": _stored(credits),
            "created_at": now, "seq": seq})
    except DuplicateKeyError:   # this very batch was posted meanwhile
        return _nothing()
    try:
        db.ledger_entries.insert_many([
            {"_id": uuid4().hex, "client_id": client_id, "year_start": year, "batch_id": batch_id,
             "row_id": l["row_id"], "code": l["code"], "description": l["description"], "debit": _stored(l["debit"]),
             "credit": _stored(l["credit"]), "reverses": l["reverses"], "created_at": now, "seq": seq + k}
            for k, l in enumerate(lines)])
    except Exception:   # take back what was written of the batch, so it can be posted again
        db.ledger_entries.delete_many({"batch_id": batch_id})
        db.posting_batches.delete_one({"_id": batch_id})
        raise
    return PostResult(batch_id=batch_id, entries_added=len(lines) - reversals, reversals_added=reversals,
                      total_debits=debits, total_credits=credits)


def posted_trial_balance(store: MongoStore, records: AgentRecords, client_id: int,
                         year_start: dt.date) -> PostedTrialBalance:
    """The year's opening balances plus every entry posted for it, laid out as posting.trial_balance lays out the
    rows', with the rows held in Suspense that are posted there. InvalidTransactions while rows need fixing, as the
    rows' trial balance raises (which rows are held is worked out from them)."""
    opening = books.opening(store, client_id, year_start)
    posted = _posted(records.db, client_id, year_start)
    journal = [JournalLine(transaction=d["row_id"], code=d["code"], description=d["description"], debit=d["debit"],
                           credit=d["credit"]) for d in posted.entries]
    _, ruled = _ruled(store, client_id, year_start)
    suspense = [SuspenseItem(row_id=r, description=tx.description, gross=tx.gross, needs=rule.needs)
                for r, tx, rule in ruled if rule.action == "hold" and posted.net.get(r, {}).get(SUSPENSE)]
    return PostedTrialBalance(**summarise(journal, opening).model_dump(), suspense=suspense)


def unposted_changes(store: MongoStore, records: AgentRecords, client_id: int, year_start: dt.date) -> int:
    """How many rows would post something on the next posting: their wanted lines differ from what is posted for
    them, or they are posted and no longer booked. InvalidTransactions while rows need fixing."""
    return len(_changed(wanted_journal(store, client_id, year_start), _posted(records.db, client_id, year_start)))
