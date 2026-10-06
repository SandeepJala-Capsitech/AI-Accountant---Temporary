"""FastAPI entry point: a thin HTTP layer over the ledgersync package."""
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ledgersync import accounts, adapter, intake, pipeline, posting
from ledgersync import ledger as books
from ledgersync.accounts import choosable
from ledgersync.checks import normalise
from ledgersync.config import Settings
from ledgersync.errors import LedgerSyncError, UnreadableFile
from ledgersync.extractor import TransactionExtractor
from ledgersync.groq_client import GroqClient
from ledgersync.jobs import JobStore
from ledgersync.matching import match
from ledgersync.models import (AnalysisResult, BusinessSettings, BusinessType, Client, ClientFields, ClientPatch,
                               ClientSummary, Ledger, LedgerRequest, ManualUpload, RowPatch, TransactionList,
                               TrialBalance)
from ledgersync.store import Store

logger = logging.getLogger("ledgersync.server")


def configure_logging(level: str) -> None:
    """LEDGERSYNC_LOG_LEVEL applies to our own loggers only. Third-party libraries stay at
    WARNING because some can log document text at DEBUG."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("ledgersync").setLevel(level)


def create_app(settings: Optional[Settings] = None, *, model_client=None, store: Optional[Store] = None) -> FastAPI:
    """The API; tests pass a stand-in for the Groq client as model_client, and a store in a temporary folder."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    model_client = model_client or GroqClient(settings)
    store = store or Store(settings.db_path)   # opened on first use, not now
    extractor = TransactionExtractor(model_client, business_name=settings.business_name)
    jobs = JobStore(ttl_seconds=settings.job_ttl_seconds, max_workers=settings.max_parallel_jobs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.warmup:
            threading.Thread(target=model_client.warm_up, name="model-warmup", daemon=True).start()
        yield
        jobs.shutdown()

    app = FastAPI(
        title="UK LedgerSync API",
        description="Qwen vision model on Groq → structured accounting data → trial balance",
        lifespan=lifespan,
    )
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

    def run_analysis(item, ctx) -> dict:
        extraction = pipeline.analyze(item, extractor, ctx)
        transactions = adapter.to_transactions([row.model_dump() for row in extraction.data],
                                               source=item.kind, settings=BusinessSettings(business_name=settings.business_name))
        return AnalysisResult(transactions=transactions, warnings=extraction.warnings,
                              model=extraction.model).model_dump(mode="json")

    @app.post("/api/analyze", status_code=202)
    def analyze_transaction(text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None)):
        """Validates the input now (413/415/422), then analyses it in a background job."""
        if file is not None and file.filename:
            data = intake.read_limited(file.file, settings.max_upload_bytes)
            item = intake.load_upload(file.filename, data, settings.max_pdf_pages)
        elif text is not None:
            item = intake.from_text(text, settings.max_upload_bytes)
        else:
            raise UnreadableFile("Provide text or a file to analyse.")
        model_client.ensure_available()   # fail fast with 503 instead of queueing doomed work
        job = jobs.submit(lambda ctx: run_analysis(item, ctx))
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
        return books.change_row(store, client_id, row_id, patch)

    @app.post("/api/clients/{client_id}/uploads", response_model=Ledger, status_code=201)
    def add_manual_rows(client_id: int, upload: ManualUpload):
        return books.add_manual(store, client_id, upload)

    @app.delete("/api/clients/{client_id}/uploads/{upload_id}", response_model=Ledger)
    def remove_upload(client_id: int, upload_id: int):
        return books.remove_upload(store, client_id, upload_id)

    @app.get("/api/clients/{client_id}/trial-balance", response_model=TrialBalance)
    def client_trial_balance(client_id: int):
        return books.trial_balance(store, client_id)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = Settings.from_env()
    uvicorn.run("server:app", host=s.host, port=s.port, reload=s.reload)
