# Groq Vision Model (MVP Demo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read documents with Groq's hosted 27B vision model, `qwen/qwen3.8-27b`, and a better prompt, keeping the local model one setting away. Measure the eval after the swap and again after the prompt change.

**Architecture:**
- A new `OpenAICompatibleClient` has the same members as `OllamaClient`, so the extractor and server take either one. `server.make_model_client` picks the client from `LEDGERSYNC_AI_PROVIDER`.
- When the client prefers images, the pipeline sends photos and scanned pages to it as upright, downscaled JPEGs, 3 per request, instead of OCR text.
- A new prompt tells the model whose books these are and gives it receipt rules. The model then returns a direction, an account chosen from the chart (as a schema enum) and the VAT shown on the document. The Phase 3 adapter maps that onto ledger `Transaction`s.

**Tech Stack:** Python 3.11 standard library `urllib` (no new dependencies), Pillow, Pydantic v2, FastAPI, the Phase 2 eval.

**Spec:** `docs/superpowers/specs/2026-09-29-groq-vision-model-design.md`

## Global Constraints

- **Provider:** the default stays `ollama`. `LEDGERSYNC_AI_PROVIDER=openai` switches to the hosted model; any other value is refused at startup.
- **Hosted-model defaults:**
  - base URL `https://api.groq.com/openai/v1`, model `qwen/qwen3.8-27b`;
  - timeout 60 s, `max_completion_tokens` 4096, `reasoning_effort` `none`;
  - 3 images per request; health cached for 300 s.
- **Rate limits (HTTP 429):** wait `retry-after` seconds (10 s if absent), at most 60 s in total per call, then fail with `ai_rate_limited` (503). Retry a dropped connection or a 5xx once, after 1 s.
- **The API key** goes only into the `Authorization` header to the configured base URL. It never appears in logs, error messages, `/api/health` or `repr(Settings)`. It lives in `.env`, which is git-ignored and never pasted into chat.
- **Images for the hosted model:** EXIF orientation applied, at most 1600 px on the long side, JPEG quality 85, base64.
- **Accounts:** the model chooses from the chart except 1200, 2200 and 2201, and picks "9998 Suspense" when unsure.
- **No new Python dependencies.** Tests make no network calls except `-m llm`; run them with `.venv/bin/python -m pytest -q`.
- **Commits** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **The free plan's per-minute limit is hit mid-analysis or mid-eval** (HTTP 429). The call waits it out for up to a minute, then fails as `ai_rate_limited`, which `run_eval.py --resume` runs again. Tests: Task 3 `test_a_rate_limit_is_waited_out_then_the_call_succeeds` and `test_a_rate_limit_that_lasts_gives_up_after_a_minute`; Task 5 `test_rate_limited_cases_count_as_unfinished`.
2. **The API key leaks** into logs, error messages, `/api/health` or a printed `Settings`. It never does. Tests: Task 1 `test_the_api_key_is_never_printed`, Task 3 `test_the_key_never_reaches_errors_or_logs`, Task 5 `test_health_never_shows_the_api_key`.
3. **A large phone photo taken sideways** (EXIF orientation 6, 3000 px) reaches the model upright and at most 1600 px. Test: Task 4 `test_a_sideways_phone_photo_is_sent_upright_small_and_as_jpeg`.
4. **A scanned statement longer than the 3-image limit** is split into requests of 3 pages, the rows are merged, and the warnings say which pages they came from. Test: Task 4 `test_scanned_pages_go_three_per_request_and_warnings_name_their_pages`.
5. **The model gives an account that can't be posted** (the bank, a code outside the chart, Suspense) or a negative amount. The row lands in Suspense, or as money out, with a warning; it never crashes and never gets a 422. Tests: Task 7 `test_accounts_that_cannot_be_posted_to_go_to_suspense`, `test_a_suspense_choice_is_flagged_for_review`, `test_negative_amounts_become_money_out`.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `ledgersync/config.py` | Settings, including the AI provider; `.env` loader | 1 |
| `.env.example` | Documented Groq settings with an empty key (committed) | 1 |
| `ledgersync/openai_client.py` | Hosted-model client: requests, strict schema, health, error handling | 2, 3 |
| `ledgersync/errors.py` | `ModelRateLimited` | 3 |
| `ledgersync/pipeline.py` | Image-first routing, `prepare_image`, 3-page batches | 4 |
| `ledgersync/ollama_client.py` | Declares `prefers_images = False` | 4 |
| `server.py` | Chooses the client; passes the business name | 5, 7 |
| `eval/run_eval.py` | `ai_rate_limited` counts as unfinished | 5 |
| `qwen_service.py` | New prompt and schema | 7 |
| `ledgersync/accounts.py` | `choosable()`; name matching removed | 7 |
| `ledgersync/adapter.py` | Reads the new row shape | 7 |
| `tests/fakes.py` | `FakeUrlopen`, `http_error`, `chat_answer` (Task 2); new `ROW` (Task 7) | 2, 7 |
| `README.md` | Hosted-model section and settings; eval runs | 5, 8 |

---

### Task 1: AI provider settings and the `.env` file

**Files:** Modify `ledgersync/config.py`; Create `.env.example`; Test `tests/test_config.py`

**Interfaces — Produces:**
- New `Settings` fields:
  - `ai_provider: str = "ollama"`
  - `ai_base_url: str = "https://api.groq.com/openai/v1"`
  - `ai_model: str = "qwen/qwen3.8-27b"`
  - `ai_api_key: str = ""` (not in `repr`)
  - `ai_timeout: float = 60.0`
  - `ai_max_output_tokens: int = 4096`
  - `ai_reasoning_effort: str = "none"`
  - `ai_max_images: int = 3`
  - `ai_health_ttl: float = 300.0`
  - `business_name: str = ""`
- `config.ENV_FILE: Path` and `config.read_env_file(path) -> dict[str, str]`
- `Settings.from_env(env=None, env_file=ENV_FILE)`

- [ ] **Step 1: Failing tests.** In `tests/test_config.py`, change the import to `from ledgersync.config import Settings, read_env_file` and append:

```python
def test_hosted_model_settings_default_to_local_ollama_and_groq_values():
    s = Settings.from_env({})
    assert s.ai_provider == "ollama"
    assert (s.ai_base_url, s.ai_model) == ("https://api.groq.com/openai/v1", "qwen/qwen3.8-27b")
    assert (s.ai_timeout, s.ai_max_output_tokens, s.ai_reasoning_effort, s.ai_max_images) == (60.0, 4096, "none", 3)
    assert (s.ai_api_key, s.business_name, s.ai_health_ttl) == ("", "", 300.0)


def test_hosted_model_settings_are_read_from_the_environment():
    s = Settings.from_env({
        "LEDGERSYNC_AI_PROVIDER": " OpenAI ", "LEDGERSYNC_AI_BASE_URL": "https://example.test/v1/",
        "LEDGERSYNC_AI_MODEL": "some/model", "LEDGERSYNC_AI_API_KEY": " sk-test ",
        "LEDGERSYNC_AI_REASONING_EFFORT": "", "LEDGERSYNC_BUSINESS_NAME": "Northbridge Consulting Ltd",
    })
    assert (s.ai_provider, s.ai_base_url, s.ai_model) == ("openai", "https://example.test/v1", "some/model")
    assert (s.ai_api_key, s.ai_reasoning_effort, s.business_name) == ("sk-test", "", "Northbridge Consulting Ltd")


def test_an_unknown_provider_is_refused_at_startup():
    with pytest.raises(ValueError, match="LEDGERSYNC_AI_PROVIDER"):
        Settings.from_env({"LEDGERSYNC_AI_PROVIDER": "grok"})


def test_the_api_key_is_never_printed():
    assert "sk-secret" not in repr(Settings(ai_api_key="sk-secret"))


def test_env_file_values_are_used_but_real_environment_variables_win(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nLEDGERSYNC_AI_PROVIDER=openai\n"
                        'export LEDGERSYNC_AI_API_KEY="sk-from-file"\n'
                        "LEDGERSYNC_AI_MODEL='file/model'\nnot a setting\n")
    monkeypatch.setenv("LEDGERSYNC_AI_MODEL", "env/model")
    monkeypatch.delenv("LEDGERSYNC_AI_PROVIDER", raising=False)
    monkeypatch.delenv("LEDGERSYNC_AI_API_KEY", raising=False)
    s = Settings.from_env(env_file=env_file)
    assert (s.ai_provider, s.ai_api_key, s.ai_model) == ("openai", "sk-from-file", "env/model")


def test_a_missing_env_file_gives_no_values(tmp_path):
    assert read_env_file(tmp_path / "absent.env") == {}
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_config.py -q`. Expected: `ImportError: cannot import name 'read_env_file'`.

- [ ] **Step 3: Implement** in `ledgersync/config.py`:
  1. Change the docstring to `"""Runtime settings, read once from environment variables and the project's .env file."""`.
  2. Change `from dataclasses import dataclass` to `from dataclasses import dataclass, field` and add `from pathlib import Path`.
  3. Add below the imports:

