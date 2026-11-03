"""The chat model the accountant agent runs on: OPENROUTER_MODEL (qwen/qwen3.8-27b), the one model that also reads the
uploaded files, through OpenRouter on the same route as the reader (model_client.openrouter_route: the hosts in
OPENROUTER_PROVIDERS first, and only hosts at a precision in OPENROUTER_QUANTIZATIONS, never a 4-bit one), told besides
to use only hosts that take every parameter sent and keep none of the data."""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openrouter import ChatOpenRouter

from ledgersync.config import Settings
from ledgersync.errors import ModelUnavailable
from ledgersync.model_client import openrouter_route

NOT_CONFIGURED = "The accountant chat needs OPENROUTER_API_KEY. Set it in .env."


def chat_model(settings: Settings) -> BaseChatModel:
    """The agent's model, settings.openrouter_model (the one that reads the files), on the reader's route
    (openrouter_route, with require_parameters) and data_collection denied, reasoning at medium effort. Each request
    may take settings.agent_timeout seconds (ChatOpenRouter takes milliseconds); after a 5XX answer or a lost
    connection the OpenRouter SDK tries again with backoff, for up to about five minutes (max_retries=2 gives 150
    seconds a retry). Building it calls nothing; ModelUnavailable when OPENROUTER_API_KEY isn't set."""
    if not settings.openrouter_api_key:
        raise ModelUnavailable(NOT_CONFIGURED)
    return ChatOpenRouter(model=settings.openrouter_model, api_key=settings.openrouter_api_key,
                          base_url=settings.openrouter_base_url, reasoning={"effort": "medium"},
                          openrouter_provider={**openrouter_route(settings, require_parameters=True),
                                               "data_collection": "deny"},
                          timeout=round(settings.agent_timeout * 1000), max_retries=2)
