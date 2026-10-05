# Phase 1 — Reliability & Security Quick Wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every failure explicit and bounded, and serve nothing but the API. That means no silent regex fallback, no frozen server, no 15-minute hangs, no binary files sent to the LLM, and no exposed source code or `.git`.

**Architecture:** A new `ledgersync/` package holds the reliability plumbing: `config`, `errors`, `jobs`, `ollama_client`, `intake`, `ocr` and a Phase-1 `pipeline`. `server.py` becomes a thin `create_app()` factory. Analyses run as background jobs on one worker thread; the browser polls `/api/jobs/{id}` for progress, results or errors. `qwen_service.py` keeps its prompt and schema but talks to Ollama only through `OllamaClient`.

**Tech Stack:** Python 3.11 (`.venv`), FastAPI 0.141 / Starlette 1.7, Pydantic 2.13, PyMuPDF 1.28 (`import pymupdf`), pdfminer.six, EasyOCR, pandas + openpyxl/xlrd, pytest 9 + httpx2 (Starlette's TestClient), Next.js 16.3 / React 18.

**Spec:** `docs/superpowers/specs/2026-09-28-ledgersync-robustness-design.md` (Phase 1)

## Global Constraints

- Run Python with `.venv/bin/python`. Run tests from the repo root with `.venv/bin/python -m pytest`.
- The server binds `127.0.0.1` by default. CORS allows only `http://localhost:3000` and `http://127.0.0.1:3000`.
- **No heuristic fallback.** AI failures surface as 503 `ai_offline`, 504 `ai_timeout` or 502 `ai_error` / `ai_output_truncated` / `ai_output_invalid`. Rows are never fabricated.
- Limits: `MAX_UPLOAD_MB=20`, `MAX_PDF_PAGES=30`, model call timeout `120` s, `keep_alive` `30m`, `num_ctx` `8192`, `num_predict` `4096`, health cache `15` s, finished jobs expire after `3600` s.
- **Never log document content** (pasted text, OCR text or model output). Log counts and error types only.
- New or changed Python uses `import pymupdf` (the `fitz` alias is deprecated in 1.28).
- Error bodies are `{"detail": {"code": "<code>", "message": "<human advice>"}}`.
- Frontend calls only relative `/api/...` URLs. Next 16 differs from older versions, so read `frontend/node_modules/next/dist/docs/` before using any Next API (see `frontend/AGENTS.md`).

## Review Focus

1. **Ollama stops mid-session after a healthy check.** The next call must fail clearly and health must be re-checked, not served from cache. Tests: `test_failed_chat_invalidates_cached_health` (Task 3), `test_ai_dies_mid_job_fails_clearly` (Task 8).
2. **User cancels while the model call is in flight.** The job ends `cancelled` and the late result is discarded. Tests: `test_result_discarded_if_cancelled_during_last_step` (Task 2), `test_cancel_running_job_via_api` (Task 8).
3. **Empty file or whitespace-only paste.** Returns 422 before any model call. Tests: `test_empty_file_is_rejected`, `test_whitespace_text_is_rejected` (Task 4), `test_missing_input_is_422` (Task 8).
4. **Browser polls a job after the server restarted.** Returns 404 `job_not_found` with retry advice, and the UI shows it instead of spinning. Tests: `test_unknown_job_raises_not_found` (Task 2), `test_unknown_job_is_404_with_retry_advice` (Task 8); UI check in Task 9.
5. **A bank ".xls" export that is really tab-separated text.** Read as a table, not rejected and not sent to the model as binary. Test: `test_tab_separated_text_named_xls_is_read_as_table` (Task 4).

---

### Task 1: Test tooling, settings and typed errors

**Files:**
- Modify: `requirements-dev.txt`, `pytest.ini`
- Create: `ledgersync/__init__.py`, `ledgersync/config.py`, `ledgersync/errors.py`
- Test: `tests/test_config.py`, `tests/test_errors.py`

**Interfaces:**
- Produces:
  - `Settings` (frozen dataclass): `ollama_host: str`, `ollama_model: str`, `ollama_timeout: float`, `ollama_keep_alive: str`, `ollama_num_ctx: int`, `ollama_num_predict: int`, `health_ttl: float`, `host: str`, `port: int`, `reload: bool`, `warmup: bool`, `cors_origins: tuple[str, ...]`, `max_upload_mb: int`, `max_pdf_pages: int`, `job_ttl_seconds: float`, `log_level: str`; property `max_upload_bytes -> int`; `Settings.from_env(env: Mapping[str, str] | None = None) -> Settings`.
  - `LedgerSyncError(message, *, code=None)` with `.status_code`, `.code`, `.message`, `.to_dict() -> {"code", "message", "status_code"}`.
  - Subclasses: `FileTooLarge` 413, `UnsupportedFile` 415, `UnreadableFile` 422, `ModelError` 502 `ai_error`, `ModelUnavailable` 503 `ai_offline`, `ModelTimeout` 504 `ai_timeout`, `JobNotFound` 404 `job_not_found`.

- [ ] **Step 1: Fix the dev requirements and install**

In `requirements-dev.txt`, replace `httpx==0.28.1` with `httpx2`. Starlette 1.7's TestClient imports `httpx2`; plain `httpx` is deprecated there. Then run:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip show httpx2 xlrd | grep -E "^(Name|Version)"
```

Write the exact installed versions back as pins: `httpx2==<shown>` in `requirements-dev.txt`, and `xlrd==<shown>` in `requirements.txt` if it differs from `2.0.1`.

- [ ] **Step 2: Put the repo root on the test import path**

`pytest.ini` becomes:

```ini
[pytest]
testpaths = tests
pythonpath = .
addopts = -m "not llm"
markers =
    llm: talks to the real local Ollama model (opt-in: pytest -m llm)
```

- [ ] **Step 3: Write the failing tests**

`tests/test_config.py`:

```python
import pytest

from ledgersync.config import Settings


def test_defaults_are_local_and_bounded():
    s = Settings.from_env({})
    assert s.host == "127.0.0.1"
    assert s.ollama_host == "http://localhost:11434"
    assert s.ollama_timeout == 120.0
    assert s.cors_origins == ("http://localhost:3000", "http://127.0.0.1:3000")
    assert s.reload is False and s.warmup is True
    assert s.max_upload_bytes == 20 * 1024 * 1024
    assert s.max_pdf_pages == 30


@pytest.mark.parametrize("raw, expected", [
    ("localhost:11434", "http://localhost:11434"),
    ("0.0.0.0", "http://0.0.0.0:11434"),
    ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
    ("https://ollama.example.com", "https://ollama.example.com"),
])
def test_ollama_host_is_normalised(raw, expected):
    assert Settings.from_env({"OLLAMA_HOST": raw}).ollama_host == expected


def test_env_overrides():
    s = Settings.from_env({
        "OLLAMA_MODEL": "qwen2.5vl:7b",
        "OLLAMA_TIMEOUT": "60",
        "LEDGERSYNC_CORS_ORIGINS": "http://a:1, http://b:2",
        "LEDGERSYNC_RELOAD": "true",
        "LEDGERSYNC_WARMUP": "0",
        "LEDGERSYNC_MAX_UPLOAD_MB": "5",
    })
    assert s.ollama_model == "qwen2.5vl:7b"
    assert s.ollama_timeout == 60.0
    assert s.cors_origins == ("http://a:1", "http://b:2")
    assert s.reload is True and s.warmup is False
    assert s.max_upload_bytes == 5 * 1024 * 1024
```

`tests/test_errors.py`:

```python
from ledgersync.errors import FileTooLarge, ModelError


def test_errors_carry_status_code_and_message():
    assert FileTooLarge("too big").to_dict() == {"code": "file_too_large", "message": "too big", "status_code": 413}


def test_code_can_be_specialised_per_instance():
    err = ModelError("cut off", code="ai_output_truncated")
    assert (err.code, err.status_code, ModelError.code) == ("ai_output_truncated", 502, "ai_error")
```

- [ ] **Step 4: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_errors.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'ledgersync'`

- [ ] **Step 5: Implement**

`ledgersync/__init__.py`:

```python
"""UK LedgerSync backend package."""
```

`ledgersync/config.py`:

```python
"""Runtime settings, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit


def _normalise_host(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.port is None:
        url = f"http://{parts.hostname}:11434"   # Ollama's default port, as the ollama CLI assumes
    return url


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:3b"
    ollama_timeout: float = 120.0
    ollama_keep_alive: str = "30m"
    ollama_num_ctx: int = 8192
    ollama_num_predict: int = 4096
    health_ttl: float = 15.0
    host: str = "127.0.0.1"
    port: int = 8085
    reload: bool = False
    warmup: bool = True
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    max_upload_mb: int = 20
    max_pdf_pages: int = 30
    job_ttl_seconds: float = 3600.0
    log_level: str = "INFO"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        d = cls()
        origins = env.get("LEDGERSYNC_CORS_ORIGINS")
        return cls(
            ollama_host=_normalise_host(env.get("OLLAMA_HOST", d.ollama_host)),
            ollama_model=env.get("OLLAMA_MODEL", d.ollama_model),
            ollama_timeout=float(env.get("OLLAMA_TIMEOUT", d.ollama_timeout)),
            ollama_keep_alive=env.get("OLLAMA_KEEP_ALIVE", d.ollama_keep_alive),
            ollama_num_ctx=int(env.get("OLLAMA_NUM_CTX", d.ollama_num_ctx)),
            ollama_num_predict=int(env.get("OLLAMA_NUM_PREDICT", d.ollama_num_predict)),
            host=env.get("LEDGERSYNC_HOST", d.host),
            port=int(env.get("LEDGERSYNC_PORT", d.port)),
            reload=_flag(env.get("LEDGERSYNC_RELOAD", "0")),
            warmup=_flag(env.get("LEDGERSYNC_WARMUP", "1")),
            cors_origins=(tuple(o.strip() for o in origins.split(",") if o.strip())
                          if origins else d.cors_origins),
            max_upload_mb=int(env.get("LEDGERSYNC_MAX_UPLOAD_MB", d.max_upload_mb)),
            max_pdf_pages=int(env.get("LEDGERSYNC_MAX_PDF_PAGES", d.max_pdf_pages)),
            log_level=env.get("LEDGERSYNC_LOG_LEVEL", d.log_level).upper(),
        )
```

`ledgersync/errors.py`:

```python
"""Typed errors that map to clear HTTP responses: {"detail": {"code", "message"}}."""
from __future__ import annotations

from typing import Optional


class LedgerSyncError(Exception):
    """Base error. Subclasses set the HTTP status and a stable machine-readable code."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "status_code": self.status_code}


class FileTooLarge(LedgerSyncError):
    status_code = 413
    code = "file_too_large"


class UnsupportedFile(LedgerSyncError):
    status_code = 415
    code = "unsupported_file"


class UnreadableFile(LedgerSyncError):
    status_code = 422
    code = "unreadable_file"


class ModelError(LedgerSyncError):
    """The model answered, but not with something usable (HTTP error, truncated, not JSON)."""

    status_code = 502
    code = "ai_error"


class ModelUnavailable(LedgerSyncError):
    status_code = 503
    code = "ai_offline"


class ModelTimeout(LedgerSyncError):
    status_code = 504
    code = "ai_timeout"


class JobNotFound(LedgerSyncError):
    status_code = 404
    code = "job_not_found"
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_errors.py -v`
Expected: 8 passed

- [ ] **Step 7: Commit**

```bash
git add requirements.txt requirements-dev.txt pytest.ini ledgersync/__init__.py ledgersync/config.py ledgersync/errors.py tests/test_config.py tests/test_errors.py
git commit -m "Add ledgersync settings and typed HTTP errors"
```

---

### Task 2: Background job store

**Files:**
- Create: `ledgersync/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `LedgerSyncError`, `JobNotFound` (Task 1).
- Produces:
  - `JobStore(ttl_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic)` with:
    - `.submit(work: Callable[[JobContext], Any]) -> Job`
    - `.get(job_id: str) -> Job` (raises `JobNotFound`)
    - `.cancel(job_id: str) -> Job`
    - `.shutdown()`
  - `Job` fields: `id`, `status` (`queued|running|succeeded|failed|cancelled`), `progress: str`, `result`, `error: dict|None`, `done: threading.Event`; `.to_dict() -> {"job_id","status","progress","result","error"}`.
  - `JobContext.report(progress: str)` and `.check_cancelled()`; both raise `JobCancelled` once cancel is requested.

- [ ] **Step 1: Write the failing tests** — `tests/test_jobs.py`:

```python
import threading
import time

import pytest

from ledgersync.errors import JobNotFound, ModelTimeout
from ledgersync.jobs import JobStore


@pytest.fixture
def store():
    s = JobStore()
    yield s
    s.shutdown()


def finished(store, job, timeout=5):
    assert job.done.wait(timeout), "job did not finish in time"
    return store.get(job.id)


def test_successful_job_returns_result(store):
    job = finished(store, store.submit(lambda ctx: {"answer": 42}))
    assert job.status == "succeeded"
    assert job.to_dict()["result"] == {"answer": 42}


def test_progress_is_visible_while_running(store):
    reported, release = threading.Event(), threading.Event()

    def work(ctx):
        ctx.report("Reading page 1 of 2")
        reported.set()
        release.wait(5)
        return "ok"

    job = store.submit(work)
    assert reported.wait(5)
    assert (store.get(job.id).status, store.get(job.id).progress) == ("running", "Reading page 1 of 2")
    release.set()
    assert finished(store, job).status == "succeeded"


def test_ledgersync_error_becomes_failed_job_with_code(store):
    def work(ctx):
        raise ModelTimeout("The local AI model did not answer within 120 seconds.")

    job = finished(store, store.submit(work))
    assert job.status == "failed"
    assert job.error == {"code": "ai_timeout", "status_code": 504,
                         "message": "The local AI model did not answer within 120 seconds."}


def test_unexpected_error_is_reported_without_internals(store):
    def work(ctx):
        raise ValueError("secret stack detail")

    job = finished(store, store.submit(work))
    assert job.status == "failed" and job.error["code"] == "internal_error"
    assert "secret" not in job.error["message"]


def test_cancel_queued_job_never_runs(store):
    release, ran = threading.Event(), threading.Event()
    first = store.submit(lambda ctx: release.wait(5))
    second = store.submit(lambda ctx: ran.set())
    assert store.cancel(second.id).status == "cancelled"
    release.set()
    finished(store, first)
    assert not ran.wait(0.2)
    assert store.get(second.id).status == "cancelled"


def test_cancel_running_job_stops_at_next_checkpoint(store):
    started = threading.Event()

    def work(ctx):
        started.set()
        while True:
            ctx.report("working")
            time.sleep(0.01)

    job = store.submit(work)
    assert started.wait(5)
    store.cancel(job.id)
    assert finished(store, job).status == "cancelled"


def test_result_discarded_if_cancelled_during_last_step(store):
    in_call, release = threading.Event(), threading.Event()

    def work(ctx):
        in_call.set()
        release.wait(5)          # stands in for a blocking model call
        return {"data": ["late"]}

    job = store.submit(work)
    assert in_call.wait(5)
    store.cancel(job.id)
    release.set()
    job = finished(store, job)
    assert job.status == "cancelled" and job.result is None


def test_jobs_run_one_at_a_time_in_order(store):
    order = []
    jobs = [store.submit(lambda ctx, i=i: order.append(i)) for i in range(3)]
    for job in jobs:
        finished(store, job)
    assert order == [0, 1, 2]


def test_unknown_job_raises_not_found(store):
    with pytest.raises(JobNotFound, match="run it again"):
        store.get("does-not-exist")


def test_finished_jobs_expire_after_ttl():
    now = [1000.0]
    store = JobStore(ttl_seconds=60, clock=lambda: now[0])
    try:
        job = finished(store, store.submit(lambda ctx: "ok"))
        now[0] += 61
        with pytest.raises(JobNotFound):
            store.get(job.id)
    finally:
        store.shutdown()
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_jobs.py -v`
Expected: `ModuleNotFoundError: No module named 'ledgersync.jobs'`

- [ ] **Step 3: Implement** — `ledgersync/jobs.py`:

```python
"""In-memory background jobs: one worker thread, progress, cancel and expiry.

Good enough for a single local user; jobs are lost when the server restarts."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .errors import JobNotFound, LedgerSyncError

logger = logging.getLogger(__name__)

QUEUED, RUNNING = "queued", "running"
SUCCEEDED, FAILED, CANCELLED = "succeeded", "failed", "cancelled"
FINISHED = frozenset({SUCCEEDED, FAILED, CANCELLED})


class JobCancelled(Exception):
    """Raised inside a job's work function once the user has cancelled it."""


@dataclass
class Job:
    id: str
    status: str = QUEUED
    progress: str = "Queued"
    result: Any = None
    error: Optional[dict] = None
    finished_at: Optional[float] = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        return {"job_id": self.id, "status": self.status, "progress": self.progress,
                "result": self.result, "error": self.error}


class JobContext:
    """Given to the work function so it can report progress and notice cancellation."""

    def __init__(self, job: Job):
        self._job = job

    def report(self, progress: str) -> None:
        self.check_cancelled()
        self._job.progress = progress

    def check_cancelled(self) -> None:
        if self._job.cancel_requested.is_set():
            raise JobCancelled()


class JobStore:
    def __init__(self, ttl_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ledgersync-job")

    def submit(self, work: Callable[[JobContext], Any]) -> Job:
        job = Job(id=uuid.uuid4().hex)
        with self._lock:
            self._purge_expired()
            self._jobs[job.id] = job
        self._executor.submit(self._run, job, work)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            return self._get_locked(job_id)

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status not in FINISHED:
                job.cancel_requested.set()
                if job.status == QUEUED:
                    self._finish_locked(job, CANCELLED, "Cancelled")
            return job

    def shutdown(self) -> None:
        for job in list(self._jobs.values()):
            job.cancel_requested.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job: Job, work: Callable[[JobContext], Any]) -> None:
        with self._lock:
            if job.status != QUEUED:          # cancelled while waiting in the queue
                return
            job.status, job.progress = RUNNING, "Starting"
        try:
            result = work(JobContext(job))
            JobContext(job).check_cancelled()  # cancelled during the last step: discard the result
            outcome = {"status": SUCCEEDED, "progress": "Done", "result": result}
        except JobCancelled:
            outcome = {"status": CANCELLED, "progress": "Cancelled"}
        except LedgerSyncError as exc:
            logger.warning("Job %s failed: %s", job.id, exc.code)
            outcome = {"status": FAILED, "progress": "Failed", "error": exc.to_dict()}
        except Exception:
            logger.exception("Job %s crashed", job.id)
            outcome = {"status": FAILED, "progress": "Failed", "error": {
                "code": "internal_error", "status_code": 500,
                "message": "Something went wrong while analysing. Details are in the server log."}}
        with self._lock:
            self._finish_locked(job, **outcome)

    def _finish_locked(self, job: Job, status: str, progress: str,
                       result: Any = None, error: Optional[dict] = None) -> None:
        if job.status in FINISHED:
            return
        job.status, job.progress, job.result, job.error = status, progress, result, error
        job.finished_at = self._clock()
        job.done.set()

    def _get_locked(self, job_id: str) -> Job:
        self._purge_expired()
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound("This analysis was not found. It may have expired or the server "
                              "restarted. Please run it again.")
        return job

    def _purge_expired(self) -> None:
        now = self._clock()
        for job_id in [j.id for j in self._jobs.values()
                       if j.finished_at is not None and now - j.finished_at > self._ttl]:
            del self._jobs[job_id]
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv/bin/python -m pytest tests/test_jobs.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add ledgersync/jobs.py tests/test_jobs.py
git commit -m "Add in-memory background job store with progress, cancel and expiry"
```

---

### Task 3: Ollama client with timeouts, retry, health cache and warm-up

**Files:**
- Create: `ledgersync/ollama_client.py`
- Test: `tests/test_ollama_client.py`

**Interfaces:**
- Consumes: `Settings`, `ModelError`, `ModelTimeout`, `ModelUnavailable` (Task 1).
- Produces:
  - `OllamaHealth(reachable: bool, model_available: bool, error: Optional[str] = None)`
  - `OllamaClient(settings, opener=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep)` with:
    - `.model -> str`
    - `.health(force: bool = False) -> OllamaHealth`
    - `.ensure_available() -> None` (raises `ModelUnavailable`)
    - `.chat_json(messages: list[dict], schema: dict, images: Optional[list[str]] = None) -> str`
    - `.warm_up() -> None` (never raises)

- [ ] **Step 1: Write the failing tests** — `tests/test_ollama_client.py`:

```python
import http.client
import io
import json
import urllib.error

import pytest

from ledgersync.config import Settings
from ledgersync.errors import ModelError, ModelTimeout, ModelUnavailable
from ledgersync.ollama_client import OllamaClient

TAGS = {"models": [{"name": "qwen2.5vl:3b", "model": "qwen2.5vl:3b"}]}
REFUSED = urllib.error.URLError(ConnectionRefusedError())
MSG = [{"role": "user", "content": "x"}]


class FakeResponse:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib.request.urlopen: returns or raises scripted outcomes in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)

    def last_payload(self):
        return json.loads(self.requests[-1][0].data)


