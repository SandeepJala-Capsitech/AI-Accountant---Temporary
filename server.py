"""FastAPI entry point: a thin HTTP layer over the ledgersync package."""
import datetime as dt
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from ledgersync import accounts, adapter, books, intake, ledger, pipeline, posting
from ledgersync.blobs import BlobStore, GridFSBlobStore
from ledgersync.accounts import choosable
from ledgersync.checks import normalise
from ledgersync.config import Settings
from ledgersync.errors import InvalidInput, LedgerSyncError, NotFound, RefusedRequest, StorageError, UnreadableFile
from ledgersync.extractor import TransactionExtractor
from ledgersync.model_client import ModelClient
from ledgersync.jobs import JobStore
from ledgersync.matching import match
from ledgersync.models import (AnalysisResult, BusinessSettings, BusinessType, Client, ClientFields, ClientPatch,
                               ClientSummary, Ledger, LedgerRequest, ManualUpload, OpeningBalances, RowPatch,
                               TransactionList, TrialBalance, UploadPatch, Year)
from ledgersync.store_mongo import MongoStore

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def configure_logging(level: str) -> None:
    """LEDGERSYNC_LOG_LEVEL applies to our own loggers only. Third-party libraries stay at
    WARNING because some can log document text at DEBUG."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("ledgersync").setLevel(level)


def create_app(settings: Optional[Settings] = None, *, model_client=None, store: Optional[MongoStore] = None,
               blobs: Optional[BlobStore] = None) -> FastAPI:
    """The API; tests pass a stand-in for the model client as model_client and a store on a test database."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    model_client = model_client or ModelClient(settings)
    if store is None:
        if not settings.mongodb_uri:
            raise StorageError("Set MONGODB_URI in .env: Super Accountant keeps its data in MongoDB.")
        store = MongoStore(settings.mongodb_uri, settings.mongodb_db_name)
    if blobs is None:
        blobs = GridFSBlobStore(store.db)   # originals live beside the rows
    extractor = TransactionExtractor(model_client, business_name=settings.business_name)
    jobs = JobStore(ttl_seconds=settings.job_ttl_seconds, max_workers=settings.max_parallel_jobs)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.warmup:
            threading.Thread(target=model_client.warm_up, name="model-warmup", daemon=True).start()
        yield
        jobs.shutdown()

    app = FastAPI(
        title="Super Accountant API",
        description="Qwen vision model on OpenRouter or Groq → structured accounting data → trial balance",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["*"])

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

    def run_analysis(item, ctx, client: Optional[Client] = None, name: str = "Pasted text", sha256: str = "",
                     bank_account: Optional[str] = None, year_start: Optional[dt.date] = None) -> dict:
        """Reads one input. For a client: the model is told its name, kind of business and its other bank accounts,
        the rows are booked with its settings, and they are saved to it as an upload when any were found (not after
        a cancel), added to the year starting year_start. A statement is for bank_account, or the client's only
        account; a client with several must say which, and is refused once the reading shows it is a statement."""
        own = None
        if client is None:
            reader, rules = extractor, BusinessSettings(business_name=settings.business_name)
        else:
            rules = ledger.settings_for(client)
            codes = ledger.bank_codes(client)
            own = bank_account or (codes[0] if len(codes) == 1 else None)
            reader = TransactionExtractor(model_client, business_name=client.name, business_type=client.business_type,
                                          bank_codes=ledger.bank_choices(client, own or rules.bank_account))
        extraction = pipeline.analyze(item, reader, ctx)
        # A line coded to the statement's own account goes to Suspense; one coded to another is a transfer.
        reading = rules.model_copy(update={"bank_account": own or rules.bank_account})
        transactions = adapter.to_transactions([row.model_dump() for row in extraction.data], source=item.kind,
                                               settings=reading, opening_balance=extraction.opening_balance,
                                               closing_balance=extraction.closing_balance, agent=extraction.agent)
        statement = any(tx.document_type == "statement" for tx in transactions)
        if client is not None and statement and own is None:
            raise InvalidInput("Choose which bank account this statement is for.")
        if client is not None:   # booked as the ledger books them: the statement's lines on its account
            transactions = [normalise(tx.model_copy(update={"bank_account": own}) if tx.document_type == "statement"
                                      else tx, rules) for tx in transactions]
        result = AnalysisResult(transactions=transactions, warnings=extraction.warnings,
                                model=extraction.model).model_dump(mode="json")
        if client is not None and transactions:
            ctx.commit()   # from here a cancel can't discard the job: the rows are being saved
            result["client_id"] = client.id
            result["upload_id"] = store.add_upload(client.id, name, item.kind, transactions, sha256=sha256,
                                                   model=extraction.model or "", warnings=extraction.warnings,
                                                   bank_account=own if statement else None,
                                                   year_start=year_start)
        return result

    @app.post("/api/analyze", status_code=202)
    def analyze_transaction(text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None),
                            client_id: Optional[int] = Form(None), bank_account: Optional[str] = Form(None),
                            year_start: Optional[dt.date] = Form(None), x_ledgersync: Optional[str] = Header(None)):
        """Validates the input now (403/404/413/415/422), then analyses it in a background job; with a
        client_id, the job saves the rows it finds to that client, a statement's on bank_account (one of its codes),
        added to the year starting year_start (the year picked when the file was added)."""
        if not x_ledgersync:
            # A form on any web page can post here without the browser asking the API first. The LedgerSync
            # pages send this header, which a form can't, and another site's script can't unless CORS allows it.
            raise RefusedRequest("Analyses are taken from the Super Accountant pages only: the X-LedgerSync "
                                 "header is missing.")
        client = None
        if client_id is not None:
            client = store.get_client(client_id)
            ledger.require_active(client)
            if bank_account:
                ledger.check_bank_account(client, bank_account)
            if year_start is not None:
                year_of(client_id, year_start)
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
            return run_analysis(item, ctx, client, name, sha256, bank_account or None,
                                year_start if client is not None else None)

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
    def list_accounts(business_type: Optional[BusinessType] = None, client_id: Optional[int] = None):
        """The chart; with a business type, only the accounts a row of that business can be coded to; with a client,
        those a row of that client can be coded to, its own bank accounts and cards among them."""
        if client_id is not None:
            client = store.get_client(client_id)
            chosen = choosable(client.business_type, ledger.bank_choices(client))
        else:
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
        return ledger.summaries(store, archived)

    @app.post("/api/clients", response_model=Client, status_code=201)
    def add_client(fields: ClientFields):
        return ledger.add_client(store, fields)

    @app.get("/api/clients/{client_id}", response_model=Client)
    def get_client(client_id: int):
        return store.get_client(client_id)

    @app.patch("/api/clients/{client_id}", response_model=Client)
    def change_client(client_id: int, patch: ClientPatch):
        return ledger.change_client(store, client_id, patch)

    # ─── A client's years ────────────────────────────────────────────────────────

    def year_of(client_id: int, start: dt.date) -> Year:
        """The client's year starting on start; 404 when it is not one."""
        year = next((y for y in books.years_of(store, client_id, dt.date.today()) if y.start == start), None)
        if year is None:
            raise NotFound("That year was not found.")
        return year

    @app.get("/api/clients/{client_id}/years", response_model=list[Year])
    def client_years(client_id: int):
        return books.years_of(store, client_id, dt.date.today())

    @app.get("/api/clients/{client_id}/years/{start}/ledger", response_model=Ledger)
    def year_ledger(client_id: int, start: dt.date):
        return books.ledger(store, client_id, start)

    @app.get("/api/clients/{client_id}/years/{start}/trial-balance", response_model=TrialBalance)
    def year_trial_balance(client_id: int, start: dt.date):
        return books.trial_balance(store, client_id, start)

    @app.get("/api/clients/{client_id}/opening-balances", response_model=OpeningBalances)
    def opening_balances(client_id: int):
        store.get_client(client_id)
        return store.opening(client_id)

    @app.put("/api/clients/{client_id}/opening-balances", response_model=OpeningBalances)
    def set_opening_balances(client_id: int, opening: OpeningBalances):
        books.set_opening(store, client_id, opening)
        return store.opening(client_id)

    @app.patch("/api/clients/{client_id}/rows/{row_id}", response_model=Ledger)
    def change_row(client_id: int, row_id: int, patch: RowPatch):
        return ledger.change_row(store, client_id, row_id, patch)

    @app.delete("/api/clients/{client_id}/rows/{row_id}", response_model=Ledger)
    def remove_row(client_id: int, row_id: int):
        return ledger.remove_row(store, client_id, row_id)

    @app.post("/api/clients/{client_id}/uploads", response_model=Ledger, status_code=201)
    def add_manual_rows(client_id: int, upload: ManualUpload):
        return ledger.add_manual(store, client_id, upload)

    @app.patch("/api/clients/{client_id}/uploads/{upload_id}", response_model=Ledger)
    def change_upload(client_id: int, upload_id: int, patch: UploadPatch):
        return ledger.set_upload_bank(store, client_id, upload_id, patch.bank_account)

    @app.delete("/api/clients/{client_id}/uploads/{upload_id}", response_model=Ledger)
    def remove_upload(client_id: int, upload_id: int):
        return ledger.remove_upload(store, client_id, upload_id)

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

    def workbook_download(content: bytes, name: str) -> Response:
        """An Excel workbook as a download under its file name."""
        plain = name.encode("ascii", "replace").decode().replace("?", "_")
        return Response(content, media_type=XLSX,
                        headers={"Content-Disposition": f"attachment; filename=\"{plain}\"; "
                                                        f"filename*=UTF-8''{quote(name)}"})

    @app.get("/api/clients/{client_id}/years/{start}/export.xlsx")
    def export_year(client_id: int, start: dt.date):
        """The year's trial balance and transactions as an Excel workbook (archived clients too)."""
        return workbook_download(*books.export(store, client_id, start, dt.date.today()))

    return app


if __name__ == "__main__":
    import uvicorn

    s = Settings.from_env()
    # A factory, so importing this module opens no database; the API starts (or refuses to) here.
    uvicorn.run("server:create_app", factory=True, host=s.host, port=s.port, reload=s.reload)
