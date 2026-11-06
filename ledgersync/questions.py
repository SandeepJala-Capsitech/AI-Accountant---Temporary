"""The accountant chat's questions for the person. The rules work them out from the books: a booked row with no date
asks for one, a row held in Suspense by the document rules asks for the document. A person's answer is applied here
without the model: a date goes through resolutions.set_date, "I don't have it" and "Not sure" leave the question
waiting. After an answer the questions are worked out again and the year is posted. A question goes when the books
no longer need it; the agent's own questions are never touched by that. An uploaded document is not an answer here:
the API reads it into the client's documents and then calls refresh."""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Annotated, Literal, NamedTuple, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field

from . import books, ledger_book
from .checks import booked, long_date
from .errors import InvalidInput, NotFound
from .evidence_rules import document_rule
from .ledger import settings_for
from .models import (ChangeDraft, Decision, DecisionDraft, Evidence, Ledger, PostResult, Question, QuestionDraft,
                     Transaction)
from .money import ZERO
from .payees import processor
from .records import AgentRecords
from .resolutions import Texts, check_quotes, set_date
from .store_mongo import MongoStore

_OVER = {"receipt_or_invoice": "£50 or more", "vat_invoice": "Over £250"}
_ASKS = {"receipt_or_invoice": "Can you upload the receipt or invoice?",
         "vat_invoice": "Can you upload the full VAT invoice?"}
# The questions a document answers: those of the document rules, and a payout or payroll report the books lack.
DOCUMENT_KINDS = frozenset({*_ASKS, "payout_report", "payroll_report"})


class DateAnswer(BaseModel):
    kind: Literal["date"]
    date: dt.date


class NoDocumentAnswer(BaseModel):
    kind: Literal["no_document"]


class NotSureAnswer(BaseModel):
    kind: Literal["not_sure"]


Answer = Annotated[Union[DateAnswer, NoDocumentAnswer, NotSureAnswer], Field(discriminator="kind")]


class Answered(NamedTuple):
    """What an answer leaves: the year's open and waiting questions, and the posting of the question's year."""
    questions: list[Question]
    post: PostResult


def _not_posted() -> PostResult:
    return PostResult(batch_id=None, entries_added=0, reversals_added=0, total_debits=ZERO, total_credits=ZERO)


def _gbp(amount) -> str:
    return f"£{amount:,.2f}"


def _on(tx: Transaction) -> str:
    return f" on {long_date(tx.date)}" if tx.date else ""


def _key(kind: str, row_id: int) -> str:
    """The key a rule question is kept under: one date question and one document question per row."""
    return f"{'date' if kind == 'date' else 'doc'}:{row_id}"


def _wanted(store: MongoStore, client_id: int,
            year_start: dt.date) -> tuple[dict[str, QuestionDraft], Ledger]:
    """The questions the year's books ask now, by key. A row with no date asks for one (a date in its file name is
    only ever offered on the row, never taken: matching dates a row from its bank line, so a row still undated has
    none). A booked row the document rules hold asks for the document it needs."""
    built = books.ledger(store, client_id, year_start)
    settings = settings_for(built.client)
    wanted: dict[str, QuestionDraft] = {}

    def want(tx: Transaction, kind: str, text: str, why: str) -> None:
        wanted[_key(kind, tx.id)] = QuestionDraft(client_id=client_id, year_start=year_start, row_ids=[tx.id],
                                                  kind=kind, text=text, why=why, source="rules")

    for tx in (t for t in built.transactions if booked(t)):
        if tx.date is None:
            want(tx, "date", f"When was this {tx.description}?",
                 f"{_gbp(tx.gross)}, no date on it and no matching bank line.")
        codes = {i.code for i in tx.issues}
        if "payout_report" in codes:
            who = processor(tx.counterparty, tx.description) or "payment processor"
            want(tx, "payout_report", f"Can you upload the {who} payout report?",
                 f"{_gbp(tx.gross)} paid out by {tx.counterparty or who}{_on(tx)} is sales less {who}'s fees and "
                 "refunds. Held in Payment Processor Clearing until the report shows each.")
        if "no_payroll" in codes and tx.date:
            want(tx, "payroll_report", f"Can you upload the payroll report for {tx.date:%B %Y}?",
                 f"{_gbp(tx.gross)} paid to {tx.counterparty or tx.description}{_on(tx)}, and no payroll report "
                 "books the wages it is for.")
        rule = document_rule(tx, settings)
        if rule.action == "hold" and rule.needs:
            when = f" on {long_date(tx.date)}" if tx.date else ""
            want(tx, rule.needs, _ASKS[rule.needs],
                 f"{_gbp(tx.gross)} to {tx.counterparty or tx.description}{when}. {_OVER[rule.needs]}, so I need it. "
                 "Held in Suspense as Documentation required until then.")
    return wanted, built