def make_client(*outcomes):
    opener, now = FakeOpener(*outcomes), [0.0]
    client = OllamaClient(Settings(), opener=opener, clock=lambda: now[0], sleep=lambda s: None)
    return client, opener, now


def test_health_reports_available_model():
    client, _, _ = make_client(TAGS)
    assert client.health() == client.health()          # second call served from cache
    assert client.health().model_available and client.health().error is None


def test_health_is_cached_for_ttl():
    client, opener, now = make_client(TAGS, TAGS)
    client.health()
    client.health()
    assert len(opener.requests) == 1
    now[0] += 16
    client.health()
    assert len(opener.requests) == 2


def test_health_when_ollama_down_explains_how_to_fix():
    client, _, _ = make_client(REFUSED)
    health = client.health()
    assert not health.reachable and "Start the Ollama app" in health.error


def test_health_when_model_missing_says_pull():
    client, _, _ = make_client({"models": [{"name": "llama3.2:latest"}]})
    health = client.health()
    assert health.reachable and not health.model_available
    assert "ollama pull qwen2.5vl:3b" in health.error


def test_ensure_available_raises_model_unavailable():
    client, _, _ = make_client(REFUSED)
    with pytest.raises(ModelUnavailable):
        client.ensure_available()


def test_chat_sends_reliability_options_and_images():
    client, opener, _ = make_client({"message": {"content": '{"transactions": []}'}, "done_reason": "stop"})
    assert client.chat_json(MSG, {"type": "object"}, images=["aGk="]) == '{"transactions": []}'
    payload = opener.last_payload()
    assert payload["stream"] is False and payload["keep_alive"] == "30m"
    assert payload["format"] == {"type": "object"}
    assert payload["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 4096}
    assert payload["messages"][-1]["images"] == ["aGk="]
    assert opener.requests[-1][1] == 120.0


def test_chat_timeout_raises_model_timeout():
    client, _, _ = make_client(TimeoutError("timed out"))
    with pytest.raises(ModelTimeout):
        client.chat_json(MSG, {})


def test_chat_connection_refused_raises_model_unavailable():
    client, _, _ = make_client(REFUSED)
    with pytest.raises(ModelUnavailable):
        client.chat_json(MSG, {})


def test_chat_retries_once_when_connection_drops():
    client, opener, _ = make_client(http.client.RemoteDisconnected("closed"),
                                    {"message": {"content": "{}"}, "done_reason": "stop"})
    assert client.chat_json(MSG, {}) == "{}"
    assert len(opener.requests) == 2


def test_chat_truncated_output_is_a_clear_error():
    client, _, _ = make_client({"message": {"content": '{"transactions": [{"desc'}, "done_reason": "length"})
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, {})
    assert info.value.code == "ai_output_truncated"


def test_chat_missing_model_404_raises_model_unavailable():
    err = urllib.error.HTTPError("http://x/api/chat", 404, "Not Found", {}, io.BytesIO(b'{"error":"not found"}'))
    client, _, _ = make_client(err)
    with pytest.raises(ModelUnavailable):
        client.chat_json(MSG, {})


def test_failed_chat_invalidates_cached_health():
    client, _, _ = make_client(TAGS, REFUSED, REFUSED)
    assert client.health().model_available
    with pytest.raises(ModelUnavailable):
        client.chat_json(MSG, {})
    assert not client.health().reachable            # re-checked, not served from the stale cache


def test_warm_up_loads_model_with_empty_messages():
    client, opener, _ = make_client(TAGS, {"done": True})
    client.warm_up()
    assert opener.last_payload() == {"model": "qwen2.5vl:3b", "messages": [], "keep_alive": "30m"}


