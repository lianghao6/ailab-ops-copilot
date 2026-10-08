import asyncio
import json

import pytest

from ailab_ops.config import Settings
from ailab_ops.models import ModelConfigurationError
from ailab_ops.runtime import build_runtime


def replay_settings(**kw):
    return Settings(model_mode="replay", llm_api_key="", user_qps=0, **kw)


def test_default_import_and_inference_never_load_legacy_rules_or_reasoner():
    """An eager legacy import must fail even when only V2 is requested."""
    import os
    import subprocess
    import sys
    from ailab_ops.config import PROJECT_ROOT

    program = """
import asyncio
import importlib.abc
import sys

class ForbidLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'ailab_ops.llm.mock', 'ailab_ops.signals',
                        'ailab_ops.serving.service', 'ailab_ops.tools.builtin'}:
            raise AssertionError('default imported legacy module: ' + fullname)

sys.meta_path.insert(0, ForbidLegacy())
from ailab_ops.config import Settings
from ailab_ops.runtime import build_runtime
from ailab_ops.serving import create_app
from ailab_ops.serving.app import create_app as compatibility_app
rt = build_runtime(Settings(model_mode='replay', llm_api_key='', user_qps=0))
try:
    result = asyncio.run(rt.investigate(case_id='case-gpu-assert'))
    assert result['phase'] == 'completed' and result['mode'] == 'replay', result
    for factory in (create_app, compatibility_app):
        assert '/v2/investigations' in {route.path for route in factory(rt).routes}
finally:
    rt.close()
online = build_runtime(Settings(model_mode='online', llm_api_key='test-key', llm_model='live-model'))
try:
    assert online.model_mode == 'online'
finally:
    online.close()
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("command", ["investigate", "demo", "ask", "eval"])
def test_public_inference_commands_require_online_key_unless_replay_is_explicit(command, capsys, monkeypatch):
    from ailab_ops import cli
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setenv("AILAB_MODEL_MODE", "online")
    monkeypatch.setenv("AILAB_LLM_API_KEY", "")
    monkeypatch.setenv("AILAB_LLM_MODEL", "live-model")
    arguments = {"investigate": ["--case", "case-gpu-assert"],
                 "demo": [], "ask": ["Diagnose case-gpu-assert."], "eval": []}
    assert cli.main([command, *arguments[command]]) == 2
    assert "llm_api_key" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["mock", "offline", "deterministic", "typo"])
def test_default_runtime_rejects_unknown_and_v1_model_modes_without_key(mode):
    with pytest.raises(ModelConfigurationError):
        build_runtime(Settings(model_mode=mode, llm_api_key=""))


def test_public_help_explains_modes_and_requires_explicit_legacy_namespace(capsys):
    from ailab_ops.cli import build_parser
    parser = build_parser()
    help_text = parser.format_help()
    assert "online" in help_text and "replay" in help_text and "key" in help_text
    for command in ("demo", "ask", "eval", "investigate", "serve"):
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args([command, "--help"])
        assert exit_info.value.code == 0
        command_help = capsys.readouterr().out
        assert "V2" in command_help and "online" in command_help and "replay" in command_help
    for command in ("bench", "compare", "inspect", "llm-stub", "gen-data"):
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args([command])
        assert exit_info.value.code == 2
    legacy = parser.parse_args(["legacy", "demo"])
    assert legacy.cmd == "legacy"


def test_make_demo_and_replay_evaluation_exercise_v2_without_key():
    import os
    import subprocess
    import sys
    from ailab_ops.config import PROJECT_ROOT

    env = {**os.environ, "AILAB_LLM_API_KEY": "", "AILAB_MODEL_MODE": "online"}
    for target, extra in (("demo", []), ("eval", ["MODE=replay"])):
        result = subprocess.run(["make", target, "PY=" + sys.executable, *extra],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert "{" in result.stdout, "V2 Make targets must emit an investigation/evaluation JSON result"
        output = json.loads(result.stdout[result.stdout.index("{"):])
        assert output["mode"] == "replay"
        if target == "demo":
            assert output["phase"] == "completed" and output["report"]["claims"]
        else:
            assert len(output["runs"]) == 3 and output["summary"]["citation_validity"]["mean"] == 1.0


def test_default_runtime_replays_complete_cited_investigation_without_key():
    rt = build_runtime(replay_settings())
    result = asyncio.run(rt.investigate(case_id="case-gpu-assert"))
    assert result["mode"] == "replay"
    assert result["phase"] == "completed"
    assert result["report"]["root_cause"] == "gpu_device_assert"
    assert result["hypotheses"] and result["evidence"] and result["trace_id"]
    assert set(result["report"]["claims"][0]["evidence_ids"]) <= set(result["evidence_ids"])
    assert result["approval_state"] == []
    assert rt.get_investigation(result["session_id"])["report"] == result["report"]


def test_online_boot_requires_credentials():
    with pytest.raises(ModelConfigurationError):
        build_runtime(Settings(model_mode="online", llm_api_key=""))


def test_configured_online_is_default(monkeypatch):
    monkeypatch.delenv("AILAB_MODEL_MODE", raising=False)
    monkeypatch.setenv("AILAB_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AILAB_LLM_MODEL", "test-model")
    assert build_runtime(Settings()).model_mode == "online"


def test_budget_stop_and_replay_miss_keep_typed_state():
    rt = build_runtime(replay_settings())
    stopped = asyncio.run(rt.investigate(case_id="case-gpu-assert", max_steps=0))
    assert stopped["stop_reason"] == "budget_exhausted:steps"
    missed = asyncio.run(rt.investigate(case_id="case-gpu-assert", question="Unrecorded question"))
    assert missed["error"]["kind"] == "replay_miss"
    assert missed["error"]["retryable"] is False
    assert missed["report"] is None


@pytest.mark.parametrize("case_id", ["case-collective-timeout", "case-insufficient-evidence"])
def test_other_curated_replays_complete(case_id):
    rt = build_runtime(replay_settings())
    result = asyncio.run(rt.investigate(case_id=case_id))
    assert result["phase"] == "completed"
    assert result["report"]["unknowns"]


def test_tenant_limits_release_after_rejection():
    rt = build_runtime(replay_settings(tenant_token_per_min=1))
    result = asyncio.run(rt.investigate(case_id="case-gpu-assert"))
    assert result["error"]["kind"] == "tenant_token_exceeded"
    assert rt.gate.in_flight == 0
    assert rt.limiter.snapshot()["tenants"]["tenant-01"]["in_flight"] == 0


def test_cli_replay_and_layered_evaluation(capsys, monkeypatch):
    from ailab_ops.cli import main
    monkeypatch.setenv("AILAB_LLM_API_KEY", "")
    assert main(["replay", "--case", "case-gpu-assert"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["mode"] == "replay" and replay["report"]["claims"]
    assert main(["eval-v2", "--mode", "replay"]) == 0
    evaluation = json.loads(capsys.readouterr().out)
    assert evaluation["mode"] == "replay"
    assert len(evaluation["runs"]) == 3
    assert evaluation["summary"]["citation_validity"]["mean"] == 1.0
    assert evaluation["summary"]["root_cause"]["mean"] == 1.0


def test_gate_rejects_second_model_and_cancellation_retains_slot_until_worker_finishes():
    import threading
    from ailab_ops.models import ModelBackendError
    started, release = threading.Event(), threading.Event()

    class BlockingGateway:
        mode, model = "online", "blocking-test"

        def complete(self, *args, **kwargs):
            started.set()
            assert release.wait(5), "test must release the simulated model"
            raise ModelBackendError("controlled outage", kind="unavailable", retryable=True)

    rt = build_runtime(replay_settings(upstream_concurrency=1, queue_maxsize=0), gateway=BlockingGateway())

    async def scenario():
        first = asyncio.create_task(rt.investigate(case_id="case-gpu-assert"))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            assert started.is_set()
            second = await rt.investigate(case_id="case-gpu-assert", user_id="second")
            assert second["error"]["kind"] == "queue_full"
            first.cancel()
            await asyncio.sleep(0.01)
            assert rt.gate.in_flight == 1
            assert rt.limiter.snapshot()["tenants"]["tenant-01"]["in_flight"] == 1
        finally:
            release.set()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        assert rt.gate.in_flight == 0
        assert rt.limiter.snapshot()["tenants"]["tenant-01"]["in_flight"] == 0
    asyncio.run(scenario())


def test_model_executor_cannot_starve_behind_waiting_orchestration_workers():
    import os
    import subprocess
    import sys
    from ailab_ops.config import PROJECT_ROOT
    program = """
