"""The accountant agent's own middleware. A tool error goes back to the model as a message, so the run carries on; a
model call that still fails after its retries says the model is unavailable; and a chat asked to stop stops at its
next model call."""
from __future__ import annotations

import logging
from typing import Callable

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from pydantic import ValidationError

from ledgersync.errors import LedgerSyncError, ModelUnavailable

logger = logging.getLogger(__name__)

STOPPED = "I stopped there, as you asked."


def _error(call: dict, text: str) -> ToolMessage:
    return ToolMessage(content=f"Error: {text}", name=call["name"], tool_call_id=call["id"], status="error")


def _problems(exc: ValidationError) -> str:
    """Each problem as "where: what", the way the model can correct it."""
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())


class ToolErrorsAsMessages(AgentMiddleware):
    """Turns a call to a tool that doesn't exist, arguments that don't fit a tool (ValidationError) and a refusal by
    the services (LedgerSyncError) into an error ToolMessage, so the model reads what went wrong and the run carries
    on. (LangChain's tool node answers arguments that don't fit the tool's schema with a message of its own before
    this sees them.) Any other exception is a fault, logged, and answered the same way without its details: a run
    that stopped there would leave the tool call unanswered in the thread, and the model refuses a thread like that,
    so every later message in the chat would fail."""

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if request.tool is None:
            return _error(call, f"There is no tool called {call['name']}. Use one of the tools you were given.")
        try:
            return handler(request)
        except ValidationError as exc:
            return _error(call, f"The arguments don't fit {call['name']}: {_problems(exc)}")
        except LedgerSyncError as exc:
            return _error(call, exc.message)
        except GraphBubbleUp:
            raise
        except Exception:   # noqa: BLE001 - a fault, answered so the thread stays usable
            logger.exception("The agent's tool %s failed", call["name"])
            return _error(call, f"{call['name']} couldn't finish because of a fault on the server. Try it once more; "
                                "if it fails again, tell the person.")


class ModelErrorsAsUnavailable(AgentMiddleware):
    """Outside the retries: a model call that still fails raises ModelUnavailable with what went wrong, so the turn
    ends saying the model isn't available."""

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except (GraphBubbleUp, LedgerSyncError):
            raise
        except Exception as exc:   # noqa: BLE001 - whatever the provider or its client raised
            raise ModelUnavailable(str(exc).strip()[:300] or type(exc).__name__) from exc


class StopWhenCancelled(AgentMiddleware):
    """Before each model call, ends the run once is_cancelled(thread_id) says the chat was asked to stop, with a
    message saying so. The tool calls already made have their results, so the thread can carry on later."""

    def __init__(self, is_cancelled: Callable[[str], bool]):
        super().__init__()
        self.is_cancelled = is_cancelled

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime) -> dict | None:
        if runtime.context is not None and self.is_cancelled(runtime.context.thread_id):
            return {"jump_to": "end", "messages": [AIMessage(content=STOPPED)]}
        return None
