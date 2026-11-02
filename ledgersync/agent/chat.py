"""The accountant chat as the API serves it (design of 2026-11-02): its threads, a message's turn, and the answers.

A turn reads the files sent with the message into the client's documents, runs the agent on the message, and then,
whatever the agent did, records the dates matching took from the bank, works the questions out and posts the year,
under the client's lock. It ends with the summary built from the records: what this chat sorted, the questions on the
year, and the posted trial balance. It runs on a thread of its own and goes on to the end if the person goes away.
An answer is applied without the model (questions.answer) and then noted in the chat's memory; a document sent for
a question is read, the year posted again, and the question says why the document didn't settle it if it didn't."""
from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Callable, Iterator, Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.constants import START
from pydantic import BaseModel, StringConstraints

from ledgersync import books, ledger_book, questions
from ledgersync.blobs import BlobStore
from ledgersync.config import Settings
from ledgersync.errors import InvalidInput, LedgerSyncError, ModelUnavailable, NotFound, YearClosed
from ledgersync.jobs import SUCCEEDED, Job
from ledgersync.ledger import require_active
from ledgersync.models import (ChatThread, Client, Decision, PostedTrialBalance, PostResult, Question,
                               Transaction)
from ledgersync.records import AgentRecords
from ledgersync.resolutions import record_automatic
from ledgersync.rows import now_iso
from ledgersync.store_mongo import MongoStore

from . import build_agent
from .llm import chat_model
from .locks import ClientLocks, ThreadRuns
from .run import ChatEvent, stream_turn
from .tools import AgentDeps, ChatContext

logger = logging.getLogger(__name__)

NEW_CHAT = "New chat"
STOPPED_CALL = "Stopped before this finished."
FAILED = "Something went wrong while I was working, so I stopped. The server log says what."
_DOCUMENT_KINDS = ("receipt_or_invoice", "vat_invoice")


class ReviewSummary(BaseModel):
    """What a turn (or an answer) ends with."""
    sorted: list[Decision]          # the applied decisions this chat made since the turn started
    questions: list[Question]       # every open or waiting question on the client's year
    trial_balance: PostedTrialBalance
    post: PostResult


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class ThreadDetail(BaseModel):
    """A thread with its messages so far and the summary its last turn or answer ended with."""
    thread: ChatThread
    messages: list[ChatMessage]
    summary: Optional[ReviewSummary]


class NewThread(BaseModel):
    client_id: int


