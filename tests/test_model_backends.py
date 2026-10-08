"""Model boundary contracts; every HTTP interaction uses MockTransport."""

import json
from pathlib import Path

import httpx
import pytest

from ailab_ops.config import Settings
from ailab_ops.llm import ChatMessage, FinishReason, ToolCall, ToolSpec
from ailab_ops.models import (
    ModelBackendError, ModelConfigurationError, OpenAIModelGateway,
    ReplayMissError, ReplayModelGateway, build_model_gateway,
)


REPLAY = Path(__file__).resolve().parents[1] / "data/v2/replays/gpu-assert.jsonl"
TOOLS = [ToolSpec("get_case_snapshot", "Read case", {"type": "object"})]
MESSAGES = [ChatMessage("user", "Diagnose case-gpu-assert.")]


def completion(content="ok"):
    return {"model": "test", "choices": [{"message": {"role": "assistant", "content": content},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


def gateway(handler, **kwargs):
    return OpenAIModelGateway(base_url="http://model.invalid/v1", model="test", api_key="secret-key",
                              transport=httpx.MockTransport(handler), backoff_base_s=0, **kwargs)


def test_openai_request_shape_and_json_response():
    def handler(request):
        assert str(request.url) == "http://model.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret-key"
        body = json.loads(request.content)
        assert body["model"] == "test"
        assert body["messages"] == [{"role": "user", "content": "Diagnose case-gpu-assert."}]
        assert body["tools"] == [{"type": "function", "function": {
            "name": "get_case_snapshot", "description": "Read case", "parameters": {"type": "object"}}}]
        assert body["tool_choice"] == "auto"
        assert body["max_tokens"] == 321
        assert body["stream"] is True
        return httpx.Response(200, json=completion())

    with gateway(handler) as model:
        response = model.complete(MESSAGES, TOOLS, max_tokens=321)
    assert response.content == "ok"
    assert response.usage.tokens_in == 11
    assert response.usage.tokens_out == 7


def test_openai_accumulates_fragmented_tool_arguments_and_usage():
    frames = [
        {"model": "test", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
         "id": "call-1", "function": {"name": "get_case_snapshot", "arguments": '{"case_'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
         "function": {"arguments": 'id":"case-gpu-assert"}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 13, "completion_tokens": 9}},
    ]
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    with gateway(lambda request: httpx.Response(200, text=body,
                 headers={"content-type": "text/event-stream"})) as model:
        response = model.complete(MESSAGES, TOOLS, max_tokens=100)
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].name == "get_case_snapshot"
    assert response.tool_calls[0].arguments == {"case_id": "case-gpu-assert"}
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert (response.usage.tokens_in, response.usage.tokens_out) == (13, 9)


@pytest.mark.parametrize("exc_type,kind,phase", [
    (httpx.ConnectTimeout, "timeout", "connect"),
    (httpx.ConnectError, "transport", "connect"),
    (httpx.ReadTimeout, "timeout", "read"),
    (httpx.ReadError, "transport", "read"),
])
def test_transport_failures_are_typed_and_do_not_leak_credentials(exc_type, kind, phase):
    def handler(request):
        raise exc_type("Authorization: Bearer secret-key", request=request)
    with gateway(handler, max_retries=0) as model:
        with pytest.raises(ModelBackendError) as error:
            model.complete(MESSAGES, max_tokens=100)
    assert error.value.kind == kind
    assert error.value.phase == phase
    assert error.value.retryable is True
    assert "secret-key" not in str(error.value)
    assert "Authorization" not in str(error.value)


def test_retryable_status_recovers_but_bad_request_does_not_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "unavailable"}) if len(calls) == 1 else httpx.Response(200, json=completion())
    with gateway(handler) as model:
        assert model.complete(MESSAGES, max_tokens=100).content == "ok"
    assert len(calls) == 2
    calls.clear()
    def bad(request):
        calls.append(request)
        return httpx.Response(400, json={"error": "invalid Authorization: secret-key"})
    with gateway(bad) as model:
        with pytest.raises(ModelBackendError) as error:
            model.complete(MESSAGES, max_tokens=100)
    assert len(calls) == 1
    assert error.value.kind == "bad_request"
    assert error.value.status == 400
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize("body,content_type", [("<html>no</html>", "application/json"),
    ("data: [DONE]\n\n", "text/event-stream")])
def test_invalid_or_empty_backend_response_is_protocol_error(body, content_type):
    with gateway(lambda request: httpx.Response(200, text=body, headers={"content-type": content_type})) as model:
        with pytest.raises(ModelBackendError) as error:
            model.complete(MESSAGES, max_tokens=100)
    assert error.value.kind == "protocol"


def test_injected_client_is_owned_by_caller():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion())))
    with OpenAIModelGateway(base_url="http://model.invalid/v1", model="test", client=client) as model:
        assert model.complete(MESSAGES, max_tokens=100).content == "ok"
    assert not client.is_closed
    client.close()


