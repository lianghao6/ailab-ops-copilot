"""The agent loop, the model adapters, and the end-to-end pipeline.

These are the tests that would catch the failures a demo hides: a loop that
never terminates, a tool error that kills the run instead of being fed back, an
answer that parses by luck, a "rewrite the arguments" guard that quietly changes
which job is being investigated.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ailab_ops.agent import Agent, AgentResult, parse_answer
from ailab_ops.llm import ChatMessage, FinishReason, LLMResponse, ToolCall, ToolSpec, Usage
from ailab_ops.llm.openai_compat import OpenAICompatClient, OpenAICompatError, classify_status
from ailab_ops.obs import METRICS, Metrics, Tracer


# --------------------------------------------------------------------------
# A scripted fake client: the loop must be testable without a model
# --------------------------------------------------------------------------


class ScriptedClient:
    """Returns a fixed sequence of responses, then a final answer.

    A real model is nondeterministic and a network dependency; neither belongs
    in a unit test of the loop. This fake implements the same protocol, so the
    loop is exercised exactly as it would be in production.
    """

    model = "scripted"

    def __init__(self, turns: list[LLMResponse], final: str = '{"root_cause": "oom_gpu"}'):
        self.turns = list(turns)
        self.final = final
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.0, max_tokens=None):
        self.calls += 1
        if self.turns:
            return self.turns.pop(0)
        return LLMResponse(
            content=self.final, finish_reason=FinishReason.STOP, usage=Usage(10, 5), model=self.model
        )

    def chat_stream(self, messages, tools=None, temperature=0.0, max_tokens=None):
        r = self.chat(messages, tools)
        yield {"type": "delta", "text": r.content}
        yield {"type": "done", "response": r}


def _tool_turn(name: str, args: dict, call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content=f"call {name}",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args, raw_arguments=json.dumps(args))],
        finish_reason=FinishReason.TOOL_CALLS,
        usage=Usage(20, 10),
        model="scripted",
    )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_loop_runs_a_tool_then_answers(registry, world):
    job = world.failed_jobs()[0]
    client = ScriptedClient([_tool_turn("get_job", {"job_id": job.job_id})])
    agent = Agent(client, registry)
    res = agent.run(f"why did {job.job_id} fail?")
    assert res.stop_reason == "stop"
    assert res.n_tool_calls == 1
    assert res.n_llm_calls == 2
    assert res.parsed is not None
    assert [s.kind for s in res.steps] == ["tool", "llm"]


def test_loop_respects_the_step_budget(registry):
    """An unbounded loop is an unbounded bill."""
    # A client that always asks for a tool: without a budget this never ends.
    loop_turns = [_tool_turn("get_exit_code_meaning", {"code": 1}, f"c{i}") for i in range(50)]
    client = ScriptedClient(loop_turns, final="")
    agent = Agent(client, registry)
    res = agent.run("why did it fail?", max_steps=4)
    assert res.stop_reason == "max_steps"
    assert res.error and "max_steps" in res.error
    assert len([s for s in res.steps if s.kind == "tool"]) <= 4


def test_tool_errors_are_fed_back_not_raised(registry):
    """A failed tool call must become a message the model can act on. Raising
    would end the run on a recoverable mistake -- and the whole advantage of
    tool-calling over a fixed pipeline is that it can recover."""
    client = ScriptedClient([_tool_turn("get_job", {"job_id": "job-does-not-exist-0000"})])
    agent = Agent(client, registry)
    res = agent.run("diagnose job-does-not-exist-0000")
    assert res.stop_reason == "stop", "a tool error should not end the run"
    tool_step = next(s for s in res.steps if s.kind == "tool")
    assert tool_step.ok is False
    assert tool_step.error


def test_unknown_tool_does_not_crash(registry):
    client = ScriptedClient([_tool_turn("rm_rf_everything", {})])
    res = Agent(client, registry).run("hi")
    assert res.stop_reason == "stop"
    assert any(s.kind == "tool" and s.ok is False for s in res.steps)


def test_hallucinated_job_id_is_replaced_from_context(registry, world):
    """The model invents a well-formed id. A syntactic check would pass it
    straight through, so the guard tests whether the id was ever mentioned.
    Read-only tools make the substitution safe."""
    job = world.failed_jobs()[0]
    client = ScriptedClient([_tool_turn("get_job", {"job_id": "job-00000000-0000"})])
    res = Agent(client, registry).run(f"why did {job.job_id} fail?")
    tool_step = next(s for s in res.steps if s.kind == "tool")
    assert tool_step.arguments["job_id"] == job.job_id
    assert "replaced unmentioned job_id" in tool_step.note
    assert tool_step.ok is True


def test_a_correctly_quoted_job_id_is_left_alone(registry, world):
    job = world.failed_jobs()[0]
    client = ScriptedClient([_tool_turn("get_job", {"job_id": job.job_id})])
    res = Agent(client, registry).run(f"why did {job.job_id} fail?")
    tool_step = next(s for s in res.steps if s.kind == "tool")
    assert tool_step.note == "", "a correct id should not be rewritten"
    assert tool_step.arguments["job_id"] == job.job_id


def test_unknown_arguments_are_dropped(registry, world):
    job = world.failed_jobs()[0]
    client = ScriptedClient([_tool_turn("get_job", {"job_id": job.job_id, "sudo": True})])
    res = Agent(client, registry).run(f"why did {job.job_id} fail?")
    tool_step = next(s for s in res.steps if s.kind == "tool")
    assert "sudo" not in tool_step.arguments
    assert tool_step.ok is True


def test_loop_records_the_assistant_turn_including_tool_calls(registry, world):
    """Dropping tool_calls from the recorded history is the classic bug that
    makes a model repeat itself: as far as it can tell, it never asked."""
    seen: list[list[ChatMessage]] = []

    class Recording(ScriptedClient):
        def chat(self, messages, tools=None, temperature=0.0, max_tokens=None):
            seen.append(list(messages))
            return super().chat(messages, tools, temperature, max_tokens)

    job = world.failed_jobs()[0]
    client = Recording([_tool_turn("get_job", {"job_id": job.job_id})])
    Agent(client, registry).run(f"why did {job.job_id} fail?")
    second_call = seen[1]
    assistants = [m for m in second_call if m.role == "assistant"]
    assert assistants and assistants[0].tool_calls, "the assistant tool call was not preserved"
    assert any(m.role == "tool" for m in second_call)


def test_llm_exception_ends_the_run_cleanly(registry):
    class Boom(ScriptedClient):
        def chat(self, messages, tools=None, temperature=0.0, max_tokens=None):
            raise RuntimeError("upstream is on fire")

    res = Agent(Boom([]), registry).run("anything")
    assert res.stop_reason == "llm_error"
    assert "upstream is on fire" in (res.error or "")


def test_system_prompt_is_included(registry):
    captured: dict = {}

    class Cap(ScriptedClient):
        def chat(self, messages, tools=None, temperature=0.0, max_tokens=None):
            captured["tools"] = [t.name for t in (tools or [])]
            captured["system"] = messages[0].content
            return super().chat(messages, tools, temperature, max_tokens)

    Agent(Cap([]), registry, system_prompt="CUSTOM PROMPT").run("hi")
    assert captured["system"] == "CUSTOM PROMPT"
    assert "get_job" in captured["tools"]


def test_tracer_records_a_span_per_step(registry, world):
    job = world.failed_jobs()[0]
    client = ScriptedClient([_tool_turn("get_job", {"job_id": job.job_id})])
    tracer = Tracer()
    res = Agent(client, registry).run(f"why did {job.job_id} fail?", tracer=tracer)
    names = [s.name for s in tracer.trace.spans]
    assert "llm.call" in names and "tool.call" in names
    assert tracer.trace.outcome == "stop"
    assert res.trace_id == tracer.trace.trace_id


def test_run_stream_emits_the_same_answer_as_run(registry, world):
    """A streamed answer must be byte-identical to a non-streamed one, or the
    two code paths will diverge and only one will be tested."""
    job = world.failed_jobs()[0]
    q = f"why did {job.job_id} fail?"

    plain = Agent(ScriptedClient([_tool_turn("get_job", {"job_id": job.job_id})]), registry).run(q)
    events = list(
        Agent(ScriptedClient([_tool_turn("get_job", {"job_id": job.job_id})]), registry).run_stream(q)
    )
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "done"
    assert "answer" in kinds
    streamed_answer = next(e["data"] for e in events if e["event"] == "answer")
    assert streamed_answer["answer"] == plain.answer


# --------------------------------------------------------------------------
# Answer parsing
# --------------------------------------------------------------------------


def test_parse_plain_json():
    p = parse_answer('{"root_cause": "oom_gpu", "confidence": 0.8}')
    assert p["root_cause"] == "oom_gpu" and p["confidence"] == 0.8


def test_parse_fenced_json():
    p = parse_answer('Here is my analysis:\n```json\n{"root_cause": "disk_full"}\n```\nHope that helps.')
    assert p["root_cause"] == "disk_full"


def test_parse_json_embedded_in_prose():
    p = parse_answer('The diagnosis is {"root_cause": "rate_limited"} because of the 429s.')
    assert p["root_cause"] == "rate_limited"


def test_parse_normalises_field_names_and_case():
    for raw in ('{"cause": "OOM_GPU"}', '{"diagnosis": "oom-gpu"}', '{"label": "Oom GPU"}'):
        assert parse_answer(raw)["root_cause"] == "oom_gpu"


def test_parse_converts_a_percentage_confidence():
    assert parse_answer('{"root_cause": "x", "confidence": 85}')["confidence"] == 0.85


def test_parse_returns_none_for_prose_without_an_answer():
    assert parse_answer("I could not determine the cause, sorry.") is None


def test_parse_handles_empty_input():
    assert parse_answer("") is None


def test_parse_keeps_unrecognised_fields_in_extra():
    p = parse_answer('{"root_cause": "x", "novel_field": 7}')
    assert p["_extra"]["novel_field"] == 7


# --------------------------------------------------------------------------
# OpenAI-compatible client
# --------------------------------------------------------------------------


def test_status_classification_separates_rate_limit_from_quota():
    """Both arrive as 429 and they are operationally opposite: one clears on its
    own, the other never does."""
    kind, retryable, _ = classify_status(429, "Too Many Requests")
    assert kind == "rate_limited" and retryable

    kind, retryable, _ = classify_status(429, "AppId quota exceeded for this month")
    assert kind == "quota" and not retryable


def test_status_classification_for_5xx_and_4xx():
    assert classify_status(503, "")[0] == "server_error"
    assert classify_status(503, "")[1] is True
    assert classify_status(400, "")[1] is False
    assert classify_status(401, "")[1] is False


def test_client_parses_a_tool_call_response():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "c1", "type": "function",
                            "function": {"name": "get_job", "arguments": '{"job_id": "job-1"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    client = OpenAICompatClient(base_url="http://test/v1", model="test")
    client._client = httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))
    resp = client.chat([ChatMessage(role="user", content="hi")])
    assert resp.tool_calls[0].name == "get_job"
    assert resp.tool_calls[0].arguments == {"job_id": "job-1"}
    assert resp.usage.tokens_in == 11 and resp.usage.tokens_out == 7
    assert resp.finish_reason == FinishReason.TOOL_CALLS


def test_client_does_not_retry_a_bad_request():
    """A 400 means we sent something wrong; retrying is pure waste and doubles
    the load on an upstream that is already unhappy."""
    import httpx

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = OpenAICompatClient(base_url="http://test/v1", model="test", max_retries=3)
    client._client = httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(OpenAICompatError) as ei:
        client.chat([ChatMessage(role="user", content="hi")])
    assert ei.value.kind == "bad_request"
    assert calls["n"] == 1


def test_client_retries_a_server_error_then_succeeds():
    import httpx

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={
            "model": "test",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    client = OpenAICompatClient(base_url="http://test/v1", model="test", max_retries=2, backoff_base_s=0.0)
    client._client = httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))
    resp = client.chat([ChatMessage(role="user", content="hi")])
    assert resp.content == "ok"
    assert calls["n"] == 2


def test_client_reports_unparseable_json_clearly():
    """A 200 with bad JSON is a protocol problem, and calling it a 'server
    error' sends the investigation to the wrong team."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    client = OpenAICompatClient(base_url="http://test/v1", model="test")
    client._client = httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(OpenAICompatError) as ei:
        client.chat([ChatMessage(role="user", content="hi")])
    assert ei.value.kind == "protocol"
    assert "unparseable" in str(ei.value)


