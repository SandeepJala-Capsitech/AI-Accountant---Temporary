"""FastAPI entry point: a thin HTTP layer over the ledgersync package."""
import datetime as dt
import hashlib
import logging
import threading
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from langgraph.checkpoint.mongodb import MongoDBSaver
from pydantic import BaseModel

from ledgersync import accounts, adapter, intake, pipeline, posting
from ledgersync.agent import (AgentRunner, Busy, Nodes, ToolDeps, build_graph, make_classify, make_fix, make_gate,
                              make_ingest, make_requests, make_snapshot, staged, stub_nodes)
from ledgersync.agent.llm import chat_model
from ledgersync.blobs import BlobStore, GridFSBlobStore, MemoryBlobStore
from ledgersync import ledger as books
from ledgersync.accounts import choosable
from ledgersync.checks import normalise
from ledgersync.config import Settings
from ledgersync.errors import (AgentNotConfigured, InvalidInput, LedgerSyncError, ModelUnavailable, NotFound,
                               RefusedRequest, UnreadableFile)
from ledgersync.extractor import TransactionExtractor
from ledgersync.model_client import ModelClient
from ledgersync.jobs import JobStore
from ledgersync.matching import match
from ledgersync.notify import Notifier, notifier_for, send_reminders
from ledgersync.models import (AnalysisResult, BusinessSettings, BusinessType, Client, ClientFields, ClientPatch,
                               ClientSummary, Ledger, LedgerRequest, ManualUpload, RowPatch, TransactionList,
                               TrialBalance)
from ledgersync.records.audit import AuditLog
from ledgersync.records.exceptions import link_status
from ledgersync.records.jobs import JobEvent, JobRecord, JobRecords
from ledgersync.records.snapshots import XLSX
from ledgersync.store import Store
from ledgersync.store_mongo import MongoStore

logger = logging.getLogger("ledgersync.server")

UNKNOWN_LINK = "This link isn't valid. Ask your accountant for a new one."
RECEIVED_NOTE = "The client sent the document."
REMINDER_EVERY_SECONDS = 3600.0   # how often the reminder thread looks for document reminders due
# What the accountant's page may see of a document request. Never the upload token: it is the client's bearer secret.
REQUEST_FIELDS = ("id", "document_type", "period", "reason", "status", "sent_at", "received_at")


class JobPeriod(BaseModel):
    """The period an agent job works on."""

    period_start: dt.date
    period_end: dt.date