def test_read_failure_after_stream_start_is_typed():
    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadTimeout("secret-key")
    with gateway(lambda request: httpx.Response(200, stream=InterruptedStream(),
                 headers={"content-type": "text/event-stream"}), max_retries=0) as model:
        with pytest.raises(ModelBackendError) as error:
            model.complete(MESSAGES, max_tokens=100)
    assert error.value.kind == "timeout"
    assert error.value.phase == "read"
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize("tail", ["", "data: [DONE]\n\n",
    'data: {"error":{"message":"secret-key","type":"server_error"}}\n\n',
    'event: error\ndata: {"message":"secret-key"}\n\n',
    'data: {"choices":[{"delta":{},"finish_reason":"unknown"}]}\n\n'])
def test_sse_incomplete_or_error_stream_refuses_partial_success(tail):
    body = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n' + tail
    with gateway(lambda request: httpx.Response(200, text=body,
                 headers={"content-type": "text/event-stream"})) as model:
        with pytest.raises(ModelBackendError) as error:
            model.complete(MESSAGES, max_tokens=100)
    assert error.value.kind == "protocol"
    assert error.value.phase == "stream"
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize("tail", ["", "data: [DONE]\n\n"])
def test_sse_valid_terminal_finish_reason_returns_complete_content(tail):
    body = ('data: {"choices":[{"delta":{"content":"complete"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n') + tail
    with gateway(lambda request: httpx.Response(200, text=body,
                 headers={"content-type": "text/event-stream"})) as model:
        response = model.complete(MESSAGES, max_tokens=100)
    assert response.content == "complete"
    assert response.finish_reason == FinishReason.STOP


def test_replays_recorded_scenario_without_inventing_responses():
    model = ReplayModelGateway(REPLAY)
    response = model.complete(MESSAGES, TOOLS, max_tokens=100)
    assert model.mode == "replay"
    assert response.tool_calls[0].name == "get_case_snapshot"
    assert response.tool_calls[0].arguments == {"case_id": "case-gpu-assert"}
    response.tool_calls[0].arguments["case_id"] = "mutated"
    assert model.complete(MESSAGES, TOOLS, max_tokens=100).tool_calls[0].arguments == {"case_id": "case-gpu-assert"}
    with pytest.raises(ReplayMissError):
        model.complete([ChatMessage("user", "Diagnose another-case.")], TOOLS, max_tokens=100)
    with pytest.raises(ReplayMissError):
        model.complete(MESSAGES, [], max_tokens=100)


