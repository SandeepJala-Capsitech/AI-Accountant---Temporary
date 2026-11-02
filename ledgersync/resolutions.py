"""The accountant chat's changes to a client's rows (design of 2026-11-02). The agent chooses an action and cites its
evidence; these services check both against the rules (R0 for every action, R1–R5 for each), then make the change or
refuse it. The model never decides: whatever the evidence doesn't support is refused, and every attempt, applied or
refused, is recorded as a decision."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Callable, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field

from . import books
from .accounts import BY_CODE, AccountType, choosable
from .checks import long_date
from .errors import InvalidInput, LedgerSyncError, NotFound
from .ledger import bank_choices, ensure_open, require_active
from .matching import CARD_DAYS, names_match
from .models import Decision, DecisionDraft, Evidence, SavedRow, Transaction
from .money import PENNY, ZERO, VatTreatment, vat_in_gross
from .records import AgentRecords
from .store_mongo import MongoStore
from .years import year_index

# How far each kind of record can be trusted, the better source first: the document itself, then the bank line,
# then an expenses sheet, a spreadsheet or a hand entry (anything else).
RANK = {"invoice": 3, "receipt": 3, "payroll": 3, "statement": 2, "expense_claim": 1}

# The text of an upload's pages, by its id (documents.text_of).
Texts = Callable[[int], list[str]]


class KeepOne(BaseModel):
    action: Literal["keep_one"]
    keep_row_id: int
    drop_row_ids: list[int]


class UsePrimary(BaseModel):
    action: Literal["use_primary"]
    primary_row_id: int
    other_row_id: int


class SplitPart(BaseModel):
    description: str
    account_code: str
    gross: Decimal
    rate: VatTreatment


class SplitVat(BaseModel):
    action: Literal["split_vat"]
    parts: list[SplitPart]


class VatFromDocument(BaseModel):
    action: Literal["set_vat_from_document"]
    vat: Decimal


Resolution = Annotated[Union[KeepOne, UsePrimary, SplitVat, VatFromDocument], Field(discriminator="action")]

# Issues that say two rows are one transaction recorded twice, and the matches that pair such rows.
_DUPLICATE_ISSUES = {"duplicate_document", "booked_twice", "total_mismatch", "date_conflict"}
# Rows a bank line can pay, so a bank line that disagrees with one is linked to it rather than left out.
_UNPAYABLE = {"receipt", "statement", "payroll"}


class _Refused(Exception):
    """A check failed; the message says why, as the decision records it."""


@dataclass
class _Change:
    """What an action does: the fields it sets on rows, by row id, or a row it splits into parts."""
    rule: str
    touched: list[int]                                   # the rows it is about, as the decision lists them
    patches: dict[int, dict] = field(default_factory=dict)   # JSON values, as the store keeps them
    split: Optional[tuple[int, list[Transaction]]] = None


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _normalised(text: str) -> str:
    """Text with each run of whitespace one space, in lower case, so a quote is found however it was spaced."""
    return " ".join(text.split()).lower()


def _kind(tx: Transaction) -> str:
    return {"expense_claim": "expenses sheet line", "statement": "bank line"}.get(tx.document_type,
                                                                                    tx.document_type.replace("_", " "))


def _rank(tx: Transaction) -> int:
    return RANK.get(tx.document_type, 1)


def _who(tx: Transaction) -> str:
    """Who a row is with: an expenses sheet line names the shop in its description (its counterparty is the
    claimant)."""
    return tx.description if tx.document_type == "expense_claim" else (tx.counterparty or tx.description)


def _near(a: SavedRow, b: SavedRow) -> bool:
    """The same counterparty, and dated within a few days of each other."""
    return (a.date is not None and b.date is not None and abs(a.date - b.date) <= CARD_DAYS
            and names_match(_who(a), _who(b)))


def _paired(a: SavedRow, b: SavedRow, codes: Optional[set[str]] = None) -> bool:
    """Matching says the rows are about each other: an issue on either names the other (one of `codes`, if given),
    or one is a copy of the other's document, or is recorded by it. (Not claimed_in: that names a whole expenses
    sheet, not the line.)"""
    def one_way(x: SavedRow, y: SavedRow) -> bool:
        records = {s.ref for s in (x.copy_of, x.recorded_by) if s}
        return y.document_ref in records or any(y.id in i.related and (codes is None or i.code in codes)
                                                for i in x.issues)
    return one_way(a, b) or one_way(b, a)


def _usable(row: SavedRow, kept: bool = True) -> None:
    """Refuses a row a split replaced, or, for a row the change keeps, one left out of the books."""
    if row.superseded_by:
        raise _Refused(f"Row {row.id} was replaced by a split.")
    if kept and row.excluded:
        raise _Refused(f"Row {row.id} is already out of the books.")


# ─── The rule of each action ───────────────────────────────────────────────────

def _keep_one(rows: dict[int, SavedRow], row_id: int, r: KeepOne, **_) -> _Change:
    """R2: the rows are one transaction (a duplicate issue links them, or the same amount and counterparty within a
    few days), and the kept row ranks at least as high as each dropped one. The dropped rows leave the books."""
    named = [r.keep_row_id, *r.drop_row_ids]
    if not r.drop_row_ids or len(set(named)) < len(named) or row_id not in named:
        raise _Refused(f"Keep one row and drop others, row {row_id} among them.")
    keep = rows[r.keep_row_id]
    _usable(keep)
    for drop in (rows[n] for n in r.drop_row_ids):
        _usable(drop, kept=False)
        if not (_paired(keep, drop, _DUPLICATE_ISSUES) or (keep.gross == drop.gross and _near(keep, drop))):
            raise _Refused(f"Nothing shows rows {keep.id} and {drop.id} are the same transaction: no duplicate issue "
                           "links them, and they differ in amount, counterparty or date.")
        if _rank(drop) > _rank(keep):
            raise _Refused(f"Row {drop.id}, a {_kind(drop)}, ranks above the {_kind(keep)} kept.")
    return _Change(rule=f"R2 Duplicate: kept the {_kind(keep)}", touched=named,
                   patches={n: {"excluded": True} for n in r.drop_row_ids})


def _use_primary(rows: dict[int, SavedRow], row_id: int, r: UsePrimary, **_) -> _Change:
    """R4: the rows conflict (an issue links them, or the same counterparty within a few days) and the primary ranks
    higher. The other leaves the books; a bank line instead pays the primary, unless the primary is a receipt, which
    books the payment itself."""
    if r.primary_row_id == r.other_row_id or row_id not in (r.primary_row_id, r.other_row_id):
        raise _Refused(f"Name two different rows, row {row_id} one of them.")
    primary, other = rows[r.primary_row_id], rows[r.other_row_id]
    _usable(primary)
    _usable(other, kept=False)
    if not (_paired(primary, other) or _near(primary, other)):
        raise _Refused(f"Rows {primary.id} and {other.id} don't conflict: nothing links them, and they aren't from the "
                       "same counterparty within a few days.")
    if _rank(primary) <= _rank(other):
        raise _Refused(f"The {_kind(primary)} doesn't rank above the {_kind(other)}.")
    links = other.document_type == "statement" and primary.document_type not in _UNPAYABLE
    patch = {"link": [primary.document_ref]} if links else {"excluded": True}
    return _Change(rule=f"R4 Conflict: used the {_kind(primary)}", touched=[primary.id, other.id],
                   patches={other.id: patch})


def _split_vat(rows: dict[int, SavedRow], row_id: int, r: SplitVat, *, b, **_) -> _Change:
    """R3: two parts or more, on accounts the client can use, adding up exactly to the row, with VAT at their rates
    within a penny a part of the VAT the document shows. The parts replace the row."""
    row = rows[row_id]
    _usable(row)
    if len(r.parts) < 2:
        raise _Refused("A split needs two parts or more.")
    allowed = {a.code for a in choosable(b.client.business_type, bank_choices(b.client))}
    for part in r.parts:
        if part.account_code not in allowed:
            raise _Refused(f"{part.account_code} isn't an account {b.client.name} can use.")
        if part.gross <= 0 or not part.description.strip():
            raise _Refused("Give each part a description and an amount above zero.")
    total = sum((p.gross for p in r.parts), ZERO)
    if total != row.gross:
        raise _Refused(f"The parts add up to £{total}; the row is £{row.gross}.")
    if row.vat is None:
        raise _Refused("The row shows no VAT to check the parts against.")
    vat = sum((vat_in_gross(p.gross, p.rate) for p in r.parts), ZERO)
    if abs(vat - row.vat) > PENNY * len(r.parts):
        raise _Refused(f"The parts' VAT is £{vat}; the document shows £{row.vat}.")
    original = next(s.tx for s in b.stored if s.id == row_id)
    parts = [original.model_copy(update=dict(
        description=p.description.strip(), account_code=p.account_code, gross=p.gross,
        vat=vat_in_gross(p.gross, p.rate), vat_treatment=p.rate, excluded=False, superseded_by=None, decision_ids=[],
        issues=[])) for p in r.parts]
    return _Change(rule="R3 Mixed VAT: split by rate", touched=[row_id], split=(row_id, parts))


def _vat_from_document(rows: dict[int, SavedRow], row_id: int, r: VatFromDocument, *, b, text, **_) -> _Change:
    """R5: VAT above zero and at most the sixth of the amount (as vat_in_gross rounds it), on an account VAT applies
    to, printed in the row's own document or that of the record it is paired with."""
    row = rows[row_id]
    _usable(row)
    if r.vat <= 0 or r.vat > vat_in_gross(row.gross, VatTreatment.STANDARD):
        raise _Refused(f"£{r.vat} can't be the VAT on £{row.gross}: at most a sixth of it, £"
                       f"{vat_in_gross(row.gross, VatTreatment.STANDARD)}.")
    account = BY_CODE.get(row.account_code)
    if not b.client.vat_registered or account is None or account.type in (AccountType.LIABILITY, AccountType.EQUITY):
        raise _Refused(f"No VAT is booked on {account.name if account else row.account_code} for {b.client.name}.")
    refs = {s.ref for s in [row.recorded_by, *row.pays] if s}
    uploads = {row.upload_id} | {o.upload_id for o in rows.values() if o.document_ref in refs}
    printed = re.compile(rf"(?<![\d.,]){re.escape(f'{r.vat:.2f}')}(?!\d)")
    if not any(printed.search(text(u)) for u in uploads):
        raise _Refused(f"£{r.vat:.2f} isn't printed in the row's document.")
    return _Change(rule="R5 VAT from the document", touched=[row_id], patches={row_id: {"vat": f"{r.vat:.2f}"}})