def rule_questions(store: MongoStore, client_id: int, year_start: dt.date) -> list[QuestionDraft]:
    """The questions the year's books ask the person now, so the agent doesn't ask them again."""
    return list(_wanted(store, client_id, year_start)[0].values())


def _settled_by(store: MongoStore, client_id: int, q: Question) -> Optional[str]:
    """For a resolved document question, "Matched to <file>" when its row is now settled by a document, the file that
    document is in; otherwise None. A bank line pays a document (pays) or is recorded by it (recorded_by, as a card
    payment is by its receipt). The document is looked up among all the client's rows: a card receipt can sit in
    another year than its line."""
    if q.kind not in DOCUMENT_KINDS or not q.row_ids:
        return None
    rows = books.all_rows(store, client_id)
    tx = next((t for t in rows if t.id == q.row_ids[0]), None)
    if tx is None:
        return None
    ref = (tx.pays[0].ref if tx.pays else tx.recorded_by.ref if tx.recorded_by else
           tx.documented_by.ref if tx.documented_by else None)
    paid = next((t for t in rows if ref and t.document_ref == ref), None)
    name = next((u["name"] for u in store.uploads(client_id) if paid is not None and u["id"] == paid.upload_id), None)
    return f"Matched to {name}" if name else None


def _same(stored: Question, draft: QuestionDraft) -> bool:
    """Whether the stored question already says what the draft says."""
    return (stored.kind, stored.text, stored.why, stored.row_ids, stored.year_start) == (
        draft.kind, draft.text, draft.why, draft.row_ids, draft.year_start)


def refresh(store: MongoStore, records: AgentRecords, client_id: int, year_start: dt.date) -> list[Question]:
    """Works the year's rule questions out from the books and keeps them: each is asked once under its key (asked
    again, it keeps its status and answer, so one waiting stays waiting), one that was resolved and is needed again
    is reopened, and one the books no longer need is resolved with no answer. The agent's questions are left alone.
    The year's questions are read once and only those that are new or have changed are written, so settling after
    every change costs a read when nothing moved. Returns the year's open and waiting questions, oldest first."""
    wanted, _ = _wanted(store, client_id, year_start)
    stored = {_key(q.kind, q.row_ids[0]): q for q in records.questions(
        client_id, year_start, statuses=("open", "waiting", "resolved")) if q.source == "rules" and q.row_ids}
    for key, draft in wanted.items():
        asked = stored.get(key)
        if asked is None or not _same(asked, draft):
            asked = records.upsert_question(draft, key)
        if asked.status == "resolved":
            records.set_question(asked.id, "open")
    for key, q in stored.items():
        if key not in wanted and q.status != "resolved":
            records.set_question(q.id, "resolved", _settled_by(store, client_id, q))
    return records.questions(client_id, year_start)


def ask(records: AgentRecords, *, client_id: int, year_start: dt.date, row_ids: list[int], text: str,
        why: str) -> Question:
    """Asks the person a question of the agent's own, once for the same rows and words (asked again, it takes the
    new reason)."""
    body = f"{client_id}|{year_start.isoformat()}|{sorted(row_ids)}|{text}"
    return records.upsert_question(
        QuestionDraft(client_id=client_id, year_start=year_start, row_ids=row_ids, kind="other", text=text, why=why,
                      source="agent"),
        f"agent:{hashlib.sha256(body.encode()).hexdigest()}")


def answer(store: MongoStore, records: AgentRecords, *, client_id: int, thread_id: Optional[str], question_id: str,
           answer: Answer, now: Optional[dt.datetime] = None, wait: int = 20) -> Answered:
    """Applies the person's answer, then works the questions out again and posts the year. A date answers a date
    question: the row is dated by resolutions.set_date (a date outside the books is InvalidInput, and the question
    stays open) and the question is resolved. "I don't have it" and "Not sure" leave the question waiting. A
    question already resolved is left as it is (and nothing is posted); the same answer twice is one decision.
    Returns the year's open and waiting questions, and the posting of the question's year (the year a date moved
    the row to is posted too). An answer outside a chat (no thread_id) makes the client due for a background review
    `wait` seconds after `now`."""
    q = records.question(question_id)
    if q is None or q.client_id != client_id:
        raise NotFound("This question was not found.")
    if q.status == "resolved":
        return Answered(records.questions(client_id, q.year_start), _not_posted())
    years, decision_ids, said = [q.year_start], [], ""
    if isinstance(answer, DateAnswer):
        if q.kind != "date" or not q.row_ids:
            raise InvalidInput("This question isn't asking for a date.")
        decision = set_date(store, records, client_id=client_id, thread_id=thread_id, row_id=q.row_ids[0],
                            date=answer.date, question_id=q.id)
        if decision.status != "applied":
            raise InvalidInput(decision.reason)
        said = long_date(answer.date)
        records.set_question(q.id, "resolved", said)
        decision_ids = [decision.id]
        if decision.year_start not in years:   # the date moved the row to another year
            years.append(decision.year_start)
    elif isinstance(answer, NoDocumentAnswer):
        if q.kind not in DOCUMENT_KINDS:
            raise InvalidInput("This question isn't asking for a document.")
        said = "I don't have it"
        records.set_question(q.id, "waiting", said)
    else:
        said = "Not sure"
        records.set_question(q.id, "waiting", said)
    if not (q.status == "waiting" and q.answer == said):   # the same answer again changes nothing to record
        made = ChangeDraft(client_id=client_id, year_start=q.year_start, actor="user", kind="question_answered",
                           row_ids=list(q.row_ids), after={"answer": said}, summary=f"Answered: {q.text}")
        records.add_change(made)
        if thread_id is None:   # in a chat, the chat's agent handles the answer
            from . import reviews   # here: reviews imports the agent
            reviews.note(records, made, now=now or reviews.utcnow(), wait=wait)
    posts = []
    for year in years:
        refresh(store, records, client_id, year)
        posts.append(ledger_book.post(store, records, client_id, year, decision_ids=decision_ids))
    return Answered(records.questions(client_id, q.year_start), posts[0])


