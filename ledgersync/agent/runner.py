"""Runs agent jobs: starts, cancels and retries them, and carries on the unfinished ones when the server starts.

Each job runs its graph on a thread from a JobStore kept for agent jobs alone, so a long run never holds up
document reading. While a client's job is preparing its rows can't be changed, and a job can't start while
the client's documents are being read. Only a job's own thread writes its feed while it runs."""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import Counter
from contextlib import contextmanager
from typing import Iterator, Optional

from ledgersync import ledger as books
from ledgersync.errors import InvalidInput, JobRunning, LedgerSyncError
from ledgersync.jobs import JobCancelled, JobStore
from ledgersync.models import Client
from ledgersync.records.jobs import JobRecord, JobRecords
from ledgersync.store_mongo import MongoStore

from .state import JobState

logger = logging.getLogger(__name__)

# Held while deciding whether a job may start or documents may be read, and while a job saves how it ended:
# so two quick starts can't both begin a job, an analysis and a job can't both get going for one client, and
# a cancel can't slip in between a job finishing and saying so. One lock for the whole process.
_LOCK = threading.Lock()

NOT_CARRIED_ON = "This job couldn't carry on from where it stopped. Details are in the server log."


class Busy:
    """Counts the analyses reading documents for each client right now."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reading: Counter[int] = Counter()

    def begin(self, client_id: int) -> None:
        with self._lock:
            self._reading[client_id] += 1

    def end(self, client_id: int) -> None:
        with self._lock:
            self._reading[client_id] -= 1
            if self._reading[client_id] <= 0:
                del self._reading[client_id]

    def busy(self, client_id: int) -> bool:
        with self._lock:
            return self._reading[client_id] > 0


class AgentRunner:
    def __init__(self, graph, records: JobRecords, store: MongoStore, jobs: JobStore, busy: Busy):
        self.records = records
        self._graph = graph
        self._store = store
        self._jobs = jobs
        self._busy = busy

    def start(self, client_id: int, period_start: dt.date, period_end: dt.date) -> JobRecord:
        """Starts a job on the client's rows for the period. Refused while the client has no uploads, while
        its documents are being read, or while it already has a job running."""
        client = self._store.get_client(client_id)
        books.require_active(client)
        if not self._store.uploads(client_id):
            raise InvalidInput("Add this client's documents before starting a job.")
        with _LOCK:
            self._refuse_to_start(client)
            job = self.records.create(client_id, period_start, period_end)
        self._submit(job)
        return job

    def cancel(self, job_id: str) -> JobRecord:
        """Asks a preparing job to stop; its thread stops it before its next step and marks it cancelled.
        A job that has already ended is returned as it is."""
        with _LOCK:
            job = self.records.get(job_id)
            if job.status == "preparing" and not job.cancel_requested:
                job = self.records.update(job_id, cancel_requested=True)
        return job

    def retry(self, job_id: str) -> JobRecord:
        """Runs a failed job again, from the step that failed."""
        with _LOCK:
            job = self.records.get(job_id)
            if job.status != "failed":
                raise InvalidInput("Only a failed job can be retried.")
            self._refuse_to_start(self._store.get_client(job.client_id))
            job = self.records.update(job_id, status="preparing", error=None)
        self._submit(job)
        return job

    def resume_unfinished(self) -> int:
        """Carries on every job left preparing when the server stopped, each from its last checkpoint, and
        returns how many. Never raises, so the server always starts: a job that can't be read is logged and
        skipped, and one that can't be carried on is logged and marked failed."""
        try:
            # Bare ids, so one job that can't be read doesn't hide the others.
            ids = [doc["_id"] for doc in self._store.db["jobs"].find({"status": "preparing"}, {"_id": 1})
                   .sort("created_at", 1)]
        except Exception:
            logger.exception("Could not look for unfinished agent jobs, so none were resumed")
            return 0
        resumed = 0
        for job_id in ids:
            try:
                job = self.records.get(job_id)
            except Exception:
                logger.exception("Agent job %s could not be read, so it was not resumed", job_id)
                continue
            try:
                self._submit(job)
                resumed += 1
            except Exception:
                logger.exception("Agent job %s could not be resumed", job_id)
                try:
                    self.records.update(job_id, status="failed", error=NOT_CARRIED_ON)
                except Exception:
                    logger.exception("Agent job %s could not be marked failed", job_id)
        if resumed:
            logger.info("Resumed %d unfinished agent job(s)", resumed)
        return resumed

    def refuse_while_running(self, client_id: int) -> None:
        """Raises JobRunning while the client's job is preparing: its rows can't change until it finishes."""
        if self.records.active_for(client_id) is not None:
            name = self._store.get_client(client_id).name
            raise JobRunning(f"{name}'s job is running. Rows can be changed once it finishes.")

    @contextmanager
    def reading(self, client_id: int) -> Iterator[None]:
        """Counts documents as being read for the client while the block runs, so no job starts meanwhile.
        Refused with JobRunning when the client's job started first (say, while this analysis was queued)."""
        with _LOCK:
            self.refuse_while_running(client_id)
            self._busy.begin(client_id)
        try:
            yield
        finally:
            self._busy.end(client_id)

    def _refuse_to_start(self, client: Client) -> None:
        """The checks a start or retry makes under _LOCK, just before the job is marked preparing."""
        if self._busy.busy(client.id):
            raise JobRunning("Documents are still being read. Start the job when they're done.")
        if self.records.active_for(client.id) is not None:
            raise JobRunning(f"A job is already running for {client.name}. Wait for it to finish or cancel it.")

    def _submit(self, job: JobRecord) -> None:
        self._jobs.submit(lambda ctx: self._run(job))

    def _run(self, job: JobRecord) -> None:
        """Runs the job's graph on this thread: from its last checkpoint when it has one, otherwise from the
        start. Then saves how it ended."""
        config = {"configurable": {"thread_id": job.id}}
        try:
            first: Optional[JobState] = None if self._graph.get_state(config).created_at else _first_state(job)
        except Exception:
            logger.exception("Agent job %s: its last checkpoint could not be read", job.id)
            self._end(job.id, error=NOT_CARRIED_ON)
            return
        try:
            self._graph.invoke(first, config, durability="sync")   # each step is saved before the next starts
        except JobCancelled:
            self._end(job.id)
        except LedgerSyncError as exc:
            logger.warning("Agent job %s failed: %s", job.id, exc.code)
            self._end(job.id, error=exc.message)
        except Exception:
            logger.exception("Agent job %s failed", job.id)
            self._end(job.id, crashed=True)
        else:
            self._end(job.id)

    def _end(self, job_id: str, *, error: Optional[str] = None, crashed: bool = False) -> None:
        """Saves how the job ended, with a last line in its feed: cancelled when a cancel was asked for (even
        if the job then finished or failed), otherwise failed with the error, or else awaiting review.
        The status is written last, so once a job isn't preparing its thread writes nothing more."""
        with _LOCK:
            job = self.records.get(job_id)
            if job.cancel_requested:
                self.records.event(job_id, job.stage, "Cancelled.")
                self.records.update(job_id, status="cancelled")
            elif error is not None or crashed:
                error = error or f"Something went wrong at the {job.stage} step. Details are in the server log."
                self.records.event(job_id, job.stage, f"Stopped: {error}")
                self.records.update(job_id, status="failed", error=error)
            else:
                self.records.event(job_id, "done", "Finished. The trial balance is ready for review.")
                self.records.update(job_id, status="awaiting_review", stage="done")


def _first_state(job: JobRecord) -> JobState:
    return JobState(job_id=job.id, client_id=job.client_id, period_start=job.period_start.isoformat(),
                    period_end=job.period_end.isoformat(), fix_rounds=0, gate_passed=False)