_ACTIONS = {"keep_one": _keep_one, "use_primary": _use_primary, "split_vat": _split_vat,
            "set_vat_from_document": _vat_from_document}
_RULES = {"keep_one": "R2 Duplicate", "use_primary": "R4 Conflict", "split_vat": "R3 Mixed VAT",
          "set_vat_from_document": "R5 VAT from the document"}


def _named(row_id: int, resolution) -> list[int]:
    """The rows a resolution names, the row it was asked about among them."""
    extra = {"keep_one": lambda r: [r.keep_row_id, *r.drop_row_ids],
             "use_primary": lambda r: [r.primary_row_id, r.other_row_id]}.get(resolution.action, lambda r: [])
    return sorted({row_id, *extra(resolution)})


# ─── Making and recording the change ───────────────────────────────────────────

def _values(tx: Transaction, fields) -> dict:
    return tx.model_dump(mode="json", include=set(fields))


def _errors(rows) -> dict[int, dict[str, str]]:
    """Each row's error-level issues, by code."""
    return {r.id: {i.code: i.message for i in r.issues if i.severity == "error"} for r in rows}


def _apply(store: MongoStore, client_id: int, change: _Change, stored: dict[int, Transaction],
           decision_id: str) -> list[int]:
    """Makes the change, the rows it changes naming the decision; the ids of any parts a split added."""
    for row_id, patch in change.patches.items():
        store.patch_row(client_id, row_id, {**patch, "decision_ids": [*stored[row_id].decision_ids, decision_id]})
    if change.split:
        return store.add_split(client_id, change.split[0], change.split[1], decision_id)
    return []