```python
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"   # git-ignored; see .env.example
PROVIDERS = ("ollama", "openai")
```

  4. Add above `@dataclass(frozen=True)`:

```python
def read_env_file(path: Optional[Path]) -> dict[str, str]:
    """KEY=VALUE lines from a .env file. Comments, blank and malformed lines are skipped, and an
    `export ` prefix and surrounding quotes are removed; a missing file gives nothing."""
    if path is None or not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values
```

  5. Add after `log_level: str = "INFO"`:

```python
    # A hosted vision model behind an OpenAI-compatible API (Groq for the MVP demo); "ollama"
    # keeps everything on this machine.
    ai_provider: str = "ollama"
    ai_base_url: str = "https://api.groq.com/openai/v1"
    ai_model: str = "qwen/qwen3.8-27b"
    ai_api_key: str = field(default="", repr=False)   # never printed or logged
    ai_timeout: float = 60.0
    # 4096 keeps a one-photo request under the free plan's 8K tokens/min even if Groq counts the
    # requested maximum; a 60-row statement's answer is about 3.5K tokens.
    ai_max_output_tokens: int = 4096
    ai_reasoning_effort: str = "none"   # the model's "thinking" would spend the free plan's tokens
    ai_max_images: int = 3              # Groq's limit per request
    ai_health_ttl: float = 300.0        # the UI polls health every 30 s; free plans count requests
    business_name: str = ""             # whose books these are: tells sales invoices from purchases
```

  6. Replace the head of `from_env`, from its signature through `origins = …`, with:

```python
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None,
                 env_file: Optional[Path] = ENV_FILE) -> "Settings":
        """Settings from `env`, or else from the process environment laid over the .env file."""
        env = {**read_env_file(env_file), **os.environ} if env is None else env
        d = cls()
        origins = env.get("LEDGERSYNC_CORS_ORIGINS")
        provider = env.get("LEDGERSYNC_AI_PROVIDER", d.ai_provider).strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"LEDGERSYNC_AI_PROVIDER must be one of {', '.join(PROVIDERS)}, not {provider!r}.")
```

  7. Add these arguments after `log_level=…` in the `return cls(…)` call:

```python
            ai_provider=provider,
            ai_base_url=env.get("LEDGERSYNC_AI_BASE_URL", d.ai_base_url).strip().rstrip("/"),
            ai_model=env.get("LEDGERSYNC_AI_MODEL", d.ai_model).strip(),
            ai_api_key=env.get("LEDGERSYNC_AI_API_KEY", "").strip(),
            ai_timeout=float(env.get("LEDGERSYNC_AI_TIMEOUT", d.ai_timeout)),
            ai_max_output_tokens=int(env.get("LEDGERSYNC_AI_MAX_OUTPUT_TOKENS", d.ai_max_output_tokens)),
            ai_reasoning_effort=env.get("LEDGERSYNC_AI_REASONING_EFFORT", d.ai_reasoning_effort).strip(),
            ai_max_images=int(env.get("LEDGERSYNC_AI_MAX_IMAGES", d.ai_max_images)),
            business_name=env.get("LEDGERSYNC_BUSINESS_NAME", "").strip(),
```

  Create `.env.example`:

```
# Copy this file to .env (git-ignored) and paste your key after LEDGERSYNC_AI_API_KEY= to read
# documents with Groq's hosted vision model. While it is on, uploaded documents are sent to Groq.
# Set LEDGERSYNC_AI_PROVIDER=ollama (or delete .env) to go back to the local model.
LEDGERSYNC_AI_PROVIDER=openai
LEDGERSYNC_AI_BASE_URL=https://api.groq.com/openai/v1
LEDGERSYNC_AI_MODEL=qwen/qwen3.8-27b
LEDGERSYNC_AI_API_KEY=
# Whose books these are (tells sales invoices from purchase invoices), e.g. Northbridge Consulting Ltd
LEDGERSYNC_BUSINESS_NAME=
```

- [ ] **Step 4: Run and confirm pass.** Run the same command, then `git check-ignore -q .env.example || echo "tracked"`. Expected: 12 passed, then `tracked` (`.gitignore` ignores `.env` but not the example).
- [ ] **Step 5: Commit** — `git add ledgersync/config.py .env.example tests/test_config.py && git commit -m "Settings: hosted AI provider (Groq) and a .env file"`

---

### Task 2: Hosted-model client — requests, strict schema, health

**Files:**
- Create `ledgersync/openai_client.py`, `tests/test_openai_client.py`
- Modify `tests/fakes.py`

**Interfaces:**
- Consumes: the Task 1 `Settings` fields.
- Produces:
  - `openai_client.strict_schema(schema: dict) -> dict`
  - `openai_client.USER_AGENT`
  - `OpenAICompatibleClient(settings, opener=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep)`, with:
    - `model: str` and `max_images: int` (properties), and `prefers_images = True`
    - `health(force=False) -> OllamaHealth`, `ensure_available() -> None`, `warm_up() -> None`
    - `chat_json(messages, schema, images=None) -> str`, where images are base64 JPEG strings
  - In `tests/fakes.py`: `FakeUrlopen(*outcomes)` (with `.requests` and `.payload(n=-1)`), `http_error(code, message="failed", retry_after=None)` and `chat_answer(content='{"transactions": []}', finish_reason="stop")`.

- [ ] **Step 1: Failing tests.** Add to `tests/fakes.py`: put `import email.message`, `import io` and `import urllib.error` at the top, and append:

```python
class FakeUrlopen:
    """Stands in for urllib.request.urlopen: returns (as JSON) or raises scripted outcomes in order."""

    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _JsonResponse(outcome)

    def payload(self, n: int = -1) -> dict:
        return json.loads(self.requests[n][0].data)


class _JsonResponse:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, message: str = "failed", retry_after=None) -> urllib.error.HTTPError:
    """An HTTP error as OpenAI-compatible providers send it: {"error": {"message", "type"}}."""
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}}).encode()
    return urllib.error.HTTPError("https://api.example.test", code, message, headers, io.BytesIO(body))


def chat_answer(content: str = '{"transactions": []}', finish_reason: str = "stop") -> dict:
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": finish_reason}]}
```

`tests/test_openai_client.py`:

```python
import json
from typing import Optional

import pytest
from fakes import FakeUrlopen, chat_answer
from pydantic import BaseModel, Field

from ledgersync.config import Settings
from ledgersync.errors import ModelError, ModelUnavailable
from ledgersync.openai_client import OpenAICompatibleClient, strict_schema

KEY = "sk-test-1234"
SETTINGS = Settings(ai_provider="openai", ai_api_key=KEY)
MODELS = {"object": "list", "data": [{"id": "qwen/qwen3.8-27b", "object": "model", "active": True}]}
MSG = [{"role": "system", "content": "rules"}, {"role": "user", "content": "doc"}]
SCHEMA = {"type": "object", "properties": {"transactions": {"type": "array", "items": {"type": "object"}}}}


def make_client(*outcomes, settings=SETTINGS):
    http, now, slept = FakeUrlopen(*outcomes), [0.0], []
    client = OpenAICompatibleClient(settings, opener=http, clock=lambda: now[0], sleep=slept.append)
    return client, http, now, slept


def test_chat_asks_the_configured_model_for_strict_json():
    client, http, _, _ = make_client(chat_answer('{"transactions": [1]}'))
    assert client.chat_json(MSG, SCHEMA) == '{"transactions": [1]}'
    req, timeout = http.requests[0]
    body = http.payload()
    assert req.full_url == "https://api.groq.com/openai/v1/chat/completions" and timeout == 60.0
    assert req.get_header("Authorization") == f"Bearer {KEY}"
    assert req.get_header("User-agent").startswith("LedgerSync")   # urllib's default agent can be blocked
    assert (body["model"], body["temperature"], body["max_completion_tokens"], body["reasoning_effort"]) == (
        "qwen/qwen3.8-27b", 0, 4096, "none")
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False


def test_images_are_sent_as_jpeg_parts_of_the_last_message():
    client, http, _, _ = make_client(chat_answer())
    client.chat_json(MSG, SCHEMA, images=["AAAA", "BBBB"])
    system, user = http.payload()["messages"]
    assert system == {"role": "system", "content": "rules"}
    assert user["content"] == [
        {"type": "text", "text": "doc"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}},
    ]


def test_reasoning_effort_is_left_out_when_blank():
    client, http, _, _ = make_client(chat_answer(), settings=Settings(ai_provider="openai", ai_api_key=KEY,
                                                                     ai_reasoning_effort=""))
    client.chat_json(MSG, SCHEMA)
    assert "reasoning_effort" not in http.payload()


def test_an_answer_cut_off_at_the_token_limit_is_an_error():
    client, _, _, _ = make_client(chat_answer('{"transactions": [', finish_reason="length"))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == "ai_output_truncated"


def test_an_empty_answer_is_an_error():
    client, _, _, _ = make_client(chat_answer(""))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == "ai_output_invalid"


def test_strict_schema_inlines_refs_closes_objects_and_drops_unsupported_keywords():
    class Row(BaseModel):
        name: str = Field(..., title="Name", description="who")
        note: Optional[str] = None

    class Doc(BaseModel):
        rows: list[Row]

    schema = strict_schema(Doc.model_json_schema())
    text = json.dumps(schema)
    row = schema["properties"]["rows"]["items"]
    assert "$ref" not in text and "$defs" not in text and "title" not in text and "default" not in text
    assert schema["additionalProperties"] is False and row["additionalProperties"] is False
    assert row["required"] == ["name", "note"] and row["properties"]["name"]["description"] == "who"
    assert row["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_the_client_reads_images_itself_three_at_a_time():
    client, _, _, _ = make_client()
    assert client.prefers_images is True and client.max_images == 3


def test_health_needs_a_key():
    client, http, _, _ = make_client(settings=Settings(ai_provider="openai"))
    health = client.health()
    assert not health.model_available and "LEDGERSYNC_AI_API_KEY" in health.error and http.requests == []


def test_health_finds_the_model_in_the_providers_list():
    client, http, _, _ = make_client(MODELS)
    assert client.health().model_available
    assert http.requests[0][0].full_url == "https://api.groq.com/openai/v1/models"


def test_health_reports_a_model_the_provider_does_not_offer():
    client, _, _, _ = make_client({"data": [{"id": "openai/gpt-oss-20b", "active": True}]})
    health = client.health()
    assert health.reachable and not health.model_available and "qwen/qwen3.8-27b" in health.error


def test_health_is_cached_for_five_minutes():
    # The UI polls every 30 s, and free plans count requests.
    client, http, now, _ = make_client(MODELS, MODELS)
    client.health()
    now[0] += 299
    client.health()
    assert len(http.requests) == 1
    now[0] += 2
    client.health()
    assert len(http.requests) == 2


def test_ensure_available_explains_a_missing_key():
    client, _, _, _ = make_client(settings=Settings(ai_provider="openai"))
    with pytest.raises(ModelUnavailable, match="LEDGERSYNC_AI_API_KEY"):
        client.ensure_available()
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_openai_client.py -q`. Expected: `ModuleNotFoundError: No module named 'ledgersync.openai_client'`.

