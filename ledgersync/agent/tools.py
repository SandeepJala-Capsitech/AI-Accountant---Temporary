"""The tools the agent works with: the row tools, the only way it reads or changes a client's rows, and the two
exception tools, for what it can't settle (see `exception_tools`).

`row_tools` builds them for one job: the client's ID and the job's ID are closed over, so they never appear in a
tool's arguments and the model can't name another client's rows. The agent can change only a row's account,
document type, Include, Link and Not the same. There is no argument for an amount, a date or VAT, and the
store's own revert (back to the extraction, which would also undo a person's edits) is not how `revert_row` works:
it puts back what this job changed, read from this job's audit entries. Only once every edited row of the row's
document holds what it held as read does it call the store's revert, which then puts back equal values and
restores the reading's warnings, which the store clears when a row is first changed.

No tool raises a LedgerSyncError: each returns {"ok": False, "error": <message>} instead, a message a person (or
the model) can act on.

Every change is saved first and then appended to the audit log, one entry for every row it changed (the store
applies a document type or Include to every row of a document). The entries come from comparing the rows
before and after the save, so the log says what the rows hold. If the process stops between the save and the
entries, the rows are changed and have no entry, and the replay finds nothing left to change, so it adds none."""
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.tools import BaseTool, tool

from ledgersync import ledger as books
from ledgersync.accounts import choosable
from ledgersync.blobs import BlobStore, GridFSBlobStore
from ledgersync.config import Settings
from ledgersync.errors import LedgerSyncError, NotFound
from ledgersync.models import DOCUMENT_TYPES, Ledger, RowPatch, SavedRow, Settlement, Transaction
from ledgersync.money import to_money
from ledgersync.notify import ConsoleNotifier, Notifier
from ledgersync.records.audit import AuditLog
from ledgersync.records.exceptions import DocumentRequests, Exceptions
from ledgersync.records.jobs import JobRecords
from ledgersync.store import EDITABLE, StoredRow
from ledgersync.store_mongo import MongoStore

# What the agent may change on a row.
CHANGEABLE = ("account_code", "document_type", "include", "link", "apart_from")
MAX_ROWS = 100          # the most rows one call returns
MAX_ACCOUNTS = 10
NOT_FOUND = "This row was not found."