def _undo(store: MongoStore, client_id: int, change: _Change, stored: dict[int, Transaction], parts: list[int]) -> None:
    """Puts the rows back as they were: the patched fields to their values, a split's row back in the books and its
    parts' upload removed."""
    for row_id, patch in change.patches.items():
        store.patch_row(client_id, row_id, _values(stored[row_id], [*patch, "decision_ids"]))
    if change.split:
        row_id = change.split[0]
        store.patch_row(client_id, row_id, _values(stored[row_id], ["superseded_by", "decision_ids"]))
        upload_id = next((r.upload_id for r in store.rows(client_id) if parts and r.id == parts[0]), None)
        if upload_id is not None:
            store.delete_upload(client_id, upload_id)


def _commit(store: MongoStore, records: AgentRecords, b, change: _Change, key: str, draft: dict) -> Decision:
    """Makes the change and records it applied; if the ledger then shows an error on a row it touched that wasn't
    there before, puts it back and records it refused (R0)."""
    stored = {s.id: s.tx for s in b.stored}
    before_errors = _errors(books._saved_rows(b, None)[0])
    decision_id = uuid4().hex
    parts = _apply(store, b.client.id, change, stored, decision_id)
    before = {str(n): _values(stored[n], p) for n, p in change.patches.items()}
    after = {str(n): dict(p) for n, p in change.patches.items()}
    if change.split:
        before[str(change.split[0])] = {"superseded_by": None}
        after[str(change.split[0])] = {"superseded_by": decision_id}
        for n, part in zip(parts, change.split[1]):
            after[str(n)] = _values(part, ["description", "account_code", "gross", "vat", "vat_treatment"])
    was = dict(before_errors)
    if change.split:   # a part starts with the errors of the row it replaces
        was.update({n: before_errors.get(change.split[0], {}) for n in parts})
    now = _errors(books.all_rows(store, b.client.id))
    for row_id in sorted({*change.touched, *parts}):
        new = [message for code, message in now.get(row_id, {}).items() if code not in was.get(row_id, {})]
        if new:
            _undo(store, b.client.id, change, stored, parts)
            return _record(records, key, draft, status="refused", before=before, after=before, rule=change.rule,
                           reason=f"It would leave row {row_id} with an error: {new[0]}")
    return _record(records, key, draft, status="applied", before=before, after=after, rule=change.rule,
                   row_ids=sorted({*change.touched, *parts}), id=decision_id)


