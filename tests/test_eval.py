"""The evaluation harness.

Two things are being tested: that the harness computes the metrics it claims to
compute, and that the agent clears a floor on the dataset. The second is the
more valuable one -- it is the project's definition of "working" -- so it is
asserted with a margin rather than exactly.
"""

from __future__ import annotations

import json

import pytest

from ailab_ops.eval import EvalReport, build_cases, run_eval, write_report


def test_build_cases_is_stratified_by_difficulty(runtime):
    """Sampling uniformly would over-represent easy scenarios, which are simply
    more common, and produce a flattering number. Each stratum must appear."""
    cases = build_cases(runtime, limit=30)
    assert cases
    difficulties = {c["truth_difficulty"] for c in cases}
    assert len(difficulties) >= 2, f"only one difficulty stratum was sampled: {difficulties}"
    for c in cases:
        assert c["job_id"] and c["question"] and c["truth"]
        assert c["truth"] in runtime.world.playbook.scenarios


def test_build_cases_can_exclude_the_unknowable(runtime):
    cases = build_cases(runtime, limit=40, include_unknown=False)
    assert all(not c["is_unknown_case"] for c in cases)


def test_build_cases_is_deterministic_for_a_seed(runtime):
    a = [c["job_id"] for c in build_cases(runtime, limit=20, seed=3)]
    b = [c["job_id"] for c in build_cases(runtime, limit=20, seed=3)]
    assert a == b


def test_evaluation_produces_a_complete_report(runtime):
    report = run_eval(runtime, build_cases(runtime, limit=40), progress=False)
    assert isinstance(report, EvalReport)
    assert report.n == 40
    assert 0.0 <= report.accuracy <= 1.0
    assert report.by_category and report.by_difficulty
    assert report.cost["total_tokens"] > 0
    assert report.latency["p50"] >= 0
    # The configuration must be recorded, or the number is unreproducible.
    assert report.config["seed"] == runtime.world.seed
    assert report.config["llm_backend"] == runtime.settings.llm_backend
    assert "retrieval" in report.config


def test_report_renders_without_crashing(runtime):
    report = run_eval(runtime, build_cases(runtime, limit=20), progress=False)
    lines = report.summary_lines()
    assert any("exact accuracy" in line for line in lines)
    assert any("abstention precision" in line for line in lines)
    assert any("false-confidence rate" in line for line in lines)
    assert any("how to read this" in line for line in lines)


def test_evaluation_meets_a_quality_floor(runtime):
    """The project's definition of working.

    The floor is deliberately below the current score so that a genuine
    regression fails the build while a small fluctuation does not. It also
    exercises the abstention metrics, which are the ones that would otherwise
    be silently ignored.
    """
    report = run_eval(runtime, build_cases(runtime, limit=80), progress=False)
    assert report.accuracy >= 0.80, (
        f"accuracy {report.accuracy:.1%} is below the floor; look at the confusion pairs "
        f"in the report for the two causes being mixed up"
    )
    assert report.parse_rate >= 0.95, "answers are not reliably parseable"
    assert report.false_confidence_rate <= 0.10, (
        f"{report.false_confidence_rate:.1%} of answers are wrong AND confident; that is a "
        f"trust problem, not an accuracy problem"
    )
    assert report.abstention_precision is not None
    assert report.abstention_precision >= 0.5, (
        "many of the agent's refusals were on cases it could have answered"
    )
    # Hard cases must not collapse entirely: a large easy/hard gap means the
    # system pattern-matches rather than reasons.
    hard = report.by_difficulty.get("hard")
    if hard:
        assert hard["accuracy"] >= 0.5, f"hard-case accuracy is only {hard['accuracy']:.0%}"


def test_the_unknowable_cases_are_actually_refused(runtime):
    """A system that never abstains scores well on accuracy and badly here. If
    this metric is not measured, that failure is invisible."""
    cases = [c for c in build_cases(runtime, limit=200) if c["is_unknown_case"]][:12]
    if len(cases) < 4:
        pytest.skip("not enough undecidable cases in this sample")
    report = run_eval(runtime, cases, progress=False)
    assert report.unknown_recall is not None
    assert report.unknown_recall >= 0.5, (
        f"the agent guessed on {1 - report.unknown_recall:.0%} of cases whose correct answer "
        f"was 'insufficient evidence'"
    )


def test_write_report_round_trips(runtime, tmp_path):
    report = run_eval(runtime, build_cases(runtime, limit=10), progress=False)
    path = write_report(report, tmp_path / "report.json")
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["n"] == report.n
    assert "by_difficulty" in data
    assert "config" in data