def test_replay_canonicalizes_arguments_and_tool_order_but_keeps_history_strict(tmp_path):
    record = {"messages": [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": "read", "arguments": '{"a":1,"b":2}'}}]},
        {"role": "tool", "content": "result", "name": "read", "tool_call_id": "call-1"}],
        "available_tool_names": ["write", "read"], "response": {"content": "recorded", "model": "recording"}}
    path = tmp_path / "replay.jsonl"
    path.write_text(json.dumps(record) + "\n")
    model = ReplayModelGateway(path)
    messages = [ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read", {"b": 2, "a": 1})]),
                ChatMessage("tool", "result", name="read", tool_call_id="call-1")]
    tools = [ToolSpec("read", "", {}), ToolSpec("write", "", {})]
    assert model.complete(messages, tools, max_tokens=100).content == "recorded"
    for changed in [list(reversed(messages)), [messages[0], ChatMessage("tool", "different", name="read", tool_call_id="call-1")],
                    [messages[0], ChatMessage("tool", "result", name="other", tool_call_id="call-1")],
                    [messages[0], ChatMessage("tool", "result", name="read", tool_call_id="call-other")],
                    [ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read", {"a": 9, "b": 2})]), messages[1]]]:
        with pytest.raises(ReplayMissError):
            model.complete(changed, tools, max_tokens=100)
    with pytest.raises(ReplayMissError):
        model.complete([ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read", raw_arguments="{bad")]),
                        messages[1]], tools, max_tokens=100)


@pytest.mark.parametrize("recorded_call", [
    {"id": "call-1", "name": "read", "arguments": {"stale": True}, "raw_arguments": '{"wire":2,"a":1}'},
    {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": '{"wire":2,"a":1}'}},
])
def test_replay_recording_and_query_use_same_wire_arguments(tmp_path, recorded_call):
    record = {"messages": [{"role": "assistant", "tool_calls": [recorded_call]}],
              "available_tool_names": [], "response": {"content": "wire match"}}
    path = tmp_path / "replay.jsonl"
    path.write_text(json.dumps(record) + "\n")
    model = ReplayModelGateway(path)
    assert model.complete([ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read",
        {"another_stale": True}, '{"a":1,"wire":2}')])], max_tokens=100).content == "wire match"
    with pytest.raises(ReplayMissError):
        model.complete([ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read",
            {"stale": True})])], max_tokens=100)
    with pytest.raises(ReplayMissError):
        model.complete([ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read",
            {"wire": 2, "a": 1}, '{"wire":3,"a":1}')])], max_tokens=100)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999"])
def test_replay_nonfinite_raw_arguments_are_typed_misses(value):
    model = ReplayModelGateway(REPLAY)
    messages = [ChatMessage("assistant", tool_calls=[ToolCall("call-1", "read",
                 raw_arguments='{"value":' + value + '}')])]
    with pytest.raises(ReplayMissError) as first:
        model.complete(messages, max_tokens=100)
    with pytest.raises(ReplayMissError) as second:
        model.complete(messages, max_tokens=100)
    assert first.value.kind == "replay_miss"
    assert len(first.value.request_hash) == 64
    assert first.value.request_hash == second.value.request_hash


def test_native_raw_arguments_cannot_false_hit_parsed_cache(tmp_path):
    record = {"messages": [{"role": "assistant", "tool_calls": [{"id": "call-1", "name": "read",
        "arguments": {"stale": True}, "raw_arguments": '{"wire":2}'}]}],
        "available_tool_names": [], "response": {"content": "wire-only recording"}}
    path = tmp_path / "replay.jsonl"
    path.write_text(json.dumps(record) + "\n")
    with pytest.raises(ReplayMissError):
        ReplayModelGateway(path).complete([ChatMessage("assistant", tool_calls=[
            ToolCall("call-1", "read", {"stale": True})])], max_tokens=100)


def test_settings_and_factory_select_modes(monkeypatch):
    monkeypatch.setenv("AILAB_MODEL_MODE", "replay")
    monkeypatch.setenv("AILAB_MODEL_REPLAY_PATH", str(REPLAY))
    assert build_model_gateway(Settings(llm_api_key="")).mode == "replay"
    monkeypatch.setenv("AILAB_MODEL_MODE", "online")
    monkeypatch.setenv("AILAB_LLM_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("AILAB_LLM_MODEL", "configured-model")
    monkeypatch.setenv("AILAB_LLM_API_KEY", "configured-key")
    model = build_model_gateway(Settings())
    assert model.mode == "online"
    assert model.model == "configured-model"
    model.close()
    for overrides in [{"llm_base_url": ""}, {"llm_model": ""}, {"llm_api_key": ""}, {"model_mode": "typo"}]:
        with pytest.raises(ModelConfigurationError):
            build_model_gateway(Settings(**overrides))


def test_online_factory_rejects_legacy_mock_defaults():
    for overrides in [{"llm_api_key": "EMPTY", "llm_model": "real-model"},
                      {"llm_api_key": "actual-key", "llm_model": "mock-diagnoser-v1"}]:
        with pytest.raises(ModelConfigurationError):
            build_model_gateway(Settings(model_mode="online", **overrides))
