"""Check published chapter facts against real project behavior and source files."""

from __future__ import annotations

import ast
import importlib
import json
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
