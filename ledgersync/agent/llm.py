"""The chat model the agent runs on: LEDGERSYNC_AGENT_MODEL, any OpenRouter model that supports tool calling,
through OpenRouter's OpenAI-compatible API."""
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from ledgersync.config import Settings
from ledgersync.errors import AgentNotConfigured

NOT_CONFIGURED = "The agent needs LEDGERSYNC_AGENT_MODEL and OPENROUTER_API_KEY. Set both in .env."


def chat_model(settings: Settings) -> BaseChatModel:
    """The agent's model on OpenRouter, at temperature 0 so the same rows get the same answers as far as the model
    allows. Raises AgentNotConfigured when the model or the OpenRouter key isn't set. Building it calls nothing."""
    if not settings.agent_model or not settings.openrouter_api_key:
        raise AgentNotConfigured(NOT_CONFIGURED)
    return ChatOpenAI(model=settings.agent_model, base_url=settings.openrouter_base_url,
                      api_key=settings.openrouter_api_key, temperature=0)
