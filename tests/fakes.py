"""Test doubles shared by the test modules."""
import email.message
import io
import json
import urllib.error

from ledgersync.errors import ModelUnavailable
from ledgersync.groq_client import ModelHealth

OFFLINE = "Cannot reach Groq at https://api.groq.com/openai/v1. Check the internet connection, then try again."

ROW = {"description": "BT Business Broadband", "date": "2026-09-01", "amount": 72.0, "direction": "out",
       "account": "7502 Telephone and Internet", "vat": None, "currency": "GBP"}


def transactions_json(*rows: dict) -> str:
    return json.dumps({"transactions": list(rows)})


class FakeModel:
    """Stands in for GroqClient: chat_json returns (or raises) `replies` in order."""

    model = "fake-model"
    max_images = 3

    def __init__(self, replies=(), healthy: bool = True):
        self.replies = list(replies)
        self.healthy = healthy
        self.calls: list[dict] = []

    def health(self, force: bool = False) -> ModelHealth:
        return ModelHealth(True, True) if self.healthy else ModelHealth(False, False, OFFLINE)

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