def test_warm_up_never_raises():
    client, _, _ = make_client(REFUSED)
    client.warm_up()
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_ollama_client.py -v`
Expected: `ModuleNotFoundError: No module named 'ledgersync.ollama_client'`

- [ ] **Step 3: Implement** — `ledgersync/ollama_client.py`:

```python
"""Client for the local Ollama server, with the reliability rules in one place:
bounded timeouts, keep_alive, explicit context size, truncation detection, one retry on a
dropped connection, a short health cache and one model call at a time."""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Settings
from .errors import ModelError, ModelTimeout, ModelUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OllamaHealth:
    reachable: bool
    model_available: bool
    error: Optional[str] = None


class OllamaClient:
    def __init__(self, settings: Settings, opener: Callable = urllib.request.urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._s = settings
        self._open = opener
        self._clock = clock
        self._sleep = sleep
        self._health: Optional[OllamaHealth] = None
        self._health_at = 0.0
        self._model_lock = threading.Semaphore(1)   # a laptop runs one generation at a time well

    @property
    def model(self) -> str:
        return self._s.ollama_model

    def health(self, force: bool = False) -> OllamaHealth:
        now = self._clock()
        if not force and self._health is not None and now - self._health_at < self._s.health_ttl:
            return self._health
        try:
            tags = self._request("/api/tags", None, timeout=3)
        except Exception as exc:
            logger.info("Ollama not reachable at %s (%s)", self._s.ollama_host, type(exc).__name__)
            health = OllamaHealth(False, False, self._unreachable_message())
        else:
            models = tags.get("models", [])
            names = {m.get("name") for m in models} | {m.get("model") for m in models}
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            health = (OllamaHealth(True, True) if wanted in names
                      else OllamaHealth(True, False, self._missing_model_message()))
        self._health, self._health_at = health, now
        return health

    def ensure_available(self) -> None:
        health = self.health()
        if not health.model_available:
            raise ModelUnavailable(health.error or self._unreachable_message())

    def chat_json(self, messages: list[dict], schema: dict, images: Optional[list[str]] = None) -> str:
        """Returns the model's JSON text, constrained to `schema` by Ollama structured outputs."""
        messages = [dict(m) for m in messages]
        if images:
            messages[-1]["images"] = images
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "keep_alive": self._s.ollama_keep_alive,
            "options": {"temperature": 0, "num_ctx": self._s.ollama_num_ctx,
                        "num_predict": self._s.ollama_num_predict},
        }
        with self._model_lock:
            data = self._chat_request(payload)
        if data.get("done_reason") == "length":
            raise ModelError("The document is too long for the AI model to answer in one pass. "
                             "Split it into smaller files and try again.", code="ai_output_truncated")
        content = (data.get("message") or {}).get("content", "")
        if not content.strip():
            raise ModelError("The AI model returned an empty answer.", code="ai_output_invalid")
        return content

    def warm_up(self) -> None:
        """Loads the model into memory so the first real request is not a cold start. Never raises."""
        try:
            if self.health(force=True).model_available:
                with self._model_lock:
                    self._request("/api/chat", {"model": self.model, "messages": [],
                                                "keep_alive": self._s.ollama_keep_alive},
                                  timeout=self._s.ollama_timeout)
                logger.info("Warmed up Ollama model %s", self.model)
        except Exception as exc:
            logger.warning("Model warm-up failed (%s)", type(exc).__name__)

    def _chat_request(self, payload: dict) -> dict:
        for attempt in (1, 2):
            try:
                return self._request("/api/chat", payload, timeout=self._s.ollama_timeout)
            except TimeoutError:
                raise ModelTimeout(self._timeout_message()) from None
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._health = None
                    raise ModelUnavailable(self._missing_model_message()) from None
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise ModelError(f"The local AI returned HTTP {exc.code}: {detail}") from None
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise ModelTimeout(self._timeout_message()) from None
                self._health = None
                raise ModelUnavailable(self._unreachable_message()) from None
            except (ConnectionResetError, http.client.HTTPException):
                self._health = None
                if attempt == 2:
                    raise ModelUnavailable(self._unreachable_message()) from None
                logger.warning("Ollama dropped the connection; retrying once")
                self._sleep(1.0)
            except json.JSONDecodeError:
                raise ModelError("The local AI returned a response that is not JSON.") from None
        raise AssertionError("unreachable")

    def _request(self, path: str, payload: Optional[dict], timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self._s.ollama_host}{path}", data=data,
                                     method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json"})
        with self._open(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _unreachable_message(self) -> str:
        return (f"The local AI (Ollama) is not reachable at {self._s.ollama_host}. "
                "Start the Ollama app and try again.")

    def _missing_model_message(self) -> str:
        return f"The AI model '{self.model}' is not installed in Ollama. Run: ollama pull {self.model}"

    def _timeout_message(self) -> str:
        return (f"The local AI model did not answer within {self._s.ollama_timeout:.0f} seconds. "
                "Try a smaller document, or raise OLLAMA_TIMEOUT.")
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv/bin/python -m pytest tests/test_ollama_client.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add ledgersync/ollama_client.py tests/test_ollama_client.py
git commit -m "Add Ollama client with bounded timeouts, retry, health cache and warm-up"
```

---

### Task 4: Upload intake validation

**Files:**
- Create: `ledgersync/intake.py`
- Test: `tests/test_intake.py`

**Interfaces:**
- Consumes: `FileTooLarge`, `UnreadableFile`, `UnsupportedFile` (Task 1).
- Produces:
  - `Intake(kind: Literal["text","table","pdf","image"], filename: Optional[str] = None, text: Optional[str] = None, data: Optional[bytes] = None)`
  - `read_limited(stream: BinaryIO, max_bytes: int) -> bytes`
  - `from_text(text: str, max_bytes: int) -> Intake`
  - `load_upload(filename: str, data: bytes, max_pdf_pages: int) -> Intake`

- [ ] **Step 1: Write the failing tests** — `tests/test_intake.py`:

```python
import io

import pymupdf
import pytest
from PIL import Image

from ledgersync.errors import FileTooLarge, UnreadableFile, UnsupportedFile
from ledgersync.intake import from_text, load_upload, read_limited

OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes(pages=1, **save_kwargs):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    return doc.tobytes(**save_kwargs)


def xlsx_bytes():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sept"
    ws.append(["Date", "Description", "Amount"])
    ws.append(["01/09/2026", "BT BROADBAND", -72])
    wb.create_sheet("Oct").append(["02/10/2026", "ACME LTD", 3600])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def load(name, data, pages=30):
    return load_upload(name, data, max_pdf_pages=pages)


def test_read_limited_rejects_oversized_stream():
    with pytest.raises(FileTooLarge):
        read_limited(io.BytesIO(b"x" * 11), max_bytes=10)


def test_read_limited_accepts_exact_limit():
    assert read_limited(io.BytesIO(b"x" * 10), max_bytes=10) == b"x" * 10


def test_csv_utf8_bom_is_stripped():
    intake = load("s.csv", "﻿Date,Amount\n01/09/2026,72.00".encode("utf-8"))
    assert intake.kind == "table" and intake.text.startswith("Date,Amount")


def test_csv_in_windows_1252_keeps_pound_sign():
    assert "£72.00" in load("s.csv", "Date,Amount\n01/09/2026,£72.00".encode("cp1252")).text


def test_utf16_unicode_text_export_is_read():
    assert "£72.00" in load("s.txt", "Date\tAmount\n01/09/2026\t£72.00".encode("utf-16")).text


def test_binary_disguised_as_csv_is_rejected():
    with pytest.raises(UnreadableFile):
        load("s.csv", b"\x00\x01\x02binary")


def test_empty_file_is_rejected():
    with pytest.raises(UnreadableFile, match="empty"):
        load("s.csv", b"")


def test_xlsx_reads_every_sheet():
    intake = load("s.xlsx", xlsx_bytes())
    assert intake.kind == "table"
    assert "BT BROADBAND" in intake.text and "ACME LTD" in intake.text and "# Sheet: Oct" in intake.text


def test_corrupt_xls_is_rejected_not_decoded_as_text():
    with pytest.raises(UnreadableFile):
        load("s.xls", OLE + b"\x00garbage" * 50)


def test_xls_uses_xlrd(monkeypatch):
    import pandas as pd
    seen = {}

    def fake_read_excel(buf, **kwargs):
        seen.update(kwargs)
        return {"Sheet1": pd.DataFrame([["01/09/2026", "BT", -72]])}

    monkeypatch.setattr(pd, "read_excel", fake_read_excel)
    intake = load("s.xls", OLE + b"\x00" * 100)
    assert seen["engine"] == "xlrd" and "BT" in intake.text


def test_tab_separated_text_named_xls_is_read_as_table():
    intake = load("export.xls", b"Date\tAmount\n01/09/2026\t72.00")
    assert intake.kind == "table" and "72.00" in intake.text


def test_pdf_is_accepted():
    intake = load("s.pdf", pdf_bytes(2))
    assert intake.kind == "pdf" and intake.data.startswith(b"%PDF")


def test_password_protected_pdf_is_rejected_with_advice():
    data = pdf_bytes(1, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(UnreadableFile, match="password"):
        load("s.pdf", data)


def test_pdf_over_page_limit_is_rejected():
    with pytest.raises(FileTooLarge, match="4 pages"):
        load("s.pdf", pdf_bytes(4), pages=3)


def test_corrupt_pdf_is_rejected():
    with pytest.raises(UnreadableFile):
        load("s.pdf", b"%PDF-1.4\n%garbage")


def test_png_is_accepted_even_with_wrong_extension():
    assert load("receipt.pdf", png_bytes()).kind == "image"


def test_corrupt_png_is_rejected():
    with pytest.raises(UnreadableFile):
        load("r.png", b"\x89PNG\r\n\x1a\n" + b"junk")


def test_text_starting_with_bm_is_not_mistaken_for_a_bitmap():
    assert load("s.csv", b"BMW Finance,72.00\n").kind == "table"


def test_unknown_binary_is_unsupported():
    with pytest.raises(UnsupportedFile):
        load("notes.docx", b"PK\x03\x04" + b"\x00" * 50)


def test_whitespace_text_is_rejected():
    with pytest.raises(UnreadableFile):
        from_text("   \n  ", max_bytes=1000)


def test_oversized_text_is_rejected():
    with pytest.raises(FileTooLarge):
        from_text("x" * 2000, max_bytes=1000)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_intake.py -v`
Expected: `ModuleNotFoundError: No module named 'ledgersync.intake'`

- [ ] **Step 3: Implement** — `ledgersync/intake.py`:

```python
"""Validates uploads before any expensive work: size, real file type (by content, not just
the name) and readability. Nothing unreadable ever reaches the model."""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import PurePath
from typing import BinaryIO, Literal, Optional

from .errors import FileTooLarge, UnreadableFile, UnsupportedFile

Kind = Literal["text", "table", "pdf", "image"]

SUPPORTED = "CSV, TSV, TXT, XLSX, XLS, PDF, PNG, JPG, WEBP, BMP or TIFF"
TEXT_KINDS = {".txt": "text", ".csv": "table", ".tsv": "table"}
EXCEL_EXTENSIONS = {".xlsx", ".xls"}

_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # legacy .xls container
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


@dataclass(frozen=True)
class Intake:
    kind: Kind
    filename: Optional[str] = None
    text: Optional[str] = None     # "text" and "table" kinds
    data: Optional[bytes] = None   # "pdf" and "image" kinds


def read_limited(stream: BinaryIO, max_bytes: int) -> bytes:
    """Reads at most max_bytes, refusing anything larger instead of loading it all."""
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise FileTooLarge(f"The file is larger than the {_limit(max_bytes)} limit.")
    return data


def from_text(text: str, max_bytes: int) -> Intake:
    if len(text.encode("utf-8")) > max_bytes:
        raise FileTooLarge(f"The pasted text is larger than the {_limit(max_bytes)} limit.")
    if not text.strip():
        raise UnreadableFile("There is no text to analyse.")
    return Intake("text", text=text.strip())


def load_upload(filename: str, data: bytes, max_pdf_pages: int) -> Intake:
    if not data:
        raise UnreadableFile(f"'{filename}' is empty.")
    ext = PurePath(filename).suffix.lower()
    if b"%PDF-" in data[:1024]:
        return Intake("pdf", filename, data=_check_pdf(data, filename, max_pdf_pages))
    if _is_image(data):
        return Intake("image", filename, data=_check_image(data, filename))
    if ext in EXCEL_EXTENSIONS:
        if data.startswith(_ZIP):
            return Intake("table", filename, text=_excel_to_text(data, filename, engine="openpyxl"))
        if data.startswith(_OLE):
            return Intake("table", filename, text=_excel_to_text(data, filename, engine="xlrd"))
        if _looks_like_text(data):    # some banks export tab-separated text or HTML named .xls
            return Intake("table", filename, text=_decode_text(data, filename))
        raise UnreadableFile(f"'{filename}' is not a valid Excel file.")
    if ext in TEXT_KINDS:
        return Intake(TEXT_KINDS[ext], filename, text=_decode_text(data, filename))
    raise UnsupportedFile(f"'{filename}' is not a supported file type. Use {SUPPORTED}.")


def _is_image(data: bytes) -> bool:
    return (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*"))
            or (data[:2] == b"BM" and data[6:10] == b"\x00\x00\x00\x00")   # BMP: reserved bytes are zero
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _check_pdf(data: bytes, filename: str, max_pages: int) -> bytes:
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise UnreadableFile(f"'{filename}' is not a valid PDF. It may be corrupt.") from None
    with doc:
        if doc.needs_pass:
            raise UnreadableFile(f"'{filename}' is password-protected. Save a copy without the "
                                 "password and upload that.")
        if doc.page_count == 0:
            raise UnreadableFile(f"'{filename}' has no pages.")
        if doc.page_count > max_pages:
            raise FileTooLarge(f"'{filename}' has {doc.page_count} pages; the limit is {max_pages}. "
                               "Split it and upload the parts.")
    return data


def _check_image(data: bytes, filename: str) -> bytes:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception:
        raise UnreadableFile(f"'{filename}' could not be read as an image. It may be corrupt.") from None
    return data


def _excel_to_text(data: bytes, filename: str, engine: str) -> str:
    import pandas as pd
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, engine=engine)
    except ImportError:
        raise UnreadableFile("Reading this spreadsheet needs a missing package. "
                             "Run: pip install -r requirements.txt") from None
    except Exception:
        raise UnreadableFile(f"'{filename}' could not be read as a spreadsheet. It may be corrupt.") from None
    parts = []
    for name, frame in sheets.items():
        frame = frame.dropna(how="all").dropna(axis=1, how="all")
        if not frame.empty:
            parts.append(f"# Sheet: {name}\n{frame.to_csv(index=False, header=False).strip()}")
    if not parts:
        raise UnreadableFile(f"'{filename}' has no data in any sheet.")
    return "\n\n".join(parts)


def _looks_like_text(data: bytes) -> bool:
    return data.startswith(_UTF16_BOMS) or b"\x00" not in data[:8192]


def _decode_text(data: bytes, filename: str) -> str:
    if data.startswith(_UTF16_BOMS):                 # Excel "Unicode Text" exports
        text = data.decode("utf-16", errors="replace")
    elif b"\x00" in data[:8192]:
        raise UnreadableFile(f"'{filename}' is not a text file.")
    else:
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):   # latin-1 accepts any byte
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise UnreadableFile(f"'{filename}' has no text.")
    return text


def _limit(max_bytes: int) -> str:
    mb = max_bytes / (1024 * 1024)
    return f"{mb:g} MB" if mb >= 1 else f"{max_bytes} bytes"
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv/bin/python -m pytest tests/test_intake.py -v`
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add ledgersync/intake.py tests/test_intake.py
git commit -m "Validate uploads by content: size, encoding, Excel, encrypted PDFs, images"
```

---

### Task 5: OCR wrapper with reading-order lines and no content logging

**Files:**
- Create: `ledgersync/ocr.py`
- Test: `tests/test_ocr.py`

**Interfaces:**
- Produces:
  - `OcrLine(text: str, confidence: float)`
  - `group_into_lines(detections: Sequence[tuple[box, text, conf]]) -> list[OcrLine]`
  - `OcrEngine(languages=("en",))` with `.installed()` (staticmethod) `-> bool`, `.loaded -> bool`, `.read_lines(image: bytes) -> list[OcrLine]` and `.read_text(image: bytes) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_ocr.py`:

```python
import logging

from ledgersync.ocr import OcrEngine, group_into_lines


def det(x, y, text, conf=0.9, w=60, h=20):
    return ([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], text, conf)


def test_label_and_amount_on_same_row_are_joined():
    lines = group_into_lines([det(200, 101, "12.50"), det(10, 100, "TOTAL"),
                              det(10, 130, "CASH"), det(200, 131, "20.00", conf=0.5)])
    assert [line.text for line in lines] == ["TOTAL 12.50", "CASH 20.00"]
    assert lines[1].confidence == 0.5


def test_rows_are_in_reading_order():
    lines = group_into_lines([det(10, 300, "last"), det(10, 10, "first"), det(10, 150, "middle")])
    assert [line.text for line in lines] == ["first", "middle", "last"]


def test_blank_detections_are_ignored():
    assert group_into_lines([det(10, 10, "  ")]) == []
    assert group_into_lines([]) == []


def test_read_text_never_logs_document_text(caplog):
    class FakeReader:
        def readtext(self, image):
            return [det(10, 10, "MRS CLIENT SECRET"), det(10, 40, "TOTAL 12.50")]

    engine = OcrEngine()
    engine._reader = FakeReader()
    caplog.set_level(logging.DEBUG)
    assert engine.read_text(b"img") == "MRS CLIENT SECRET\nTOTAL 12.50"
    assert "SECRET" not in caplog.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_ocr.py -v`
Expected: `ModuleNotFoundError: No module named 'ledgersync.ocr'`

- [ ] **Step 3: Implement** — `ledgersync/ocr.py`:

```python
"""EasyOCR wrapper: thread-safe lazy loading and reading-order lines.

Never logs document text; client receipts and statements are personal data."""
from __future__ import annotations

import importlib.util
import logging
import statistics
import threading
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float


def group_into_lines(detections: Sequence) -> list[OcrLine]:
    """Groups EasyOCR (box, text, confidence) detections into lines, top to bottom and left
    to right, so "TOTAL" and "12.50" printed on one receipt row stay together."""
    boxes = []
    for box, text, confidence in detections:
        ys = [float(point[1]) for point in box]
        xs = [float(point[0]) for point in box]
        if str(text).strip():
            boxes.append({"top": min(ys), "bottom": max(ys), "left": min(xs),
                          "text": str(text).strip(), "conf": float(confidence)})
    if not boxes:
        return []
    tolerance = statistics.median(b["bottom"] - b["top"] for b in boxes) / 2
    boxes.sort(key=lambda b: (b["top"] + b["bottom"]) / 2)
    rows: list[list[dict]] = []
    for b in boxes:
        centre = (b["top"] + b["bottom"]) / 2
        if rows and abs(centre - statistics.mean((r["top"] + r["bottom"]) / 2 for r in rows[-1])) <= tolerance:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [OcrLine(" ".join(b["text"] for b in sorted(row, key=lambda b: b["left"])),
                    min(b["conf"] for b in row))
            for row in rows]


class OcrEngine:
    def __init__(self, languages: Sequence[str] = ("en",)):
        self._languages = list(languages)
        self._reader = None
        self._lock = threading.Lock()

    @staticmethod
    def installed() -> bool:
        return importlib.util.find_spec("easyocr") is not None

    @property
    def loaded(self) -> bool:
        return self._reader is not None

    def read_lines(self, image: bytes) -> list[OcrLine]:
        with self._lock:   # loaded once; used by one thread at a time
            if self._reader is None:
                import easyocr
                logger.info("Loading EasyOCR (first use)")
                self._reader = easyocr.Reader(self._languages, verbose=False)
            detections = self._reader.readtext(image)
        lines = group_into_lines(detections)
        logger.debug("OCR produced %d lines", len(lines))
        return lines

    def read_text(self, image: bytes) -> Optional[str]:
        return "\n".join(line.text for line in self.read_lines(image)).strip() or None
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv/bin/python -m pytest tests/test_ocr.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add ledgersync/ocr.py tests/test_ocr.py
git commit -m "Add thread-safe OCR wrapper that keeps receipt rows together"
```

---

### Task 6: Qwen extractor on OllamaClient, per-row validation, no fallback

**Files:**
- Modify: `qwen_service.py` (whole file; the prompt text in `_build_system_prompt` stays exactly as it is)
- Create: `tests/fakes.py`
- Test: `tests/test_qwen_service.py`

**Interfaces:**
- Consumes: `ModelError` (Task 1), `OllamaHealth` and the `OllamaClient` interface (Task 3).
- Produces:
  - `TransactionExtractionResult(success: bool, count: int, data: List[AccountingTransaction], warnings: List[str] = [], model: Optional[str] = None, raw_model_output: Optional[str] = None)`. `validation_error` is removed.
  - `QwenAccountingExtractor(client)` with `.client` and `.extract_accounting_data(text_input: Optional[str] = None, images: Optional[List[str]] = None) -> TransactionExtractionResult`.
  - Test doubles in `tests/fakes.py`: `FakeOllama(replies=(), healthy=True)`, `FakeOcr(text=None, error=None)`, `ROW`, `transactions_json(*rows)`, `OFFLINE`.

- [ ] **Step 1: Create the shared test doubles** — `tests/fakes.py`:

```python
"""Test doubles shared by the test modules."""
import json

from ledgersync.errors import ModelUnavailable
from ledgersync.ollama_client import OllamaHealth

OFFLINE = ("The local AI (Ollama) is not reachable at http://localhost:11434. "
           "Start the Ollama app and try again.")

ROW = {"description": "BT Business Broadband", "date": "2026-09-01", "amount": 72.0,
       "currency": "GBP", "type": "expense", "account": "Telephone & Internet"}


def transactions_json(*rows: dict) -> str:
    return json.dumps({"transactions": list(rows)})


class FakeOllama:
    """Stands in for OllamaClient: chat_json returns (or raises) `replies` in order."""

    model = "fake-model"

    def __init__(self, replies=(), healthy: bool = True):
        self.replies = list(replies)
        self.healthy = healthy
        self.calls: list[dict] = []

    def health(self, force: bool = False) -> OllamaHealth:
        return OllamaHealth(True, True) if self.healthy else OllamaHealth(False, False, OFFLINE)

    def ensure_available(self) -> None:
        if not self.healthy:
            raise ModelUnavailable(OFFLINE)

    def chat_json(self, messages, schema, images=None) -> str:
        self.calls.append({"messages": messages, "schema": schema, "images": images})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def warm_up(self) -> None:
        pass


class FakeOcr:
    loaded = False

    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    def read_text(self, image: bytes):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text
```

- [ ] **Step 2: Write the failing tests** — `tests/test_qwen_service.py`:

```python
import json

import pytest
from fakes import ROW, FakeOllama, transactions_json

from ledgersync.errors import ModelError
from qwen_service import QwenAccountingExtractor


def extract(reply, text="x", images=None):
    client = FakeOllama([reply])
    return QwenAccountingExtractor(client).extract_accounting_data(text, images=images), client


def test_valid_rows_are_returned_with_model_name():
    result, _ = extract(transactions_json(ROW), "BT Broadband 72.00")
    assert result.success and result.count == 1 and result.model == "fake-model"
    assert result.data[0].amount == 72.0 and result.warnings == []


def test_one_bad_row_does_not_fail_the_batch():
    result, _ = extract(transactions_json(dict(ROW, amount=-72.0), ROW, dict(ROW, type="transfer")))
    assert result.count == 1 and len(result.warnings) == 2
    assert result.warnings[0].startswith("Row 1 skipped (amount:")
    assert result.warnings[1].startswith("Row 3 skipped (type:")


def test_invalid_json_is_a_model_error():
    with pytest.raises(ModelError) as info:
        extract("not json")
    assert info.value.code == "ai_output_invalid"


def test_answer_without_a_list_is_a_model_error():
    with pytest.raises(ModelError):
        extract(json.dumps({"rows": "nope"}))


def test_fenced_json_and_bare_lists_are_accepted():
    result, _ = extract("```json\n" + json.dumps([ROW]) + "\n```")
    assert result.count == 1


def test_images_and_schema_are_sent_to_the_model():
    _, client = extract(transactions_json(), None, images=["aGk="])
    assert client.calls[0]["images"] == ["aGk="]
    assert "transactions" in client.calls[0]["schema"]["properties"]


def test_no_heuristic_fallback_exists():
    assert not hasattr(QwenAccountingExtractor, "_run_smart_router_fallback")
```

- [ ] **Step 3: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_qwen_service.py -v`
Expected: FAIL. `QwenAccountingExtractor(client)` has the wrong constructor signature, and `_run_smart_router_fallback` still exists.

- [ ] **Step 4: Rewrite `qwen_service.py`**

The schema classes and the prompt text are unchanged. The file becomes:

```python
import json
import logging
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ledgersync.errors import ModelError

logger = logging.getLogger(__name__)


# --- Pydantic Data Schema for Step 2 JSON Output Validation ---
class AccountingTransaction(BaseModel):
    # List every field as required in the JSON schema so Ollama's structured output
    # always emits them (null for an unknown date); runtime defaults still apply.
    model_config = ConfigDict(json_schema_extra={
        "required": ["description", "date", "amount", "currency", "type", "account"]
    })

    description: str = Field(..., description="Short name/description of item or service")
    date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD or null if unavailable")
    amount: float = Field(..., ge=0, description="Numeric transaction amount")
    currency: Optional[str] = Field("GBP", description="Currency code (e.g. GBP)")
    type: Literal["expense", "revenue"] = Field(..., description="Transaction classification: 'expense' or 'revenue'")
    account: str = Field(..., description="Basic accounting account category name")

    @field_validator("type", mode="before")
    @classmethod
    def _lowercase_type(cls, v):
        return v.lower() if isinstance(v, str) else v

class ExtractedTransactions(BaseModel):
    """Top-level shape the model must return; its JSON schema is sent to Ollama as `format`."""
    transactions: List[AccountingTransaction]


class TransactionExtractionResult(BaseModel):
    success: bool
    count: int
    data: List[AccountingTransaction]
    warnings: List[str] = []
    model: Optional[str] = None
    raw_model_output: Optional[str] = None


class QwenAccountingExtractor:
    """Builds the prompt, calls the local model through an OllamaClient and validates each row.

    There is deliberately no heuristic fallback: when the model is unavailable the caller gets
    ModelUnavailable / ModelTimeout instead of plausible-looking wrong numbers."""

    def __init__(self, client):
        self.client = client

    def extract_accounting_data(self, text_input: Optional[str] = None,
                                images: Optional[List[str]] = None) -> TransactionExtractionResult:
        raw_output = self.client.chat_json(
            [{"role": "user", "content": self._build_system_prompt(text_input)}],
            ExtractedTransactions.model_json_schema(),
            images=images,
        )
        return self._validate_and_parse_json(raw_output)

    def _build_system_prompt(self, text_content: Optional[str]) -> str:
        return f"""You are a UK accounting data extraction model.
Analyze the given financial input and extract all transaction records into a JSON object with a "transactions" array. Use standard UK accountancy categories (e.g., Sales, Revenue, Expense, Professional Fees, Cost of Goods Sold, Utilities, etc.).

Input Content:
"{text_content or 'Receipt/Invoice image attached'}"

Required JSON Output Format:
Return ONLY a JSON object matching this exact schema:
{{
  "transactions": [
    {{
      "description": "string (name of item/service)",
      "date": "YYYY-MM-DD or null",
      "amount": number (positive numerical value),
      "currency": "GBP",
      "type": "expense" or "revenue",
      "account": "string (UK accounting category like 'Office Equipment', 'Sales', 'Revenue', 'Utilities')"
    }}
  ]
}}

Strict Rules:
- Return ONLY the JSON object, with no conversational text or markdown.
- If there are no transactions, return {{"transactions": []}}.
- Input dates are UK format (DD/MM/YYYY): "03/09/2026" is 3 September 2026, i.e. "2026-09-03".
"""

    def _validate_and_parse_json(self, raw_text: str) -> TransactionExtractionResult:
        """Parses the model output. Rows that fail validation become warnings, not a failed batch."""
        cleaned = raw_text.strip()
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            raise ModelError("The AI model's answer was not valid JSON.", code="ai_output_invalid") from None
        rows = parsed.get("transactions") if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            raise ModelError("The AI model's answer did not contain a list of transactions.",
                             code="ai_output_invalid")

        valid, warnings = [], []
        for number, row in enumerate(rows, start=1):
            try:
                valid.append(AccountingTransaction.model_validate(row))
            except ValidationError as exc:
                reasons = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}"
                                    for e in exc.errors())
                warnings.append(f"Row {number} skipped ({reasons}).")
        if warnings:
            logger.info("Skipped %d invalid row(s) from the model", len(warnings))
        return TransactionExtractionResult(success=True, count=len(valid), data=valid, warnings=warnings,
                                           model=self.client.model, raw_model_output=raw_text)