def test_streaming_accumulates_fragmented_tool_arguments():
    """Tool arguments arrive as a JSON *string* spread over many chunks. Reading
    them as an object works against a non-streaming endpoint and breaks here.
    """
    import httpx

    frames = [
        'data: {"choices":[{"delta":{"role":"assistant","content":""},"index":0}]}\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"get_job","arguments":""}}]},"index":0}]}\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"job_"}}]},"index":0}]}\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"id\\": \\"job-1\\"}"}}]},"index":0}]}\n',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}\n',
        "data: [DONE]\n",
    ]
    body = "".join(frames).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = OpenAICompatClient(base_url="http://test/v1", model="test")
    client._client = httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))
    events = list(client.chat_stream([ChatMessage(role="user", content="hi")]))
    done = events[-1]["response"]
    assert done.tool_calls[0].name == "get_job"
    assert done.tool_calls[0].arguments == {"job_id": "job-1"}


def test_streaming_tolerates_a_malformed_frame():
    """One corrupt SSE frame must not kill the stream: the remaining frames are
    still perfectly usable."""
    import httpx

    body = (
        'data: {"choices":[{"delta":{"content":"hel"}}]}\n'
        "data: {this is not json\n"
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n'
        "data: [DONE]\n"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = OpenAICompatClient(base_url="http://test/v1", model="test")
    client._client = httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))
    events = list(client.chat_stream([ChatMessage(role="user", content="hi")]))
    assert events[-1]["response"].content == "hello"


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


