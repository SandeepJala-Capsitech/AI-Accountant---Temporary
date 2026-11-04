"""The accountant agent's tools. Each takes the client, the year the chat is on and the chat itself from the run's
context (ToolRuntime), never from the model's arguments, and its arguments are checked by Pydantic. They read the
books, the documents, the questions and the decisions. Only resolve_issue changes rows, through resolutions.resolve,
which checks the evidence and records the decision; ask_user adds a question. No tool sets a date or posts: dates come
from the bank lines or from the person, and the server posts at the end of each turn. Each tool says what it is
doing as a step of the turn, under its tool call's id, which run.stream_turn passes on."""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Optional

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool
from pydantic import Field
from pydantic_core import to_jsonable_python

from ledgersync import books, documents, ledger_book, questions
from ledgersync.blobs import BlobStore
from ledgersync.checks import booked
from ledgersync.errors import InvalidInput, NotFound
from ledgersync.evidence_rules import DocumentRule, document_rule
from ledgersync.accounts import choosable
from ledgersync.ledger import bank_choices, settings_for
from ledgersync.models import Evidence, Ledger, SavedRow
from ledgersync.records import AgentRecords
from ledgersync.resolutions import Resolution, resolve
from ledgersync.store_mongo import MongoStore

Family = Literal["date", "duplicate", "combined_vat", "conflict", "missing_vat", "documents", "other"]

# The family of each issue code, as list_issues sorts them. The codes are those checks.py, matching.py,
# statements.py, file_dates.py and adapter.py give; any code not here is "other". A row the document rules hold in
# Suspense (evidence_rules) is "documents" too.
FAMILIES: dict[str, Family] = {
    # The date: none, read differently from the bank's, outside the year, or only in the file name.
    "date_missing": "date", "date_conflict": "date", "date_out_of_period": "date", "before_first_year": "date",
    "file_date": "date",
    # One purchase recorded twice: by two records, a copy of a document, or lines over a document's total.
    "booked_twice": "duplicate", "duplicate_document": "duplicate", "total_mismatch": "duplicate",
    # One VAT total over items at different rates.
    "mixed_items": "combined_vat", "vat_rate_mismatch": "combined_vat",
    # Two records of one purchase disagree on its amount or its VAT.
    "amount_conflict": "conflict", "claim_vat": "conflict",
    # VAT the document shows that the row doesn't book.
    "vat_not_booked": "missing_vat", "not_vat_invoice": "missing_vat",
    # A document that isn't a transaction, or that is missing.
    "not_booked": "documents", "open_document": "documents", "no_payroll": "documents",
    # The reader found no account for a purchase and parked it in Suspense: a question about the account.
    "account_unclear": "other",
}

# The matches that tie a row to another record of the same purchase (claimed_in names a whole expenses sheet).
_MATCHES = ("paid_by", "pays", "recorded_by", "copy_of")


@dataclass
class ChatContext:
    """Who a run works for, from the server: the client, the year the chat is on, and the chat."""
    client_id: int
    thread_id: str
    year_start: dt.date


def _never(thread_id: str) -> bool:
    return False


@dataclass
class AgentDeps:
    """What the tools need: the store and the agent's records, on the app's one MongoDB client; where the original
    files are kept; the vision model that transcribes images and scans (ModelClient; None: they can't be read); and
    whether a chat was asked to stop, by its thread id."""
    store: MongoStore
    records: AgentRecords
    blobs: BlobStore
    vision: Optional[Any] = None
    is_cancelled: Callable[[str], bool] = field(default=_never)


def family_of(code: str) -> Family:
    return FAMILIES.get(code, "other")


def _step(runtime: ToolRuntime, text: str, state: str = "running", reason: Optional[str] = None) -> None:
    """Tells the turn what the tool is doing, as a step under its tool call's id."""
    step = {"id": runtime.tool_call_id, "text": text, "state": state, **({"reason": reason} if reason else {})}
    runtime.stream_writer({"step": step})


def _kept(fields: dict) -> dict:
    """The fields that say something: without None, empty lists or empty dicts."""
    return {k: v for k, v in fields.items() if v is not None and v != [] and v != {}}


