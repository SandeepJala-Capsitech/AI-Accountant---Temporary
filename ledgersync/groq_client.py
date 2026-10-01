"""Client for Groq's hosted vision model, through Groq's OpenAI-compatible chat API. The API key
travels only in the Authorization header: it is never logged or put into an error message."""
from __future__ import annotations

import http.client
import json
import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .config import Settings
from .errors import ModelError, ModelRateLimited, ModelTimeout, ModelUnavailable

logger = logging.getLogger(__name__)

USER_AGENT = "LedgerSync/0.3"   # urllib's default agent is refused by some API gateways
_STRICT_KEYWORDS = {"type", "properties", "required", "items", "enum", "anyOf", "description"}
RATE_LIMIT_WAIT = 60.0      # most seconds one call waits out 429s before giving up
DEFAULT_RETRY_AFTER = 10.0  # when a 429 has no Retry-After header
RECHECK_FAILURE = 15.0      # a failed health check is tried again this soon (successes: settings.health_ttl)


@dataclass(frozen=True)
class ModelHealth:
    reachable: bool
    model_available: bool
    error: Optional[str] = None


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


class GroqClient:
    def __init__(self, settings: Settings, opener: Callable = urllib.request.urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._s = settings
        self._open = opener
        self._clock = clock
        self._sleep = sleep
        self._health: Optional[ModelHealth] = None
        self._health_at = 0.0

    @property
    def model(self) -> str:
        return self._s.groq_model

    @property
    def max_images(self) -> int:
        return self._s.groq_max_images

    def health(self, force: bool = False) -> ModelHealth:
        now = self._clock()
        ttl = self._s.health_ttl if self._health is not None and self._health.model_available else RECHECK_FAILURE
        if not force and self._health is not None and now - self._health_at < ttl:
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
            "max_completion_tokens": self._s.groq_max_output_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "transactions", "schema": strict_schema(schema), "strict": True}},
        }
        if self._s.groq_reasoning_effort:
            payload["reasoning_effort"] = self._s.groq_reasoning_effort
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
            logger.warning("Groq check failed (%s)", type(exc).__name__)

    def _check(self) -> ModelHealth:
        if not self._s.groq_api_key:
            return ModelHealth(False, False, "Set GROQ_API_KEY in .env, then restart the API.")
        try:
            listing = self._request("GET", "/models", None, timeout=10)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:   # rate limited: the key works, and the next chat call waits it out
                return self._health or ModelHealth(True, True)
            return ModelHealth(True, False, self._http_error(exc.code, self._detail(exc)).message)
        except Exception as exc:
            logger.info("Groq not reachable (%s)", type(exc).__name__)
            return ModelHealth(False, False, self._unreachable_message())
        offered = {m.get("id") for m in listing.get("data", []) if m.get("active", True)}
        if self.model in offered:
            return ModelHealth(True, True)
        return ModelHealth(True, False, self._missing_model_message())

    def _chat(self, payload: dict) -> dict:
        """POST /chat/completions. Waits out rate limits (up to RATE_LIMIT_WAIT seconds), retries a
        dropped connection or a server error once, and turns every other failure into a typed
        error that names neither the document nor the key."""
        waited, retried = 0.0, False
        while True:
            try:
                return self._request("POST", "/chat/completions", payload, timeout=self._s.groq_timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = self._retry_after(exc)
                    if waited + wait > RATE_LIMIT_WAIT:
                        raise ModelRateLimited(self._rate_limit_message(wait, self._detail(exc))) from None
                    logger.info("Groq rate limit reached; waiting %.0f s", wait)
                    self._sleep(wait)
                    waited += wait
                elif exc.code >= 500 and not retried:
                    logger.warning("Groq returned HTTP %d; retrying once", exc.code)
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
                logger.warning("Lost the connection to Groq; retrying once")
                retried = True
                self._sleep(1.0)
            except json.JSONDecodeError:
                raise ModelError("Groq returned a response that is not JSON.",
                                 code="ai_output_invalid") from None

    def _http_error(self, status: int, detail: str) -> Exception:
        if status in (401, 403):
            self._health = None
            return ModelUnavailable("Groq rejected the API key. Check GROQ_API_KEY in .env, then restart the API.")
        if status == 404:
            self._health = None
            return ModelUnavailable(self._missing_model_message())
        lowered = detail.lower()
        if status == 413 or (status == 400 and ("context" in lowered or "reduce the length" in lowered)):
            return ModelError("This document is too large for Groq's limits on the current plan. "
                              "Split it into smaller files and try again.", code="ai_input_too_long")
        return ModelError(f"Groq returned HTTP {status}: {detail}")

    def _detail(self, exc: urllib.error.HTTPError) -> str:
        """The provider's own error message, shortened, with the key blanked in case it is echoed."""
        raw = exc.read().decode("utf-8", "replace")
        try:
            message = str(json.loads(raw)["error"]["message"])
        except (ValueError, KeyError, TypeError):
            message = raw
        if self._s.groq_api_key:   # blank first: shortening first could cut the key in half
            message = message.replace(self._s.groq_api_key, "***")
        return message[:300]

    @staticmethod
    def _rate_limit_message(wait: float, detail: str) -> str:
        """When to try again, from Retry-After: a free plan's daily limit can be minutes or hours away."""
        minutes = max(1, math.ceil(wait / 60))
        when = ("about a minute" if minutes == 1 else f"about {minutes} minutes" if minutes < 90
                else f"about {round(minutes / 60)} hours")
        return f"Groq's rate limit is used up; try again in {when}. ({detail})"

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float:
        try:
            return max(1.0, float(exc.headers.get("retry-after")))
        except (AttributeError, TypeError, ValueError):
            return DEFAULT_RETRY_AFTER

    def _timeout_message(self) -> str:
        return (f"Groq did not answer within {self._s.groq_timeout:.0f} seconds. "
                "Try again, or raise GROQ_TIMEOUT.")

    def _request(self, method: str, path: str, payload: Optional[dict], timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self._s.groq_base_url}{path}", data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._s.groq_api_key}",
            "User-Agent": USER_AGENT,
        })
        with self._open(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _unreachable_message(self) -> str:
        return f"Cannot reach Groq at {self._s.groq_base_url}. Check the internet connection, then try again."

    def _missing_model_message(self) -> str:
        return f"Groq does not offer the model '{self.model}'. Check GROQ_MODEL in .env, then restart the API."
