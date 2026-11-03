"""The agent eval's transcript (eval/run_agent_eval.py --transcripts): a message of the agent's history as JSON, the
thread's messages read from a checkpointer, and the file written for a run. No database: the checkpointer is a stand-in."""
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pymongo.errors import ServerSelectionTimeoutError

import run_agent_eval as agent_eval


class Saver:
    """Gives get_tuple the way a checkpointer does: the latest checkpoint of the thread asked for."""

    def __init__(self, messages=None, *, channels=None, missing=False, error=None):
        self.asked, self.missing, self.error = [], missing, error
        self.channels = {"messages": messages} if channels is None else channels

    def get_tuple(self, config):
        self.asked.append(config)
        if self.error:
            raise self.error
        return None if self.missing else SimpleNamespace(checkpoint={"channel_values": self.channels})


def test_an_ai_message_keeps_its_text_and_its_tool_calls():
    message = AIMessage(content="Checking the bank.",
                        tool_calls=[{"name": "get_overview", "args": {"year": "2026"}, "id": "call_1",
                                     "type": "tool_call"}],
                        response_metadata={"finish_reason": "tool_calls"})
    assert agent_eval.message_json(message) == {
        "type": "ai", "content": "Checking the bank.", "finish_reason": "tool_calls",
        "tool_calls": [{"name": "get_overview", "args": {"year": "2026"}, "id": "call_1"}]}


def test_an_ai_message_keeps_the_provider_and_model_that_answered():
    metadata = {"finish_reason": "stop", "provider": "DeepInfra", "model_name": "qwen/qwen3.8-27b",
                "model_provider": "openrouter", "id": "gen-1"}   # model_provider is LangChain's, not the host
    out = agent_eval.message_json(AIMessage(content="Done.", response_metadata=metadata))
    assert out["served_by"] == {"provider": "DeepInfra", "model_name": "qwen/qwen3.8-27b"}
    assert out["finish_reason"] == "stop"


def test_served_by_holds_what_the_metadata_has_and_is_left_out_when_it_has_nothing():
    # langchain-openrouter 0.2.9 names the model but not the host: the SDK drops OpenRouter's `provider` field.
    only_model = AIMessage(content="Done.", response_metadata={"model_name": "qwen/qwen3.8-27b",
                                                               "model_provider": "openrouter"})
    assert agent_eval.message_json(only_model)["served_by"] == {"model_name": "qwen/qwen3.8-27b"}
    nothing = ({}, {"finish_reason": "stop", "model_provider": "openrouter"}, {"provider": None, "model_name": ""})
    for metadata in nothing:
        assert "served_by" not in agent_eval.message_json(AIMessage(content="Done.", response_metadata=metadata))


def test_only_an_ai_message_says_who_served_it():
    metadata = {"provider": "DeepInfra", "model_name": "qwen/qwen3.8-27b"}
    for message in (HumanMessage("Hi", response_metadata=metadata),
                    ToolMessage(content="ok", tool_call_id="call_1", response_metadata=metadata)):
        assert "served_by" not in agent_eval.message_json(message)


def test_an_ai_message_with_no_tool_calls_has_an_empty_list():
    assert agent_eval.message_json(AIMessage(content="Done."))["tool_calls"] == []


def test_a_tool_message_keeps_its_status_and_all_of_its_result():
    result = "row 7 " * 5000
    out = agent_eval.message_json(ToolMessage(content=result, name="resolve", tool_call_id="call_1", status="error"))
    assert out == {"type": "tool", "content": result, "name": "resolve", "tool_call_id": "call_1", "status": "error"}
    assert len(out["content"]) == 30000


def test_human_and_system_messages_are_their_text():
    assert agent_eval.message_json(HumanMessage("Review these documents")) == {
        "type": "human", "content": "Review these documents"}
    assert agent_eval.message_json(SystemMessage("You are the accountant."))["type"] == "system"