def _out(value) -> Any:
    """The value as JSON: money and dates as strings ("12.50", "2026-10-14")."""
    return to_jsonable_python(value)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _held(row: SavedRow, settings) -> Optional[DocumentRule]:
    """The document rule holding the row in Suspense, if one does (only a booked row is held)."""
    if not booked(row):
        return None
    rule = document_rule(row, settings)
    return rule if rule.action == "hold" else None


def _brief(row: SavedRow, names: dict[int, str], held: Optional[DocumentRule] = None) -> dict:
    """A row as the tools list it."""
    return _kept({
        "row_id": row.id, "upload_id": row.upload_id, "file": names.get(row.upload_id), "kind": row.document_type,
        "date": row.date, "description": row.description, "counterparty": row.counterparty,
        "direction": row.direction.value, "gross": row.gross, "vat": row.vat, "account_code": row.account_code,
        "account_name": row.account_name,
        "issues": [_kept({"code": i.code, "family": family_of(i.code), "severity": i.severity, "message": i.message,
                          "related_rows": i.related}) for i in row.issues],
        "held": {"needs": held.needs, "reason": held.reason} if held else None,
    })


def _to_look_at(built: Ledger) -> list[tuple[SavedRow, Optional[DocumentRule], set[str]]]:
    """The year's rows that need a look, by row id: those with a warning or an error, and those the document rules
    hold in Suspense; each with its rule if held and the families it falls in. Rows a decision took out of the books
    or replaced are left out."""
    settings = settings_for(built.client)
    found = []
    for row in sorted(built.transactions, key=lambda r: r.id):
        if row.excluded or row.superseded_by:
            continue
        held = _held(row, settings)
        families = {family_of(i.code) for i in row.issues if i.severity != "info"} | ({"documents"} if held else set())
        if families:
            found.append((row, held, families))
    return found


def _related(row: SavedRow, rows: list[SavedRow]) -> list[SavedRow]:
    """The rows an issue on the row names, and the records matching pairs it with, either way round."""
    def refs(r: SavedRow) -> set[str]:
        matched = [s for name in _MATCHES for s in (getattr(r, name) if isinstance(getattr(r, name), list)
                                                    else [getattr(r, name)]) if s]
        return {s.ref for s in matched}

    named, its = {n for i in row.issues for n in i.related}, refs(row)
    return [o for o in rows if o.id != row.id and (o.id in named or (o.document_ref and o.document_ref in its)
                                                   or (row.document_ref and row.document_ref in refs(o)))]


