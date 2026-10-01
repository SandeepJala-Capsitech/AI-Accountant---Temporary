import json
import logging
import urllib.error
from typing import Optional

import pytest
from fakes import FakeUrlopen, chat_answer, http_error
from pydantic import BaseModel, Field

from ledgersync.config import Settings
from ledgersync.errors import ModelError, ModelRateLimited, ModelTimeout, ModelUnavailable
from ledgersync.groq_client import GroqClient, strict_schema

KEY = "sk-test-1234"
SETTINGS = Settings(groq_api_key=KEY)
MODELS = {"object": "list", "data": [{"id": "qwen/qwen3.8-27b", "object": "model", "active": True}]}
MSG = [{"role": "system", "content": "rules"}, {"role": "user", "content": "doc"}]
SCHEMA = {"type": "object", "properties": {"transactions": {"type": "array", "items": {"type": "object"}}}}


def make_client(*outcomes, settings=SETTINGS):
    http, now, slept = FakeUrlopen(*outcomes), [0.0], []
    client = GroqClient(settings, opener=http, clock=lambda: now[0], sleep=slept.append)
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
    client, http, _, _ = make_client(chat_answer(), settings=Settings(groq_api_key=KEY, groq_reasoning_effort=""))
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


def test_the_client_sends_one_page_per_request_by_default():
    # Three pages are about 7.2K tokens before any answer: over the free plan's 8K tokens a minute.
    client, _, _, _ = make_client()
    assert client.max_images == 1


def test_health_needs_a_key():
    client, http, _, _ = make_client(settings=Settings())
    health = client.health()
    assert not health.model_available and "GROQ_API_KEY" in health.error and http.requests == []


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
    # Settings are read once at startup, so the fix needs a restart.
    client, _, _, _ = make_client(settings=Settings())
    with pytest.raises(ModelUnavailable, match="GROQ_API_KEY in .env, then restart the API"):
        client.ensure_available()


@pytest.mark.parametrize("status, error, code, words", [
    (401, ModelUnavailable, "ai_offline", "Groq rejected the API key"),
    (403, ModelUnavailable, "ai_offline", "Groq rejected the API key"),
    (404, ModelUnavailable, "ai_offline", "Groq does not offer the model 'qwen/qwen3.8-27b'"),
    (413, ModelError, "ai_input_too_long", "too large for Groq"),
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
    assert info.value.code == "ai_offline" and "Cannot reach Groq" in info.value.message


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


def test_a_failed_check_is_tried_again_soon_so_the_app_recovers_with_the_network():
    # Only successful checks are kept for five minutes; a failure must not lock the app out that long.
    client, http, now, _ = make_client(urllib.error.URLError(ConnectionRefusedError()), MODELS)
    assert not client.health().model_available
    now[0] += 20
    client.ensure_available()
    assert len(http.requests) == 2


def test_a_daily_limit_says_when_it_resets_instead_of_a_minute():
    # Groq's free plan also caps tokens per day; its Retry-After can be many minutes.
    client, _, _, slept = make_client(http_error(429, "Rate limit reached on tokens per day (TPD). "
                                                      "Please try again in 16m27s.", retry_after=987))
    with pytest.raises(ModelRateLimited) as info:
        client.chat_json(MSG, SCHEMA)
    assert slept == [] and "about 17 minutes" in info.value.message and "in a minute" not in info.value.message


def test_the_key_is_blanked_before_a_long_error_is_shortened():
    # Shortening first could cut a real-length key in half and leave most of it in the message.
    key = "gsk_" + "a1b2" * 13
    client, _, _, _ = make_client(http_error(400, "x" * 270 + key), settings=Settings(groq_api_key=key))
    with pytest.raises(ModelError) as info:
        client.chat_json(MSG, SCHEMA)
    assert key[:20] not in info.value.message
