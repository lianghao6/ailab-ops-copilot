"""Each observation must explain its own source coverage, without snapshot-first."""

import asyncio
import json

import pytest

from ailab_ops.cases.loader import load_case
from ailab_ops.runtime import build_runtime
from ailab_ops.tools.cases import build_case_registry
from test_orchestrator import ScriptedGateway, call, cited_report
from test_v2_runtime import replay_settings


@pytest.mark.parametrize("kind,coverage,time_range,count", [
    ("logs", {"complete": False, "available_ranks": [0], "available_sources": ["launcher"],
              "retained_from": "2026-09-30T03:20:00Z", "missing": ["worker stderr for ranks 0-3",
              "launcher history before retained tail", "scheduler termination events"]},
     ["2026-09-30T03:20:00Z", "2026-09-30T03:20:02Z"], 2),
    ("metrics", {"available": False, "note": "Exporter was not enabled for this run."}, None, 0),
])
def test_independent_read_preserves_coverage_in_result_context_and_archive(kind, coverage, time_range, count):
    case_id = "case-insufficient-evidence"
    name = "get_case_" + kind
    args = {"case_id": case_id}
    tools = build_case_registry(load_case(case_id))
    direct = tools.call(name, args)
    assert direct.ok and not direct.truncated
    assert isinstance(direct.data, dict), "read result must include both rows and source telemetry"
    assert direct.data["telemetry"] == coverage
    assert len(direct.data["rows"]) == count
    assert direct.evidence_items[0].metadata["telemetry"] == coverage
    gateway = ScriptedGateway(call(name, args), cited_report)
    rt = build_runtime(replay_settings(), gateway=gateway)
    try:
        result = asyncio.run(rt.investigate(case_id=case_id))
        assert result["phase"] == "completed"
        record = rt.record(result["session_id"])
        assert [entry["tool"] for entry in record.orchestrator.registry.call_log] == [name]
        payload = json.loads(gateway.requests[1][0][-1].content)
        assert payload["data"]["telemetry"] == coverage
        item = payload["evidence_items"][0]
        assert item["metadata"]["telemetry"] == coverage
        assert item["metadata"]["source_note"] == (
            "No signal-sender audit, container termination reason, or node health record was retained.")
        assert item["observed_time_range"] == time_range
        assert item["truncated"] is False
        assert json.loads(item["excerpt"])["telemetry"] == coverage
        assert result["evidence"] == [item]
        assert result["session_id"] not in record.orchestrator.sessions
        assert rt.get_investigation(result["session_id"])["evidence"] == [item]
    finally:
        rt.close()


def test_retained_metric_range_includes_last_sample_without_claiming_full_collection():
    world = load_case("case-gpu-assert")
    world.metrics = [{"ts_start": "2026-09-30T00:00:00Z", "step_s": 30, "values": [1, 2, 3]},
                     {"ts_start": "2026-09-30T00:00:30Z", "step_s": 60, "values": [1, 2]}]
    world.telemetry["metrics"] = {"available": True, "complete": False,
        "retained_from": "2026-09-30T00:00:00Z", "missing": ["samples before collection"]}
    result = build_case_registry(world).call("get_case_metrics", {"case_id": world.case_id})
    assert result.evidence_items is not None
    evidence = result.evidence_items[0]
    assert evidence.observed_time_range == ("2026-09-30T00:00:00Z", "2026-09-30T00:01:30Z")
    assert evidence.metadata["telemetry"] == world.telemetry["metrics"]
    assert result.truncated is False and evidence.truncated is False