- [ ] **Step 3: Implement** — `ledgersync/openai_client.py`:

```python
"""Client for a hosted model behind an OpenAI-compatible API (Groq for the MVP demo). It has
the same members as OllamaClient, so the extractor and server take either one. The API key
travels only in the Authorization header: it is never logged or put into an error message."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from .config import Settings
from .errors import ModelError, ModelUnavailable
from .ollama_client import OllamaHealth   # same shape: reachable, model_available, error

logger = logging.getLogger(__name__)

USER_AGENT = "LedgerSync/0.3"   # urllib's default agent is refused by some API gateways
_STRICT_KEYWORDS = {"type", "properties", "required", "items", "enum", "anyOf", "description"}


def strict_schema(schema: dict) -> dict:
    """The Pydantic JSON schema in the form strict structured outputs accept: $defs inlined,
    every object closed (additionalProperties false) with all its properties required, and
    only supported keywords kept (no titles, defaults or formats)."""
    defs = schema.get("$defs", {})

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return convert(defs[node["$ref"].rsplit("/", 1)[-1]])
        out = {}
        for key, value in node.items():
            if key == "properties":
                out[key] = {name: convert(sub) for name, sub in value.items()}
            elif key in _STRICT_KEYWORDS:
                out[key] = convert(value)
        if out.get("type") == "object" or "properties" in out:
            out["required"] = list(out.get("properties", {}))
            out["additionalProperties"] = False
        return out

    return convert(schema)


class OpenAICompatibleClient:
    prefers_images = True   # a large vision model reads photos and scanned pages itself: no OCR

    def __init__(self, settings: Settings, opener: Callable = urllib.request.urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._s = settings
        self._open = opener
        self._clock = clock
        self._sleep = sleep
        self._health: Optional[OllamaHealth] = None
        self._health_at = 0.0

    @property
    def model(self) -> str:
        return self._s.ai_model

    @property
    def max_images(self) -> int:
        return self._s.ai_max_images

    def health(self, force: bool = False) -> OllamaHealth:
        now = self._clock()
        if not force and self._health is not None and now - self._health_at < self._s.ai_health_ttl:
            return self._health
        self._health, self._health_at = self._check(), now
        return self._health

    def ensure_available(self) -> None:
        health = self.health()
        if not health.model_available:
            raise ModelUnavailable(health.error or self._unreachable_message())

    def chat_json(self, messages: list[dict], schema: dict, images: Optional[list[str]] = None) -> str:
        """The model's JSON text, held to `schema` by strict structured outputs. Images are
        base64 JPEGs (pipeline.prepare_image), added to the last message."""
        messages = [dict(m) for m in messages]
        if images:
            messages[-1]["content"] = [
                {"type": "text", "text": messages[-1]["content"]},
                *({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}
                  for image in images),
            ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": self._s.ai_max_output_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "transactions", "schema": strict_schema(schema), "strict": True}},
        }
        if self._s.ai_reasoning_effort:
            payload["reasoning_effort"] = self._s.ai_reasoning_effort
        choice = (self._chat(payload).get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise ModelError("The document is too long for the AI model to answer in one pass. "
                             "Split it into smaller files and try again.", code="ai_output_truncated")
        content = (choice.get("message") or {}).get("content") or ""
        if not content.strip():
            raise ModelError("The AI model returned an empty answer.", code="ai_output_invalid")
        return content

    def warm_up(self) -> None:
        """Nothing to load on a hosted model: checks the key and model once. Never raises."""
        try:
            self.health(force=True)
        except Exception as exc:
            logger.warning("AI provider check failed (%s)", type(exc).__name__)

    def _check(self) -> OllamaHealth:
        if not self._s.ai_api_key:
            return OllamaHealth(False, False, "Set LEDGERSYNC_AI_API_KEY in .env to use the hosted AI model.")
        try:
            listing = self._request("GET", "/models", None, timeout=10)
        except Exception as exc:
            logger.info("AI provider not reachable (%s)", type(exc).__name__)
            return OllamaHealth(False, False, self._unreachable_message())
        offered = {m.get("id") for m in listing.get("data", []) if m.get("active", True)}
        if self.model in offered:
            return OllamaHealth(True, True)
        return OllamaHealth(True, False, self._missing_model_message())

    def _chat(self, payload: dict) -> dict:
        return self._request("POST", "/chat/completions", payload, timeout=self._s.ai_timeout)

    def _request(self, method: str, path: str, payload: Optional[dict], timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self._s.ai_base_url}{path}", data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._s.ai_api_key}",
            "User-Agent": USER_AGENT,
        })
        with self._open(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _unreachable_message(self) -> str:
        return (f"Cannot reach the AI provider at {self._s.ai_base_url}. "
                "Check the internet connection and LEDGERSYNC_AI_BASE_URL, then try again.")

    def _missing_model_message(self) -> str:
        return (f"The AI model '{self.model}' is not available from the provider at "
                f"{self._s.ai_base_url}. Check LEDGERSYNC_AI_MODEL.")
```

- [ ] **Step 4: Run and confirm pass.** Run the same command, then `.venv/bin/python -m pytest -q`. Expected: 12 passed; then the whole suite passes.
- [ ] **Step 5: Commit** — `git add ledgersync/openai_client.py tests/test_openai_client.py tests/fakes.py && git commit -m "Add a client for hosted models behind an OpenAI-compatible API"`

---

### Task 3: Hosted-model errors — rate limits, refusals, dropped connections

**Files:** Modify `ledgersync/errors.py`, `ledgersync/openai_client.py`; Test `tests/test_openai_client.py`

**Interfaces:**
- Consumes: Task 2's client and fakes.
- Produces:
  - `errors.ModelRateLimited` (503, code `ai_rate_limited`, a subclass of `ModelUnavailable`)
  - `openai_client.RATE_LIMIT_WAIT = 60.0` and `DEFAULT_RETRY_AFTER = 10.0`

- [ ] **Step 1: Failing tests.** In `tests/test_openai_client.py`:
  - add `import logging` and `import urllib.error` to the imports;
  - import `http_error` from fakes, alongside the other helpers;
  - import `ModelRateLimited` and `ModelTimeout` from `ledgersync.errors`;
  - then append:

```python
@pytest.mark.parametrize("status, error, code, words", [
    (401, ModelUnavailable, "ai_offline", "rejected the API key"),
    (403, ModelUnavailable, "ai_offline", "rejected the API key"),
    (404, ModelUnavailable, "ai_offline", "qwen/qwen3.8-27b"),
    (413, ModelError, "ai_input_too_long", "too large"),
])
def test_provider_refusals_become_clear_errors(status, error, code, words):
    client, _, _, _ = make_client(http_error(status))
    with pytest.raises(error) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == code and words in info.value.message


def test_a_context_length_400_means_the_document_is_too_long():
    client, _, _, _ = make_client(http_error(400, "Please reduce the length of the messages or completion."))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == "ai_input_too_long"


def test_other_400s_pass_on_the_providers_message():
    client, _, _, _ = make_client(http_error(400, "response_format is invalid"))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == "ai_error" and "response_format is invalid" in info.value.message


def test_a_rate_limit_is_waited_out_then_the_call_succeeds():
    client, _, _, slept = make_client(http_error(429, "rate limit", retry_after=7), chat_answer())
    assert client.chat_json(MSG, SCHEMA) == '{"transactions": []}'
    assert slept == [7.0]


def test_a_rate_limit_without_retry_after_waits_ten_seconds():
    client, _, _, slept = make_client(http_error(429), chat_answer())
    client.chat_json(MSG, SCHEMA)
    assert slept == [10.0]


def test_a_rate_limit_that_lasts_gives_up_after_a_minute():
    client, _, _, slept = make_client(*(http_error(429, retry_after=25) for _ in range(3)))
    with pytest.raises(ModelRateLimited) as info:
        client.chat_json(MSG, SCHEMA)
    assert (info.value.code, info.value.status_code, slept) == ("ai_rate_limited", 503, [25.0, 25.0])


def test_a_server_error_is_retried_once():
    client, _, _, slept = make_client(http_error(503), chat_answer())
    assert client.chat_json(MSG, SCHEMA) == '{"transactions": []}' and slept == [1.0]
    client, _, _, _ = make_client(http_error(500), http_error(502))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == "ai_error"


def test_no_connection_after_one_retry_is_ai_offline():
    client, _, _, _ = make_client(*(urllib.error.URLError(ConnectionRefusedError()) for _ in range(2)))
    with pytest.raises(ModelUnavailable) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.code == "ai_offline" and "Cannot reach the AI provider" in info.value.message


def test_a_slow_provider_times_out_with_504():
    client, _, _, _ = make_client(TimeoutError())
    with pytest.raises(ModelTimeout) as info:
        client.chat_json(MSG, SCHEMA)
    assert info.value.status_code == 504


def test_the_key_never_reaches_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    client, _, _, _ = make_client(http_error(400, f"bad request from key {KEY}"))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert KEY not in info.value.message and KEY not in caplog.text


def test_health_reports_a_rejected_key():
    client, _, _, _ = make_client(http_error(401))
    health = client.health()
    assert health.reachable and not health.model_available and "rejected the API key" in health.error


def test_health_counts_a_rate_limited_check_as_available():
    # Being rate limited proves the key works; refusing analyses for the 5-minute cache would not help.
    client, _, _, _ = make_client(http_error(429))
    assert client.health().model_available
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_openai_client.py -q`. Expected: `ImportError: cannot import name 'ModelRateLimited'`.

- [ ] **Step 3: Implement.**

`ledgersync/errors.py`: append

```python
class ModelRateLimited(ModelUnavailable):
    """The hosted model's rate limit (e.g. a free plan's tokens per minute) is used up for now."""

    code = "ai_rate_limited"
```

`ledgersync/openai_client.py`:
- Add `import http.client` to the imports.
- Change the errors import to `from .errors import ModelError, ModelRateLimited, ModelTimeout, ModelUnavailable`.
- Add below `_STRICT_KEYWORDS`:

```python
RATE_LIMIT_WAIT = 60.0      # most seconds one call waits out 429s before giving up
DEFAULT_RETRY_AFTER = 10.0  # when a 429 has no Retry-After header
```

Replace `_check` and `_chat` with:

```python
    def _check(self) -> OllamaHealth:
        if not self._s.ai_api_key:
            return OllamaHealth(False, False, "Set LEDGERSYNC_AI_API_KEY in .env to use the hosted AI model.")
        try:
            listing = self._request("GET", "/models", None, timeout=10)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:   # rate limited: the key works, and the next chat call waits it out
                return self._health or OllamaHealth(True, True)
            return OllamaHealth(True, False, self._http_error(exc.code, self._detail(exc)).message)
        except Exception as exc:
            logger.info("AI provider not reachable (%s)", type(exc).__name__)
            return OllamaHealth(False, False, self._unreachable_message())
        offered = {m.get("id") for m in listing.get("data", []) if m.get("active", True)}
        if self.model in offered:
            return OllamaHealth(True, True)
        return OllamaHealth(True, False, self._missing_model_message())

    def _chat(self, payload: dict) -> dict:
        """POST /chat/completions. Waits out rate limits (up to RATE_LIMIT_WAIT seconds), retries a
        dropped connection or a server error once, and turns every other failure into a typed
        error that names neither the document nor the key."""
        waited, retried = 0.0, False
        while True:
            try:
                return self._request("POST", "/chat/completions", payload, timeout=self._s.ai_timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = self._retry_after(exc)
                    if waited + wait > RATE_LIMIT_WAIT:
                        raise ModelRateLimited("The AI provider's rate limit is used up for now (free plans "
                                               "allow a few requests a minute). Try again in a minute.") from None
                    logger.info("AI provider rate limit reached; waiting %.0f s", wait)
                    self._sleep(wait)
                    waited += wait
                elif exc.code >= 500 and not retried:
                    logger.warning("AI provider returned HTTP %d; retrying once", exc.code)
                    retried = True
                    self._sleep(1.0)
                else:
                    raise self._http_error(exc.code, self._detail(exc)) from None
            except TimeoutError:
                raise ModelTimeout(self._timeout_message()) from None
            except (urllib.error.URLError, ConnectionResetError, http.client.HTTPException) as exc:
                if isinstance(getattr(exc, "reason", None), TimeoutError):
                    raise ModelTimeout(self._timeout_message()) from None
                if retried:
                    self._health = None
                    raise ModelUnavailable(self._unreachable_message()) from None
                logger.warning("Lost the connection to the AI provider; retrying once")
                retried = True
                self._sleep(1.0)
            except json.JSONDecodeError:
                raise ModelError("The AI provider returned a response that is not JSON.",
                                 code="ai_output_invalid") from None

    def _http_error(self, status: int, detail: str) -> Exception:
        if status in (401, 403):
            self._health = None
            return ModelUnavailable("The AI provider rejected the API key. Check LEDGERSYNC_AI_API_KEY in .env.")
        if status == 404:
            self._health = None
            return ModelUnavailable(self._missing_model_message())
        lowered = detail.lower()
        if status == 413 or (status == 400 and ("context" in lowered or "reduce the length" in lowered)):
            return ModelError("This document is too large for the AI provider's limits on the current plan. "
                              "Split it into smaller files and try again.", code="ai_input_too_long")
        return ModelError(f"The AI provider returned HTTP {status}: {detail}")

    def _detail(self, exc: urllib.error.HTTPError) -> str:
        """The provider's own error message, shortened, with the key blanked in case it is echoed."""
        raw = exc.read().decode("utf-8", "replace")
        try:
            message = str(json.loads(raw)["error"]["message"])
        except (ValueError, KeyError, TypeError):
            message = raw
        message = message[:300]
        return message.replace(self._s.ai_api_key, "***") if self._s.ai_api_key else message

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float:
        try:
            return max(1.0, float(exc.headers.get("retry-after")))
        except (AttributeError, TypeError, ValueError):
            return DEFAULT_RETRY_AFTER

    def _timeout_message(self) -> str:
        return (f"The AI provider did not answer within {self._s.ai_timeout:.0f} seconds. "
                "Try again, or raise LEDGERSYNC_AI_TIMEOUT.")
```

- [ ] **Step 4: Run and confirm pass.** Run the same command, then `.venv/bin/python -m pytest -q`. Expected: 27 passed in `test_openai_client.py`; then the whole suite passes.
- [ ] **Step 5: Commit** — `git add ledgersync/errors.py ledgersync/openai_client.py tests/test_openai_client.py && git commit -m "Hosted-model client: wait out rate limits, clear errors, the key never shown"`

---

### Task 4: Photos and scans go to the vision model as images

**Files:** Modify `ledgersync/pipeline.py`, `ledgersync/ollama_client.py`; Test `tests/test_pipeline.py`

**Interfaces:**
- Consumes: client attributes `prefers_images`, `max_images` and `model` (`OpenAICompatibleClient` from Task 2; `OllamaClient` declares `prefers_images = False`). Clients without the attribute count as `False`.
- Produces:
  - `pipeline.prepare_image(data: bytes, max_side: int = 1600) -> str` (base64 JPEG)
  - `analyze(...)`: same signature. For a client that prefers images it returns one `TransactionExtractionResult` merged over all its requests.

- [ ] **Step 1: Failing tests.** In `tests/test_pipeline.py`:
  - add `import base64` to the imports;
  - add `from qwen_service import TransactionExtractionResult`;
  - change the pipeline import to `from ledgersync.pipeline import analyze, encode_images, prepare_image`;
  - then append:

```python
class VisionExtractor(RecordingExtractor):
    """An extractor whose model reads images itself, at most three per request."""

    def __init__(self):
        super().__init__()
        self.client = type("Client", (), {"model": "vision-model", "prefers_images": True, "max_images": 3})()

    def extract_accounting_data(self, text_input=None, images=None):
        self.calls.append({"text": text_input, "images": images})
        return TransactionExtractionResult(success=True, count=0, data=[], model="vision-model",
                                           warnings=[f"Row 1 skipped (call {len(self.calls)})."])


def photo(size=(3000, 1000), orientation=None):
    buf = io.BytesIO()
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    Image.new("RGB", size, "white").save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def scanned_pdf(pages):
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=png())
    return doc.tobytes()


def test_photos_go_straight_to_a_vision_first_model_without_ocr():
    ex, ocr = VisionExtractor(), FakeOcr(text="TOTAL 12.50")
    analyze(Intake("image", data=photo()), ex, ocr, ctx())
    assert ocr.calls == 0 and ex.calls[0]["text"] is None and len(ex.calls[0]["images"]) == 1


def test_a_sideways_phone_photo_is_sent_upright_small_and_as_jpeg():
    # EXIF orientation 6: the phone was turned 90 degrees; the pixels are stored sideways.
    sent = Image.open(io.BytesIO(base64.b64decode(prepare_image(photo((3000, 1000), orientation=6)))))
    assert sent.format == "JPEG" and max(sent.size) == 1600 and sent.size[0] < sent.size[1]


def test_scanned_pages_go_three_per_request_and_warnings_name_their_pages():
    ex = VisionExtractor()
    result = analyze(Intake("pdf", data=scanned_pdf(5)), ex, FakeOcr(text="unused"), ctx())
    assert [len(c["images"]) for c in ex.calls] == [3, 2]
    assert result.warnings == ["Pages 1-3: Row 1 skipped (call 1).", "Pages 4-5: Row 1 skipped (call 2)."]


def test_a_single_scanned_page_needs_no_page_labels():
    ex = VisionExtractor()
    result = analyze(Intake("pdf", data=scanned_pdf(1)), ex, FakeOcr(), ctx())
    assert len(ex.calls) == 1 and result.warnings == ["Row 1 skipped (call 1)."]


def test_a_pdf_with_a_text_layer_still_goes_as_text():
    ex = VisionExtractor()
    analyze(Intake("pdf", data=text_pdf()), ex, FakeOcr(), ctx())
    assert "Invoice total 120.00" in ex.calls[0]["text"] and ex.calls[0]["images"] is None
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_pipeline.py -q`. Expected: `ImportError: cannot import name 'prepare_image'`.

- [ ] **Step 3: Implement.**
  - In `ledgersync/ollama_client.py`, add as the first line of `class OllamaClient:`:

```python
    prefers_images = False   # the small local model gets OCR text first; it fits its 4K context
```

  - Replace `ledgersync/pipeline.py` with:

```python
"""Phase 1 routing: turns a validated upload into model input and runs the extractor.

A client that prefers images (a hosted vision model) gets photos and scanned pages as images,
never OCR text; the local model gets OCR text first, as before. Replaced in Phase 4 by the
hybrid pipeline (deterministic parsing + LLM classification)."""
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
    vision_first = getattr(extractor.client, "prefers_images", False)
    text, images = intake.text, None
    if intake.kind == "pdf":
        ctx.report("Extracting text from the PDF")
        text = _pdf_text(intake.data)
        if not text and vision_first:
            return _read_pages(_pdf_pages(intake.data), extractor, ctx)
        text = text or _scanned_pdf_text(intake.data, ocr, ctx)
        if not text:
            images = encode_images(intake.data, is_pdf=True)
    elif intake.kind == "image":
        if vision_first:
            return _read_pages([intake.data], extractor, ctx)
        ctx.report("Reading the image")
        text = _ocr_text(ocr, intake.data)
        if not text:
            images = encode_images(intake.data, is_pdf=False)
    ctx.report(f"Asking the AI model ({extractor.client.model})")
    return extractor.extract_accounting_data(text_input=text, images=images)


def prepare_image(data: bytes, max_side: int = 1600) -> str:
    """A photo or page as a vision model should get it: upright (the camera's EXIF orientation
    applied), at most max_side pixels on its long side, as a base64 JPEG."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as img:
        upright = ImageOps.exif_transpose(img).convert("RGB")
    upright.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    upright.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def encode_images(data: bytes, is_pdf: bool) -> list[str]:
    """Base64 images for the local vision model; PDFs are rendered page by page."""
    pages = _pdf_pages(data) if is_pdf else [data]
    return [base64.b64encode(page).decode("ascii") for page in pages]


def _read_pages(pages: list[bytes], extractor, ctx: JobContext):
    """Sends page images to the model, client.max_images per request, and merges the answers.
    With several requests, each warning says which pages it came from."""
    size = max(1, extractor.client.max_images)
    several = len(pages) > size
    result = None
    for start in range(0, len(pages), size):
        batch = pages[start:start + size]
        label = f"Page {start + 1}" if len(batch) == 1 else f"Pages {start + 1}-{start + len(batch)}"
        ctx.report(f"Asking the AI model ({extractor.client.model})" + (f": {label.lower()}" if several else ""))
        part = extractor.extract_accounting_data(text_input=None, images=[prepare_image(p) for p in batch])
        if several:
            part = part.model_copy(update={"warnings": [f"{label}: {w}" for w in part.warnings]})
        result = part if result is None else result.model_copy(update={
            "count": result.count + part.count, "data": result.data + part.data,
            "warnings": result.warnings + part.warnings})
    return result


def _pdf_pages(data: bytes) -> list[bytes]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [page.get_pixmap(dpi=150).tobytes("png") for page in doc]


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

- [ ] **Step 4: Run and confirm pass.** Run the same command, then `.venv/bin/python -m pytest -q`. Expected: 13 passed in `test_pipeline.py` (8 existing + 5 new); then the whole suite passes.
- [ ] **Step 5: Commit** — `git add ledgersync/pipeline.py ledgersync/ollama_client.py tests/test_pipeline.py && git commit -m "Pipeline: a vision-first model gets photos and scans as upright JPEGs, 3 pages per request"`

---

### Task 5: Choose the provider; rate-limited cases re-run; live test; docs

**Files:**
- Modify `server.py`, `eval/run_eval.py`, `tests/test_api.py`, `tests/test_eval_runner.py`, `tests/test_live_llm.py` and `README.md`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `server.make_model_client(settings) -> OllamaClient | OpenAICompatibleClient`
  - `"ai_rate_limited"` added to `run_eval.HARNESS_ERRORS`

- [ ] **Step 1: Failing tests.**
  - In `tests/test_api.py`:
    - add `from fakes import FakeUrlopen, http_error` (extending the existing fakes import);
    - add `from ledgersync.ollama_client import OllamaClient` and `from ledgersync.openai_client import OpenAICompatibleClient`;
    - change `from server import create_app` to `from server import create_app, make_model_client`;
    - then append:

```python
def test_the_provider_setting_chooses_the_model_client():
    assert isinstance(make_model_client(Settings(ai_provider="openai", ai_api_key="k")), OpenAICompatibleClient)
    assert isinstance(make_model_client(Settings()), OllamaClient)


def test_health_never_shows_the_api_key(make_client):
    # Pins the rule at the API boundary: health passes on the client's error, which never holds the key.
    hosted = OpenAICompatibleClient(Settings(ai_provider="openai", ai_api_key="sk-secret"),
                                    opener=FakeUrlopen(http_error(401, "Invalid API Key sk-secret")))
    body = make_client(hosted).get("/api/health").text
    assert "sk-secret" not in body and "rejected the API key" in body
```

  - In `tests/test_eval_runner.py`, append:

```python
def test_rate_limited_cases_count_as_unfinished():
    # A free plan's per-minute limit is not a model failure: --resume runs those cases again.
    assert exit_code({"cases": [{"case": "a", "error": "ai_rate_limited"}]}) == 1
```

  - In `tests/test_live_llm.py`:
    - add `from pathlib import Path`, `from ledgersync.openai_client import OpenAICompatibleClient` and `from ledgersync.pipeline import prepare_image`;
    - add `FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"`;
    - then append:

```python
def test_hosted_vision_model_reads_a_receipt_photo():
    settings = Settings.from_env()
    if settings.ai_provider != "openai" or not settings.ai_api_key:
        pytest.skip("set LEDGERSYNC_AI_PROVIDER=openai and LEDGERSYNC_AI_API_KEY (e.g. in .env) to run")
    client = OpenAICompatibleClient(settings)
    client.ensure_available()
    photo = (FIXTURES / "img-train-ticket" / "input.png").read_bytes()
    result = QwenAccountingExtractor(client).extract_accounting_data(images=[prepare_image(photo)])
    assert any(abs(abs(t.amount) - 87.50) < 0.01 for t in result.data)
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_api.py tests/test_eval_runner.py -q`. Expected: `ImportError: cannot import name 'make_model_client'`. After Step 3, `test_health_never_shows_the_api_key` passes; it already held after Task 3, and this test pins the rule at the API. `test_rate_limited_cases_count_as_unfinished` fails until `HARNESS_ERRORS` changes.

