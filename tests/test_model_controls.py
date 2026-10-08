"""Cross-layer model retry and terminal-response regressions, without network."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from ailab_ops.models import ModelBackendError
from ailab_ops.llm.base import FinishReason
from ailab_ops.runtime import build_runtime
from ailab_ops.serving.app import create_app
from test_model_backends import MESSAGES, gateway
from test_v2_runtime import replay_settings


@pytest.mark.parametrize("header,expected", [
    ("NaN", 0.0), ("Infinity", 0.0), ("-Infinity", 0.0),
    ("-5", 0.0), ("86400", 30.0), ("1e999", 0.0), ("2", 2.0),
])
def test_retry_after_wait_and_api_json_share_finite_bounded_hint(monkeypatch, header, expected):
    sleeps = []
    monkeypatch.setattr("ailab_ops.models.openai.time.sleep", sleeps.append)
    handler = lambda request: httpx.Response(503, headers={"Retry-After": header})
    with gateway(handler, max_retries=1) as model:
        with pytest.raises(ModelBackendError) as failure:
            model.complete(MESSAGES)
        assert sleeps == [expected]
        assert failure.value.retry_after_s == expected
    sleeps.clear()
    rt = build_runtime(replay_settings(), gateway=gateway(handler, max_retries=0))
    with TestClient(create_app(rt), raise_server_exceptions=False) as client:
        response = client.post("/v2/investigations", json={"case_id": "case-gpu-assert"})
        assert response.status_code == 503, response.text
        assert response.json()["error"]["retry_after_s"] == expected
        assert sleeps == []


def test_retry_after_cap_is_configurable(monkeypatch):
    sleeps = []
    monkeypatch.setattr("ailab_ops.models.openai.time.sleep", sleeps.append)
    with gateway(lambda request: httpx.Response(503, headers={"Retry-After": "86400"}),
                 max_retries=1, retry_max_delay_s=0.25) as model:
        with pytest.raises(ModelBackendError) as failure:
            model.complete(MESSAGES)
    assert sleeps == [0.25]
    assert failure.value.retry_after_s == 0.25


async def wait_started(event):
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.005)
    pytest.fail("controlled HTTP request did not start")


def test_cancel_during_http_attempt_prevents_every_new_retry():
    started, release = threading.Event(), threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        started.set()
        assert release.wait(3)
        return httpx.Response(503, headers={"Retry-After": "0"})

    rt = build_runtime(replay_settings(upstream_concurrency=1), gateway=gateway(handler, max_retries=2))

    async def scenario():
        task = asyncio.create_task(rt.investigate(case_id="case-gpu-assert"))
        try:
            await wait_started(started)
            task.cancel()
            await asyncio.sleep(0.01)
            assert rt.gate.in_flight == 1
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 1
        assert rt.gate.in_flight == 0
        record = next(iter(rt.records.values()))
        assert next(iter(record.orchestrator.states.values())).stop_reason == "cancelled"
        assert record.error is None

    try:
        asyncio.run(scenario())
    finally:
        rt.close()


def test_cancel_interrupts_backoff_and_releases_gate_promptly():
    started = threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        started.set()
        return httpx.Response(503, headers={"Retry-After": "1"})

    rt = build_runtime(replay_settings(upstream_concurrency=1), gateway=gateway(handler, max_retries=1))

    async def scenario():
        task = asyncio.create_task(rt.investigate(case_id="case-gpu-assert"))
        await wait_started(started)
        await asyncio.sleep(0.03)
        before = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - before < 0.5
        assert len(calls) == 1
        assert rt.gate.in_flight == 0

    try:
        asyncio.run(scenario())
    finally:
        rt.close()


@pytest.mark.parametrize("max_retries", [0, 2])
def test_failed_attempt_after_deadline_stops_as_budget_not_backend(max_retries):
    calls = []

    def handler(request):
        calls.append(request)
        threading.Event().wait(0.05)
        return httpx.Response(503, headers={"Retry-After": "0"})

    rt = build_runtime(replay_settings(), gateway=gateway(handler, max_retries=max_retries))
    try:
        result = asyncio.run(rt.investigate(case_id="case-gpu-assert", deadline_s=0.02))
        assert result["stop_reason"] == "budget_exhausted:deadline"
        assert result["error"] is None
        assert len(calls) == 1
        assert rt.gate.in_flight == 0
    finally:
        rt.close()


def test_backoff_is_capped_by_remaining_deadline_and_http_timeout():
    calls = []

    def handler(request):
        calls.append(request)
        assert all(0 < value <= 0.05 for value in request.extensions["timeout"].values())
        return httpx.Response(503, headers={"Retry-After": "1"})

    rt = build_runtime(replay_settings(), gateway=gateway(handler, max_retries=1))
    try:
        before = time.monotonic()
        result = asyncio.run(rt.investigate(case_id="case-gpu-assert", deadline_s=0.05))
        assert result["stop_reason"] == "budget_exhausted:deadline"
        assert result["error"] is None
        assert time.monotonic() - before < 0.5
        assert len(calls) == 1
    finally:
        rt.close()


def test_deadline_interrupt_releases_half_open_breaker_probe_for_next_investigation():
    def handler(request):
        threading.Event().wait(0.04)
        return httpx.Response(503)

    rt = build_runtime(replay_settings(), gateway=gateway(handler, max_retries=0))
    breaker = rt.breakers.get("model")
    breaker.failure_threshold, breaker.cooldown_s = 1, 0
    breaker.record_failure()
    try:
        expired = asyncio.run(rt.investigate(case_id="case-gpu-assert", deadline_s=0.02))
        assert expired["stop_reason"] == "budget_exhausted:deadline"
        next_result = asyncio.run(rt.investigate(case_id="case-gpu-assert", deadline_s=1))
        assert next_result["error"]["kind"] == "server_error", "interrupted probe must not lock the breaker"
    finally:
        rt.close()


@pytest.mark.parametrize("reason", ["length", "error"])
@pytest.mark.parametrize("later_stop", [False, True])
def test_sse_terminal_failure_with_valid_tools_executes_nothing(reason, later_stop):
    frames = [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "read-1",
        "function": {"name": "get_case_logs", "arguments": '{"case_id":"case-gpu-assert"}'}}]},
        "finish_reason": reason}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}]
    if later_stop:
        frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    handler = lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    with gateway(handler) as model:
        assert model.complete(MESSAGES).finish_reason == FinishReason(reason)
    rt = build_runtime(replay_settings(), gateway=gateway(handler))
    try:
        result = asyncio.run(rt.investigate(case_id="case-gpu-assert", max_steps=1))
        assert result["evidence_ids"] == []
        assert result["evidence"] == []
        assert result["budget"]["tokens_used"] == 2
        record = rt.record(result["session_id"])
        assert record.orchestrator.registry.call_log == []
    finally:
        rt.close()