CLOSED_BY_THE_AI = "Closed by the AI: "


def close(store: MongoStore, records: AgentRecords, *, client_id: int, year_start: dt.date, question_id: str, reason: str,
          evidence: list[Evidence], texts: Texts, actor: str, trigger: str, review_id: Optional[str] = None,
          suggest: bool = False) -> Decision:
    """Closes a question of the agent's own that the books have since answered, and records it as a decision. Only an
    open or waiting question of the agent's can be closed. It needs one of two things: a change by a person, made
    after the question was asked, to one of its rows (the change's summary is the evidence), or quotes from the rows'
    documents that pass the same check as a resolution's. Without either the decision is refused. Asked again once
    closed it returns that decision; a closing that a person undid is refused. suggest=True records the closing as a
    suggested decision and leaves the question as it is."""
    q = records.question(question_id)
    if q is None or q.client_id != client_id:
        raise NotFound("This question was not found.")
    if q.year_start != year_start:
        raise InvalidInput("That question belongs to another year.")
    key = f"close|{q.id}"
    if (done := records.decision_by_key(client_id, key)) is not None and done.status == "applied" \
            and not records.is_rejected(client_id, key):
        return done
    if q.source != "agent" or q.status not in ("open", "waiting"):
        raise InvalidInput("Only an open question the AI asked can be closed.")
    draft = dict(client_id=client_id, year_start=q.year_start, action="close_question",
                 rule="Closed a question the books answer", row_ids=list(q.row_ids), actor=actor,
                 trigger=trigger, review_id=review_id)

    def refused(why: str) -> Decision:
        return records.append_decision(DecisionDraft(**draft, status="refused", reason=why),
                                       f"{key}|refused|{uuid4().hex}")[0]

    if records.is_rejected(client_id, key):
        return refused("A person undid this change.")
    theirs = [c for c in records.changes(client_id, year_start=q.year_start, since=q.created_at, limit=1000)
              if c.actor == "user" and c.kind != "question_answered" and set(c.row_ids) & set(q.row_ids)]
    if theirs:
        quotes = [Evidence(upload_id=0, upload_name="change record", quote=theirs[-1].summary)]
    else:
        b = books._books(store, client_id)
        rows = {r.id: r for r in books._saved_rows(b, None)[0]}
        why = check_quotes(b, rows, list(q.row_ids), evidence, texts) if evidence else None
        if not evidence or why is not None:
            return refused(why or "Nothing since the question was asked answers it.")
        names = {u["id"]: u["name"] for u in b.uploads}
        quotes = [e.model_copy(update={"upload_name": names.get(e.upload_id, "")}) for e in evidence]
    before, after = {"question": {"status": q.status, "answer": q.answer}}, {"question": {"status": "resolved"}}
    if suggest:
        return records.append_decision(DecisionDraft(**draft, status="suggested", reason=reason, evidence=quotes, before=before,
                                                     after=after, resolution={"action": "close_question",
                                                                              "question_id": q.id, "reason": reason}), f"{key}|suggested")[0]
    made = records.append_decision(DecisionDraft(**draft, status="applied", reason=reason, evidence=quotes, before=before,
                                                 after=after), key)[0]
    records.set_question(q.id, "resolved", f"{CLOSED_BY_THE_AI}{reason}")
    records.add_change(ChangeDraft(client_id=client_id, year_start=q.year_start, actor=actor, kind="question_closed",
                                   row_ids=list(q.row_ids), before=before, after=after,
                                   summary=f"Closed the question: {q.text}", in_chat=trigger == "chat"))
    return made