```

This removes `__init__(model_name, ollama_host)`, `_ollama_request`, `load_model`, `is_loaded`, `_encode_images`, `_run_model_inference` and `_run_smart_router_fallback`, and the imports only they used (`os`, `base64`, `urllib.request`, `Union`, `Dict`, `Any`).

- [ ] **Step 5: Run and confirm pass**

Run: `.venv/bin/python -m pytest tests/test_qwen_service.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add qwen_service.py tests/fakes.py tests/test_qwen_service.py
git commit -m "Remove regex fallback; validate model rows individually via OllamaClient"
```

---

### Task 7: Phase-1 analysis pipeline

**Files:**
- Create: `ledgersync/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes:
  - `Intake` (Task 4), `JobContext` and `JobCancelled` (Task 2).
  - Any extractor with `.client.model` and `.extract_accounting_data(text_input=, images=)` (Task 6).
  - Any OCR with `.read_text(bytes) -> Optional[str]` (Task 5).
- Produces:
  - `analyze(intake: Intake, extractor, ocr, ctx: JobContext)`: returns the extractor's result and reports progress.
  - `encode_images(data: bytes, is_pdf: bool) -> list[str]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_pipeline.py`:

```python
import io
import logging

import pymupdf
import pytest
from fakes import FakeOcr
from PIL import Image

from ledgersync.intake import Intake
from ledgersync.jobs import Job, JobCancelled, JobContext
from ledgersync.pipeline import analyze, encode_images


class RecordingExtractor:
    def __init__(self):
        self.calls = []
        self.client = type("Client", (), {"model": "fake-model"})()

    def extract_accounting_data(self, text_input=None, images=None):
        self.calls.append({"text": text_input, "images": images})
        return "RESULT"


def ctx():
    return JobContext(Job(id="test"))


def png():
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), "white").save(buf, "PNG")
    return buf.getvalue()


def text_pdf(text="Invoice total 120.00"):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def image_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=png())
    return doc.tobytes()


def test_text_goes_straight_to_the_model():
    ex = RecordingExtractor()
    assert analyze(Intake("text", text="BT 72.00"), ex, FakeOcr(), ctx()) == "RESULT"
    assert ex.calls == [{"text": "BT 72.00", "images": None}]


def test_pdf_text_layer_is_used():
    ex = RecordingExtractor()
    analyze(Intake("pdf", data=text_pdf()), ex, FakeOcr(), ctx())
    assert "Invoice total 120.00" in ex.calls[0]["text"]


def test_scanned_pdf_is_ocrd_page_by_page():
    ex, ocr = RecordingExtractor(), FakeOcr(text="TOTAL 12.50")
    analyze(Intake("pdf", data=image_pdf()), ex, ocr, ctx())
    assert ocr.calls == 1 and ex.calls[0]["text"] == "TOTAL 12.50"


def test_image_without_ocr_text_goes_to_vision_model():
    ex = RecordingExtractor()
    analyze(Intake("image", data=png()), ex, FakeOcr(text=None), ctx())
    assert ex.calls[0]["text"] is None and len(ex.calls[0]["images"]) == 1


def test_ocr_crash_falls_back_to_vision_model():
    ex = RecordingExtractor()
    analyze(Intake("image", data=png()), ex, FakeOcr(error=RuntimeError("torch missing")), ctx())
    assert len(ex.calls[0]["images"]) == 1


def test_cancel_before_model_call_skips_it():
    ex, job = RecordingExtractor(), Job(id="test")
    job.cancel_requested.set()
    with pytest.raises(JobCancelled):
        analyze(Intake("text", text="x"), ex, FakeOcr(), JobContext(job))
    assert ex.calls == []


def test_document_text_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    analyze(Intake("image", data=png()), RecordingExtractor(), FakeOcr(text="MRS CLIENT SECRET 12.50"), ctx())
    assert "CLIENT SECRET" not in caplog.text


def test_encode_images_renders_each_pdf_page():
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    assert len(encode_images(doc.tobytes(), is_pdf=True)) == 2
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: `ModuleNotFoundError: No module named 'ledgersync.pipeline'`

- [ ] **Step 3: Implement** — `ledgersync/pipeline.py`:

```python
"""Phase 1 routing: turns a validated upload into model input and runs the extractor.

Replaced in Phase 4 by the hybrid pipeline (deterministic parsing + LLM classification)."""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