class ThreadPatch(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


@dataclass(frozen=True)
class ChatFile:
    """A file sent to the chat: its name, its bytes (read up to the upload limit and one byte more, so a larger one
    is refused), its type, and for a statement the bank account it goes on (None: the client's first current
    account)."""
    name: str
    data: bytes
    content_type: str
    bank_account: Optional[str] = None


# Reads a file for the client into the year, as /api/analyze reads an upload: the job reading it; LedgerSyncError
# when the file is refused before it is read.
Reader = Callable[[Client, ChatFile, dt.date], Job]


@dataclass(frozen=True)
class Since:
    """Where a turn or an answer starts in the client's decisions: the time (to the second), and the decisions
    already made in that second."""
    at: str
    before: frozenset[str]


def since_now(records: AgentRecords, client_id: int) -> Since:
    at = now_iso()
    return Since(at, frozenset(d.id for d in records.decisions(client_id, since=at)))


def sorted_since(records: AgentRecords, thread: ChatThread, since: Since) -> list[Decision]:
    """The decisions this chat applied since then, oldest first."""
    return [d for d in records.decisions(thread.client_id, since=since.at)
            if d.id not in since.before and d.status == "applied" and d.thread_id == thread.id]


def summary(store: MongoStore, records: AgentRecords, thread: ChatThread, since: Since,
            post: PostResult) -> ReviewSummary:
    """The summary from the records: what this chat sorted since then, the open and waiting questions on its year,
    the year's posted trial balance, and the posting."""
    return ReviewSummary(sorted=sorted_since(records, thread, since),
                         questions=records.questions(thread.client_id, thread.year_start),
                         trial_balance=ledger_book.posted_trial_balance(store, records, thread.client_id,
                                                                        thread.year_start),
                         post=post)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _error(code: str, message: str) -> ChatEvent:
    return ChatEvent("error", {"code": code, "message": message})


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _gbp(amount) -> str:
    return f"£{Decimal(str(amount)):,.2f}"


def _title(text: str) -> str:
    """A chat's title from its first message: its first 60 characters, on one line."""
    return " ".join(text.split())[:60]


def _open_year(store: MongoStore, client_id: int) -> dt.date:
    """The start of the client's open year: the one holding today if it is open, otherwise the latest open one."""
    today = dt.date.today()
    open_years = [y for y in books.years_of(store, client_id, today) if not y.closed]
    if not open_years:
        raise YearClosed("Every year of this client is closed. Reopen one to chat about it.")
    return next((y.start for y in open_years if y.start <= today <= y.end), open_years[-1].start)


def _messages(messages: list) -> list[ChatMessage]:
    """The chat's messages as the page shows them: the person's, and the agent's replies (a turn's replies in one,
    apart by a blank line). Tool calls and their results are left out."""
    shown: list[ChatMessage] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            shown.append(ChatMessage(role="user", text=m.text))
        elif isinstance(m, AIMessage) and m.text:
            if shown and shown[-1].role == "assistant":
                shown[-1] = ChatMessage(role="assistant", text=f"{shown[-1].text}\n\n{m.text}")
            else:
                shown.append(ChatMessage(role="assistant", text=m.text))
    return shown


def _close_open_calls(agent, thread_id: str) -> None:
    """Answers each tool call of the chat's last model reply that has no result, which a server that stopped while
    the tool ran leaves behind, with "Stopped before this finished.": the model refuses a chat with a call
    unanswered."""
    config = _config(thread_id)
    messages = agent.get_state(config).values.get("messages", [])
    last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    open_calls = [c for c in (last.tool_calls if last else []) if c["id"] not in answered]
    if open_calls:
        agent.update_state(config, {"messages": [ToolMessage(STOPPED_CALL, tool_call_id=c["id"], name=c["name"],
                                                             status="error") for c in open_calls]},
                           as_node="tools")


def _outcome(job: Job) -> tuple[list[dict], Optional[dict]]:
    """Waits for the job reading a file: the rows it read, or its error ({code, message, status_code})."""
    job.done.wait()
    if job.status == SUCCEEDED:
        return job.result["transactions"], None
    return [], job.error or {"code": "cancelled", "message": "Reading it was cancelled.", "status_code": 409}


def _unread(step: dict, name: str, reason: str) -> dict:
    return {**step, "text": f"Couldn't read {name}: {reason}", "state": "failed", "reason": reason}


def _mismatch(name: str, rows: list[dict], payment: Optional[Transaction]) -> str:
    """Why a document sent for a question didn't settle it: what it is for against the payment."""
    read = rows[0] if rows else None
    theirs = (f"{read.get('counterparty') or read['description']}, {_gbp(read.get('document_total') or read['gross'])}"
              if read else "nothing to pay")
    ours = f"{payment.counterparty or payment.description}, {_gbp(payment.gross)}" if payment else "the payment"
    return f"Read {name}, but it doesn't match: {theirs} against {ours}."


def _until_done(events: queue.Queue) -> Iterator[ChatEvent]:
    while True:
        event = events.get()
        yield event
        if event.type == "done":
            return


class _NoModel(BaseChatModel):
    """Stands in for the agent's model when it can't be had (OPENROUTER_API_KEY isn't set), so the chats' memory
    can still be read and written. A turn checks Chat.unavailable and never calls it."""

    message: str

    @property
    def _llm_type(self) -> str:
        return "unavailable"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise ModelUnavailable(self.message)


class Chat:
    """The chat of one app: its records and agent (made on first use, so the app starts without the database
    answering or a model key), which chats are running, and the clients' locks."""

    def __init__(self, settings: Settings, store: MongoStore, blobs: BlobStore, *, read: Reader, vision=None,
                 model: Optional[BaseChatModel] = None):
        """read: reads a file sent to the chat. vision: the model client that transcribes images for the agent's
        tools. model: the agent's chat model; by default LEDGERSYNC_AGENT_MODEL through OpenRouter."""
        self.settings, self.store, self.blobs, self.read_file, self.vision = settings, store, blobs, read, vision
        self._model = model
        self.locks, self.runs = ClientLocks(), ThreadRuns()
        self.unavailable: Optional[str] = None   # why the agent's model can't be used, once the agent is made
        self._making = threading.Lock()
        self._records: Optional[AgentRecords] = None
        self._agent = None

    @property
    def records(self) -> AgentRecords:
        with self._making:
            if self._records is None:
                self._records = AgentRecords(self.store.db)
            return self._records

    @property
    def agent(self):
        records = self.records
        with self._making:
            if self._agent is None:
                model = self._model
                if model is None:
                    try:
                        model = chat_model(self.settings)
                    except ModelUnavailable as exc:
                        model, self.unavailable = _NoModel(message=exc.message), exc.message
                deps = AgentDeps(store=self.store, records=records, blobs=self.blobs, vision=self.vision,
                                 is_cancelled=self.runs.is_cancelled)
                self._agent = build_agent(self.settings, deps, model=model)
            return self._agent

    # ─── Threads ───────────────────────────────────────────────────────────────

    def thread(self, thread_id: str) -> ChatThread:
        found = self.records.thread(thread_id)
        if found is None:
            raise NotFound("This chat was not found.")
        return found

    def new_thread(self, client_id: int) -> ChatThread:
        """A chat on the client's open year, called "New chat" until its first message."""
        require_active(self.store.get_client(client_id))
        return self.records.create_thread(client_id, _open_year(self.store, client_id), NEW_CHAT)

    def detail(self, thread: ChatThread) -> ThreadDetail:
        """The chat's messages from its memory, and its last summary with the questions and the posted trial
        balance as they are now (another chat on the client may have answered or posted since)."""
        state = self.agent.get_state(_config(thread.id))
        kept = self.records.summary(thread.id)
        last = None if kept is None else ReviewSummary.model_validate(kept).model_copy(update={
            "questions": self.records.questions(thread.client_id, thread.year_start),
            "trial_balance": ledger_book.posted_trial_balance(self.store, self.records, thread.client_id,
                                                              thread.year_start)})
        return ThreadDetail(thread=thread, messages=_messages(state.values.get("messages", [])), summary=last)

    def delete(self, thread: ChatThread) -> None:
        """Removes the chat and its memory; StillWorking while a message runs on it."""
        with self.runs.running(thread.id):
            self.agent.checkpointer.delete_thread(thread.id)
            self.records.delete_thread(thread.id)

    # ─── A message ─────────────────────────────────────────────────────────────

    def send(self, thread: ChatThread, text: str, files: list[ChatFile]) -> Iterator[ChatEvent]:
        """Takes the chat's one run (StillWorking while it has one) and works the message on a thread of its own,
        which lets the run go when it ends however it ends. Returns its events as they come, ending with done."""
        self.runs.start(thread.id)
        events: queue.Queue = queue.Queue()

        def work() -> None:
            try:
                for event in self.turn(thread, text, files):
                    events.put(event)
            except LedgerSyncError as exc:
                events.put(_error(exc.code, exc.message))
            except Exception:   # noqa: BLE001 - every failure ends the turn with an error event
                logger.exception("A chat turn failed")
                events.put(_error("agent_failed", FAILED))
            finally:
                self.runs.finish(thread.id)
                events.put(ChatEvent("done", {}))

        try:
            threading.Thread(target=work, name=f"chat-{thread.id[:8]}", daemon=True).start()
        except BaseException:
            self.runs.finish(thread.id)
            raise
        return _until_done(events)

    def turn(self, thread: ChatThread, text: str, files: list[ChatFile]) -> Iterator[ChatEvent]:
        """One message: the files read, the agent's run (or model_unavailable), then the dates from the bank
        recorded, the questions worked out and the year posted, and the summary. The first message titles the
        chat."""
        client = self.store.get_client(thread.client_id)
        since = since_now(self.records, client.id)
        self.records.touch_thread(thread.id, title=_title(text) if thread.title == NEW_CHAT else None)
        if files:
            yield ChatEvent("status", {"text": f"Reading {_count(len(files), 'file')}…"})
            yield from self._read_all(client, thread.year_start, files)
        yield ChatEvent("status", {"text": "Working on it…"})
        agent = self.agent
        if self.unavailable:
            yield _error("model_unavailable", self.unavailable)
        else:
            _close_open_calls(agent, thread.id)
            yield from stream_turn(agent, ChatContext(client.id, thread.id, thread.year_start), text)
        yield ChatEvent("status", {"text": "Posting the books…"})
        with self.locks.hold(client.id):
            post = self._write(thread, since)
        yield ChatEvent("summary", self._summarised(thread, since, post).model_dump(mode="json"))

    def _read_all(self, client: Client, year_start: dt.date, files: list[ChatFile]) -> Iterator[ChatEvent]:
        """Starts reading every file (side by side, as many as the job registry runs at once), then says how each
        went, in order: "Reading <name>…", then "Read <name> (n rows)" or "Couldn't read <name>: <reason>"."""
        reading = []
        for k, file in enumerate(files):
            step = {"id": f"read-{k}", "text": f"Reading {file.name}…", "state": "running", "file": file.name}
            yield ChatEvent("step", step)
            try:
                reading.append((step, file.name, self.read_file(client, file, year_start)))
            except LedgerSyncError as exc:
                yield ChatEvent("step", _unread(step, file.name, exc.message))
        for step, name, job in reading:
            rows, error = _outcome(job)
            yield ChatEvent("step", _unread(step, name, error["message"]) if error else {
                **step, "text": f"Read {name} ({_count(len(rows), 'row')})", "state": "done", "rows": len(rows)})

    def _write(self, thread: ChatThread, since: Since) -> PostResult:
        """With the client's lock held: records the rows matching dated from the bank, works the questions out and
        posts the year, with the decisions this chat made since then behind the posting."""
        record_automatic(self.store, self.records, client_id=thread.client_id, year_start=thread.year_start,
                         thread_id=thread.id)
        questions.refresh(self.store, self.records, thread.client_id, thread.year_start)
        return ledger_book.post(self.store, self.records, thread.client_id, thread.year_start,
                                decision_ids=[d.id for d in sorted_since(self.records, thread, since)])

    def _summarised(self, thread: ChatThread, since: Since, post: PostResult) -> ReviewSummary:
        """The summary, kept as the chat's last."""
        done = summary(self.store, self.records, thread, since, post)
        self.records.save_summary(thread.id, done.model_dump(mode="json"))
        return done

    # ─── Answers ───────────────────────────────────────────────────────────────

    def _question(self, thread: ChatThread, question_id: str) -> Question:
        asked = self.records.question(question_id)
        if asked is None or asked.client_id != thread.client_id:
            raise NotFound("This question was not found.")
        return asked

    def answer(self, thread: ChatThread, question_id: str, answer: questions.Answer) -> ReviewSummary:
        """Applies the person's answer without the model (questions.answer works the questions out and posts),
        notes it in the chat's memory as "Answered: <question> → <answer>", and returns the summary. StillWorking
        while a message runs on the chat; an answer to a question already resolved changes nothing."""
        require_active(self.store.get_client(thread.client_id))
        with self.runs.running(thread.id):
            asked = self._question(thread, question_id)
            since = since_now(self.records, thread.client_id)
            with self.locks.hold(thread.client_id):
                questions.answer(self.store, self.records, client_id=thread.client_id, thread_id=thread.id,
                                 question_id=question_id, answer=answer)
                post = ledger_book.post(self.store, self.records, thread.client_id, thread.year_start)
            if asked.status != "resolved":
                said = self.records.question(question_id)
                agent = self.agent
                _close_open_calls(agent, thread.id)
                agent.update_state(_config(thread.id),
                                   {"messages": [HumanMessage(f"Answered: {said.text} → {said.answer}")]},
                                   as_node=START)
            self.records.touch_thread(thread.id)
            return self._summarised(thread, since, post)

    def document(self, thread: ChatThread, question_id: str, file: ChatFile) -> ReviewSummary:
        """Reads the file sent for a question into the client's documents, then records, works the questions out and
        posts as a turn ends. If the question is still asked, its reason says the file didn't match: "Read <name>,
        but it doesn't match: <payee>, £<total> against <payee>, £<amount>." Returns the summary."""
        client = self.store.get_client(thread.client_id)
        require_active(client)
        asked = self._question(thread, question_id)
        if asked.kind not in _DOCUMENT_KINDS:
            raise InvalidInput("This question isn't asking for a document.")
        since = since_now(self.records, client.id)
        rows, error = _outcome(self.read_file(client, file, thread.year_start))
        if error:
            refused = LedgerSyncError(f"Couldn't read {file.name}: {error['message']}", code=error.get("code"))
            refused.status_code = error.get("status_code", 422)
            raise refused
        with self.locks.hold(client.id):
            post = self._write(thread, since)
            still = self.records.question(question_id)
            if still.status != "resolved":
                payment = next((r.tx for r in self.store.rows(client.id) if r.id in still.row_ids[:1]), None)
                self.records.set_question_why(question_id, _mismatch(file.name, rows, payment))
        self.records.touch_thread(thread.id)
        return self._summarised(thread, since, post)
