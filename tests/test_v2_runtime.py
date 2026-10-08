import asyncio
import json

import pytest

from ailab_ops.config import Settings
from ailab_ops.models import ModelConfigurationError
from ailab_ops.runtime import build_runtime


def replay_settings(**kw):
    return Settings(model_mode="replay", llm_api_key="", user_qps=0, **kw)


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