from .intake import Intake
from .jobs import JobContext

logger = logging.getLogger(__name__)


def analyze(intake: Intake, extractor, ocr, ctx: JobContext):
    """Runs one analysis and returns the extractor's TransactionExtractionResult."""
    text, images = intake.text, None
    if intake.kind == "pdf":
        ctx.report("Extracting text from the PDF")
        text = _pdf_text(intake.data) or _scanned_pdf_text(intake.data, ocr, ctx)
        if not text:
            images = encode_images(intake.data, is_pdf=True)
    elif intake.kind == "image":
        ctx.report("Reading the image")
        text = _ocr_text(ocr, intake.data)
        if not text:
            images = encode_images(intake.data, is_pdf=False)
    ctx.report(f"Asking the AI model ({extractor.client.model})")
    return extractor.extract_accounting_data(text_input=text, images=images)


def encode_images(data: bytes, is_pdf: bool) -> list[str]:
    """Base64 images for the vision model; PDFs are rendered page by page."""
    if not is_pdf:
        return [base64.b64encode(data).decode("ascii")]
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [base64.b64encode(page.get_pixmap(dpi=150).tobytes("png")).decode("ascii") for page in doc]


def _pdf_text(data: bytes) -> Optional[str]:
    from pdfminer.high_level import extract_text
    try:
        return extract_text(io.BytesIO(data)).strip() or None
    except Exception as exc:
        logger.warning("PDF text extraction failed (%s); trying OCR", type(exc).__name__)
        return None


def _scanned_pdf_text(data: bytes, ocr, ctx: JobContext) -> Optional[str]:
    import pymupdf
    texts = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            ctx.report(f"Reading scanned page {number} of {doc.page_count}")
            text = _ocr_text(ocr, page.get_pixmap(dpi=200).tobytes("png"))
            if text:
                texts.append(text)
    return "\n".join(texts) or None


def _ocr_text(ocr, image: bytes) -> Optional[str]:
    try:
        return ocr.read_text(image)
    except Exception as exc:
        logger.warning("OCR failed (%s); the vision model will read the image instead", type(exc).__name__)
        return None
```

- [ ] **Step 4: Run and confirm pass**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add ledgersync/pipeline.py tests/test_pipeline.py
git commit -m "Add Phase 1 analysis pipeline with progress, no temp files"
```

---

### Task 8: Thin FastAPI app (jobs, health, errors, CORS, localhost) and safe legacy UI

**Files:**
- Modify: `server.py` (whole file)
- Move: `index.html`, `app.js`, `styles.css` → `legacy/` (`git mv`)
- Modify: `legacy/app.js` (three edits below)
- Delete: `test_api.py` at the repo root. It drives the old synchronous API, and `tests/` replaces it. The spec scheduled this for Phase 5, but Phase 1 already changes the API it calls.
- Test: `tests/test_api.py`, `tests/test_live_llm.py`

**Interfaces:**
- Consumes:
  - `Settings` (Task 1), `JobStore` (Task 2), `OllamaClient` (Task 3), `intake` (Task 4), `OcrEngine` (Task 5).
  - `QwenAccountingExtractor` and `AccountingTransaction` (Task 6), `pipeline.analyze` (Task 7).
  - `FakeOllama`, `FakeOcr`, `ROW`, `transactions_json` (`tests/fakes.py`).
- Produces:
  - `create_app(settings: Optional[Settings] = None, *, ollama=None, ocr=None) -> FastAPI`
  - Module-level `app`.
  - HTTP API:
    - `GET /api/health`, returning `{"status","model","ollama_reachable","model_available","ai_error","ocr_installed","ocr_loaded"}`
    - `POST /api/analyze`, returning 202 `{"job_id","status"}`
    - `GET /api/jobs/{id}` and `DELETE /api/jobs/{id}`, returning the job dict
    - `POST /api/trial-balance` (unchanged)

- [ ] **Step 1: Write the failing tests** — `tests/test_api.py`:

```python
import json
import threading
import time

import pymupdf
import pytest
from fakes import ROW, FakeOcr, FakeOllama, transactions_json
from fastapi.testclient import TestClient

from ledgersync.config import Settings
from ledgersync.errors import ModelTimeout, ModelUnavailable
from server import create_app


@pytest.fixture
def make_client():
    clients = []

    def _make(ollama=None):
        settings = Settings(warmup=False, max_upload_mb=1, max_pdf_pages=3)
        app = create_app(settings, ollama=ollama or FakeOllama([transactions_json(ROW)]), ocr=FakeOcr())
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def wait_for(client, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def run_text_job(client, text="BT Broadband 72.00"):
    resp = client.post("/api/analyze", data={"text": text})
    assert resp.status_code == 202, resp.text
    return wait_for(client, resp.json()["job_id"])


def test_health_reports_model_status(make_client):
    body = make_client().get("/api/health").json()
    assert body["model"] == "fake-model" and body["model_available"] is True
    assert {"ollama_reachable", "ai_error", "ocr_installed", "ocr_loaded"} <= set(body)


def test_health_when_ai_offline_still_answers(make_client):
    body = make_client(FakeOllama(healthy=False)).get("/api/health").json()
    assert body["ollama_reachable"] is False and "Start the Ollama app" in body["ai_error"]


def test_analyze_text_runs_as_job(make_client):
    job = run_text_job(make_client())
    assert job["status"] == "succeeded"
    assert job["result"]["data"][0]["amount"] == 72.0 and job["result"]["model"] == "fake-model"


def test_analyze_when_ai_offline_is_503_with_advice(make_client):
    resp = make_client(FakeOllama(healthy=False)).post("/api/analyze", data={"text": "BT 72.00"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "ai_offline" and "Ollama" in resp.json()["detail"]["message"]


def test_model_timeout_fails_the_job_with_504_code(make_client):
    job = run_text_job(make_client(FakeOllama([ModelTimeout("The local AI model did not answer within 120 seconds.")])))
    assert job["status"] == "failed"
    assert (job["error"]["code"], job["error"]["status_code"]) == ("ai_timeout", 504)


def test_ai_dies_mid_job_fails_clearly(make_client):
    job = run_text_job(make_client(FakeOllama([ModelUnavailable("Lost connection to the local AI (Ollama).")])))
    assert job["status"] == "failed" and job["error"]["code"] == "ai_offline"


def test_partial_success_returns_valid_rows_and_warnings(make_client):
    job = run_text_job(make_client(FakeOllama([transactions_json(dict(ROW, amount=-72.0), ROW)])))
    assert job["result"]["count"] == 1 and len(job["result"]["warnings"]) == 1


def test_upload_over_limit_is_413(make_client):
    resp = make_client().post("/api/analyze", files={"file": ("big.csv", b"a,b\n" * 300_000, "text/csv")})
    assert resp.status_code == 413 and resp.json()["detail"]["code"] == "file_too_large"


def test_unsupported_file_is_415(make_client):
    resp = make_client().post("/api/analyze", files={"file": ("notes.docx", b"PK\x03\x04" + b"\x00" * 50, "application/octet-stream")})
    assert resp.status_code == 415


def test_corrupt_xls_is_422_and_never_reaches_the_model(make_client):
    ollama = FakeOllama([])
    resp = make_client(ollama).post("/api/analyze", files={"file": ("s.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "application/vnd.ms-excel")})
    assert resp.status_code == 422 and ollama.calls == []


def test_password_protected_pdf_is_422(make_client):
    doc = pymupdf.open()
    doc.new_page()
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    resp = make_client().post("/api/analyze", files={"file": ("s.pdf", data, "application/pdf")})
    assert resp.status_code == 422 and "password" in resp.json()["detail"]["message"]


def test_missing_input_is_422(make_client):
    resp = make_client().post("/api/analyze", data={})
    assert resp.status_code == 422


def test_cancel_running_job_via_api(make_client):
    class BlockingOllama(FakeOllama):
        def __init__(self):
            super().__init__()
            self.entered, self.release = threading.Event(), threading.Event()

        def chat_json(self, messages, schema, images=None):
            self.entered.set()
            self.release.wait(5)
            return transactions_json(ROW)

    ollama = BlockingOllama()
    client = make_client(ollama)
    job_id = client.post("/api/analyze", data={"text": "x"}).json()["job_id"]
    assert ollama.entered.wait(5)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    ollama.release.set()
    job = wait_for(client, job_id)
    assert job["status"] == "cancelled" and job["result"] is None


def test_unknown_job_is_404_with_retry_advice(make_client):
    resp = make_client().get("/api/jobs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "job_not_found" and "run it again" in resp.json()["detail"]["message"]


def test_trial_balance_still_works(make_client):
    resp = make_client().post("/api/trial-balance", json=[ROW])
    assert resp.status_code == 200 and resp.json()["total_debits"] == 72.0


def test_source_code_and_git_are_not_served(make_client):
    client = make_client()
    for path in ("/server.py", "/qwen_service.py", "/.git/config", "/requirements.txt", "/.venv/pyvenv.cfg"):
        assert client.get(path).status_code == 404, path


def test_legacy_ui_is_still_served(make_client):
    resp = make_client().get("/")
    assert resp.status_code == 200 and "UK Accounting Transaction Portal" in resp.text


def test_cors_allows_only_the_frontend(make_client):
    client = make_client()
    preflight = {"Access-Control-Request-Method": "GET"}
    ok = client.options("/api/health", headers={"Origin": "http://localhost:3000", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = client.options("/api/health", headers={"Origin": "http://evil.example", **preflight})
    assert "access-control-allow-origin" not in bad.headers
```

`tests/test_live_llm.py` (opt-in, needs the real Ollama):

```python
import pytest

from ledgersync.config import Settings
from ledgersync.ollama_client import OllamaClient
from qwen_service import QwenAccountingExtractor

pytestmark = pytest.mark.llm


def test_real_model_extracts_a_simple_bill():
    client = OllamaClient(Settings.from_env())
    client.ensure_available()
    result = QwenAccountingExtractor(client).extract_accounting_data("BT Business Broadband monthly bill 72.00")
    assert any(abs(t.amount - 72.0) < 0.01 for t in result.data)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: `ImportError: cannot import name 'create_app' from 'server'`

- [ ] **Step 3: Move the legacy UI so only it is served**

```bash
mkdir -p legacy && git mv index.html app.js styles.css legacy/
git rm test_api.py
```

- [ ] **Step 4: Make `legacy/app.js` poll jobs** (it keeps working until Phase 5 retires it)

Insert this function immediately above the line `// Universal Step 1 -> Step 2 Local Qwen AI Pipeline`:

```js
// The API runs analyses as background jobs: start one, then poll until it finishes.
async function analyzeViaJob(formData) {
  const start = await fetch('/api/analyze', { method: 'POST', body: formData });
  if (!start.ok) {
    throw new Error(`API Error ${start.status}: ${await start.text()}`);
  }
  const { job_id } = await start.json();
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const res = await fetch(`/api/jobs/${job_id}`);
    if (!res.ok) {
      throw new Error(`API Error ${res.status}: ${await res.text()}`);
    }
    const job = await res.json();
    if (job.status === 'succeeded') return job.result;
    if (job.status === 'failed') throw new Error(job.error.message);
    if (job.status === 'cancelled') throw new Error('Analysis was cancelled.');
  }
}

```

In `processStep1InputWithQwen`, replace:

```js
    const response = await fetch('/api/analyze', {
      method: 'POST',
      body: formData
    });

    if (!response.ok) {
      throw new Error(`API Error ${response.status}: ${await response.text()}`);
    }

    const result = await response.json();
```

with:

```js
    const result = await analyzeViaJob(formData);
```

Also replace its `catch` block. The fallback functions it calls do not exist, and a failure must be visible:

```js
  } catch (err) {
    console.error('Step 2 Qwen error, falling back to local format mapper:', err);
    // Fallback if local backend server is restarting
    if (file) {
      const ext = file.name.split('.').pop().toLowerCase();
      if (ext === 'csv') parseCSVFileFallback(file);
      else if (ext === 'xlsx' || ext === 'xls') parseExcelFileFallback(file);
    }
  } finally {
```

becomes:

```js
  } catch (err) {
    console.error('Step 2 analysis failed:', err);
    alert('Analysis failed: ' + err.message);
  } finally {
```

In the manual-entry submit handler, replace:

```js
      const response = await fetch('/api/analyze', { method: 'POST', body: formData });
      if (response.ok) {
        const result = await response.json();
        if (result.success && result.data && result.data.length > 0) {
          // Use Qwen's account only if user didn't provide one
          resolvedCategory = userCategory || result.data[0].account;
        }
      }
```

with:

```js
      const result = await analyzeViaJob(formData);
      if (result.success && result.data && result.data.length > 0) {
        // Use Qwen's account only if user didn't provide one
        resolvedCategory = userCategory || result.data[0].account;
      }
```

- [ ] **Step 5: Rewrite `server.py`**

```python
"""FastAPI entry point: a thin HTTP layer over the ledgersync package."""
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ledgersync import intake, pipeline
from ledgersync.config import Settings
from ledgersync.errors import LedgerSyncError, UnreadableFile
from ledgersync.jobs import JobStore
from ledgersync.ocr import OcrEngine
from ledgersync.ollama_client import OllamaClient
from qwen_service import AccountingTransaction, QwenAccountingExtractor

logger = logging.getLogger("ledgersync.server")

# Only this folder is served as static files: never the project root (source, .git, .venv).
LEGACY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy")


# ─── Step 3 — Trial Balance Models ────────────────────────────────────────────

class TrialBalanceLine(BaseModel):
    account: str
    debit: float   # positive value when this account has a debit balance
    credit: float  # positive value when this account has a credit balance


class TrialBalanceResult(BaseModel):
    lines: List[TrialBalanceLine]
    total_debits: float
    total_credits: float
    is_balanced: bool


def create_app(settings: Optional[Settings] = None, *, ollama=None, ocr=None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ollama = ollama or OllamaClient(settings)
    ocr = ocr or OcrEngine()
    extractor = QwenAccountingExtractor(ollama)
    jobs = JobStore(ttl_seconds=settings.job_ttl_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.warmup:
            threading.Thread(target=ollama.warm_up, name="ollama-warmup", daemon=True).start()
        yield
        jobs.shutdown()

    app = FastAPI(
        title="UK LedgerSync API",
        description="Local Qwen AI (Ollama) → structured accounting data → trial balance",
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
        health = ollama.health()
        return {
            "status": "online",
            "model": ollama.model,
            "ollama_reachable": health.reachable,
            "model_available": health.model_available,
            "ai_error": health.error,
            "ocr_installed": OcrEngine.installed(),
            "ocr_loaded": ocr.loaded,
        }

    # ─── Step 2 — Analyze (background job) ─────────────────────────────────────

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
        ollama.ensure_available()   # fail fast with 503 instead of queueing doomed work
        job = jobs.submit(lambda ctx: pipeline.analyze(item, extractor, ocr, ctx).model_dump(mode="json"))
        return {"job_id": job.id, "status": job.status}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return jobs.get(job_id).to_dict()

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str):
        return jobs.cancel(job_id).to_dict()

    # ─── Step 3 — Trial Balance ───────────────────────────────────────────────

    @app.post("/api/trial-balance", response_model=TrialBalanceResult)
    def generate_trial_balance(transactions: List[AccountingTransaction]):
        """
        Step 3 — Trial Balance Generator.

        Debit/Credit treatment (basic double-entry):
          - expense transactions → Debit side
          - revenue transactions → Credit side

        Groups by account name, sums amounts, checks if trial balance balances.
        """
        account_map: Dict[str, Dict[str, float]] = {}

        for tx in transactions:
            acc = tx.account or "Uncategorised"
            if acc not in account_map:
                account_map[acc] = {"debit": 0.0, "credit": 0.0}

            if tx.type.lower() == "expense":
                account_map[acc]["debit"] += tx.amount
            else:
                account_map[acc]["credit"] += tx.amount

        lines = [
            TrialBalanceLine(account=acc, debit=round(v["debit"], 2), credit=round(v["credit"], 2))
            for acc, v in sorted(account_map.items())
        ]

        total_debits = round(sum(l.debit for l in lines), 2)
        total_credits = round(sum(l.credit for l in lines), 2)

        return TrialBalanceResult(
            lines=lines,
            total_debits=total_debits,
            total_credits=total_credits,
            is_balanced=abs(total_debits - total_credits) < 0.01
        )

    # ─── Legacy HTML UI (retired in Phase 5) ────────────────────────────────────
    if os.path.isdir(LEGACY_DIR):
        app.mount("/", StaticFiles(directory=LEGACY_DIR, html=True), name="legacy")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = Settings.from_env()
    uvicorn.run("server:app", host=s.host, port=s.port, reload=s.reload)
```