def _record(records: AgentRecords, key: str, draft: dict, id: Optional[str] = None, **fields) -> Decision:
    return records.append_decision(DecisionDraft(**{**draft, **fields}), key, id=id)[0]


def resolve(store: MongoStore, records: AgentRecords, *, client_id: int, thread_id: Optional[str], actor: str,
            row_id: int, resolution: Resolution, evidence: list[Evidence], reason: str, texts: Texts) -> Decision:
    """Checks the resolution against its evidence and the rules, and makes it, or refuses it; either way it is
    recorded, once: asked again with the same rows and arguments, it returns the decision made the first time.
    R0, for every action: the rows are the client's, their year is open, each quote is in the text of its upload
    (whatever its spacing and case), each upload is the client's, and afterwards no row touched has a new error."""
    named = _named(row_id, resolution)
    key = _key(f"{client_id}|{named}|{resolution.model_dump_json()}")
    if (done := records.decision_by_key(client_id, key)) is not None:
        return done
    b = books._books(store, client_id)
    rows = {r.id: r for r in books._saved_rows(b, None)[0]}
    place = {s.id: k for k, s in enumerate(b.stored)}
    year = b.years[b.placed[place[row_id]]] if row_id in place else b.years[-1]
    uploads = {u["id"]: u["name"] for u in b.uploads}
    draft = dict(client_id=client_id, thread_id=thread_id, year_start=year.start, action=resolution.action,
                 rule=_RULES[resolution.action], row_ids=named, reason=reason, actor=actor,
                 evidence=[e.model_copy(update={"upload_name": uploads.get(e.upload_id, "")}) for e in evidence])
    pages: dict[int, str] = {}

    def text(upload_id: int) -> str:
        if upload_id not in pages:
            try:
                pages[upload_id] = _normalised(" ".join(texts(upload_id) or []))
            except LedgerSyncError:
                pages[upload_id] = ""
        return pages[upload_id]

    try:
        if b.client.archived:
            raise _Refused(f"{b.client.name} is archived.")
        stray = [n for n in named if n not in rows]
        if stray:
            raise _Refused(f"Row {stray[0]} isn't one of {b.client.name}'s rows.")
        try:
            ensure_open(b.client, [b.years[b.placed[place[n]]].start for n in named])
        except LedgerSyncError as exc:
            raise _Refused(exc.message) from exc
        if not evidence:
            raise _Refused("No evidence was cited.")
        for e in evidence:
            if e.upload_id not in uploads:
                raise _Refused(f"Upload {e.upload_id} isn't one of {b.client.name}'s documents.")
            if not _normalised(e.quote) or _normalised(e.quote) not in text(e.upload_id):
                raise _Refused(f"\"{e.quote}\" isn't in {uploads[e.upload_id]}.")
        change = _ACTIONS[resolution.action](rows, row_id, resolution, b=b, text=text)
    except _Refused as refused:
        return _record(records, key, draft, status="refused", reason=str(refused))
    return _commit(store, records, b, change, key, draft)


