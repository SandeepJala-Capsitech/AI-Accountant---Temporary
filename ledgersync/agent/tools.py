"""The row tools the agent works with. They are the only way the agent reads or changes a client's rows.

`row_tools` builds them for one job: the client's ID and the job's ID are closed over, so they never appear in a
tool's arguments and the model can't name another client's rows. The agent can change only a row's account,
document type, Include, Link and Not the same. There is no argument for an amount, a date or VAT, and putting
a row back is refused when it would undo a person's edit of one.

No tool raises a LedgerSyncError: each returns {"ok": False, "error": <message>} instead, a message a person (or
the model) can act on.

Every change is saved first and then appended to the audit log. If the process stops between the two, the row
is changed and has no entry, and the replay finds nothing left to change, so it does not add one."""
import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.tools import BaseTool, tool

from ledgersync import ledger as books
from ledgersync.accounts import choosable
from ledgersync.errors import LedgerSyncError, NotFound
from ledgersync.models import DOCUMENT_TYPES, Ledger, RowPatch, SavedRow, Settlement, Transaction
from ledgersync.money import to_money
from ledgersync.records.audit import AuditLog
from ledgersync.records.jobs import JobRecords
from ledgersync.store import EDITABLE, StoredRow
from ledgersync.store_mongo import MongoStore

# What the agent may change on a row, and what putting a row back may change.
CHANGEABLE = ("account_code", "document_type", "include", "link", "apart_from")
REVERTIBLE = {"account_code", "document_type"}
MAX_ROWS = 100          # the most rows one call returns
MAX_ACCOUNTS = 10
NOT_FOUND = "This row was not found."


@dataclass
class ToolDeps:
    """What the tools (and the steps that use them) are built on."""

    store: MongoStore
    records: JobRecords
    audit: AuditLog


def _failed(exc: LedgerSyncError) -> dict:
    return {"ok": False, "error": exc.message}


def _check_why(reasoning: str, confidence: Optional[float] = None) -> Optional[str]:
    """What is wrong with the reasoning (1 to 500 characters) or the confidence (0 to 1), or None."""
    if not 1 <= len(reasoning.strip()) <= 500:
        return "Give your reasoning in 1 to 500 characters."
    if confidence is not None and not 0 <= confidence <= 1:
        return "Confidence must be from 0 to 1."
    return None


def _page(limit: int) -> int:
    return max(1, min(limit, MAX_ROWS))


def _value(tx: Transaction, field: str) -> Any:
    """A row's field as the audit log keeps it: plain JSON (an enum as its text, lists copied)."""
    value = getattr(tx, field)
    if field in ("link", "apart_from"):
        return None if value is None else list(value)
    return getattr(value, "value", value)