- [ ] **Step 6: Run the API tests and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_api.py -v`
Expected: 18 passed

Run: `.venv/bin/python -m pytest`
Expected: 90 passed, 1 deselected.

- [ ] **Step 7: Run the live model test**

Run: `.venv/bin/python -m pytest -m llm -v`
Expected: 1 passed (Ollama running with `qwen2.5vl:3b`)

- [ ] **Step 8: Commit**

```bash
git add server.py legacy/ tests/test_api.py tests/test_live_llm.py
git commit -m "Serve analyses as jobs, fix health, bind localhost, restrict CORS, stop exposing project root"
```

---

### Task 9: Frontend on jobs: relative URLs, progress, cancel, readable errors

**Files:**
- Modify: `frontend/next.config.ts` (whole file), `frontend/src/app/page.tsx` (whole file), `frontend/src/app/globals.css` (append)
- Create: `frontend/src/lib/api.ts`

**Interfaces:**
- Consumes: the HTTP API from Task 8.
- Produces:
  - Types `AccountingTransaction`, `AnalyzeResult`, `TrialBalanceResult`, `Health`, and class `ApiError(message, code)`.
  - `getHealth()`, `analyze(formData, onProgress, isCancelled)` and `trialBalance(transactions)`.

- [ ] **Step 1: Check the Next 16 docs for the two APIs used**

```bash
ls frontend/node_modules/next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/ | grep -i -E "rewrites|proxy"
grep -n "proxyTimeout" frontend/node_modules/next/dist/server/lib/router-utils/proxy-request.js
```

Expected: `rewrites.md` is listed, and `proxyTimeout || 30000` shows the 30-second default we override.

- [ ] **Step 2: `frontend/next.config.ts`**

```ts
import type { NextConfig } from 'next'

const apiUrl = process.env.API_URL ?? 'http://127.0.0.1:8085'

const nextConfig: NextConfig = {
  experimental: {
    // The rewrite proxy gives up after 30s by default; a cold local model can take ~40s.
    proxyTimeout: 120_000,
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
      },
    ]
  },
}

export default nextConfig
```

- [ ] **Step 3: `frontend/src/lib/api.ts`**

```ts
// Client for the LedgerSync API. Paths are relative so every request goes through the
// Next.js rewrite in next.config.ts: no CORS and no hardcoded host.

export interface AccountingTransaction {
  description: string
  date: string | null
  amount: number
  currency: string
  type: 'expense' | 'revenue'
  account: string
}

export interface AnalyzeResult {
  success: boolean
  count: number
  data: AccountingTransaction[]
  warnings: string[]
  model: string | null
}

export interface TrialBalanceLine {
  account: string
  debit: number
  credit: number
}

export interface TrialBalanceResult {
  lines: TrialBalanceLine[]
  total_debits: number
  total_credits: number
  is_balanced: boolean
}

export interface Health {
  model: string
  ollama_reachable: boolean
  model_available: boolean
  ai_error: string | null
}

interface Job {
  job_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: string
  result: AnalyzeResult | null
  error: { code: string; message: string } | null
}

export class ApiError extends Error {
  code: string

  constructor(message: string, code: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

const POLL_INTERVAL_MS = 1_000

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const res = await fetch(path, { ...init, signal: controller.signal })
    .catch(() => {
      throw controller.signal.aborted
        ? new ApiError('The LedgerSync API did not respond in time.', 'timeout')
        : new ApiError('Cannot reach the LedgerSync API. Is `python server.py` running?', 'network')
    })
    .finally(() => clearTimeout(timer))
  if (!res.ok) throw await errorFrom(res)
  return (await res.json()) as T
}

async function errorFrom(res: Response): Promise<ApiError> {
  let detail: unknown
  try {
    detail = (await res.json()).detail
  } catch {
    detail = undefined // not JSON, e.g. a proxy error page
  }
  const code = `http_${res.status}`
  if (typeof detail === 'string') return new ApiError(detail, code)
  if (Array.isArray(detail)) {
    return new ApiError(detail.map(d => (d as { msg?: string }).msg ?? String(d)).join('; '), code)
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const d = detail as { message: string; code?: string }
    return new ApiError(d.message, d.code ?? code)
  }
  return new ApiError(`The LedgerSync API returned HTTP ${res.status}.`, code)
}

export function getHealth(): Promise<Health> {
  return request<Health>('/api/health', {}, 10_000)
}

// Starts an analysis job and polls it until it finishes, reporting progress along the way.
// isCancelled is checked between polls; cancelling also tells the server to stop the job.
export async function analyze(
  formData: FormData,
  onProgress: (progress: string) => void,
  isCancelled: () => boolean,
): Promise<AnalyzeResult> {
  const { job_id } = await request<{ job_id: string }>('/api/analyze', { method: 'POST', body: formData }, 120_000)
  for (;;) {
    if (isCancelled()) {
      await request(`/api/jobs/${job_id}`, { method: 'DELETE' }).catch(() => undefined)
      throw new ApiError('Analysis cancelled.', 'cancelled')
    }
    const job = await request<Job>(`/api/jobs/${job_id}`)
    if (job.status === 'succeeded' && job.result) return job.result
    if (job.status === 'failed') throw new ApiError(job.error?.message ?? 'Analysis failed.', job.error?.code ?? 'failed')
    if (job.status === 'cancelled') throw new ApiError('Analysis cancelled.', 'cancelled')
    onProgress(job.progress)
    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS))
  }
}

export function trialBalance(transactions: AccountingTransaction[]): Promise<TrialBalanceResult> {
  return request<TrialBalanceResult>('/api/trial-balance', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(transactions),
  })
}
```

- [ ] **Step 4: `frontend/src/app/page.tsx`** (whole file; the markup and CSS classes are unchanged except where noted)

```tsx
'use client'

import { useState, useRef, useCallback, useEffect } from 'react'
import {
  analyze as runAnalysisJob,
  ApiError,
  getHealth,
  trialBalance,
  type AccountingTransaction,
  type AnalyzeResult,
  type Health,
  type TrialBalanceResult,
} from '@/lib/api'

// ─── Utilities ────────────────────────────────────────────────────────────────

