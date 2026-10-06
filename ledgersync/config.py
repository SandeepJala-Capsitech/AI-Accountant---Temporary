"""Runtime settings, read once from environment variables and the project's .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"   # git-ignored; see .env.example


def _flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8085
    reload: bool = False
    warmup: bool = True
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")
    max_upload_mb: int = 20
    max_pdf_pages: int = 30
    job_ttl_seconds: float = 3600.0
    max_parallel_jobs: int = 2            # documents read at once, from any input; 1 reads them one at a time
    log_level: str = "INFO"
    # Groq's hosted vision model, through its OpenAI-compatible API. The key lives in .env.
    groq_api_key: str = field(default="", repr=False)   # never printed or logged
    groq_model: str = "qwen/qwen3.8-27b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout: float = 60.0
    # Room for long statements: a 60-row statement's answer, with document_total, document_vat and
    # mixed_items on every row, is over 4K tokens.
    groq_max_output_tokens: int = 8192
    groq_reasoning_effort: str = "high"   # thinking keeps the model to the receipt rules; "none" is ~10x faster than "low"
    groq_max_images: int = 3              # pages per request, Groq's most; set 1 on the free plan: 3 overflow its 8K tokens/min
    health_ttl: float = 300.0             # the UI polls health every 30 s; the free plan counts requests
    business_name: str = ""               # whose books these are: tells sales invoices from purchases

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None,
                 env_file: Optional[Path] = ENV_FILE) -> "Settings":
        """Settings from `env`, or else from the process environment laid over the .env file."""
        env = {**read_env_file(env_file), **os.environ} if env is None else env
        d = cls()
        origins = env.get("LEDGERSYNC_CORS_ORIGINS")
        return cls(
            host=env.get("LEDGERSYNC_HOST", d.host),
            port=int(env.get("LEDGERSYNC_PORT", d.port)),
            reload=_flag(env.get("LEDGERSYNC_RELOAD", "0")),
            warmup=_flag(env.get("LEDGERSYNC_WARMUP", "1")),
            cors_origins=(tuple(o.strip() for o in origins.split(",") if o.strip())
                          if origins else d.cors_origins),
            max_upload_mb=int(env.get("LEDGERSYNC_MAX_UPLOAD_MB", d.max_upload_mb)),
            max_pdf_pages=int(env.get("LEDGERSYNC_MAX_PDF_PAGES", d.max_pdf_pages)),
            max_parallel_jobs=max(1, int(env.get("LEDGERSYNC_MAX_PARALLEL_JOBS", d.max_parallel_jobs))),
            log_level=env.get("LEDGERSYNC_LOG_LEVEL", d.log_level).upper(),
            groq_api_key=env.get("GROQ_API_KEY", "").strip(),
            groq_model=env.get("GROQ_MODEL", d.groq_model).strip(),
            groq_base_url=env.get("GROQ_BASE_URL", d.groq_base_url).strip().rstrip("/"),
            groq_timeout=float(env.get("GROQ_TIMEOUT", d.groq_timeout)),
            groq_max_output_tokens=int(env.get("GROQ_MAX_OUTPUT_TOKENS", d.groq_max_output_tokens)),
            groq_reasoning_effort=env.get("GROQ_REASONING_EFFORT", d.groq_reasoning_effort).strip(),
            groq_max_images=int(env.get("GROQ_MAX_IMAGES", d.groq_max_images)),
            business_name=env.get("LEDGERSYNC_BUSINESS_NAME", "").strip(),
        )