def make_tools(deps: AgentDeps, actor: str) -> list[BaseTool]:
    """The tools, working on deps. actor: who resolve_issue records its decisions as made by ("agent:<model>")."""
    store, records = deps.store, deps.records

    def texts(client_id: int) -> Callable[[int], list[str]]:
        """The pages of one of the client's uploads, by its id (none for an upload that isn't the client's)."""
        uploads = {u["id"]: u for u in store.uploads(client_id)}

        def pages(upload_id: int) -> list[str]:
            upload = uploads.get(upload_id)
            return documents.text_of(upload, deps.blobs, records, deps.vision) if upload else []
        return pages

    @tool
    def get_overview(runtime: ToolRuntime[ChatContext]) -> dict:
        """The client, its years, and the year this chat is on: its rows and documents, how many rows need a look in
        each family of issue, how many are held in Suspense for a document, the questions still open and the
        decisions made so far, and the accounts a row can be coded to (code and name)."""
        ctx = runtime.context
        _step(runtime, "Looking over the books…")
        built = books.ledger(store, ctx.client_id, ctx.year_start)
        looked = _to_look_at(built)
        result = {
            "client": {"name": built.client.name, "business_type": built.client.business_type,
                       "vat_registered": built.client.vat_registered},
            "years": [y.label for y in built.years],
            "year": {"start": built.year.start, "end": built.year.end, "label": built.year.label,
                     "closed": built.year.closed},
            "rows": len(built.transactions), "documents": len(built.uploads),
            "need_a_look": dict(Counter(f for _, _, families in looked for f in families)),
            "held_in_suspense": sum(held is not None for _, held, _ in looked),
            "open_questions": len(records.questions(ctx.client_id, ctx.year_start)),
            "decisions": len(records.decisions(ctx.client_id, ctx.year_start)),
            "accounts": [{"code": a.code, "name": a.name}
                         for a in choosable(built.client.business_type, bank_choices(built.client))],
        }
        _step(runtime, "Looked over the books", "done")
        return _out(result)

    @tool
    def list_issues(runtime: ToolRuntime[ChatContext],
                    family: Annotated[Optional[Family], Field(description="Only rows with an issue of this family; "
                                                                           "all rows that need a look when left out.")]
                    = None,
                    after_row_id: Annotated[int, Field(ge=0, description="List rows after this row id, to page "
                                                                          "through them.")] = 0,
                    limit: Annotated[int, Field(ge=1, le=20, description="The most rows to list.")] = 20) -> dict:
        """The year's rows that need a look, by row id: each with its issues (code, family, message and the rows it
        names) and, if the document rules hold it in Suspense, the document it needs. Families: date, duplicate,
        combined_vat (one VAT total over items at different rates), conflict (two records disagree), missing_vat (VAT
        on the document but not booked), documents (a document missing, or held in Suspense for one), other."""
        ctx = runtime.context
        _step(runtime, "Listing what needs a look…")
        built = books.ledger(store, ctx.client_id, ctx.year_start)
        rows = [(row, held) for row, held, families in _to_look_at(built)
                if row.id > after_row_id and (family is None or family in families)]
        names = {u.id: u.name for u in built.uploads}
        page = rows[:limit]
        result = _kept({"rows": [_brief(row, names, held) for row, held in page],
                        "next_after_row_id": page[-1][0].id if len(rows) > limit else None})
        _step(runtime, f"Found {_count(len(page), 'row')} that {'needs' if len(page) == 1 else 'need'} a look", "done")
        return _out(result)

    @tool
    def get_row(row_id: Annotated[int, Field(description="The row's id.")],
                runtime: ToolRuntime[ChatContext]) -> dict:
        """One row in full: what was read, what is booked, its issues, the records it is matched with (paid_by,
        pays, recorded_by, claimed_in, copy_of), what the original reading said before a person's edit, the rows it
        is related to, and the decisions already made on it."""
        ctx = runtime.context
        _step(runtime, f"Looking at row {row_id}…")
        rows = books.all_rows(store, ctx.client_id)
        row = next((r for r in rows if r.id == row_id), None)
        if row is None:
            raise NotFound(f"Row {row_id} was not found among this client's rows.")
        names = {u["id"]: u["name"] for u in store.uploads(ctx.client_id)}
        held = _held(row, settings_for(store.get_client(ctx.client_id)))
        result = {**_brief(row, names, held), **_kept({
            "booked": booked(row), "vat_posted": row.vat_posted, "net": row.net,
            "contra_account_code": row.contra_account_code, "document_number": row.document_number,
            "document_total": row.document_total, "document_net": row.document_net,
            "supplier_vat_number": row.supplier_vat_number, "not_vat_invoice": row.not_vat_invoice or None,
            "excluded": row.excluded or None, "superseded_by": row.superseded_by, "edited": row.edited or None,
            "as_read": row.original, "paid_by": row.paid_by, "pays": row.pays, "recorded_by": row.recorded_by,
            "claimed_in": row.claimed_in, "copy_of": row.copy_of, "date_found": row.date_found,
            "amount_found": row.amount_found, "vat_found": row.vat_found,
            "related": [_brief(o, names) for o in _related(row, rows)],
            "decisions": [{"decision_id": d.id, "rule": d.rule, "status": d.status, "reason": d.reason}
                          for d in records.decisions(ctx.client_id) if row_id in d.row_ids],
        })}
        _step(runtime, f"Looked at row {row_id}", "done")
        return _out(result)

    @tool
    def list_documents(runtime: ToolRuntime[ChatContext]) -> dict:
        """The year's documents: each upload's id, file name, kind, how many rows were read from it, and for a bank
        statement whether its lines agree with the balances it prints."""
        ctx = runtime.context
        _step(runtime, "Listing the documents…")
        uploads = books.ledger(store, ctx.client_id, ctx.year_start).uploads
        result = {"documents": [_kept({"upload_id": u.id, "name": u.name, "kind": u.kind, "rows": u.rows,
                                       "added": u.created_at, "bank_account": u.bank_account,
                                       "statement_check": _kept(u.statement.model_dump()) if u.statement else None})
                                for u in uploads]}
        _step(runtime, f"Listed {_count(len(uploads), 'document')}", "done")
        return _out(result)

    @tool
    def read_document(upload_id: Annotated[int, Field(description="The document's upload id.")],
                      runtime: ToolRuntime[ChatContext],
                      page: Annotated[int, Field(ge=1, description="Which page of its text, from 1.")] = 1) -> dict:
        """The text of one of the client's original documents, a page at a time (12,000 characters at most a page),
        with how many pages it has. Images and scans are transcribed once and kept."""
        ctx = runtime.context
        upload = next((u for u in store.uploads(ctx.client_id) if u["id"] == upload_id), None)
        if upload is None:
            raise NotFound(f"Document {upload_id} was not found among this client's documents.")
        name = upload["name"]
        _step(runtime, f"Opening {name}…")
        pages = documents.text_of(upload, deps.blobs, records, deps.vision)
        if not pages:
            raise InvalidInput(f"{name} has no text to read: it was typed in, or replaced by a split.")
        if page > len(pages):
            raise InvalidInput(f"{name} has {_count(len(pages), 'page')} of text.")
        _step(runtime, f"Opened {name}" + (f", page {page}" if page > 1 else ""), "done")
        return {"upload_id": upload_id, "name": name, "page": page, "pages": len(pages), "text": pages[page - 1]}

    @tool
    def resolve_issue(row_id: Annotated[int, Field(description="The row the resolution is about.")],
                      resolution: Resolution,
                      evidence: Annotated[list[Evidence], Field(
                          min_length=1, max_length=3,
                          description="One to three quotes, each exactly as printed in the document with this upload "
                                      "id: the row's own, that of a record it is matched with, or the bank line "
                                      "that pays the invoice or receipt being kept. upload_name is "
                                      "filled in for you.")],
                      reason: Annotated[str, Field(min_length=1, max_length=400,
                                                   description="Why, in a sentence or two the person will read.")],
                      runtime: ToolRuntime[ChatContext]) -> dict:
        """Resolves an issue on a row, when the evidence settles it. keep_one: the same purchase recorded more than
        once; keep the best-ranked record and drop the other records of it. A bank line that pays the receipt or
        invoice you keep is its payment, not a duplicate: leave it out of drop_row_ids. row_id can be the kept row.
        use_primary: two records disagree; use the better-ranked one. split_vat: one VAT total over items at
        different rates; split the row into parts that add up exactly to it, with VAT at each part's rate matching the
        VAT printed. set_vat_from_document: the document prints VAT the row doesn't book. The services check the
        resolution against the evidence and refuse what it doesn't support, saying
        why. Sent again once applied, it returns the same decision."""
        ctx = runtime.context
        _step(runtime, f"Checking row {row_id}…")
        decision = resolve(store, records, client_id=ctx.client_id, thread_id=ctx.thread_id, actor=actor,
                           row_id=row_id, resolution=resolution, evidence=evidence, reason=reason,
                           texts=texts(ctx.client_id))
        if decision.status != "applied":
            _step(runtime, f"Didn't change row {row_id}", "failed", decision.reason)
            return {"status": "refused", "why": decision.reason}
        _step(runtime, f"Row {row_id}: {decision.rule}", "done")
        return _out({"decision_id": decision.id, "status": "applied", "rule": decision.rule,
                     "before": decision.before, "after": decision.after})

    @tool
    def ask_user(row_ids: Annotated[list[int], Field(description="The rows the question is about.")],
                 question: Annotated[str, Field(min_length=1, max_length=200,
                                                description="One specific question, in a plain sentence.")],
                 why: Annotated[str, Field(min_length=1, max_length=300,
                                           description="Why it matters, and what would settle it.")],
                 runtime: ToolRuntime[ChatContext]) -> dict:
        """Asks the person a question the evidence can't settle and the rules don't already ask. It is shown as a
        card with the summary, and stays open on the client until it is answered. Asked again with the same rows
        and words, it is the same question."""
        ctx = runtime.context
        _step(runtime, "Writing a question…")
        known = {r.id for r in store.rows(ctx.client_id)}
        stray = [n for n in row_ids if n not in known]
        if stray:
            raise NotFound(f"Row {stray[0]} was not found among this client's rows.")
        # The books ask these after the turn; asking too would put two questions to the person about one thing.
        covered: dict[int, list[str]] = {}
        for q in questions.rule_questions(store, ctx.client_id, ctx.year_start):
            for n in q.row_ids:
                if n in row_ids:
                    covered.setdefault(n, []).append(q.kind)
        if covered:
            what = {"date": "a date", "receipt_or_invoice": "a receipt or invoice", "vat_invoice": "a VAT invoice"}
            named = ", ".join(f"row {n} ({' and '.join(what.get(k, k) for k in kinds)})"
                              for n, kinds in sorted(covered.items()))
            if len(covered) == len(set(row_ids)):
                message = f"The books already ask the person about {named}, so there's no need to ask."
                _step(runtime, "The books already ask this", "done")
                return {"asked": False, "message": message}
            row_ids = [n for n in row_ids if n not in covered]
            dropped = f"The books already ask the person about {named}, so the question is about the other rows only."
        else:
            dropped = None
        asked = questions.ask(records, client_id=ctx.client_id, year_start=ctx.year_start, row_ids=row_ids,
                              text=question.strip(), why=why.strip())
        _step(runtime, f"Asked: {asked.text}", "done")
        return {"question_id": asked.id, "status": asked.status, **({"note": dropped} if dropped else {})}

    @tool
    def list_questions(runtime: ToolRuntime[ChatContext]) -> dict:
        """The questions still open on the client's year, from this chat or earlier ones: what each asks and why,
        its rows, whether it is open or waiting (the person said "I don't have it" or "Not sure"), and who asked it
        (the rules or the agent)."""
        ctx = runtime.context
        _step(runtime, "Checking the open questions…")
        asked = records.questions(ctx.client_id, ctx.year_start)
        result = {"questions": [_kept({"question_id": q.id, "kind": q.kind, "text": q.text, "why": q.why,
                                       "status": q.status, "answer": q.answer, "row_ids": q.row_ids,
                                       "asked_by": q.source, "asked_at": q.created_at}) for q in asked]}
        _step(runtime, f"{_count(len(asked), 'question')} still open" if asked else "No questions open", "done")
        return _out(result)

    @tool
    def list_decisions(runtime: ToolRuntime[ChatContext]) -> dict:
        """The decisions made on the client's year so far, oldest first, by the agent, the rules or the person:
        each with its rule, whether it was applied or refused, its rows, the reason, the evidence quoted, and the
        values before and after."""
        ctx = runtime.context
        _step(runtime, "Checking the decisions so far…")
        made = records.decisions(ctx.client_id, ctx.year_start)
        result = {"decisions": [_kept({
            "decision_id": d.id, "made_at": d.created_at, "by": d.actor, "action": d.action, "rule": d.rule,
            "status": d.status, "row_ids": d.row_ids, "reason": d.reason,
            "evidence": [{"upload_id": e.upload_id, "file": e.upload_name, "quote": e.quote} for e in d.evidence],
            "before": d.before, "after": d.after}) for d in made]}
        _step(runtime, f"Read {_count(len(made), 'decision')}", "done")
        return _out(result)

    @tool
    def get_trial_balance(runtime: ToolRuntime[ChatContext]) -> dict:
        """The trial balance of what is posted for the client's year (the opening balances and every entry posted so
        far): each account's debit and credit, the totals, whether it balances, the rows held in Suspense and what
        each needs, and any problems. The system posts at the end of each turn, so this turn's changes aren't in it
        yet."""
        ctx = runtime.context
        _step(runtime, "Checking the posted trial balance…")
        tb = ledger_book.posted_trial_balance(store, records, ctx.client_id, ctx.year_start)
        result = _kept({
            "lines": [{"code": l.code, "name": l.name, "debit": l.debit, "credit": l.credit} for l in tb.lines],
            "total_debits": tb.total_debits, "total_credits": tb.total_credits, "is_balanced": tb.is_balanced,
            "entries_posted": len(tb.journal),
            "suspense": [_kept(s.model_dump()) for s in tb.suspense], "problems": tb.problems})
        _step(runtime, "Checked the posted trial balance", "done")
        return _out(result)

    return [get_overview, list_issues, get_row, list_documents, read_document, resolve_issue, ask_user,
            list_questions, list_decisions, get_trial_balance]