def configure_logging(level: str) -> None:
    """LEDGERSYNC_LOG_LEVEL applies to our own loggers only. Third-party libraries stay at
    WARNING because some can log document text at DEBUG."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("ledgersync").setLevel(level)


def create_app(settings: Optional[Settings] = None, *, model_client=None, store: Optional[Store] = None,
               blobs: Optional[BlobStore] = None, agent_nodes: Optional[Nodes] = None,
               notifier: Optional[Notifier] = None) -> FastAPI:
    """The API; tests pass a stand-in for the model client as model_client, a store in a temporary folder,
    stand-ins for the agent job's steps as agent_nodes, and a notifier that keeps the emails it is given."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    model_client = model_client or ModelClient(settings)
    if store is None:
        if settings.mongodb_uri:
            store = MongoStore(settings.mongodb_uri, settings.mongodb_db_name)
        else:
            store = Store(settings.db_path)
    if blobs is None:
        # Originals live beside the rows in MongoDB; without it they last until a restart (agent jobs need MongoDB anyway).
        blobs = GridFSBlobStore(store.db) if isinstance(store, MongoStore) else MemoryBlobStore()
    extractor = TransactionExtractor(model_client, business_name=settings.business_name)
    jobs = JobStore(ttl_seconds=settings.job_ttl_seconds, max_workers=settings.max_parallel_jobs)
    busy = Busy()
    notifier = notifier or notifier_for(settings)
    deps_lock = threading.Lock()
    built: list[ToolDeps] = []

    def tool_deps() -> Optional[ToolDeps]:
        """What the agent's tools, the client's upload link and the reminders work with, or None without MongoDB.
        Built on first use, so importing this module (which makes `app`) writes nothing to the database."""
        if not isinstance(store, MongoStore):
            return None
        with deps_lock:
            if not built:
                built.append(ToolDeps(store=store, records=JobRecords(store.db), audit=AuditLog(store.db),
                                      notifier=notifier, blobs=blobs, settings=settings))
            return built[0]

    runner: Optional[AgentRunner] = None
    agent_jobs: Optional[JobStore] = None
    if isinstance(store, MongoStore) and (settings.agent_model or agent_nodes is not None):
        deps = tool_deps()
        records = deps.records
        # LangGraph's checkpoints go in a database of their own beside the store's: <name>_checkpoints.
        checkpointer = MongoDBSaver(store.client, f"{store.db_name}_checkpoints")
        def model():
            return chat_model(settings)

        # The built steps; steps passed in by a test win. The agents build their model when they run, so the app
        # starts without one. The gate and the graph are given the same number of fix rounds, so the gate knows
        # which round is the last.
        steps = agent_nodes if agent_nodes is not None else replace(
            stub_nodes(records), ingest=make_ingest(deps), classify=make_classify(deps, model_factory=model),
            gate=make_gate(deps, max_fix_rounds=settings.agent_max_fix_rounds),
            fix=make_fix(deps, model_factory=model), requests=make_requests(deps), snapshot=make_snapshot(deps))
        graph = build_graph(staged(steps, records), checkpointer, max_fix_rounds=settings.agent_max_fix_rounds)
        # Threads of their own, so a long agent run never holds up document reading.
        agent_jobs = JobStore(ttl_seconds=settings.job_ttl_seconds, max_workers=settings.max_parallel_jobs)
        runner = AgentRunner(graph, records, store, agent_jobs, busy)

    def agent() -> AgentRunner:
        if runner is None:
            raise AgentNotConfigured("Agent jobs need MongoDB and LEDGERSYNC_AGENT_MODEL. Set both in .env.")
        return runner

    def unlocked(client_id: int) -> None:
        """Refuses a change to the client's rows while its agent job runs (without agent jobs, nothing is locked)."""
        if runner is not None:
            runner.refuse_while_running(client_id)

    def reading(client: Optional[Client]):
        """Counts an analysis for a client as documents being read, so no job starts for the client meanwhile."""
        return runner.reading(client.id) if runner is not None and client is not None else nullcontext()

    def remind(stop: threading.Event) -> None:
        """Sends the document reminders due, then looks again every hour until the app stops. Never raises: an
        error is logged and the next hour tries again."""
        while True:
            try:
                sent = send_reminders(tool_deps(), dt.datetime.now(dt.timezone.utc))
                if sent:
                    logger.info("Sent %d document reminder email(s)", sent)
            except Exception:
                logger.exception("Document reminders could not be sent; the next hour tries again")
            if stop.wait(REMINDER_EVERY_SECONDS):
                return

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.warmup:
            threading.Thread(target=model_client.warm_up, name="model-warmup", daemon=True).start()
        if runner is not None:
            runner.resume_unfinished()   # never raises: a job it can't carry on is logged and marked failed
        stop_reminders = threading.Event()
        reminders = None
        if isinstance(store, MongoStore):   # requests are kept in MongoDB, so without it there are none to remind
            reminders = threading.Thread(target=remind, args=(stop_reminders,), name="document-reminders", daemon=True)
            reminders.start()
        yield
        stop_reminders.set()
        if reminders is not None:
            reminders.join(timeout=10)
        jobs.shutdown()
        if agent_jobs is not None:
            agent_jobs.shutdown()        # a job still queued stays preparing and carries on at the next start

    app = FastAPI(
        title="Super Accountant API",
        description="Qwen vision model on OpenRouter or Groq → structured accounting data → trial balance",
        lifespan=lifespan,
    )
    app.state.busy = busy
    app.state.agent_runner = runner
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["*"])

    @app.exception_handler(LedgerSyncError)
    async def ledgersync_error(request: Request, exc: LedgerSyncError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": {"code": exc.code, "message": exc.message}})

    # ─── Health ────────────────────────────────────────────────────────────────

    @app.get("/api/health")
    def health_check():
        health = model_client.health()
        return {
            "status": "online",
            "model": model_client.model,
            "ai_reachable": health.reachable,
            "model_available": health.model_available,
            "ai_error": health.error,
            "max_upload_mb": settings.max_upload_mb,
            "max_parallel_jobs": settings.max_parallel_jobs,   # the UI sends this many files at once
        }

    # ─── Step 2 — Analyze (background job) ─────────────────────────────────────

    def run_analysis(item, ctx, client: Optional[Client] = None, name: str = "Pasted text", sha256: str = "") -> dict:
        """Reads one input. For a client: the model is told its name and kind of business, the rows are booked
        with its settings, and they are saved to it as an upload when any were found (not after a cancel)."""
        if client is None:
            reader, rules = extractor, BusinessSettings(business_name=settings.business_name)
        else:
            rules = books.settings_for(client)
            reader = TransactionExtractor(model_client, business_name=client.name, business_type=client.business_type)
        extraction = pipeline.analyze(item, reader, ctx)
        transactions = adapter.to_transactions([row.model_dump() for row in extraction.data], source=item.kind,
                                               settings=rules, opening_balance=extraction.opening_balance,
                                               closing_balance=extraction.closing_balance, agent=extraction.agent)
        result = AnalysisResult(transactions=transactions, warnings=extraction.warnings,
                                model=extraction.model).model_dump(mode="json")
        if client is not None and transactions:
            ctx.commit()   # from here a cancel can't discard the job: the rows are being saved
            result["client_id"] = client.id
            result["upload_id"] = store.add_upload(client.id, name, item.kind, transactions, sha256=sha256,
                                                   model=extraction.model or "", warnings=extraction.warnings)
        return result

    @app.post("/api/analyze", status_code=202)
    def analyze_transaction(text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None),
                            client_id: Optional[int] = Form(None), x_ledgersync: Optional[str] = Header(None)):
        """Validates the input now (403/404/409/413/415/422), then analyses it in a background job; with a
        client_id, the job saves the rows it finds to that client."""
        if not x_ledgersync:
            # A form on any web page can post here without the browser asking the API first. The LedgerSync
            # pages send this header, which a form can't, and another site's script can't unless CORS allows it.
            raise RefusedRequest("Analyses are taken from the Super Accountant pages only: the X-LedgerSync "
                                 "header is missing.")
        client = None
        if client_id is not None:
            client = store.get_client(client_id)
            books.require_active(client)
            unlocked(client_id)
        name, sha256, content_type = "Pasted text", "", ""
        if file is not None and file.filename:
            data = intake.read_limited(file.file, settings.max_upload_bytes)
            item = intake.load_upload(file.filename, data, settings.max_pdf_pages)
            name, sha256 = file.filename, hashlib.sha256(data).hexdigest()
            content_type = file.content_type or "application/octet-stream"
        elif text is not None:
            item = intake.from_text(text, settings.max_upload_bytes)
        else:
            raise UnreadableFile("Provide text or a file to analyse.")
        model_client.ensure_available()   # fail fast with 503 instead of queueing doomed work
        if sha256:
            blobs.put(data, name=name, content_type=content_type)

        def work(ctx):
            with reading(client):
                return run_analysis(item, ctx, client, name, sha256)

        job = jobs.submit(work)
        return {"job_id": job.id, "status": job.status}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return jobs.get(job_id).to_dict()

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str):
        return jobs.cancel(job_id).to_dict()

    # ─── Ledger ─────────────────────────────────────────────────────────────────

    @app.get("/api/accounts")
    def list_accounts(business_type: Optional[BusinessType] = None):
        """The chart; with a business type, only the accounts a row of that business can be coded to."""
        chosen = choosable(business_type) if business_type else accounts.CHART
        return [{"code": a.code, "name": a.name, "type": a.type.value, "vat": a.vat.value} for a in chosen]

    @app.post("/api/transactions/validate", response_model=TransactionList)
    def validate_transactions(request: LedgerRequest):
        """Splits VAT, works out the other side of each row, matches bank lines to the documents they pay
        and lists issues; no posting."""
        ready = [normalise(tx, request.settings) for tx in request.transactions]
        return TransactionList(transactions=match(ready, request.settings))

    @app.post("/api/trial-balance", response_model=TrialBalance)
    def generate_trial_balance(request: LedgerRequest):
        """Double-entry trial balance; 422 when a transaction cannot be posted."""
        return posting.trial_balance(request.transactions, request.settings)

    # ─── Clients: saved work (design of 2026-10-06) ─────────────────────────────

    @app.get("/api/clients", response_model=list[ClientSummary])
    def list_clients(archived: bool = False):
        return books.summaries(store, archived)

    @app.post("/api/clients", response_model=Client, status_code=201)
    def add_client(fields: ClientFields):
        return books.add_client(store, fields)

    @app.get("/api/clients/{client_id}", response_model=Client)
    def get_client(client_id: int):
        return store.get_client(client_id)

    @app.patch("/api/clients/{client_id}", response_model=Client)
    def change_client(client_id: int, patch: ClientPatch):
        return books.change_client(store, client_id, patch)

    @app.get("/api/clients/{client_id}/ledger", response_model=Ledger)
    def client_ledger(client_id: int):
        return books.build(store, client_id)

    @app.patch("/api/clients/{client_id}/rows/{row_id}", response_model=Ledger)
    def change_row(client_id: int, row_id: int, patch: RowPatch):
        unlocked(client_id)
        return books.change_row(store, client_id, row_id, patch)

    @app.delete("/api/clients/{client_id}/rows/{row_id}", response_model=Ledger)
    def remove_row(client_id: int, row_id: int):
        unlocked(client_id)
        return books.remove_row(store, client_id, row_id)

    @app.post("/api/clients/{client_id}/uploads", response_model=Ledger, status_code=201)
    def add_manual_rows(client_id: int, upload: ManualUpload):
        unlocked(client_id)
        return books.add_manual(store, client_id, upload)

    @app.delete("/api/clients/{client_id}/uploads/{upload_id}", response_model=Ledger)
    def remove_upload(client_id: int, upload_id: int):
        unlocked(client_id)
        return books.remove_upload(store, client_id, upload_id)

    @app.get("/api/clients/{client_id}/uploads/{upload_id}/file")
    def download_original(client_id: int, upload_id: int):
        """The file an upload was read from, as it was sent; 404 when the upload was pasted text or the file is gone."""
        store.get_client(client_id)
        kept = "The original file wasn't kept for this upload."
        upload = next((u for u in store.uploads(client_id) if u["id"] == upload_id), None)
        if upload is None:
            raise NotFound("That upload was not found.")
        if not upload["sha256"] or not blobs.exists(upload["sha256"]):
            raise NotFound(kept)
        blob = blobs.get(upload["sha256"])
        plain = blob.name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
        return Response(blob.data, media_type=blob.content_type,
                        headers={"Content-Disposition": f"attachment; filename=\"{plain}\"; "
                                                        f"filename*=UTF-8''{quote(blob.name)}"})

    @app.get("/api/clients/{client_id}/trial-balance", response_model=TrialBalance)
    def client_trial_balance(client_id: int):
        return books.trial_balance(store, client_id)

    def workbook_download(content: bytes, name: str) -> Response:
        """An Excel workbook as a download under its file name."""
        plain = name.encode("ascii", "replace").decode().replace("?", "_")
        return Response(content, media_type=XLSX,
                        headers={"Content-Disposition": f"attachment; filename=\"{plain}\"; "
                                                        f"filename*=UTF-8''{quote(name)}"})

    @app.get("/api/clients/{client_id}/export.xlsx")
    def export_client(client_id: int):
        """The client's trial balance and transactions as an Excel workbook (archived clients too)."""
        return workbook_download(*books.export(store, client_id, dt.date.today()))

    # ─── Agent jobs: the client's rows worked to a trial balance for review ─────

    @app.post("/api/clients/{client_id}/agent-jobs", response_model=JobRecord, status_code=202)
    def start_agent_job(client_id: int, period: JobPeriod):
        """Starts a job on the client's rows; its thread works through the steps while the page polls."""
        return agent().start(client_id, period.period_start, period.period_end)

    @app.get("/api/agent-jobs/{job_id}", response_model=JobRecord)
    def get_agent_job(job_id: str):
        return agent().records.get(job_id)

    @app.get("/api/agent-jobs/{job_id}/events", response_model=list[JobEvent])
    def agent_job_events(job_id: str, after: int = 0):
        """The job's feed after the line numbered `after`, oldest first: the page passes the last seq it showed."""
        records = agent().records
        records.get(job_id)   # an unknown job is 404, not an empty feed
        return records.events(job_id, after)

    @app.post("/api/agent-jobs/{job_id}/cancel", response_model=JobRecord)
    def cancel_agent_job(job_id: str):
        return agent().cancel(job_id)

    @app.post("/api/agent-jobs/{job_id}/retry", response_model=JobRecord, status_code=202)
    def retry_agent_job(job_id: str):
        return agent().retry(job_id)

    @app.get("/api/clients/{client_id}/agent-jobs/latest", response_model=Optional[JobRecord])
    def latest_agent_job(client_id: int):
        """The client's newest job, whatever its status, or null when it has had none: the page shows it when it
        opens. Without agent jobs this is null too, not the 503 of the other job routes, so the page still opens
        with "No job yet" and Start job gives the reason."""
        deps = tool_deps()
        return deps.records.latest_for(client_id) if deps is not None else None

    def job_deps(job_id: str) -> ToolDeps:
        """What the job's records are kept with. NotFound for an unknown job; the 503 of the other job routes without
        agent jobs."""
        agent().records.get(job_id)
        return tool_deps()

    @app.get("/api/agent-jobs/{job_id}/exceptions")
    def agent_job_exceptions(job_id: str):
        """What the agent couldn't settle in the job, in the order it logged it: the description, the assumption
        it made instead, its confidence (0 to 1) and whether it is still open."""
        return job_deps(job_id).exceptions.for_job(job_id)

    @app.get("/api/agent-jobs/{job_id}/requests")
    def agent_job_requests(job_id: str):
        """The documents the job asked the client for, in the order it asked, with where each has got to. Only the
        fields in REQUEST_FIELDS: the upload token is never given out here."""
        return [{field: asked[field] for field in REQUEST_FIELDS} for asked in job_deps(job_id).requests.for_job(job_id)]

    def snapshots_of(job_id: str):
        """The job's trial balance snapshots. NotFound for an unknown job; the 503 of the other job routes without
        agent jobs."""
        return job_deps(job_id).snapshots

    @app.get("/api/agent-jobs/{job_id}/snapshots")
    def agent_job_snapshots(job_id: str):
        """The trial balances the job saved, oldest version first: after posting, then after the gate, each with
        its lines, totals and problems (money as exact strings) and the blob ID of its workbook."""
        return snapshots_of(job_id).list(job_id)

    @app.get("/api/agent-jobs/{job_id}/snapshots/{version}/export.xlsx")
    def agent_job_snapshot_workbook(job_id: str, version: int):
        """The Excel workbook saved with one version of the job's trial balance; 404 for an unknown version."""
        snapshots = snapshots_of(job_id)
        blob = tool_deps().blobs.get(snapshots.get(job_id, version)["excel_blob_id"])
        return workbook_download(blob.data, blob.name)

    # ─── The client's upload link: the documents the agent asked for ───────────

    def linked(token: str) -> tuple[ToolDeps, Client, list[dict]]:
        """The requests one email's link is for, with their client. NotFound (UNKNOWN_LINK) when the link
        matches none, or its client is gone or archived."""
        deps = tool_deps()
        asked = deps.requests.by_token(token) if deps is not None else []
        if not asked:
            raise NotFound(UNKNOWN_LINK)
        try:
            client = store.get_client(asked[0]["client_id"])
        except NotFound:
            raise NotFound(UNKNOWN_LINK) from None
        if client.archived:
            raise NotFound(UNKNOWN_LINK)
        return deps, client, asked

    def take_in(deps: ToolDeps, claimed: dict, upload_id: int) -> None:
        """After the client's file is saved as an upload: the request this upload claimed is received and its
        exception resolved (once every document asked for it has arrived), each audited once as the system, and
        the client's latest job is superseded by a new run (see AgentRunner.documents_arrived)."""
        client_id, job_id = claimed["client_id"], claimed["job_id"]
        received = deps.requests.mark_received(claimed["id"], upload_id=upload_id, at=dt.datetime.now(dt.timezone.utc))
        if received is None:   # its claim stalled and another upload took it: that one records the receipt
            logger.warning("Request %s was no longer this upload's when upload %s was saved", claimed["id"], upload_id)
        else:
            deps.audit.append(actor="system", action="receive_document", client_id=client_id, job_id=job_id,
                              target={"request_id": claimed["id"]}, before={"status": "receiving"},
                              after={"status": "received", "upload_id": upload_id})
            waiting = [r for r in deps.requests.for_job(job_id)
                       if r["exception_id"] == claimed["exception_id"] and r["status"] not in ("received", "replaced")]
            if not waiting:
                _, resolved = deps.exceptions.resolve_once(claimed["exception_id"], note=RECEIVED_NOTE)
                if resolved:
                    deps.audit.append(actor="system", action="resolve_exception", client_id=client_id,
                                      job_id=job_id, target={"exception_id": claimed["exception_id"]},
                                      before={"status": "open"}, after={"status": "resolved", "note": RECEIVED_NOTE})
        if runner is not None:
            runner.documents_arrived(client_id)   # the rows changed either way

    @app.get("/api/requests/{token}")
    def requested_documents(token: str):
        """What the link asks the client for: the client's name and each document with its reason, and its status:
        "pending" until it arrives, then "received" (from the moment an upload of it is being read), or "replaced"
        once a newer request asks for the same document. Nothing else from the job."""
        _, client, asked = linked(token)
        now = dt.datetime.now(dt.timezone.utc)
        return {"client_name": client.name,
                "documents": [{"id": r["id"], "document_type": r["document_type"], "period": r["period"],
                               "reason": r["reason"], "status": link_status(r, now)} for r in asked]}

    @app.post("/api/requests/{token}/{request_id}/file", status_code=202)
    def send_requested_document(token: str, request_id: str, file: UploadFile = File(...),
                                x_ledgersync: Optional[str] = Header(None)):
        """Checks the file as /api/analyze does, claims the request for this upload (one at a time), keeps the
        file, then reads it in a background job into a new upload for the client: as the system, so past the row
        lock while a job runs. Once it is saved, the request is received, its exception resolved and the job
        superseded (see take_in); if the reading fails first, the claim is let go so the file can be sent again.
        The page polls /api/jobs/{job_id}."""
        if not x_ledgersync:
            raise RefusedRequest("Documents are taken from the Super Accountant pages only: the X-LedgerSync "
                                 "header is missing.")
        deps, client, asked = linked(token)
        if not any(r["id"] == request_id for r in asked):
            raise NotFound("This link doesn't ask for that document.")
        if not file.filename:
            raise UnreadableFile("Choose a file to send.")
        data = intake.read_limited(file.file, settings.max_upload_bytes)
        item = intake.load_upload(file.filename, data, settings.max_pdf_pages)
        name, sha256 = file.filename, hashlib.sha256(data).hexdigest()
        try:
            model_client.ensure_available()   # fail fast instead of queueing doomed work
        except ModelUnavailable:
            raise ModelUnavailable("Documents can't be read just now. Try again in a few minutes.") from None
        claimed = deps.requests.claim(request_id, token=token, now=dt.datetime.now(dt.timezone.utc))
        if claimed is None:   # received, replaced, or another upload of it is being read
            raise InvalidInput("This document has already arrived. Thank you.")

        def work(ctx):
            try:
                busy.begin(client.id)   # documents being read: no new run starts for the client meanwhile
                try:
                    result = run_analysis(item, ctx, client, name, sha256)
                finally:
                    busy.end(client.id)   # before take_in, which may start the superseding run
                if "upload_id" not in result:
                    raise UnreadableFile(f"No transactions were found in '{name}'. Check it's the document asked "
                                         "for, then send it again.")
            except BaseException:
                deps.requests.release(claimed)   # nothing was saved: the client can send the file again
                raise
            take_in(deps, claimed, result["upload_id"])
            return result

        try:
            blobs.put(data, name=name, content_type=file.content_type or "application/octet-stream")
            job = jobs.submit(work)
        except BaseException:
            deps.requests.release(claimed)
            raise
        return {"job_id": job.id, "status": job.status}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = Settings.from_env()
    uvicorn.run("server:app", host=s.host, port=s.port, reload=s.reload)
