"""FastAPI entry point: a thin HTTP layer over the ledgersync package."""
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ledgersync import accounts, adapter, intake, pipeline, posting
from ledgersync.checks import normalise
from ledgersync.config import Settings
from ledgersync.errors import LedgerSyncError, UnreadableFile
from ledgersync.extractor import TransactionExtractor
from ledgersync.groq_client import GroqClient
from ledgersync.jobs import JobStore
from ledgersync.models import AnalysisResult, BusinessSettings, LedgerRequest, TransactionList, TrialBalance

logger = logging.getLogger("ledgersync.server")


def configure_logging(level: str) -> None:
    """LEDGERSYNC_LOG_LEVEL applies to our own loggers only. Third-party libraries stay at
    WARNING because some log document text at DEBUG (pdfminer logs every parsed token)."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("ledgersync").setLevel(level)


def create_app(settings: Optional[Settings] = None, *, model_client=None) -> FastAPI:
    """The API; tests pass a stand-in for the Groq client as model_client."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    model_client = model_client or GroqClient(settings)
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
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])

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
    def list_accounts():
        return [{"code": a.code, "name": a.name, "type": a.type.value, "vat": a.vat.value} for a in accounts.CHART]

    @app.post("/api/transactions/validate", response_model=TransactionList)
    def validate_transactions(request: LedgerRequest):
        """Splits VAT, fills in the bank account and lists issues; no posting."""
        return TransactionList(transactions=[normalise(tx, request.settings) for tx in request.transactions])

    @app.post("/api/trial-balance", response_model=TrialBalance)
    def generate_trial_balance(request: LedgerRequest):
        """Double-entry trial balance; 422 when a transaction cannot be posted."""
        return posting.trial_balance(request.transactions, request.settings)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = Settings.from_env()
    uvicorn.run("server:app", host=s.host, port=s.port, reload=s.reload)