def set_date(store: MongoStore, records: AgentRecords, *, client_id: int, thread_id: Optional[str], row_id: int,
             date: dt.date, question_id: str, texts: Optional[Texts] = None) -> Decision:
    """R1 from a person's answer to a question: dates the row, the person being the evidence, and records it. The
    date must be within the client's books, from the first day of its first year to the end of its latest year;
    otherwise InvalidInput, so the question is asked again. texts is accepted for a call like resolve's: an answer
    needs no document."""
    b = books._books(store, client_id)
    require_active(b.client)
    place = {s.id: k for k, s in enumerate(b.stored)}
    if row_id not in place:
        raise NotFound("This row was not found.")
    start, end = b.years[0].start, b.years[-1].end
    if not start <= date <= end:
        raise InvalidInput(f"That date is outside the books, which run from {long_date(start)} to {long_date(end)}.")
    ensure_open(b.client, [b.years[b.placed[place[row_id]]].start, date])
    key = _key(f"{client_id}|set_date|{row_id}|{date.isoformat()}|{question_id}")
    if (done := records.decision_by_key(client_id, key)) is not None:
        return done
    draft = dict(client_id=client_id, thread_id=thread_id, actor="user", action="set_date", row_ids=[row_id],
                 year_start=b.years[year_index(b.client.first_year_end, date)].start, rule="R1 Date from the person",
                 reason=f"The person answered question {question_id}: {long_date(date)}.")
    change = _Change(rule=draft["rule"], touched=[row_id], patches={row_id: {"date": date.isoformat()}})
    return _commit(store, records, b, change, key, draft)


def record_automatic(store: MongoStore, records: AgentRecords, *, client_id: int, year_start: dt.date,
                     thread_id: Optional[str]) -> list[Decision]:
    """Records the year's rows that matching dated from the bank (R1): each undated receipt paired with the bank line
    that paid it takes the line's date, with the line as evidence. Matching already dates them, so no row is changed.
    Returns the decisions it added; run again, it adds none."""
    built = books.ledger(store, client_id, year_start)
    names = {u.id: u.name for u in built.uploads}
    added = []
    for row in built.transactions:
        if row.date is None or not any(i.code == "date_from_bank" for i in row.issues):
            continue
        refs = {p.ref for p in row.paid_by}
        line = next((r for r in built.transactions if r.document_type == "statement" and r.document_ref in refs), None)
        if line is None:
            continue
        day = row.date.isoformat()
        decision, new = records.append_decision(DecisionDraft(
            client_id=client_id, thread_id=thread_id, year_start=year_start, action="set_date",
            rule="R1 Date from the bank", status="applied", row_ids=[row.id], actor="rules",
            evidence=[Evidence(upload_id=line.upload_id, upload_name=names.get(line.upload_id, ""),
                               quote=line.description)],
            reason=f"No date on the {_kind(row)}; dated {long_date(row.date)} from the bank line "
                   f"{line.description} for £{line.gross}.",
            before={str(row.id): {"date": None}}, after={str(row.id): {"date": day}}),
            _key(f"{client_id}|auto-date|{row.id}|{day}"))
        if new:
            added.append(decision)
    return added
