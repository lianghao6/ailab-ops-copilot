"""Observed signals can reach independent V2 knowledge and cited reports."""

import asyncio
import json

import pytest

from ailab_ops.cases.loader import load_case
from ailab_ops.config import PROJECT_ROOT
from ailab_ops.runtime import build_runtime
from ailab_ops.tools.cases import build_case_registry
from test_orchestrator import ScriptedGateway, call, control
from test_v2_runtime import replay_settings


def test_default_read_tool_searches_v2_runbooks_with_stable_source_locations():
    registry = build_case_registry(load_case("case-gpu-assert"))
    tool = registry.get("search_runbooks")
    assert tool is not None, "V2 must expose its independent Runbooks to the model"
    assert tool.kind == "read"
    result = registry.call("search_runbooks", {"query": "CUDA device assertion", "top_k": 1})
    assert result.ok and len(result.data["matches"]) == 1
    hit = result.data["matches"][0]
    assert hit["doc_id"] == "runbook:gpu-assert"
    assert hit["source"] == "data/v2/knowledge/runbooks/gpu-assert.md"
    assert hit["line_start"] == 1 and hit["line_end"] > 1
    assert hit["excerpt"] == (PROJECT_ROOT / hit["source"]).read_text()
    evidence = result.evidence_items[0]
    assert evidence.metadata["doc_id"] == hit["doc_id"]
    assert evidence.metadata["source"] == hit["source"]
    assert evidence.excerpt == hit["excerpt"]
    again = registry.call("search_runbooks", {"query": "CUDA device assertion", "top_k": 1})
    assert again.data == result.data


def test_explicit_runbook_directory_never_falls_back_to_legacy_or_default_knowledge(tmp_path):
    (tmp_path / "separate.md").write_text("# Isolated operational note\n\nOnly quuxsentinel is described here.\n")
    registry = build_case_registry(load_case("case-gpu-assert"), knowledge_root=tmp_path)
    found = registry.call("search_runbooks", {"query": "quuxsentinel"})
    assert found.ok
    assert [hit["doc_id"] for hit in found.data["matches"]] == ["runbook:separate"]
    absent = registry.call("search_runbooks", {"query": "CUDA"})
    assert absent.ok and absent.data["matches"] == [] and absent.evidence_items == []


def test_large_runbook_excerpt_is_bounded_and_marks_truncation_at_source(tmp_path):
    (tmp_path / "large.md").write_text("# Large source\n\n" + "quuxsentinel observation\n" * 500)
    registry = build_case_registry(load_case("case-gpu-assert"), knowledge_root=tmp_path)
    result = registry.call("search_runbooks", {"query": "quuxsentinel"})
    assert result.ok and result.truncated
    evidence = result.evidence_items[0]
    assert evidence.truncated and len(evidence.excerpt) <= 4000
    assert evidence.metadata["line_end"] == len(evidence.excerpt.splitlines())


def test_observed_log_drives_query_then_runbook_evidence_is_cited_and_archived():
    observed_query = []

    def search_after_observation(messages):
        payload = json.loads(messages[-1].content)
        query = next(row["message"] for row in payload["data"]["rows"] if "CUDA error" in row["message"])
        observed_query.append(query)
        return call("search_runbooks", {"query": query, "top_k": 1}, "knowledge-1")

    def cite_runbook(messages):
        results = [json.loads(message.content) for message in messages if message.role == "tool"]
        assert results[-1]["ok"], results[-1]
        book = results[-1]["evidence_items"][0]
        assert book["metadata"]["doc_id"] == "runbook:gpu-assert"
        assert "asynchronously" in book["excerpt"]
        return control("report", report={"root_cause": "local device assertion", "confidence": 0.8,
            "summary": "A device assertion is observed; its precise origin remains unproven.",
            "claims": [{"text": "The CUDA error is asynchronous; preserve original inputs for reproduction.",
                        "evidence_ids": [results[0]["evidence_items"][0]["evidence_id"], book["evidence_id"]]}],
            "unknowns": ["The specific input or kernel defect"]})

    gateway = ScriptedGateway(call("get_case_logs", {"case_id": "case-gpu-assert"}),
                              search_after_observation, cite_runbook)
    rt = build_runtime(replay_settings(), gateway=gateway)
    try:
        result = asyncio.run(rt.investigate(case_id="case-gpu-assert"))
        assert result["phase"] == "completed", result["stop_reason"]
        record = rt.record(result["session_id"])
        calls = record.orchestrator.registry.call_log
        assert [entry["tool"] for entry in calls] == ["get_case_logs", "search_runbooks"]
        assert calls[1]["arguments"]["query"] == observed_query[0]
        assert all("search_runbooks" in [tool.name for tool in request[1]] for request in gateway.requests)
        book = next(item for item in result["evidence"] if item["source_tool"] == "search_runbooks")
        assert book["evidence_id"] in result["report"]["claims"][0]["evidence_ids"]
        assert book["metadata"]["source"] == "data/v2/knowledge/runbooks/gpu-assert.md"
        assert record.orchestrator.get_evidence(result["session_id"], book["evidence_id"]).to_dict() == book
    finally:
        rt.close()


def test_unmatched_knowledge_query_does_not_invent_a_document_observation():
    rt = build_runtime(replay_settings(), gateway=ScriptedGateway(
        call("search_runbooks", {"query": "quuxnomatchsentinel"})))
    try:
        result = asyncio.run(rt.investigate(case_id="case-gpu-assert", max_steps=1))
        registry = rt.record(result["session_id"]).orchestrator.registry
        assert registry.call_log and registry.call_log[0]["ok"]
        assert result["evidence"] == []
    finally:
        rt.close()


@pytest.mark.parametrize("case_id", ["case-gpu-assert", "case-collective-timeout", "case-insufficient-evidence"])
def test_strict_authored_replay_observes_then_retrieves_and_cites_knowledge(case_id):
    rt = build_runtime(replay_settings())
    try:
        result = asyncio.run(rt.investigate(case_id=case_id))
        assert result["phase"] == "completed"
        calls = rt.record(result["session_id"]).orchestrator.registry.call_log
        names = [entry["tool"] for entry in calls]
        assert "search_runbooks" in names
        assert names.index("get_case_logs") < names.index("search_runbooks")
        query = calls[names.index("search_runbooks")]["arguments"]["query"]
        assert any(row["message"] in query for row in load_case(case_id).logs)
        knowledge_ids = {item["evidence_id"] for item in result["evidence"] if item["source_tool"] == "search_runbooks"}
        assert knowledge_ids
        citations = {eid for claim in result["report"]["claims"] for eid in claim["evidence_ids"]}
        assert knowledge_ids <= citations
        missed = asyncio.run(rt.investigate(case_id=case_id, question="Different unrecorded knowledge question"))
        assert missed["error"]["kind"] == "replay_miss"
    finally:
        rt.close()