def _canon(field: str, value: Any) -> Any:
    """A value in a form that compares equal however it was written: Link and Not the same as sets (their order
    means nothing; None, automatic, is not the empty list), amounts as pennies, dates as text, enums as text."""
    if field in ("link", "apart_from"):
        return None if value is None else frozenset(value)
    if field == "include":
        return bool(value)
    if field in ("gross", "vat"):
        return to_money(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return getattr(value, "value", value)


def _differs(field: str, one: Any, other: Any) -> bool:
    return _canon(field, one) != _canon(field, other)


def _put_back(row: StoredRow) -> set[str]:
    """The fields putting this row back would change: where it differs from what was read before its first edit."""
    if row.original is None:
        return set()
    return {f for f in EDITABLE if _differs(f, row.original.get(f), getattr(row.tx, f))}


def _short(row: SavedRow, ledger: Ledger) -> dict:
    """A row as the tools list it, money as exact strings."""
    upload = next((u for u in ledger.uploads if u.id == row.upload_id), None)
    return {"row_id": row.id, "date": row.date.isoformat() if row.date else None, "description": row.description,
            "counterparty": row.counterparty, "direction": row.direction.value, "gross": str(row.gross),
            "vat": None if row.vat is None else str(row.vat), "account_code": row.account_code,
            "document_type": row.document_type,
            "upload": {"id": row.upload_id, "name": upload.name if upload else ""},
            "issues": [{"code": i.code, "message": i.message, "severity": i.severity, "related": list(i.related)}
                       for i in row.issues]}


def _settlement(found: Settlement) -> dict:
    return {"ref": found.ref, "amount": str(found.amount), "date": found.date.isoformat() if found.date else None,
            "description": found.description, "kind": found.kind}


def _row(ledger: Ledger, row_id: int) -> SavedRow:
    found = next((r for r in ledger.transactions if r.id == row_id), None)
    if found is None:
        raise NotFound(NOT_FOUND)
    return found


def row_tools(deps: ToolDeps, client_id: int, job_id: str) -> dict[str, BaseTool]:
    """The six row tools for one job, keyed by name. Each is for this client's rows alone, and its audit entries
    carry this job's ID."""
    store, audit = deps.store, deps.audit

    def ledger() -> Ledger:
        return books.build(store, client_id)

    def stored(row_id: int) -> tuple[list[StoredRow], Optional[StoredRow]]:
        rows = store.rows(client_id)
        return rows, next((r for r in rows if r.id == row_id), None)

    @tool(parse_docstring=True)
    def lookup_account(query: str) -> list[dict]:
        """Find accounts this client's rows can be booked to, by the start of the code or by words in the name.

        Args:
            query: Part of an account code, such as "75", or words from its name, such as "telephone internet".
        """
        try:
            words = query.lower().split()
            if not words:
                return {"ok": False, "error": "Enter part of an account's code or name."}
            accounts = choosable(store.get_client(client_id).business_type)
            found = [a for a in accounts
                     if a.code.startswith(query.strip()) or all(w in a.name.lower() for w in words)]
            return [{"code": a.code, "name": a.name, "type": a.type.value} for a in found[:MAX_ACCOUNTS]]
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def list_rows_to_review(after_row_id: int = 0, limit: int = 20) -> dict:
        """List the rows that still need a look (they have an error or a warning), by row ID, a page at a time.

        Gives {"rows": [...], "more": true or false}. When "more" is true, call again with after_row_id set to
        the last row_id you were given.

        Args:
            after_row_id: Only rows with a higher row ID than this. Use 0 for the first page.
            limit: How many rows to give, up to 100.
        """
        try:
            built = ledger()
            due = sorted((r for r in built.transactions if books.needs_review(r) and r.id > after_row_id),
                         key=lambda r: r.id)
            size = _page(limit)
            return {"rows": [_short(r, built) for r in due[:size]], "more": len(due) > size}
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def get_row(row_id: int) -> dict:
        """Read one row in full: what it says, its issues, and what it is matched with.

        Gives the row's fields, whether it is included, its link and its not-the-same list, then candidates (what a
        bank line could pay), pays (what it pays), paid_by (what paid a document), copy_of, recorded_by and
        claimed_in, and related_rows: the rows its issues are about.

        Args:
            row_id: The row's ID.
        """
        try:
            built = ledger()
            row = _row(built, row_id)
            related = sorted({i for found in row.issues for i in found.related} - {row_id})
            by_id = {r.id: r for r in built.transactions}
            return {**_short(row, built), "include": row.include,
                    "link": None if row.link is None else list(row.link), "apart_from": list(row.apart_from),
                    "candidates": [[_settlement(s) for s in group] for group in row.candidates],
                    "pays": [_settlement(s) for s in row.pays], "paid_by": [_settlement(s) for s in row.paid_by],
                    "copy_of": _settlement(row.copy_of) if row.copy_of else None,
                    "recorded_by": _settlement(row.recorded_by) if row.recorded_by else None,
                    "claimed_in": _settlement(row.claimed_in) if row.claimed_in else None,
                    "related_rows": [_short(by_id[i], built) for i in related if i in by_id]}
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def update_row(row_id: int, reasoning: str, confidence: float, account_code: Optional[str] = None,
                   document_type: Optional[str] = None, include: Optional[bool] = None,
                   link: Optional[list[str]] = None, apart_from: Optional[list[int]] = None) -> dict:
        """Change one row's account, document type, Include, Link or Not the same. Give only what should change.

        This can never change an amount, a date or VAT: those are what the document shows. Every change is
        recorded with your reasoning. Asking for what a row already has changes nothing.

        Args:
            row_id: The row's ID.
            reasoning: Why, in 1 to 500 characters. A person reads it.
            confidence: How sure you are, from 0 to 1.
            account_code: The account to book it to, a code from lookup_account.
            document_type: What kind of document it is: receipt, invoice, expense_claim, statement, agent_statement, quote, pro_forma, purchase_order, remittance_advice, supplier_statement or other.
            include: Whether to book a document that is not a transaction (a quote, say).
            link: On a bank line, the refs of the documents it pays; an empty list says it pays none.
            apart_from: Row IDs of other rows that are not the same as this one, so they are never paired.
        """
        try:
            problem = _check_why(reasoning, confidence)
            if problem:
                return {"ok": False, "error": problem}
            given = {name: value for name, value in (
                ("account_code", account_code), ("document_type", document_type), ("include", include),
                ("link", link), ("apart_from", apart_from)) if value is not None}
            if not given:
                return {"ok": False, "error": "Say what to change: " + ", ".join(CHANGEABLE[:-1])
                                              + f" or {CHANGEABLE[-1]}."}
            if document_type is not None and document_type not in DOCUMENT_TYPES:
                return {"ok": False, "error": "Choose a document type from: " + ", ".join(DOCUMENT_TYPES) + "."}
            before: dict = {}
            _, current = stored(row_id)
            if current is not None:
                before = {f: _value(current.tx, f) for f in given if _differs(f, given[f], getattr(current.tx, f))}
                if not before:   # a replay, or nothing to do: nothing to save and nothing to audit
                    built = ledger()
                    return {"ok": True, "row": _short(_row(built, row_id), built)}
            built = books.change_row(store, client_id, row_id, RowPatch(**given))   # NotFound for a row not here
            row = _row(built, row_id)
            audit.append(actor="agent", action="update_row", client_id=client_id, job_id=job_id,
                         target={"row_id": row_id}, before=before, after={f: _value(row, f) for f in before},
                         note=f"{reasoning.strip()} (confidence {confidence})")
            return {"ok": True, "row": _short(row, built)}
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def revert_row(row_id: int, reasoning: str) -> dict:
        """Put a row back to what was read from its document, undoing the changes made to its account and document type.

        A row that was never changed is left as it is. A row whose amount, date or VAT a person edited can't be
        put back by the agent.

        Args:
            row_id: The row's ID.
            reasoning: Why, in 1 to 500 characters. A person reads it.
        """
        try:
            problem = _check_why(reasoning)
            if problem:
                return {"ok": False, "error": problem}
            before: dict = {}
            rows, current = stored(row_id)
            if current is not None:
                before = {f: _value(current.tx, f) for f in _put_back(current)}
                if not before:   # never changed, or already as it was read
                    built = ledger()
                    return {"ok": True, "row": _short(_row(built, row_id), built)}
                ref = current.tx.document_ref   # the store puts back every row of the same document too
                same_document = [r for r in rows if r.id == row_id or (ref and r.tx.document_ref == ref)]
                if any(_put_back(r) - REVERTIBLE for r in same_document):
                    return {"ok": False, "error": (
                        "A person has edited this row's amount, date, VAT or other details, which the agent can't "
                        "change, and putting it back would undo that. Leave it for a person.")}
            built = books.change_row(store, client_id, row_id, RowPatch(revert=True))   # NotFound for a row not here
            row = _row(built, row_id)
            audit.append(actor="agent", action="revert_row", client_id=client_id, job_id=job_id,
                         target={"row_id": row_id}, before=before, after={f: _value(row, f) for f in before},
                         note=reasoning.strip())
            return {"ok": True, "row": _short(row, built)}
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def list_rows(account_code: Optional[str] = None, upload_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        """List this client's rows by row ID, for one account, one document, or all of them.

        Args:
            account_code: Only rows booked to this account.
            upload_id: Only rows from this document (the id in a row's upload).
            limit: How many rows to give, up to 100.
        """
        try:
            built = ledger()
            wanted = sorted((r for r in built.transactions
                             if (account_code is None or r.account_code == account_code)
                             and (upload_id is None or r.upload_id == upload_id)), key=lambda r: r.id)
            return [_short(r, built) for r in wanted[:_page(limit)]]
        except LedgerSyncError as exc:
            return _failed(exc)

    return {t.name: t for t in (lookup_account, list_rows_to_review, get_row, update_row, revert_row, list_rows)}