- [ ] **Step 3: Implement.**
  - **`server.py`:**
    - Add `from ledgersync.openai_client import OpenAICompatibleClient`.
    - Add above `create_app`:

```python
def make_model_client(settings: Settings):
    """The model client the settings choose: the local Ollama server, or a hosted model behind an
    OpenAI-compatible API (LEDGERSYNC_AI_PROVIDER=openai, e.g. Groq)."""
    if settings.ai_provider == "openai":
        return OpenAICompatibleClient(settings)
    return OllamaClient(settings)
```

    - In `create_app`:
      - change `ollama = ollama or OllamaClient(settings)` to `ollama = ollama or make_model_client(settings)`;
      - change the warm-up thread's name to `"model-warmup"`;
      - change the FastAPI `description` to `"Qwen vision model (local Ollama or a hosted API) → structured accounting data → trial balance"`.
  - **`eval/run_eval.py`:** replace the `HARNESS_ERRORS` line and its comment with:

```python
# The harness or a rate limit stopped these cases, not the model: --resume runs them again.
HARNESS_ERRORS = frozenset({"api_unreachable", "eval_timeout", "job_not_found", "ai_rate_limited"})
```

  - **`README.md`:**
    - In the module list, change the `ledgersync/` line's "Ollama client" to "Ollama and hosted-model clients".
    - Append these rows to the configuration table:

```markdown
| `LEDGERSYNC_AI_PROVIDER` | `ollama` | `openai` reads documents with a hosted model (see below) |
| `LEDGERSYNC_AI_BASE_URL` | `https://api.groq.com/openai/v1` | OpenAI-compatible API of the hosted model |
| `LEDGERSYNC_AI_MODEL` | `qwen/qwen3.8-27b` | Hosted model; it must read images |
| `LEDGERSYNC_AI_API_KEY` | — | Key for the hosted model: put it in `.env`, never commit it |
| `LEDGERSYNC_AI_TIMEOUT` | `60` | Seconds to wait for the hosted model |
| `LEDGERSYNC_AI_MAX_OUTPUT_TOKENS` | `4096` | Longest answer; raise it on a paid plan for very long statements |
| `LEDGERSYNC_AI_REASONING_EFFORT` | `none` | The model's "thinking" (`none`, `low`, …); off saves tokens |
| `LEDGERSYNC_AI_MAX_IMAGES` | `3` | Pages per request (Groq's limit) |
| `LEDGERSYNC_BUSINESS_NAME` | — | Whose books these are; tells sales invoices from purchases |
```

    - Add this section after the configuration notes, before "## Tests":

```markdown
## Hosted vision model (Groq)

For the MVP demo the app can read documents with Groq's hosted `qwen/qwen3.8-27b` (27B, reads
images) instead of the local model: copy `.env.example` to `.env`, paste your key after
`LEDGERSYNC_AI_API_KEY=`, and restart `server.py`. Photos and scanned pages then go to the model as
images, without OCR, so EasyOCR is never loaded. To go back to the local model, set
`LEDGERSYNC_AI_PROVIDER=ollama` or delete `.env`.

- **Documents leave this machine** while the hosted model is on: they are sent to Groq.
- Groq's free plan for this model allows about 30 requests and 8,000 tokens a minute and 200,000
  tokens a day (checked 2026-09-29); each image counts as 2,048 tokens. At the limit the app waits
  up to a minute, then reports a rate limit; `eval/run_eval.py --resume` runs those documents again.
- The model is a Groq *preview* model and may change; `LEDGERSYNC_AI_MODEL` switches it.
- `.venv/bin/python -m pytest -m llm -k hosted` checks the key and model with one receipt photo.
```

- [ ] **Step 4: Run and confirm pass.** Run `.venv/bin/python -m pytest -q`. Expected: all pass. The live test is deselected unless `-m llm`.
- [ ] **Step 5: Commit** — `git add server.py eval/run_eval.py tests/test_api.py tests/test_eval_runner.py tests/test_live_llm.py README.md && git commit -m "Choose the model provider by setting; rate-limited eval cases re-run; hosted-model docs"`

---

### Task 6: Measure the swap (eval `groq-swap`)

This task keeps the Phase 1 prompt and measures only the model change.

- [ ] **Step 1: Check the key without printing it.** Run:

```bash
.venv/bin/python -c "from ledgersync.config import Settings; s = Settings.from_env(); print(s.ai_provider, s.ai_model, 'key set' if s.ai_api_key else 'NO KEY')"
```

Expected: `openai qwen/qwen3.8-27b key set`. If it prints `ollama` or `NO KEY`, **stop and ask the user** to copy `.env.example` to `.env` and paste their key into it. The key must never be pasted into chat.

- [ ] **Step 2: Live smoke test.** Run `.venv/bin/python -m pytest -m llm tests/test_live_llm.py -k hosted -q`. Expected: 1 passed.

If it fails with `ai_input_too_long`, the free plan counts the requested output against its 8K tokens/min. Set `LEDGERSYNC_AI_MAX_OUTPUT_TOKENS=2048` in `.env`, run it again, and record the setting in the eval note.

- [ ] **Step 3: Start the API** with launch config `api`, which reads `.env`. Confirm with `curl -s 127.0.0.1:8085/api/health`. Expected: `"model":"qwen/qwen3.8-27b"` and `"model_available":true`.

- [ ] **Step 4: Run the eval** in the background:

```bash
.venv/bin/python eval/run_eval.py --label groq-swap --note "Groq qwen/qwen3.8-27b on the free plan, reasoning_effort none, max_completion_tokens 4096; Phase 1 prompt; photos and scans sent as images (no OCR); API on an 8 GB Apple M2"
```

If cases end with `ai_rate_limited`, wait a minute and run the same command with `--resume`. Expected: a summary table. Every case either has rows or a model error; no harness errors remain.

- [ ] **Step 5:** Stop the API. **Commit** — `git add eval/results/*-groq-swap.json && git commit -m "Record the Groq swap accuracy run"`

---

### Task 7: New prompt, chart-of-accounts choice, VAT as printed

**Files:**
- Modify `qwen_service.py`, `ledgersync/accounts.py`, `ledgersync/adapter.py` and `server.py`
- Tests: `tests/test_qwen_service.py`, `tests/test_adapter.py`, `tests/test_accounts.py`, `tests/fakes.py`, `tests/test_api.py`, `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `accounts.CHART`, `BY_CODE`, `BANK`, `SALES_VAT`, `PURCHASE_VAT`, `SUSPENSE`; the Phase 3 `Transaction` (`vat` is the VAT shown, `vat_posted` the VAT booked).
- Produces:
  - `accounts.choosable() -> tuple[Account, ...]`
  - `qwen_service.ACCOUNT_CHOICES: tuple[str, ...]` (`"<code> <name>"`)
  - model rows `{description, date, amount, direction: "in"|"out", account: one of ACCOUNT_CHOICES, vat: float|None, currency}`
  - `QwenAccountingExtractor(client, business_name: str = "")`
- Removes `accounts.match_name` and `accounts.NameMatch`, which have no users left.

- [ ] **Step 1: Failing tests.**
  - `tests/fakes.py`: replace `ROW` with:

```python
ROW = {"description": "BT Business Broadband", "date": "2026-09-01", "amount": 72.0, "direction": "out",
       "account": "7502 Telephone and Internet", "vat": None, "currency": "GBP"}
```

  - `tests/test_eval_runner.py`: replace `HMRC_ROW` and `PAYE_ROW` with:

```python
HMRC_ROW = {"description": "HMRC VAT settlement payment", "date": "2026-09-07", "amount": 1450.0,
            "direction": "out", "account": "2202 VAT Liability", "vat": None, "currency": "GBP"}
```

```python
PAYE_ROW = {"description": "HMRC PAYE and National Insurance", "date": "2026-09-19", "amount": 2140.37,
            "direction": "out", "account": "2210 PAYE and National Insurance", "vat": None, "currency": "GBP"}
```

  - `tests/test_api.py`:
    - in `test_partial_success_returns_valid_rows_and_warnings`, change `dict(ROW, type="transfer")` to `dict(ROW, direction="sideways")`;
    - append:

```python
def test_the_business_name_setting_reaches_the_model():
    ollama = FakeOllama([transactions_json(ROW)])
    with TestClient(create_app(Settings(warmup=False, business_name="Acme Ltd"), ollama=ollama, ocr=FakeOcr())) as client:
        run_text_job(client)
    assert "Acme Ltd" in ollama.calls[0]["messages"][0]["content"]
```

  - `tests/test_qwen_service.py`:
    - change the import to `from qwen_service import ACCOUNT_CHOICES, ExtractedTransactions, QwenAccountingExtractor`;
    - in `test_one_bad_row_does_not_fail_the_batch`, change `dict(ROW, type="transfer")` to `dict(ROW, direction="sideways")` and `"Row 3 skipped (type:"` to `"Row 3 skipped (direction:"`;
    - append:

```python
def test_the_model_must_pick_an_account_from_the_chart():
    account = ExtractedTransactions.model_json_schema()["$defs"]["AccountingTransaction"]["properties"]["account"]
    assert account["enum"] == list(ACCOUNT_CHOICES) and "7502 Telephone and Internet" in account["enum"]
    assert not [choice for choice in account["enum"] if choice[:4] in ("1200", "2200", "2201")]


def test_instructions_and_the_document_travel_separately():
    _, client = extract(transactions_json(ROW), "BT Broadband 72.00")
    system, user = client.calls[0]["messages"]
    assert system["role"] == "system" and "7502 Telephone and Internet" in system["content"]
    assert user == {"role": "user", "content": "<document>\nBT Broadband 72.00\n</document>"}


def test_images_are_announced_in_the_user_message():
    _, client = extract(transactions_json(), None, images=["aGk=", "aGk="])
    assert client.calls[0]["messages"][1]["content"] == "The document is attached as 2 image(s)."


def test_the_business_name_tells_sales_from_purchases():
    client = FakeOllama([transactions_json(ROW)])
    QwenAccountingExtractor(client, business_name="Northbridge Consulting Ltd").extract_accounting_data("x")
    assert "An invoice issued by Northbridge Consulting Ltd is a sale" in client.calls[0]["messages"][0]["content"]


def test_rows_carry_direction_and_printed_vat():
    result, _ = extract(transactions_json(dict(ROW, direction="in", vat=12.0)))
    assert (result.data[0].direction, result.data[0].vat) == ("in", 12.0)
```

  - Replace `tests/test_accounts.py` with:

```python
import json
from pathlib import Path

from ledgersync.accounts import BY_CODE, CHART, AccountType, choosable

FIXTURES = Path(__file__).parent.parent / "eval" / "fixtures"


def test_codes_are_unique_four_digit_strings():
    codes = [a.code for a in CHART]
    assert len(codes) == len(set(codes)) and all(len(c) == 4 and c.isdigit() for c in codes)


def test_every_code_the_eval_expects_exists():
    used = {r["account_code"] for p in FIXTURES.glob("*/expected.json") for r in json.loads(p.read_text())["rows"]}
    assert used and used <= set(BY_CODE)