@dataclass
class ToolDeps:
    """What the tools (and the steps that use them) are built on. Left out, the exceptions, the document requests
    and the original files are kept in the store's database, emails go to the log, and the settings are the
    defaults."""

    store: MongoStore
    records: JobRecords
    audit: AuditLog
    exceptions: Optional[Exceptions] = None
    requests: Optional[DocumentRequests] = None
    notifier: Optional[Notifier] = None
    blobs: Optional[BlobStore] = None
    settings: Optional[Settings] = None

    def __post_init__(self):
        if self.exceptions is None:
            self.exceptions = Exceptions(self.store.db)
        if self.requests is None:
            self.requests = DocumentRequests(self.store.db)
        if self.notifier is None:
            self.notifier = ConsoleNotifier()
        if self.blobs is None:
            self.blobs = GridFSBlobStore(self.store.db)
        if self.settings is None:
            self.settings = Settings()


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

    def as_read(row_id: int) -> bool:
        """Whether the row's document is back to how it was read: some row of it was edited (it keeps what it held
        before its first edit), and every such row holds that again. A document is the rows with the row's
        document reference, as the store spreads a whole-document change."""
        rows = store.rows(client_id)
        mine = next((r for r in rows if r.id == row_id), None)
        if mine is None:
            return False
        ref = mine.tx.document_ref
        edited = [r for r in rows if r.original is not None and (r.id == row_id or (ref and r.tx.document_ref == ref))]
        return bool(edited) and not any(_differs(f, r.original.get(f), getattr(r.tx, f)) for r in edited
                                        for f in EDITABLE)

    def record(action: str, note: str, rows_before: list[StoredRow], built: Ledger) -> None:
        """One audit entry for every row whose agent-editable fields differ from before the save, in row ID
        order, each with only the fields that changed."""
        was = {r.id: r.tx for r in rows_before}
        for now in sorted(built.transactions, key=lambda r: r.id):
            then = was.get(now.id)
            changed = [f for f in CHANGEABLE if then is not None and _differs(f, getattr(then, f), getattr(now, f))]
            if changed:
                audit.append(actor="agent", action=action, client_id=client_id, job_id=job_id,
                             target={"row_id": now.id}, before={f: _value(then, f) for f in changed},
                             after={f: _value(now, f) for f in changed}, note=note)

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
        recorded with your reasoning. Asking for what a row already has changes nothing. A document type or
        Include given for one line of a document applies to all its lines.

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
            rows, current = stored(row_id)
            if current is None:
                raise NotFound(NOT_FOUND)
            # Only what differs from the row now: a replay, or a field already as asked, writes nothing (a document
            # type or Include is applied to every row of the document, so a field sent again could overwrite theirs).
            changes = {f: v for f, v in given.items() if _differs(f, v, getattr(current.tx, f))}
            if not changes:
                built = ledger()
                return {"ok": True, "row": _short(_row(built, row_id), built)}
            built = books.change_row(store, client_id, row_id, RowPatch(**changes))
            record("update_row", f"{reasoning.strip()} (confidence {confidence})", rows, built)
            return {"ok": True, "row": _short(_row(built, row_id), built)}
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def revert_row(row_id: int, reasoning: str) -> dict:
        """Undo what you changed on a row in this job, putting its account, document type, Include, Link and Not the same back to what they were before your first change.

        A row you haven't changed is left as it is, and so is anything a person chose. Amounts, dates and VAT are never touched.

        Args:
            row_id: The row's ID.
            reasoning: Why, in 1 to 500 characters. A person reads it.
        """
        try:
            problem = _check_why(reasoning)
            if problem:
                return {"ok": False, "error": problem}
            rows, current = stored(row_id)
            if current is None:
                raise NotFound(NOT_FOUND)
            # What each field held before this job's first change to the row, from the audit log.
            first: dict[str, Any] = {}
            for entry in audit.entries(client_id=client_id, job_id=job_id):
                if entry["target"] == {"row_id": row_id}:
                    for field, value in (entry["before"] or {}).items():
                        if field in CHANGEABLE:
                            first.setdefault(field, value)
            undo = {f: v for f, v in first.items() if _differs(f, v, getattr(current.tx, f))}
            built = None
            if undo:
                built = books.change_row(store, client_id, row_id, RowPatch(**undo))
                record("revert_row", reasoning.strip(), rows, built)
            if first and as_read(row_id):
                # The first change to a row clears the reading's warnings (the store counts it as a person's review).
                # With every edited row of the document back as read, the store's revert only puts back values that
                # are already there, so it changes nothing the audit log tracks, and brings the warnings back. When a
                # person edited a row of the document before the job, this is left alone: it would undo their edit.
                built = books.change_row(store, client_id, row_id, RowPatch(revert=True))
            built = built or ledger()
            return {"ok": True, "row": _short(_row(built, row_id), built)}
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


# What the client reads of a request goes into a fixed email, so it must be short plain text: no link or address
# a document's text could have talked the agent into sending.
_NOT_PLAIN = re.compile(r"http|www\.|@", re.IGNORECASE)
REASON_RULE = "Keep the reason under 200 characters, as plain text without links or email addresses."
TYPE_RULE = "Name the document in 1 to 60 characters, as plain text without links or email addresses."
PERIOD_RULE = "Give the period in 1 to 40 characters, as plain text without links or email addresses."
NO_EXCEPTION = "Give the exception_id that log_exception gave you for this job."


def _one_line(text: str) -> str:
    """The text on one line: line breaks, tabs and runs of spaces become single spaces."""
    return " ".join(text.split())


def _plain(text: str, longest: int) -> bool:
    return 1 <= len(text) <= longest and not _NOT_PLAIN.search(text)


