"""The chat model the accountant agent runs on: LEDGERSYNC_AGENT_MODEL (openai/gpt-oss-120b) through OpenRouter, which
is told to use only hosts that take every parameter sent and keep none of the data."""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openrouter import ChatOpenRouter

from ledgersync.config import Settings
from ledgersync.errors import ModelUnavailable

NOT_CONFIGURED = "The accountant chat needs OPENROUTER_API_KEY. Set it in .env."


def chat_model(settings: Settings) -> BaseChatModel:
    """The agent's model, reasoning at medium effort. Each request may take settings.agent_timeout seconds
    (ChatOpenRouter takes milliseconds); after a 5XX answer or a lost connection the OpenRouter SDK tries again with
    backoff, for up to about five minutes (max_retries=2 gives 150 seconds a retry). Building it calls nothing;
    ModelUnavailable when OPENROUTER_API_KEY isn't set."""
    if not settings.openrouter_api_key:
        raise ModelUnavailable(NOT_CONFIGURED)
    return ChatOpenRouter(model=settings.agent_model, api_key=settings.openrouter_api_key,
                          base_url=settings.openrouter_base_url, reasoning={"effort": "medium"},
                          openrouter_provider={"require_parameters": True, "data_collection": "deny"},
                          timeout=round(settings.agent_timeout * 1000), max_retries=2)