def test_a_loopback_backend_ignores_the_proxy_environment(monkeypatch):
    """A machine with `http_proxy` exported routes loopback through the proxy,
    and the failure looks exactly like a backend outage: a 503 from an unrelated
    server. httpx honours the variable even for 127.0.0.1, so it has to be
    switched off for loopback explicitly."""
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    loopback = OpenAICompatClient(base_url="http://127.0.0.1:9/v1", model="x")
    assert loopback.client.trust_env is False
    remote = OpenAICompatClient(base_url="http://model-gateway.internal/v1", model="x")
    assert remote.client.trust_env is True


def test_client_retries_and_the_agent_survives_a_flaky_backend():
    """End to end: a backend that fails most of its calls must still produce a
    diagnosis, because the client retries retryable failures."""
    import httpx

    from ailab_ops.tools import build_registry

    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] % 3 != 0:
            return httpx.Response(503, json={"error": "unavailable"})
        body = json.loads(request.content)
        # Reply with a well-formed final answer; we are testing the retry path,
        # not the reasoner.
        return httpx.Response(200, json={
            "model": "flaky",
            "choices": [{"message": {"role": "assistant", "content": '{"root_cause": "x"}'},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })

    client = OpenAICompatClient(base_url="http://flaky/v1", model="flaky",
                                max_retries=3, backoff_base_s=0.0)
    client._client = httpx.Client(base_url="http://flaky/v1", transport=httpx.MockTransport(handler))

    class _Empty:
        names: list[str] = []

        def specs(self):
            return []

        def get(self, name):
            return None

        def call(self, name, args, step=0):
            raise AssertionError("no tools should be called")

    res = Agent(client, _Empty()).run("diagnose something")
    assert res.stop_reason == "stop"
    assert res.parsed["root_cause"] == "x"
    assert state["n"] > 1, "the client did not retry"


def test_metrics_percentiles_are_observed_values():
    m = Metrics()
    for v in range(1, 101):
        m.observe("latency", float(v))
    s = m.histogram_stats("latency")
    assert s["count"] == 100
    assert s["p50"] in {v for v in range(1, 101)}
    assert s["p99"] >= s["p90"] >= s["p50"]


def test_cost_accounting_is_per_tenant():
    m = Metrics()
    m.record_cost("t1", 1_000_000, 1_000_000, 0.15, 0.60)
    m.record_cost("t2", 1_000_000, 0, 0.15, 0.60)
    assert abs(m.tenant_usd_today("t1") - 0.75) < 1e-9
    assert abs(m.tenant_usd_today("t2") - 0.15) < 1e-9
    assert abs(m.total_usd() - 0.90) < 1e-9


def test_tenant_token_window_rolls():
    m = Metrics()
    m.record_cost("t1", 1000, 0, 0.0, 0.0)
    assert m.tenant_tokens_last_minute("t1") == 1000
    assert m.tenant_tokens_last_minute("t1", now=__import__("time").time() + 120) == 0


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_legacy_pipeline_answers_a_generated_job(runtime, world):
    """Preserve the V1 simulation's structured result and tool-interface contract."""
    job = next(j for j in world.failed_jobs() if not j.is_insufficient_evidence)
    res, tracer = runtime.diagnose(
        f"Why did {job.job_id} ({job.name}) fail? Give me the root cause and remediation."
    )
    assert res.stop_reason == "stop", f"run failed: {res.error}"
    assert res.parsed is not None, "the final answer was not parseable as a diagnosis"
    assert res.parsed["root_cause"], "no root cause was produced"
    assert res.n_tool_calls >= 3, "a diagnosis should read the job, the log and the metrics"
    assert res.tokens_total > 0
    assert tracer.trace.spans, "the run produced no trace"


def test_legacy_pipeline_matches_the_shared_playbook_on_a_sample(runtime, world):
    """Regression floor for authored V1 rules, not model capability or an architecture ceiling."""
    from ailab_ops.llm.mock import MockLLMClient

    jobs = [j for j in world.failed_jobs()][:60]
    assert len(jobs) >= 20
    correct = 0
    for job in jobs:
        client = MockLLMClient()
        agent = Agent(client, runtime.registry)
        res = agent.run(f"Why did {job.job_id} fail? Give me the root cause.")
        pred = (res.parsed or {}).get("root_cause")
        if pred == job.root_cause:
            correct += 1
    acc = correct / len(jobs)
    assert acc >= 0.75, f"end-to-end accuracy is only {acc:.1%} on a 60-job sample"


def test_runtime_never_exposes_ground_truth_through_its_tools(runtime, world):
    """The end-to-end version of the tool-leak test: drive the whole pipeline and
    check the answer was not simply read from the dataset."""
    job = next(j for j in world.failed_jobs() if j.difficulty == "hard")
    res, _ = runtime.diagnose(f"Why did {job.job_id} fail?")
    # The tool call arguments must never have asked for ground truth.
    for step in res.steps:
        if step.kind == "tool":
            assert "root_cause" not in json.dumps(step.arguments)
