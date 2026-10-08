"""Check published chapter facts against real project behavior and source files."""

from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
COURSE = ROOT / "docs" / "course"
sys.path.insert(0, str(COURSE))
deck = importlib.import_module("deck")


@pytest.fixture(scope="module")
def chapter_one(tmp_path_factory):
    module = importlib.import_module("lessons.l1")
    path = deck.build(module.LESSON, tmp_path_factory.mktemp("chapter-one") / "lesson-1.pdf")
    reader = PdfReader(path)
    return module, reader, "\n".join(page.extract_text() for page in reader.pages)


def test_chapter_one_publishes_v2_reader_route_and_honest_replay_boundary(chapter_one):
    _, reader, text = chapter_one
    assert reader.metadata.title == "第 1 课 · 从 LLM 到 Agent"
    for anchor in ("case-gpu-assert", "job-v2-101", "authored replay", "人工编写回放",
                   "上下文", "结构化输出", "工具调用", "最小循环", "课后阅读"):
        assert anchor in text
    for stale_claim in ("口语化讲稿", "小面试", "下节课开始写代码", "kill the model service",
                        "手写一个 agent loop", "HYPOTHESIS SCORES", "不调模型一样能出诊断"):
        assert stale_claim not in text


def test_chapter_one_sources_and_snippets_resolve_to_current_code(chapter_one):
    module, _, text = chapter_one
    sources = [b for b in module.LESSON.blocks if isinstance(b, deck.Source)]
    assert sources, "a project reader must locate its actual V2 source"
    for source in sources:
        path = ROOT / source.path
        assert path.is_file(), source.path
        assert source.path in text
        if path.suffix == ".py" and source.symbol:
            names = {node.name for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            assert source.symbol.split(".")[-1] in names, source
    for index, block in enumerate(module.LESSON.blocks[:-1]):
        if isinstance(block, deck.Code) and block.caption.startswith("源码摘录"):
            source = module.LESSON.blocks[index + 1]
            assert isinstance(source, deck.Source), block.caption
            excerpt = textwrap.dedent(block.text).strip()
            current = (ROOT / source.path).read_text()
            # Ignore indentation introduced by a surrounding class/function,
            # while still detecting stale fields, arguments or branches.
            expected_lines = [line.strip() for line in excerpt.splitlines()]
            source_lines = [line.strip() for line in current.splitlines()]
            assert any(source_lines[i:i + len(expected_lines)] == expected_lines
                       for i in range(len(source_lines))), block.caption


def test_chapter_one_documented_replay_result_is_observed_not_invented(chapter_one):
    module, _, text = chapter_one
    assert hasattr(module, "REPLAY_OBSERVATION"), "chapter needs traceable replay measurements"
    result = subprocess.run([sys.executable, "-m", "ailab_ops.cli", "investigate",
                             "--case", "case-gpu-assert", "--mode", "replay"],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    actual = {"mode": observed["mode"], "phase": observed["phase"],
              "steps_used": observed["budget"]["steps_used"],
              "tokens_used": observed["budget"]["tokens_used"],
              "evidence_count": len(observed["evidence"]),
              "root_cause": observed["report"]["root_cause"],
              "confidence": observed["report"]["confidence"],
              "tool_calls": [event["tool"] for event in observed["timeline"]["events"]
                             if event["kind"] == "tool" and event["event"] == "result"]}
    assert actual == module.REPLAY_OBSERVATION
    assert "预置" in text and "在线" in text


def test_chapter_one_preserves_case_and_diagrams_in_student_pdf(chapter_one):
    module, _, text = chapter_one
    compact_text = re.sub(r"\s+", "", text)
    assert any(isinstance(block, deck.SequenceDiagram) for block in module.LESSON.blocks)
    assert any(isinstance(block, deck.Diagram) for block in module.LESSON.blocks)
    assert "Indexing.cu" in text and "srcIndex<srcSelectDimSize" in compact_text
    assert "600" in text and "01:01:00" in text and "01:11:00" in text
    assert "Theoffendinginputandprecisekerneloriginremainunproven." in compact_text
    assert "data/v2/replays/investigations.jsonl" in text


@pytest.fixture(scope="module")
def chapter_two(tmp_path_factory):
    module = importlib.import_module("lessons.l2")
    path = deck.build(module.LESSON, tmp_path_factory.mktemp("chapter-two") / "lesson-2.pdf")
    reader = PdfReader(path)
    return module, reader, "\n".join(page.extract_text() for page in reader.pages)


def test_chapter_two_publishes_control_contract_and_current_phase_names(chapter_two):
    from ailab_ops.investigation.models import InvestigationPhase
    module, reader, text = chapter_two
    assert reader.metadata.title == "第 2 课 · 让调查过程可控"
    for phase in InvestigationPhase:
        assert phase.value in text
    for field in ("max_steps", "max_tokens", "deadline_at", "steps_used", "tokens_used"):
        assert field in text
    for concept in ("动态计划", "停止条件", "duplicate_call", "幂等", "错误回喂",
                    "Retry-After", "上下文", "证据索引", "进程重启", "人工编写回放", "课后阅读"):
        assert concept in text
    assert any(isinstance(block, deck.Diagram) for block in module.LESSON.blocks)
    assert any(isinstance(block, deck.SequenceDiagram) for block in module.LESSON.blocks)
    for stale in ("口语化讲稿", "小面试", "实操 60", "亲手拆掉", "src/ailab_ops/agent/loop.py"):
        assert stale not in text


def test_chapter_two_source_excerpts_match_executable_v2_code(chapter_two):
    module, _, text = chapter_two
    for source in (b for b in module.LESSON.blocks if isinstance(b, deck.Source)):
        path = ROOT / source.path
        assert path.is_file(), source.path
        assert source.path in text
        if path.suffix == ".py" and source.symbol:
            names = {node.name for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            assert source.symbol.split(".")[-1] in names, source
    for index, block in enumerate(module.LESSON.blocks[:-1]):
        if isinstance(block, deck.Code) and block.caption.startswith("源码摘录"):
            source = module.LESSON.blocks[index + 1]
            assert isinstance(source, deck.Source)
            expected = [line.strip() for line in block.text.strip().splitlines()]
            actual = [line.strip() for line in (ROOT / source.path).read_text().splitlines()]
            assert any(actual[i:i + len(expected)] == expected for i in range(len(actual))), block.caption


@pytest.mark.parametrize("name,args,exit_code", [
    ("steps", ["--max-steps", "2"], 2),
    ("tokens", ["--max-tokens", "200"], 2),
    ("completed", [], 0),
])
def test_chapter_two_budget_measurements_match_real_replay(chapter_two, name, args, exit_code):
    module, _, _ = chapter_two
    assert hasattr(module, "BUDGET_OBSERVATIONS"), "budget examples must be reproducible observations"
    result = subprocess.run([sys.executable, "-m", "ailab_ops.cli", "investigate", "--case",
                             "case-gpu-assert", "--mode", "replay", *args], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    observed = json.loads(result.stdout)
    actual = {"phase": observed["phase"], "stop_reason": observed["stop_reason"],
              "steps_used": observed["budget"]["steps_used"],
              "tokens_used": observed["budget"]["tokens_used"],
              "evidence_count": len(observed["evidence"]),
              "has_report": observed["report"] is not None}
    assert actual == module.BUDGET_OBSERVATIONS[name]


@pytest.fixture(scope="module")
def chapter_three(tmp_path_factory):
    module = importlib.import_module("lessons.l3")
    path = deck.build(module.LESSON, tmp_path_factory.mktemp("chapter-three") / "lesson-3.pdf")
    reader = PdfReader(path)
    return module, reader, "\n".join(page.extract_text() for page in reader.pages)


def test_chapter_three_publishes_v2_evidence_route_and_scope(chapter_three):
    module, reader, text = chapter_three
    assert reader.metadata.title == "第 3 课 · RAG、证据链与可信回答"
    for concept in ("观察后检索", "重排", "证据不足", "人工编写回放", "authored replay",
                    "词法", "向量", "混合", "主张", "语义", "V1 基线", "生成日志",
                    "truncated", "complete", "missing_citation", "unknown_evidence",
                    "duplicate_citation", "课后阅读"):
        assert concept in text
    for stale in ("口语化讲稿", "小面试", "实操 1", "286 条真实失败日志", "等权融合一定变差"):
        assert stale not in text
    assert any(isinstance(b, deck.Diagram) for b in module.LESSON.blocks)


def test_chapter_three_source_excerpts_resolve_to_real_code(chapter_three):
    module, _, text = chapter_three
    for source in (b for b in module.LESSON.blocks if isinstance(b, deck.Source)):
        path = ROOT / source.path
        assert path.is_file(), source.path
        assert source.path in text
        if path.suffix == ".py" and source.symbol:
            names = {n.name for n in ast.walk(ast.parse(path.read_text()))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            assert source.symbol.split(".")[-1] in names, source
    for i, block in enumerate(module.LESSON.blocks[:-1]):
        if isinstance(block, deck.Code) and block.caption.startswith("源码摘录"):
            source = module.LESSON.blocks[i + 1]
            assert isinstance(source, deck.Source)
            expected = [line.strip() for line in block.text.strip().splitlines()]
            actual = [line.strip() for line in (ROOT / source.path).read_text().splitlines()]
            assert any(actual[j:j + len(expected)] == expected for j in range(len(actual))), block.caption


@pytest.mark.parametrize("case_id", ["case-gpu-assert", "case-insufficient-evidence"])
def test_chapter_three_replay_evidence_and_refusal_are_observed(chapter_three, case_id):
    module, _, _ = chapter_three
    assert hasattr(module, "REPLAY_OBSERVATIONS")
    result = subprocess.run([sys.executable, "-m", "ailab_ops.cli", "investigate", "--case",
                             case_id, "--mode", "replay"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    o = json.loads(result.stdout)
    actual = {"mode": o["mode"], "phase": o["phase"], "steps_used": o["budget"]["steps_used"],
              "tokens_used": o["budget"]["tokens_used"], "evidence_count": len(o["evidence"]),
              "root_cause": o["report"]["root_cause"], "confidence": o["report"]["confidence"],
              "material_claims": sum(c["material"] for c in o["report"]["claims"]),
              "unknown_count": len(o["report"]["unknowns"]),
              "evidence_tools": [e["source_tool"] for e in o["evidence"]]}
    assert actual == module.REPLAY_OBSERVATIONS[case_id]


def test_chapter_three_query_measurements_match_current_runbook_tool(chapter_three):
    from ailab_ops.tools.runbooks import build_runbook_tool
    module, _, _ = chapter_three
    assert hasattr(module, "QUERY_OBSERVATIONS")
    tool = build_runbook_tool()
    for expected in module.QUERY_OBSERVATIONS:
        result = tool.fn(query=expected["query"], top_k=3)
        actual = {"query": expected["query"], "total_matches": result.data["total_matches"],
                  "matches": [{"doc_id": m["doc_id"], "score": m["score"],
                               "truncated": m["truncated"]} for m in result.data["matches"]]}
        assert actual == expected


def test_chapter_three_v1_baseline_sweep_is_reproducible_and_labelled(chapter_three):
    module, _, text = chapter_three
    assert hasattr(module, "V1_BASELINE")
    # V1 equal-score candidates inherit set order. Fix hash seed in a fresh
    # process; changing os.environ in this interpreter does not reset hashing.
    script = textwrap.dedent('''
        import json
        from ailab_ops.datagen.taxonomy import load_playbook
        from ailab_ops.legacy.runtime import build_legacy_runtime
        from ailab_ops.rag import build_knowledge_base
        from ailab_ops.signals import extract_log_evidence
        world = build_legacy_runtime().world
        pb = load_playbook()
        queries = []
        for job in world.jobs:
            if job.status == "SUCCEEDED" or not job.root_cause or job.is_insufficient_evidence:
                continue
            q = (extract_log_evidence(world.logs_for(job.job_id), pb).first_error or "")[:200]
            if q:
                queries.append((q, job.root_cause))
        kb = build_knowledge_base(pb)
        results = []
        for weight in [1.0, 0.0, 0.5, 0.7, 0.85, 0.9]:
            kb.retriever.lexical_weight = weight
            count = sum(kb.retriever.search(q, top_k=1).hits[0].doc.metadata.get("scenario") == truth
                        for q, truth in queries)
            results.append({"lexical_weight": weight, "correct_top1": count})
        print(json.dumps({"n_queries": len(queries), "n_docs": len(kb.docs), "results": results}))
    ''')
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True,
                            text=True, env={**os.environ, "PYTHONHASHSEED": "0"})
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual == {k: module.V1_BASELINE[k] for k in ("n_queries", "n_docs", "results")}
    assert "faults.yaml" in text and "seed=20260929" in text
    assert "PYTHONHASHSEED=0" in text and "同分" in text
    assert "不是 V2" in text and "共享" in text


@pytest.fixture(scope="module")
def chapter_four(tmp_path_factory):
    module = importlib.import_module("lessons.l4")
    path = deck.build(module.LESSON, tmp_path_factory.mktemp("chapter-four") / "lesson-4.pdf")
    reader = PdfReader(path)
    return module, reader, "\n".join(page.extract_text() for page in reader.pages)


def test_chapter_four_publishes_current_security_contract(chapter_four):
    module, reader, text = chapter_four
    assert reader.metadata.title == "第 4 课 · 安全边界与人工审批"
    for concept in ("最小权限", "提示注入", "人工编写回放", "authored replay", "敏感字段",
                    "allow_read", "require_approval", "deny", "annotate_incident",
                    "reason", "risk", "rollback", "evidence_ids", "expires_at",
                    "approved_by", "execution_replayed", "approval_expired", "课后阅读"):
        assert concept in text
    for stale in ("口语化讲稿", "小面试", "实操 1", "kill the model service", "不调模型一样能出诊断"):
        assert stale not in text
    assert any(isinstance(b, deck.SequenceDiagram) for b in module.LESSON.blocks)
    assert any(isinstance(b, deck.Diagram) for b in module.LESSON.blocks)
    assert "没有独立" in text and "预期影响" in text
    assert "未经认证" in text and "进程" in text and "模拟" in text


def test_chapter_four_source_excerpts_match_current_code(chapter_four):
    module, _, text = chapter_four
    for source in (b for b in module.LESSON.blocks if isinstance(b, deck.Source)):
        path = ROOT / source.path
        assert path.is_file(), source.path
        assert source.path in text
        if path.suffix == ".py" and source.symbol:
            names = {n.name for n in ast.walk(ast.parse(path.read_text()))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            assert source.symbol.split(".")[-1] in names, source
    for i, block in enumerate(module.LESSON.blocks[:-1]):
        if isinstance(block, deck.Code) and block.caption.startswith("源码摘录"):
            source = module.LESSON.blocks[i + 1]
            assert isinstance(source, deck.Source)
            expected = [line.strip() for line in block.text.strip().splitlines()]
            actual = [line.strip() for line in (ROOT / source.path).read_text().splitlines()]
            assert any(actual[j:j + len(expected)] == expected for j in range(len(actual))), block.caption


@pytest.mark.parametrize("decision", ["execute", "reject", "expire"])
def test_chapter_four_documented_approval_lifecycle_matches_real_api(chapter_four, decision):
    from datetime import datetime
    from fastapi.testclient import TestClient
    from ailab_ops.config import Settings
    from ailab_ops.runtime import build_runtime
    from ailab_ops.serving.app import create_app
    module, _, _ = chapter_four
    assert hasattr(module, "APPROVAL_OBSERVATIONS"), "approval numbers need reproducible provenance"
    rt = build_runtime(Settings(model_mode="replay", llm_api_key="", user_qps=0))
    with TestClient(create_app(rt)) as client:
        initial = client.post("/v2/investigations", json={"case_id": "case-gpu-assert"}).json()
        path = "/v2/investigations/" + initial["session_id"]
        proposal = {**module.ACTION_PROPOSAL, "evidence_ids": initial["evidence_ids"]}
        request = client.post(path + "/approvals", json={"proposal": proposal}).json()
        apath = "/v2/approvals/" + request["request_id"]
        preapproval = client.post(apath + "/execute", json={"actor": "operator"})
        approved_phase = None
        result = None
        replay_equal = None
        if decision == "execute":
            approved = client.post(apath + "/approve", json={"actor": "reviewer"})
            assert approved.status_code == 200
            approved_phase = client.get(path).json()["phase"]
            result = client.post(apath + "/execute", json={"actor": "operator"}).json()
            replay_equal = result == client.post(apath + "/execute", json={"actor": "operator"}).json()
        elif decision == "reject":
            assert client.post(apath + "/reject", json={"actor": "reviewer", "reason": "Need input evidence"}).status_code == 200
        else:
            service = rt.approval_service(request["request_id"])
            deadline = datetime.fromisoformat(request["expires_at"])
            service._now = lambda: deadline
        final = client.get(path).json()
        actual = {"initial_phase": initial["phase"], "evidence_count": len(initial["evidence"]),
                  "pending_status": request["status"], "before_approval_http": preapproval.status_code,
                  "approved_phase": approved_phase, "final_status": final["approvals"][0]["status"],
                  "final_phase": final["phase"], "stop_reason": final["stop_reason"],
                  "result": result, "duplicate_result_equal": replay_equal,
                  "approval_events": [e["event"] for e in final["timeline"]["events"] if e["kind"] == "approval"]}
        assert actual == module.APPROVAL_OBSERVATIONS[decision]
        assert final["report"] == initial["report"]
        assert rt.record(initial["session_id"]).orchestrator.registry.get("annotate_incident").calls == 0


def test_chapter_four_redaction_observation_is_real_presentation_boundary(chapter_four):
    from ailab_ops.config import Settings
    from ailab_ops.runtime import build_runtime
    module, _, _ = chapter_four
    assert hasattr(module, "REDACTION_OBSERVATION")
    rt = build_runtime(Settings(model_mode="replay", llm_api_key="", user_qps=0))
    try:
        actual = rt.present({"api_key": "demo-key", "reasoning_content": "private notes",
                             "note": "Authorization: Bearer demo-key", "tokens_used": 1000})
        assert actual == module.REDACTION_OBSERVATION
    finally:
        rt.close()