const fmt = (n: number) =>
  n === 0
    ? ''
    : `£${Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

// ─── Main Page Component ──────────────────────────────────────────────────────

export default function Home() {
  // Step 1 state
  const [activeTab, setActiveTab] = useState<'upload' | 'paste' | 'manual'>('paste')
  const [pasteText, setPasteText] = useState('')
  const [manualDesc, setManualDesc] = useState('')
  const [manualAmount, setManualAmount] = useState('')
  const [manualType, setManualType] = useState<'expense' | 'revenue'>('expense')
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // API / model status
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState('')

  // Step 2 state
  const [analyzing, setAnalyzing] = useState(false)
  const [progress, setProgress] = useState('')
  const [analyzeError, setAnalyzeError] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const [modelName, setModelName] = useState<string | null>(null)
  const [transactions, setTransactions] = useState<AccountingTransaction[]>([])
  const cancelRef = useRef(false)

  // Step 3 state
  const [generating, setGenerating] = useState(false)
  const [tbResult, setTbResult] = useState<TrialBalanceResult | null>(null)
  const [tbError, setTbError] = useState('')

  // ── AI online / offline indicator ──────────────────────────────────────────

  useEffect(() => {
    let alive = true
    const check = () =>
      getHealth()
        .then(h => {
          if (alive) { setHealth(h); setHealthError('') }
        })
        .catch((e: unknown) => {
          if (alive) { setHealth(null); setHealthError(e instanceof Error ? e.message : String(e)) }
        })
    check()
    const timer = setInterval(check, 30_000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  // ── Step 1 → Step 2: run an analysis job ───────────────────────────────────

  const runAnalysis = useCallback(async (
    formData: FormData,
    toTransactions: (result: AnalyzeResult) => AccountingTransaction[] = result => result.data,
  ) => {
    cancelRef.current = false
    setAnalyzing(true)
    setProgress('Uploading…')
    setAnalyzeError('')
    setWarnings([])
    setModelName(null)
    setTransactions([])
    setTbResult(null)
    setTbError('')
    try {
      const result = await runAnalysisJob(formData, setProgress, () => cancelRef.current)
      setWarnings(result.warnings)
      setModelName(result.model)
      const rows = toTransactions(result)
      if (!rows.length) throw new ApiError('No transactions were found in this input.', 'empty')
      setTransactions(rows)
    } catch (e: unknown) {
      setAnalyzeError(e instanceof Error ? e.message : String(e))
    } finally {
      setAnalyzing(false)
      setProgress('')
    }
  }, [])

  const cancelAnalysis = () => {
    cancelRef.current = true
    setProgress('Cancelling…')
  }

  const handlePaste = () => {
    if (!pasteText.trim()) return
    const fd = new FormData()
    fd.append('text', pasteText.trim())
    runAnalysis(fd)
  }

  const handleManual = () => {
    const amt = parseFloat(manualAmount)
    if (!manualDesc.trim() || isNaN(amt)) return
    const description = manualDesc.trim()
    const amount = Math.abs(amt)
    const type = manualType
    const verb = type === 'revenue' ? 'received' : 'paid'
    const fd = new FormData()
    fd.append('text', `${description} - ${verb} GBP ${amount}`)
    // The model only suggests the account; everything else is exactly what the user typed.
    runAnalysis(fd, result => [{
      description,
      date: null,
      amount,
      currency: 'GBP',
      type,
      account: result.data[0]?.account ?? 'Uncategorised',
    }])
  }

  const handleFiles = (files: FileList | null) => {
    if (!files || !files.length) return
    const fd = new FormData()
    fd.append('file', files[0])
    if (fileInputRef.current) fileInputRef.current.value = '' // lets the same file be chosen again
    runAnalysis(fd)
  }

  // ── Step 2 → Step 3: call /api/trial-balance ───────────────────────────────

  const generateTrialBalance = useCallback(async () => {
    if (!transactions.length) return
    setGenerating(true)
    setTbError('')
    setTbResult(null)
    try {
      setTbResult(await trialBalance(transactions))
    } catch (e: unknown) {
      setTbError(e instanceof Error ? e.message : String(e))
    } finally {
      setGenerating(false)
    }
  }, [transactions])

  // ── Pipeline step state ────────────────────────────────────────────────────
  const step1Done = transactions.length > 0
  const step2Active = analyzing || step1Done
  const step3Active = tbResult !== null || generating

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="page-wrapper">

      {/* Header */}
      <header className="header">
        <div className="header-icon">£</div>
        <div>
          <div className="header-title">UK LedgerSync</div>
          <div className="header-sub">
            {health
              ? health.model_available
                ? <span className="health-ok">● AI online · {health.model}</span>
                : <span className="health-off">● AI offline · {health.ai_error}</span>
              : <span className="health-off">● {healthError || 'Checking API…'}</span>}
          </div>
        </div>
      </header>

      {/* Pipeline stepper */}
      <div className="pipeline">
        <div className={`pipeline-step ${step1Done ? 'done' : 'active'}`}>
          <div className="step-num">{step1Done ? '✓' : '1'}</div>
          Input
        </div>
        <div className="pipeline-arrow" />
        <div className={`pipeline-step ${step3Active ? 'done' : step2Active ? 'active' : ''}`}>
          <div className="step-num">{step3Active ? '✓' : '2'}</div>
          Qwen AI
        </div>
        <div className="pipeline-arrow" />
        <div className={`pipeline-step ${step3Active ? 'active' : ''}`}>
          <div className="step-num">3</div>
          Trial Balance
        </div>
      </div>

      {/* ── STEP 1 — Input ─────────────────────────────────────────────────── */}
      <div className="section">
        <div className="section-header">
          <span className="step-badge badge-1">Step 1</span>
          <span className="section-title">Provide Financial Input</span>
          <span className="section-sub">Upload · Paste · Manual</span>
        </div>
        <div className="section-body">

          {/* Tabs */}
          <div className="tabs">
            {(['paste', 'upload', 'manual'] as const).map(t => (
              <button
                key={t}
                className={`tab-btn ${activeTab === t ? 'active' : ''}`}
                onClick={() => setActiveTab(t)}
              >
                {t === 'paste' ? '📋 Quick Paste' : t === 'upload' ? '📁 File Upload' : '✏️ Manual Entry'}
              </button>
            ))}
          </div>

          {/* Quick Paste */}
          <div className={`tab-panel ${activeTab === 'paste' ? 'active' : ''}`}>
            <textarea
              id="pasteInput"
              className="textarea"
              placeholder={`Paste transaction text or CSV rows…\n\nExamples:\n  Office chair purchased for £500\n  Consulting services sold for £2,000\n  BT Business Broadband monthly bill 72.00`}
              value={pasteText}
              onChange={e => setPasteText(e.target.value)}
            />
            <div className="row-end">
              <button
                id="analyzePasteBtn"
                className="btn btn-primary"
                onClick={handlePaste}
                disabled={analyzing || !pasteText.trim()}
              >
                {analyzing ? <><span className="spinner" /> Analysing…</> : '→ Analyse with Qwen'}
              </button>
            </div>
          </div>

          {/* File Upload */}
          <div className={`tab-panel ${activeTab === 'upload' ? 'active' : ''}`}>
            <input
              type="file"
              ref={fileInputRef}
              style={{ display: 'none' }}
              accept=".csv,.tsv,.txt,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tiff"
              onChange={e => handleFiles(e.target.files)}
            />
            <div
              id="dropzone"
              className={`dropzone ${dragOver ? 'drag-over' : ''}`}
              onClick={() => !analyzing && fileInputRef.current?.click()}
              onDragOver={e => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => { e.preventDefault(); setDragOver(false); if (!analyzing) handleFiles(e.dataTransfer.files) }}
            >
              <div className="dropzone-icon">📥</div>
              <h3>Drop a file here or click to browse</h3>
              <p>Bank statements, VAT receipts, invoices, spreadsheets</p>
              <div className="file-tags">
                {['.CSV', '.XLSX', '.PDF', '.PNG / .JPG'].map(t => (
                  <span key={t} className="file-tag">{t}</span>
                ))}
              </div>
            </div>
          </div>

          {/* Manual Entry */}
          <div className={`tab-panel ${activeTab === 'manual' ? 'active' : ''}`}>
            <div className="form-grid">
              <div style={{ gridColumn: '1 / -1' }}>
                <label htmlFor="mDesc">Description / Payee</label>
                <input
                  id="mDesc"
                  className="input"
                  placeholder="e.g. Office chair, Consulting to ACME Corp"
                  value={manualDesc}
                  onChange={e => setManualDesc(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="mAmount">Amount (£)</label>
                <input
                  id="mAmount"
                  className="input"
                  type="number"
                  step="0.01"
                  placeholder="500.00"
                  value={manualAmount}
                  onChange={e => setManualAmount(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="mType">Transaction Type</label>
                <select
                  id="mType"
                  className="input"
                  value={manualType}
                  onChange={e => setManualType(e.target.value as 'expense' | 'revenue')}
                >
                  <option value="expense">Expense (Payment Out)</option>
                  <option value="revenue">Revenue (Payment In)</option>
                </select>
              </div>
            </div>
            <div className="row-end">
              <button
                id="analyzeManualBtn"
                className="btn btn-primary"
                onClick={handleManual}
                disabled={analyzing || !manualDesc.trim() || !manualAmount}
              >
                {analyzing ? <><span className="spinner" /> Analysing…</> : '→ Analyse with Qwen'}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Connector */}
      {(analyzing || transactions.length > 0 || analyzeError) && (
        <div className="connector">↓</div>
      )}

      {/* ── STEP 2 — Structured Data ────────────────────────────────────────── */}
      {(analyzing || transactions.length > 0 || analyzeError) && (
        <div className="section">
          <div className="section-header">
            <span className="step-badge badge-2">Step 2</span>
            <span className="section-title">Qwen AI — Structured Output</span>
            {transactions.length > 0 && (
              <span className="section-sub">
                {transactions.length} transaction{transactions.length !== 1 ? 's' : ''} extracted
                {modelName ? ` · ${modelName}` : ''}
              </span>
            )}
          </div>
          <div className="section-body">

            {analyzing && (
              <div className="status-msg status-processing">
                <span className="spinner" />
                <span>{progress || 'Working…'}</span>
                <button className="btn btn-ghost" onClick={cancelAnalysis} disabled={progress === 'Cancelling…'}>
                  Cancel
                </button>
              </div>
            )}

            {analyzeError && (
              <div className="status-msg status-error">
                ⚠ {analyzeError}
              </div>
            )}

            {warnings.length > 0 && (
              <div className="status-msg status-warning">
                <div>
                  {warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
                </div>
              </div>
            )}

            {transactions.length > 0 && (
              <>
                <div className="data-table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Description</th>
                        <th>Amount</th>
                        <th>Type</th>
                        <th>Account / Category</th>
                        <th>Currency</th>
                      </tr>
                    </thead>
                    <tbody>
                      {transactions.map((tx, i) => (
                        <tr key={i}>
                          <td>{tx.description}</td>
                          <td className="amount-cell">
                            £{tx.amount.toLocaleString('en-GB', { minimumFractionDigits: 2 })}
                          </td>
                          <td>
                            <span className={`badge-type ${tx.type === 'expense' ? 'badge-expense' : 'badge-revenue'}`}>
                              {tx.type}
                            </span>
                          </td>
                          <td>{tx.account}</td>
                          <td style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{tx.currency || 'GBP'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="row-end" style={{ marginTop: '1.25rem' }}>
                  <button
                    id="generateTbBtn"
                    className="btn btn-green"
                    onClick={generateTrialBalance}
                    disabled={generating}
                  >
                    {generating
                      ? <><span className="spinner" /> Generating…</>
                      : '⟹ Generate Trial Balance'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Connector */}
      {(generating || tbResult || tbError) && (
        <div className="connector">↓</div>
      )}

      {/* ── STEP 3 — Trial Balance ──────────────────────────────────────────── */}
      {(generating || tbResult || tbError) && (
        <div className="section">
          <div className="section-header">
            <span className="step-badge badge-3">Step 3</span>
            <span className="section-title">Trial Balance</span>
            {tbResult && (
              <span className="section-sub">
                {tbResult.lines.length} account{tbResult.lines.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>
          <div className="section-body">

            {generating && (
              <div className="status-msg status-processing">
                <span className="spinner" />
                Computing trial balance…
              </div>
            )}

            {tbError && (
              <div className="status-msg status-error">⚠ {tbError}</div>
            )}

            {tbResult && (
              <>
                <div className="data-table-wrap">
                  <table className="tb-table">
                    <thead>
                      <tr>
                        <th style={{ textAlign: 'left' }}>Account</th>
                        <th>Debit (£)</th>
                        <th>Credit (£)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tbResult.lines.map((line, i) => (
                        <tr key={i}>
                          <td className="tb-account">{line.account}</td>
                          <td className={line.debit > 0 ? 'debit-val' : 'empty-cell'}>
                            {line.debit > 0 ? fmt(line.debit) : '—'}
                          </td>
                          <td className={line.credit > 0 ? 'credit-val' : 'empty-cell'}>
                            {line.credit > 0 ? fmt(line.credit) : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr className="tb-total-row">
                        <td>Totals</td>
                        <td className="debit-val">{fmt(tbResult.total_debits) || '£0.00'}</td>
                        <td className="credit-val">{fmt(tbResult.total_credits) || '£0.00'}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>

                <div>
                  <span className={`balance-pill ${tbResult.is_balanced ? 'pill-balanced' : 'pill-unbalanced'}`}>
                    {tbResult.is_balanced
                      ? '✓ Trial Balance Balances'
                      : `⚠ Out of Balance by £${Math.abs(tbResult.total_debits - tbResult.total_credits).toFixed(2)}`}
                  </span>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 5: Append the new styles to `frontend/src/app/globals.css`**

```css
/* ── Job progress, warnings and AI status ───────────── */
.btn-ghost {
  margin-left: auto; padding: 0.3rem 0.8rem;
  background: transparent; color: inherit; border: 1px solid currentColor;
}
.btn-ghost:hover:not(:disabled) { background: rgba(255,255,255,0.06); }
.btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
.status-warning { background: rgba(245,158,11,0.1); color: var(--amber); border: 1px solid rgba(245,158,11,0.25); }
.health-ok  { color: var(--green); }
.health-off { color: var(--amber); }
```

- [ ] **Step 6: Typecheck and build**

```bash
(cd frontend && npx tsc --noEmit)
(cd frontend && NEXT_TELEMETRY_DISABLED=1 npm run build)
```

Expected: `tsc` prints nothing and exits 0; the build finishes with the `/` route listed.

- [ ] **Step 7: End-to-end in the browser pane**

Create `.claude/launch.json` (local tooling; not committed):

```json
{
  "version": "0.0.1",
  "configurations": [
    { "name": "api", "runtimeExecutable": ".venv/bin/python", "runtimeArgs": ["server.py"], "port": 8085 },
    { "name": "api-offline", "runtimeExecutable": "/usr/bin/env", "runtimeArgs": ["OLLAMA_HOST=http://127.0.0.1:9", ".venv/bin/python", "server.py"], "port": 8085 },
    { "name": "web", "runtimeExecutable": "npm", "runtimeArgs": ["--prefix", "frontend", "run", "dev"], "port": 3000 }
  ]
}
```

Start `api` and `web`, open `http://localhost:3000`, and check:
1. The header shows `● AI online · qwen2.5vl:3b`.
2. Quick Paste `BT Business Broadband monthly bill 72.00` shows progress text, then a 72.00 row with `· qwen2.5vl:3b`; Generate Trial Balance renders.
3. Manual Entry `Office chair` / `500` / Revenue produces exactly £500.00 and `revenue`.
4. Uploading the same CSV twice in a row triggers two analyses (the input reset works).
5. Clicking **Cancel** during an analysis shows `Analysis cancelled.`
6. In the browser console, `await (await fetch('/api/jobs/nope')).json()` returns `detail.code === "job_not_found"`.
7. Stop `api` and start `api-offline`. After at most 30s the header shows `● AI offline · The local AI (Ollama) is not reachable…`; Quick Paste then shows the same message (503) instead of any rows.

Stop `api-offline` when done.

- [ ] **Step 8: Commit**

```bash
git add frontend/next.config.ts frontend/src/lib/api.ts frontend/src/app/page.tsx frontend/src/app/globals.css
git commit -m "Frontend: relative API URLs, job progress and cancel, readable errors, AI status"
```

---

### Task 10: README and Phase 1 verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README**

Replace the file list and the Run and Configuration sections with the following:

````markdown
- `server.py` — FastAPI routes (port 8085, localhost only)
- `ledgersync/` — settings, typed errors, background jobs, Ollama client, upload checks, OCR, pipeline
- `qwen_service.py` — model prompt, schema and per-row validation
- `frontend/` — Next.js UI (port 3000); calls the API through its `/api` rewrite

## Run

```bash
.venv/bin/python server.py                # API on http://127.0.0.1:8085
(cd frontend && npm run dev)              # UI on http://localhost:3000
```

Analyses run as background jobs: `POST /api/analyze` returns `{job_id}`, then
`GET /api/jobs/{job_id}` reports progress and the result; `DELETE` cancels.
If Ollama is down, the API answers 503 with how to fix it; it never guesses.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | `qwen2.5vl:3b` | Ollama model tag |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_TIMEOUT` | `120` | Seconds to wait for one model call |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded |
| `OLLAMA_NUM_CTX` | `8192` | Model context window (tokens) |
| `OLLAMA_NUM_PREDICT` | `4096` | Maximum tokens per answer |
| `LEDGERSYNC_HOST` / `LEDGERSYNC_PORT` | `127.0.0.1` / `8085` | API bind address |
| `LEDGERSYNC_RELOAD` | `0` | Auto-reload on code changes (development) |
| `LEDGERSYNC_WARMUP` | `1` | Load the model at startup to avoid a cold first request |
| `LEDGERSYNC_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed browser origins |
| `LEDGERSYNC_MAX_UPLOAD_MB` | `20` | Largest accepted upload |
| `LEDGERSYNC_MAX_PDF_PAGES` | `30` | Most pages accepted in one PDF |
| `LEDGERSYNC_LOG_LEVEL` | `INFO` | Log level (document contents are never logged) |
| `API_URL` (frontend) | `http://127.0.0.1:8085` | Where the Next.js `/api` rewrite sends requests |

The old HTML UI is still served at http://127.0.0.1:8085/ from `legacy/` until Phase 5 retires it.
````

- [ ] **Step 2: Full verification**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -m llm -q
```

Expected: all pass (no failures, the `llm` test deselected in the first run), then 1 passed.

With `api` running (the browser-pane config from Task 9), check what the server exposes:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8085/server.py
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8085/.git/config
curl -s --max-time 3 "http://$(ipconfig getifaddr en0):8085/api/health" || echo "not reachable from LAN (expected)"
```

Expected: `404`, `404`, `not reachable from LAN (expected)`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "README: jobs API, new settings, localhost-only defaults"
```