def exception_tools(deps: ToolDeps, client_id: int, job_id: str, stage: str) -> dict[str, BaseTool]:
    """The two tools for what the agent can't settle, keyed by name, for one job's stage: `log_exception` and
    `request_client_document`. Each new exception or request is audited once; asking again for one already saved
    returns it and writes nothing."""
    exceptions, requests, audit = deps.exceptions, deps.requests, deps.audit

    @tool(parse_docstring=True)
    def log_exception(description: str, assumption: str, confidence: float, row_ids: list[int] = []) -> dict:
        """Record something you couldn't settle and the assumption you made instead, then carry on with the job.

        Log one whenever you're unsure, with a confidence below 0.7. Logging the same description for the same rows
        again gives the same exception_id and changes nothing.

        Args:
            description: What couldn't be settled, in 1 to 500 characters, such as "No invoice for the £1,200.00 paid to Acme". A person reads it.
            assumption: What you did instead, in 1 to 500 characters, such as "Booked as 5000 Purchases".
            confidence: How sure you are of the assumption, from 0 to 1.
            row_ids: The IDs of the rows it is about, if any.
        """
        try:
            description, assumption = description.strip(), assumption.strip()
            if not 1 <= len(description) <= 500:
                return {"ok": False, "error": "Describe what couldn't be settled in 1 to 500 characters."}
            if not 1 <= len(assumption) <= 500:
                return {"ok": False, "error": "Give the assumption you made in 1 to 500 characters."}
            if not 0 <= confidence <= 1:
                return {"ok": False, "error": "Confidence must be from 0 to 1."}
            unknown = sorted(set(row_ids) - {r.id for r in deps.store.rows(client_id)})
            if unknown:
                return {"ok": False, "error": f"These rows weren't found: {', '.join(map(str, unknown))}."}
            logged, new = exceptions.log_once(client_id=client_id, job_id=job_id, stage=stage,
                                              description=description, assumption=assumption,
                                              confidence=confidence, row_ids=row_ids)
            if new:
                audit.append(actor="agent", action="log_exception", client_id=client_id, job_id=job_id,
                             target={"exception_id": logged["id"]}, before=None,
                             after={"stage": stage, "description": description, "assumption": assumption,
                                    "confidence": logged["confidence"], "row_ids": logged["row_ids"]})
            return {"ok": True, "exception_id": logged["id"]}
        except LedgerSyncError as exc:
            return _failed(exc)

    @tool(parse_docstring=True)
    def request_client_document(document_type: str, period: str, reason: str, exception_id: str) -> dict:
        """Ask the client for a missing document, linked to the exception you logged about it.

        You never write to the client. When the run ends, the system emails the client one link for every document
        asked for, and the client reads the document type, the period and the reason. Asking again for the same
        document for the same exception changes nothing.

        Args:
            document_type: The document, in 1 to 60 characters, such as "Invoice from Acme Ltd".
            period: The period it covers, in 1 to 40 characters, such as "March 2026".
            reason: Why it's needed, under 200 characters of plain text without links or email addresses.
            exception_id: The exception_id that log_exception gave you for this.
        """
        try:
            document_type, period, reason = _one_line(document_type), _one_line(period), _one_line(reason)
            if not _plain(reason, 200):
                return {"ok": False, "error": REASON_RULE}
            if not _plain(document_type, 60):
                return {"ok": False, "error": TYPE_RULE}
            if not _plain(period, 40):
                return {"ok": False, "error": PERIOD_RULE}
            try:
                linked = exceptions.get(exception_id)
            except NotFound:
                linked = None
            if linked is None or (linked["job_id"], linked["client_id"]) != (job_id, client_id):
                return {"ok": False, "error": NO_EXCEPTION}
            made, new = requests.create_once(client_id=client_id, job_id=job_id, exception_id=exception_id,
                                             document_type=document_type, period=period, reason=reason)
            if new:
                audit.append(actor="agent", action="request_client_document", client_id=client_id, job_id=job_id,
                             target={"request_id": made["id"]}, before=None,
                             after={"exception_id": exception_id, "document_type": document_type, "period": period,
                                    "reason": reason})
            return {"ok": True, "request_id": made["id"]}
        except LedgerSyncError as exc:
            return _failed(exc)

    return {t.name: t for t in (log_exception, request_client_document)}
