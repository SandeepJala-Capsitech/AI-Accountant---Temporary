"""The accountant chat's questions for the person. The rules work them out from the books: a booked row with no date
asks for one, a row held in Suspense by the document rules asks for the document. A person's answer is applied here
without the model: a date goes through resolutions.set_date, "I don't have it" and "Not sure" leave the question
waiting. After an answer the questions are worked out again and the year is posted. A question goes when the books
no longer need it; the agent's own questions are never touched by that. An uploaded document is not an answer here:
the API reads it into the client's documents and then calls refresh."""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

from . import books, ledger_book
from .checks import booked, long_date
from .errors import InvalidInput, NotFound
from .evidence_rules import document_rule
from .ledger import settings_for
from .models import Question, QuestionDraft, Transaction
from .records import AgentRecords
from .resolutions import set_date
from .store_mongo import MongoStore

_OVER = {"receipt_or_invoice": "£50 or more", "vat_invoice": "Over £250"}
_ASKS = {"receipt_or_invoice": "Can you upload the receipt or invoice?",
         "vat_invoice": "Can you upload the full VAT invoice?"}


class DateAnswer(BaseModel):
    kind: Literal["date"]
    date: dt.date


class NoDocumentAnswer(BaseModel):
    kind: Literal["no_document"]


class NotSureAnswer(BaseModel):
    kind: Literal["not_sure"]


Answer = Annotated[Union[DateAnswer, NoDocumentAnswer, NotSureAnswer], Field(discriminator="kind")]


def _gbp(amount) -> str:
    return f"£{amount:,.2f}"


def _key(kind: str, row_id: int) -> str:
    """The key a rule question is kept under: one date question and one document question per row."""
    return f"{'date' if kind == 'date' else 'doc'}:{row_id}"


def _wanted(store: MongoStore, client_id: int, year_start: dt.date) -> dict[str, QuestionDraft]:
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
        rule = document_rule(tx, settings)
        if rule.action == "hold" and rule.needs:
            when = f" on {long_date(tx.date)}" if tx.date else ""
            want(tx, rule.needs, _ASKS[rule.needs],
                 f"{_gbp(tx.gross)} to {tx.counterparty or tx.description}{when}. {_OVER[rule.needs]}, so I need it. "
                 "Held in Suspense as Documentation required until then.")
    return wanted


def refresh(store: MongoStore, records: AgentRecords, client_id: int, year_start: dt.date) -> list[Question]:
    """Works the year's rule questions out from the books and keeps them: each is asked once under its key (asked
    again, it keeps its status and answer, so one waiting stays waiting), one that was resolved and is needed again
    is reopened, and one the books no longer need is resolved with no answer. The agent's questions are left alone. Returns the
    year's open and waiting questions, oldest first."""
    wanted = _wanted(store, client_id, year_start)
    for key, draft in wanted.items():
        asked = records.upsert_question(draft, key)
        if asked.status == "resolved":
            records.set_question(asked.id, "open")
    for q in records.questions(client_id, year_start):
        if q.source == "rules" and q.row_ids and _key(q.kind, q.row_ids[0]) not in wanted:
            records.set_question(q.id, "resolved", None)
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
           answer: Answer) -> list[Question]:
    """Applies the person's answer, then works the questions out again and posts the year. A date answers a date
    question: the row is dated by resolutions.set_date (a date outside the books is InvalidInput, and the question
    stays open) and the question is resolved. "I don't have it" and "Not sure" leave the question waiting. A
    question already resolved is left as it is; the same answer twice is one decision. Returns the year's open and
    waiting questions."""
    q = records.question(question_id)
    if q is None or q.client_id != client_id:
        raise NotFound("This question was not found.")
    if q.status == "resolved":
        return records.questions(client_id, q.year_start)
    years, decision_ids = [q.year_start], []
    if isinstance(answer, DateAnswer):
        if q.kind != "date" or not q.row_ids:
            raise InvalidInput("This question isn't asking for a date.")
        decision = set_date(store, records, client_id=client_id, thread_id=thread_id, row_id=q.row_ids[0],
                            date=answer.date, question_id=q.id)
        if decision.status != "applied":
            raise InvalidInput(decision.reason)
        records.set_question(q.id, "resolved", long_date(answer.date))
        decision_ids = [decision.id]
        if decision.year_start not in years:   # the date moved the row to another year
            years.append(decision.year_start)
    elif isinstance(answer, NoDocumentAnswer):
        if q.kind not in _ASKS:
            raise InvalidInput("This question isn't asking for a document.")
        records.set_question(q.id, "waiting", "I don't have it")
    else:
        records.set_question(q.id, "waiting", "Not sure")
    for year in years:
        refresh(store, records, client_id, year)
        ledger_book.post(store, records, client_id, year, decision_ids=decision_ids)
    return records.questions(client_id, q.year_start)