def test_posting_accounts_have_the_right_types():
    assert BY_CODE["1200"].type == AccountType.ASSET
    assert BY_CODE["2200"].type == BY_CODE["2201"].type == AccountType.LIABILITY
    assert BY_CODE["4000"].type == AccountType.INCOME and BY_CODE["7502"].type == AccountType.EXPENSE


def test_a_model_may_choose_any_account_but_the_bank_and_the_vat_control_accounts():
    # The bank is the other side of every posting, and the ledger splits VAT itself.
    assert {a.code for a in choosable()} == set(BY_CODE) - {"1200", "2200", "2201"}
```

  - Replace `tests/test_adapter.py` with:

```python
import datetime as dt
from decimal import Decimal

import pytest

from ledgersync.adapter import parse_date, to_transactions
from ledgersync.models import BusinessSettings

ROW = {"description": "BT Business Broadband", "date": "2026-09-01", "amount": 72.0, "direction": "out",
       "account": "7502 Telephone and Internet", "vat": None, "currency": "GBP"}


def adapt(*rows):
    return to_transactions(list(rows), source="table", settings=BusinessSettings())


def issues_of(t):
    return [(i.code, i.severity) for i in t.issues]


def test_model_rows_become_ledger_transactions():
    [t] = adapt(ROW)
    assert (t.direction.value, t.gross, t.account_code, t.date) == ("out", Decimal("72.00"), "7502", dt.date(2026, 9, 1))
    assert (t.vat_posted, t.source, t.method) == (Decimal("12.00"), "table", "llm")


def test_vat_printed_on_the_document_is_booked_as_printed():
    [t] = adapt({**ROW, "vat": 10.0})
    assert (t.vat, t.vat_posted, t.net) == (Decimal("10.00"), Decimal("10.00"), Decimal("62.00"))
    assert "vat_estimated" not in [code for code, _ in issues_of(t)]


def test_money_in_stays_money_in():
    [t] = adapt({**ROW, "direction": "in", "account": "4000 Sales"})
    assert (t.direction.value, t.account_code) == ("in", "4000")


def test_negative_amounts_become_money_out():
    # Baseline: bank rows the model kept negative were dropped, so bank exports scored 20%.
    [t] = adapt({**ROW, "amount": -72.0, "direction": "in", "vat": -12.0})
    assert (t.direction.value, t.gross, t.vat) == ("out", Decimal("72.00"), Decimal("12.00"))
    assert "direction_conflict" in [code for code, _ in issues_of(t)]


@pytest.mark.parametrize("account", ["9999 Nonsense", "1200 Bank Current Account", "", None])
def test_accounts_that_cannot_be_posted_to_go_to_suspense(account):
    [t] = adapt({**ROW, "account": account})
    assert t.account_code == "9998" and ("account_not_recognised", "warning") in issues_of(t)
    assert not [i for i in t.issues if i.severity == "error"]


def test_a_suspense_choice_is_flagged_for_review():
    [t] = adapt({**ROW, "account": "9998 Suspense"})
    assert t.account_code == "9998" and ("account_not_recognised", "warning") in issues_of(t)


def test_zero_or_unreadable_amounts_are_skipped():
    assert adapt({**ROW, "amount": 0}, {**ROW, "amount": None}) == []


def test_uk_dates_are_read_day_first():
    assert parse_date("03/09/2026") == dt.date(2026, 9, 3)
    assert parse_date("2026-09-03 00:00:00") == dt.date(2026, 9, 3)
    assert parse_date("sometime") is None


def test_one_absurd_amount_does_not_sink_the_other_rows():
    # A model row of 1e26 used to raise inside to_money and fail the whole analysis job.
    assert [t.gross for t in adapt({**ROW, "amount": 1e26}, ROW)] == [Decimal("72.00")]
```

- [ ] **Step 2: Run and confirm failure.** Run `.venv/bin/python -m pytest tests/test_accounts.py tests/test_adapter.py tests/test_qwen_service.py tests/test_api.py tests/test_eval_runner.py -q`. Expected:
  - `test_accounts.py` fails to import `choosable`;
  - `test_qwen_service.py` fails to import `ACCOUNT_CHOICES`;
  - the adapter tests fail on direction and account;
  - the new API test fails, because the business name is not in the prompt;
  - the eval-runner tests fail, because the old schema skips rows that have no `type`.

- [ ] **Step 3: Implement.**

**`ledgersync/accounts.py`:**
- Delete `import re`, `NamedTuple` from the typing import, `_BALANCE_SHEET`, `NameMatch`, `_norm`, `_NAMES`, `_BY_LENGTH` and `match_name`.
- Change the `aliases` comment to `# examples shown to the model next to the name`.
- Append:

```python
def choosable() -> tuple[Account, ...]:
    """Accounts a model may pick for a transaction: every account except the bank (the other
    side of each posting) and the VAT control accounts (the ledger splits VAT itself)."""
    return tuple(a for a in CHART if a.code not in (BANK, SALES_VAT, PURCHASE_VAT))
```

**`qwen_service.py`:** replace everything above `def _validate_and_parse_json` with:

```python
import json
import logging
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ledgersync.accounts import choosable
from ledgersync.errors import ModelError

logger = logging.getLogger(__name__)

# The accounts a model may choose, as "<code> <name>", e.g. "7502 Telephone and Internet".
ACCOUNT_CHOICES: tuple[str, ...] = tuple(f"{a.code} {a.name}" for a in choosable())


class AccountingTransaction(BaseModel):
    """One row as the model returns it; its JSON schema holds the model's answer to this shape."""
    # Every field is listed as required so structured output always emits it (null when unknown).
    model_config = ConfigDict(json_schema_extra={
        "required": ["description", "date", "amount", "direction", "account", "vat", "currency"]
    })

    description: str = Field(..., description="Who was paid or who paid, and what for")
    date: Optional[str] = Field(None, description="YYYY-MM-DD, or null when the document shows no date")
    amount: float = Field(..., description="The money that moved, including VAT, as a positive number")
    direction: Literal["in", "out"] = Field(
        ..., description="in: money into the business's bank account; out: money paid out of it")
    account: Literal[ACCOUNT_CHOICES] = Field(..., description="The account from the chart of accounts")
    vat: Optional[float] = Field(None, description="The VAT amount printed on the document, or null")
    currency: Optional[str] = Field("GBP", description="Currency code, e.g. GBP")

    @field_validator("direction", mode="before")
    @classmethod
    def _lowercase_direction(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class ExtractedTransactions(BaseModel):
    """Top-level shape the model must return; its JSON schema goes to the model client."""
    transactions: List[AccountingTransaction]


class TransactionExtractionResult(BaseModel):
    success: bool
    count: int
    data: List[AccountingTransaction]
    warnings: List[str] = []
    model: Optional[str] = None
    raw_model_output: Optional[str] = None


class QwenAccountingExtractor:
    """Builds the prompt, calls the model through a client (OllamaClient or
    OpenAICompatibleClient) and validates each row.

    There is deliberately no heuristic fallback: when the model is unavailable the caller gets
    ModelUnavailable / ModelTimeout instead of plausible-looking wrong numbers."""

    def __init__(self, client, business_name: str = ""):
        self.client = client
        self.business_name = business_name

    def extract_accounting_data(self, text_input: Optional[str] = None,
                                images: Optional[List[str]] = None) -> TransactionExtractionResult:
        messages = [{"role": "system", "content": self._instructions()},
                    {"role": "user", "content": self._document(text_input, images)}]
        raw_output = self.client.chat_json(messages, ExtractedTransactions.model_json_schema(), images=images)
        return self._validate_and_parse_json(raw_output)

    def _instructions(self) -> str:
        """What to extract and how; the document itself travels in a separate message."""
        name = self.business_name
        whose = f"{name}, a UK business" if name else "a UK business"
        invoices = (f" An invoice issued by {name} is a sale (in); an invoice or receipt addressed to {name} "
                    "is a purchase (out)." if name else "")
        chart = "\n".join(f"{a.code} {a.name}" + (f" (e.g. {', '.join(a.aliases[:3])})" if a.aliases else "")
                          for a in choosable())
        return f"""You keep the books of {whose}. List each movement of money into or out of the business's bank account that the document records, as a JSON object with a "transactions" array.

Rules:
1. Direction: "in" is money the business received (sales, refunds from suppliers, loans received, capital paid in); "out" is money it paid (purchases, expenses, bills, wages, taxes to HMRC, refunds to customers, the owner's drawings, transfers to savings).{invoices}
2. A receipt or an invoice is ONE transaction: the total paid or charged, including VAT. Item lines, subtotals, discounts, cash tendered, change and card-payment lines are not transactions.
3. A bank statement or spreadsheet has one transaction per payment row. The amount is the money that moved, as a positive number; take the direction from the paid in / paid out columns or the sign. Balances and totals are not transactions.
4. Account: the best match from the chart of accounts below; "9998 Suspense" if none fits or you are unsure.
5. VAT: the VAT amount printed on the document for that transaction, or null when none is printed. Never calculate VAT.
6. Dates are UK format (DD/MM/YYYY): "03/09/2026" is 3 September 2026, written "2026-09-03". Use null when there is no date.
7. If there are no transactions, return {{"transactions": []}}.

Chart of accounts:
{chart}"""

    @staticmethod
    def _document(text_input: Optional[str], images: Optional[List[str]]) -> str:
        if images:
            return f"The document is attached as {len(images)} image(s)."
        return f"<document>\n{text_input or ''}\n</document>"
```

**`ledgersync/adapter.py`:**
- Replace the module docstring with:

```python
"""Temporary (until Phase 4): maps the model's rows onto ledger Transactions. The model picks the
account from the chart and says whether money went in or out; a negative amount still means
money out, whatever the model said."""
```

- Change the accounts import to `from .accounts import BY_CODE, SUSPENSE`.
- Replace `to_transactions` with:

```python
def to_transactions(rows: list[dict], source: str, settings: BusinessSettings) -> list[Transaction]:
    result = []
    for row in rows:
        amount = to_money(row.get("amount"))
        if not amount:
            continue
        issues = []
        said_in = row.get("direction") == "in"
        if said_in and amount < 0:
            issues.append(Issue(code="direction_conflict", severity="warning",
                                message="The model said money in but the amount was negative; recorded as money out."))
        code = str(row.get("account") or "")[:4]
        if code not in BY_CODE or code == settings.bank_account:
            issues.append(Issue(code="account_not_recognised", severity="warning",
                                message=f"'{row.get('account')}' is not an account to post to; "
                                        "it went to Suspense for review."))
            code = SUSPENSE
        elif code == SUSPENSE:
            issues.append(Issue(code="account_not_recognised", severity="warning",
                                message="The model was not sure which account this is; it went to Suspense for review."))
        vat = to_money(row.get("vat"))
        tx = Transaction(date=parse_date(row.get("date")), description=str(row.get("description") or ""),
                         direction=Direction.IN if said_in and amount > 0 else Direction.OUT,
                         gross=abs(amount), vat=abs(vat) if vat is not None and amount < 0 else vat,
                         account_code=code, currency=row.get("currency") or "GBP", source=source,
                         method="llm", issues=issues)
        result.append(normalise(tx, settings))
    return result
```

**`server.py`:**
- `extractor = QwenAccountingExtractor(ollama, business_name=settings.business_name)`.
- In `run_analysis`, `settings=BusinessSettings()` becomes `settings=BusinessSettings(business_name=settings.business_name)`.

- [ ] **Step 4: Run and confirm pass.** Run the Step 2 command, then `.venv/bin/python -m pytest -q`. Expected: all pass. `grep -rn "match_name\|account_guessed" ledgersync tests server.py qwen_service.py` prints nothing.
- [ ] **Step 5: Commit** — `git add qwen_service.py ledgersync/accounts.py ledgersync/adapter.py server.py tests && git commit -m "Prompt: whose books, receipt rules, accounts from the chart, VAT as printed"`

---

### Task 8: Measure the new prompt and demo it

- [ ] **Step 1: Start the API as the fixtures' business.**
  - Add the launch config `api-groq-eval` to `.claude/launch.json`, which is untracked. Use `runtimeExecutable` `/usr/bin/env` with `runtimeArgs`:
    - `LEDGERSYNC_BUSINESS_NAME=Northbridge Consulting Ltd`
    - `/Users/sandeepjala/ACTING OFFICE- WORK/AI-Accountant---Temporary/.venv/bin/python`
    - `/Users/sandeepjala/ACTING OFFICE- WORK/AI-Accountant---Temporary/server.py`

    and port 8085.
  - Start it and confirm that `/api/health` shows `qwen/qwen3.8-27b` available.

- [ ] **Step 2: Run the eval** in the background, and use `--resume` after `ai_rate_limited` cases:

```bash
.venv/bin/python eval/run_eval.py --label groq-prompt --note "Groq qwen/qwen3.8-27b on the free plan, reasoning_effort none, max_completion_tokens 4096; new prompt (whose books, receipt rules, chart enum, VAT as printed); LEDGERSYNC_BUSINESS_NAME=Northbridge Consulting Ltd; photos and scans sent as images"
```

- [ ] **Step 3: Compare.** Run:

```bash
.venv/bin/python - <<'EOF'
import glob, json
runs = {"phase3": "eval/results/2026-09-28-phase3.json",
        "groq-swap": sorted(glob.glob("eval/results/*-groq-swap.json"))[-1],
        "groq-prompt": sorted(glob.glob("eval/results/*-groq-prompt.json"))[-1]}
reports = {k: json.load(open(v)) for k, v in runs.items()}
print(f"{'':14}" + "".join(f"{k:>13}" for k in reports))
for label, get in [("correct rows", lambda r: r["summary"]["correct_rows"]),
                   ("precision", lambda r: r["summary"]["row_precision"]),
                   ("recall", lambda r: r["summary"]["row_recall"]),
                   ("account", lambda r: r["summary"]["field_accuracy"]["account"]),
                   ("vat", lambda r: r["summary"]["field_accuracy"]["vat"]),
                   ("image correct", lambda r: r["by_kind"]["image"]["correct_rows"]),
                   ("errors", lambda r: r["summary"]["errors"])]:
    print(f"{label:14}" + "".join(f"{str(get(r)):>13}" for r in reports.values()))
EOF
```

Expected (the spec's done-when): `groq-prompt` beats `phase3` on correct rows (above 0.415) and account accuracy (above 0.339). If it doesn't, report the numbers as they are; don't tune the prompt beyond this plan.

- [ ] **Step 4: Browser demo on Groq.**
  - Stop `api-groq-eval`. Start `api` and the UI: `web` on port 3000, or, if the user's own servers hold 8085/3000, `api-alt` and `web-alt` after an `API_URL=http://127.0.0.1:8086` build.
  - In the browser pane, do three things:
    - paste `BT Business Broadband monthly bill 72.00`;
    - upload `eval/fixtures/img-tesco-receipt/input.png`. If the pane has no file picker, inject its base64 into the file input through a `DataTransfer` and dispatch `change`;
    - make a manual entry: `Office chair`, 500, expense.
  - Expected:
    - each input gives rows with an account from the chart;
    - the receipt is one £12.50 money-out row, if the model follows rule 2 (report what it does);
    - the trial balance shows **✓ Trial Balance Balances**;
    - there are no console errors.

- [ ] **Step 5: README and commit.**
  - In `README.md` "Measuring accuracy", after the baseline paragraph, add this paragraph, with X, Y and Z taken from Step 3:

    > Two runs used Groq's hosted `qwen/qwen3.8-27b` on the free plan (thinking off, photos sent as images). The first, `groq-swap`, changed only the model and reached X correct rows. The second, `groq-prompt`, added the new prompt and reached Y correct rows with Z account accuracy. Phase 3's local model had 0.415 and 0.339.

  - Then run `git add eval/results/*-groq-prompt.json README.md && git commit -m "Record the Groq prompt accuracy run"`.