import asyncio
from concurrent.futures import ThreadPoolExecutor
from ailab_ops.config import Settings
from ailab_ops.runtime import build_runtime
async def main():
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    rt = build_runtime(Settings(model_mode='replay'))
    try:
        result = await rt.investigate(case_id='case-gpu-assert')
        assert result['phase'] == 'completed', result
    finally:
        rt.close()
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


class BlockingSuccessfulGateway:
    """Controlled external model boundary, with genuine normal responses."""
    mode, model = "online", "successful-blocking-test"

    def __init__(self, *, tool_response=False):
        import threading
        self.started, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.tool_response = tool_response

    def complete(self, *args, **kwargs):
        from ailab_ops.llm.base import FinishReason, LLMResponse, ToolCall, Usage
        self.calls += 1
        self.started.set()
        assert self.release.wait(5), "test must release the model"
        if self.tool_response:
            return LLMResponse(tool_calls=[ToolCall("read", "get_case_logs", {"case_id": "case-gpu-assert"})],
                finish_reason=FinishReason.TOOL_CALLS, usage=Usage(10, 7))
        return LLMResponse(content='{"type":"plan","plan":["Read observations"]}', usage=Usage(10, 7))


async def eventually(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    assert predicate(), "condition did not become true"


def test_global_admission_precedes_executor_submission_across_tenants():
    from concurrent.futures import ThreadPoolExecutor
    gateway = BlockingSuccessfulGateway()
    rt = build_runtime(replay_settings(upstream_concurrency=1, queue_maxsize=0), gateway=gateway)

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        first = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", tenant_id="one", max_steps=1))
        await eventually(gateway.started.is_set)
        second = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", tenant_id="two", max_steps=1))
        try:
            done, _ = await asyncio.wait([second], timeout=0.2)
            assert second in done, "request must reject before waiting in the executor queue"
            assert second.result()["error"]["kind"] == "queue_full"
            assert gateway.calls == 1
        finally:
            gateway.release.set()
            await asyncio.gather(first, second)
        assert rt.gate.in_flight == 0
    try:
        asyncio.run(scenario())
    finally:
        rt.close()


def test_cost_is_rechecked_after_queue_wait_before_model_start():
    gateway = BlockingSuccessfulGateway()
    rt = build_runtime(replay_settings(upstream_concurrency=1, queue_maxsize=2,
        tenant_cost_per_day_usd=0.000001, price_input_per_mtok=1), gateway=gateway)

    async def scenario():
        first = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", max_steps=1))
        await eventually(gateway.started.is_set)
        second = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", user_id="two", max_steps=1))
        try:
            await eventually(lambda: rt.gate.queued == 1)
        finally:
            gateway.release.set()
        _, rejected = await asyncio.gather(first, second)
        assert gateway.calls == 1
        assert rejected["error"]["kind"] == "tenant_cost_exceeded"
        assert rejected["error"]["retryable"] is False
        assert rejected["budget"]["tokens_used"] == 0
        assert rt.gate.in_flight == 0
        assert rt.limiter.snapshot()["tenants"]["tenant-01"]["in_flight"] == 0
    try:
        asyncio.run(scenario())
    finally:
        rt.close()


def test_queued_deadline_expires_without_starting_a_model_call():
    gateway = BlockingSuccessfulGateway()
    rt = build_runtime(replay_settings(upstream_concurrency=1, queue_maxsize=2, queue_timeout_s=2), gateway=gateway)

    async def scenario():
        first = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", max_steps=1))
        await eventually(gateway.started.is_set)
        second = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", user_id="two", deadline_s=0.03))
        try:
            done, _ = await asyncio.wait([second], timeout=0.2)
            assert second in done, "investigation deadline must bound admission wait"
            expired = second.result()
            assert expired["stop_reason"] == "budget_exhausted:deadline"
            assert expired["error"] is None
            assert expired["budget"]["tokens_used"] == 0
            assert gateway.calls == 1
        finally:
            gateway.release.set()
            await asyncio.gather(first, second)
        assert rt.gate.in_flight == rt.gate.queued == 0
    try:
        asyncio.run(scenario())
    finally:
        rt.close()


def test_cancelled_normal_model_response_is_accounted_but_no_tools_or_later_models_run():
    gateway = BlockingSuccessfulGateway(tool_response=True)
    rt = build_runtime(replay_settings(upstream_concurrency=1), gateway=gateway)

    async def scenario():
        task = asyncio.create_task(rt.investigate(case_id="case-gpu-assert"))
        await eventually(gateway.started.is_set)
        task.cancel()
        await asyncio.sleep(0.01)
        assert rt.gate.in_flight == 1
        gateway.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        session_id = next(iter(rt.records))
        result = rt.get_investigation(session_id)
        assert result["phase"] == "stopped" and result["stop_reason"] == "cancelled"
        assert result["budget"]["tokens_used"] == 17
        assert rt.limiter.snapshot()["tenants"]["tenant-01"]["tokens_last_min"] == 17
        assert gateway.calls == 1
        assert rt.record(session_id).orchestrator.registry.call_log == []
        assert rt.gate.in_flight == 0
    try:
        asyncio.run(scenario())
    finally:
        gateway.release.set()
        rt.close()


def test_cancellation_removes_queued_investigation_immediately():
    gateway = BlockingSuccessfulGateway()
    rt = build_runtime(replay_settings(upstream_concurrency=1, queue_maxsize=1), gateway=gateway)

    async def scenario():
        first = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", max_steps=1))
        await eventually(gateway.started.is_set)
        second = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", tenant_id="two"))
        try:
            await eventually(lambda: rt.gate.queued == 1)
            second.cancel()
            done, _ = await asyncio.wait([second], timeout=0.2)
            assert second in done, "queued cancellation must not wait for the current model"
            with pytest.raises(asyncio.CancelledError):
                await second
            assert rt.gate.queued == 0 and rt.gate.in_flight == 1
            assert rt.limiter.snapshot()["tenants"]["two"]["in_flight"] == 0
            session_id = next(sid for sid, record in rt.records.items() if record.tenant_id == "two")
            assert rt.get_investigation(session_id, "two")["stop_reason"] == "cancelled"
        finally:
            gateway.release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        assert gateway.calls == 1 and rt.gate.in_flight == 0
    try:
        asyncio.run(scenario())
    finally:
        rt.close()


def test_cancelled_gate_handoff_returns_slot_to_next_waiter():
    from ailab_ops.serving.gate import UpstreamGate

    async def scenario():
        gate = UpstreamGate(max_concurrency=1, queue_maxsize=2)
        await gate.acquire()
        cancelled = asyncio.create_task(gate.acquire())
        following = asyncio.create_task(gate.acquire())
        await eventually(lambda: gate.queued == 2)
        await gate.release()
        # The Future owns the slot, but acquire() has not resumed yet.
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await asyncio.wait_for(following, timeout=0.2)
        assert gate.in_flight == 1 and gate.queued == 0
        await gate.release()
        assert gate.in_flight == 0
    asyncio.run(scenario())


def test_runtime_release_before_cancelled_waiter_cleanup_does_not_strand_successor(monkeypatch):
    gateway = BlockingSuccessfulGateway()
    rt = build_runtime(replay_settings(upstream_concurrency=1, queue_maxsize=2, queue_timeout_s=0.1), gateway=gateway)

    async def scenario():
        first = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", tenant_id="first", max_steps=1))
        await eventually(gateway.started.is_set)
        cancelled = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", tenant_id="cancelled", max_steps=1))
        await eventually(lambda: rt.gate.queued == 1)
        following = asyncio.create_task(rt.investigate(case_id="case-gpu-assert", tenant_id="following", max_steps=1))
        original_release = rt.gate.release
        cancellation_injected = False

        async def cancel_then_release():
            nonlocal cancellation_injected
            if not cancellation_injected:
                cancellation_injected = True
                cancelled.cancel()
            await original_release()

        try:
            await eventually(lambda: rt.gate.queued == 2)
            # Inject cancellation exactly at the real runtime's release edge;
            # both the model accounting and the gate handoff stay real.
            monkeypatch.setattr(rt.gate, "release", cancel_then_release)
            gateway.release.set()
            initial, stopped, successor = await asyncio.gather(first, cancelled, following, return_exceptions=True)
            assert isinstance(stopped, asyncio.CancelledError)
            assert initial["error"] is None
            assert successor["error"] is None
            assert successor["budget"]["tokens_used"] == 17
            assert gateway.calls == 2
            assert rt.gate.in_flight == rt.gate.queued == 0
            assert rt.gate.stats().admitted == 2 and rt.gate.stats().rejected_timeout == 0
            assert all(tenant["in_flight"] == 0 for tenant in rt.limiter.snapshot()["tenants"].values())
            cancelled_id = next(sid for sid, record in rt.records.items() if record.tenant_id == "cancelled")
            assert rt.get_investigation(cancelled_id, "cancelled")["stop_reason"] == "cancelled"
        finally:
            gateway.release.set()
            await asyncio.gather(first, cancelled, following, return_exceptions=True)
    try:
        asyncio.run(scenario())
    finally:
        rt.close()