def test_list_content_is_joined_and_its_reasoning_kept_and_labelled():
    message = AIMessage(content=[{"type": "reasoning", "reasoning": "The bank line comes first."},
                                 {"type": "text", "text": "Checking "}, {"type": "text", "text": "the bank."},
                                 {"type": "thinking", "thinking": "Then the receipt."}],
                        additional_kwargs={"reasoning_content": "Provider's own note.",
                                           "reasoning_details": [{"type": "reasoning.text", "text": "step"}]})
    out = agent_eval.message_json(message)
    assert out["content"] == "Checking the bank."
    assert out["reasoning"] == [
        {"from": "content block (reasoning)", "text": "The bank line comes first."},
        {"from": "content block (thinking)", "text": "Then the receipt."},
        {"from": "additional_kwargs.reasoning_content", "text": "Provider's own note."},
        {"from": "additional_kwargs.reasoning_details", "details": [{"type": "reasoning.text", "text": "step"}]}]


def test_a_garbled_tool_call_is_kept():
    message = AIMessage(content="", invalid_tool_calls=[{"name": "resolve", "args": "{not json", "id": "call_2",
                                                         "error": "bad json", "type": "invalid_tool_call"}])
    assert agent_eval.message_json(message)["invalid_tool_calls"][0]["args"] == "{not json"


def test_thread_messages_reads_the_latest_checkpoint_of_the_thread():
    saver = Saver([HumanMessage("Review these documents"), AIMessage(content="Done.")])
    assert [m["type"] for m in agent_eval.thread_messages(saver, "t-1")] == ["human", "ai"]
    assert saver.asked == [{"configurable": {"thread_id": "t-1"}}]


@pytest.mark.parametrize("saver", [Saver(missing=True), Saver(channels={}), Saver(channels={"messages": None})])
def test_a_thread_with_no_checkpoint_or_no_messages_has_none(saver):
    assert agent_eval.thread_messages(saver, "t-1") == []


def test_write_transcript_saves_messages_events_answers_and_the_result(tmp_path):
    trace = {"thread_id": "t-1", "client_id": 4, "events": [("status", {"text": "Working on it…"}),
                                                           ("token", {"text": "Hi"}), ("done", {})],
             "answers": [{"question": {"kind": "date"}, "route": "POST /x/answer", "sent": {"kind": "date"},
                          "status": 200, "body": {"ok": True}}]}
    saver = Saver([HumanMessage("Review these documents"), AIMessage(content="Done.")])
    path = agent_eval.write_transcript(tmp_path / "runs", "demo", 2, saver=saver, trace=trace, result={"case": "x"})

    assert path == tmp_path / "runs" / "demo-r2.json"
    saved = json.loads(path.read_text())
    assert saved["thread_id"] == "t-1" and saved["client_id"] == 4 and saved["run"] == 2
    assert [m["type"] for m in saved["messages"]] == ["human", "ai"]
    assert saved["events"][1] == ["token", {"text": "Hi"}] and len(saved["events"]) == 3
    assert saved["answers"] == trace["answers"] and saved["result"] == {"case": "x"}
    assert "error" not in saved and "messages_error" not in saved


def test_a_checkpoint_that_cant_be_read_is_recorded_without_the_cluster_address(tmp_path):
    saver = Saver(error=ServerSelectionTimeoutError("mongodb+srv://user:secret@cluster0.example.net timed out"))
    path = agent_eval.write_transcript(tmp_path, "clean-month", 1, saver=saver, trace={"thread_id": "t-1"},
                                       result=None, error="RuntimeError: the message was refused")
    saved = json.loads(path.read_text())
    assert saved["messages"] == [] and saved["messages_error"] == "ServerSelectionTimeoutError"
    assert saved["error"] == "RuntimeError: the message was refused" and saved["result"] is None
    assert "secret" not in path.read_text() and "cluster0" not in path.read_text()


def test_no_transcripts_by_default():
    parser = agent_eval.build_parser()
    assert parser.parse_args([]).transcripts is None
    assert str(parser.parse_args(["--transcripts", "runs"]).transcripts) == "runs"
