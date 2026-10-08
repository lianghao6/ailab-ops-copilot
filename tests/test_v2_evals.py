"""Layered evaluation catches unsupported answers without hiding good observations."""

from dataclasses import replace
from pathlib import Path
import math

import pytest

from ailab_ops.evidence import Evidence, EvidenceStore
from ailab_ops.investigation.models import (Budget, Claim, InvestigationPhase,
                                           InvestigationReport, InvestigationState)


def fixture():
    store = EvidenceStore()
    observed = store.add(Evidence("read_logs", {"job_id": "j1"}, "Local assertion", "CUDA assert before timeout"))
    state = InvestigationState("s1", "Why?", Budget(10, 100, None, tokens_used=20),
        case_id="case-test", phase=InvestigationPhase.COMPLETED, evidence_ids=[observed.evidence_id],
        report=InvestigationReport("gpu_device_assert", .8, "Failure", [Claim("Local assertion", [observed.evidence_id])]))
    label = {"case_id": "case-test", "root_cause": "gpu_device_assert", "expected_status": "completed",
             "required_tools": ["read_logs", "read_metrics"],
             "required_evidence": [{"source_tool": "read_logs", "excerpt": "CUDA assert"}, "memory samples"],
             "max_latency_ms": 100, "max_tokens": 50}
    return state, store, label


def test_scores_each_layer_independently_and_retains_raw_costs():
    from ailab_ops.evals import score_investigation
    state, store, label = fixture()
    state.report.root_cause = "wrong"
    result = score_investigation(state, label, evidence=store.to_context(state.evidence_ids),
        events=[{"kind": "tool", "tool": "read_logs"}, {"kind": "policy", "outcome": "allow_read"}], latency_ms=25)
    assert result.tool_choice == .5
    assert result.required_evidence == .5
    assert result.citation_validity == 1
    assert result.root_cause == 0
    assert result.abstention == 1
    assert result.policy_compliance == 1
    assert result.latency == 1 and result.token_use == 1
    assert result.latency_ms == 25 and result.tokens_used == 20


@pytest.mark.parametrize("citations", [[], ["ev-forged"], ["DUPLICATE", "DUPLICATE"]])
def test_invalid_citations_do_not_change_correct_root_score(citations):
    from ailab_ops.evals import score_investigation
    state, store, label = fixture()
    state.report.claims[0].evidence_ids = [state.evidence_ids[0] if x == "DUPLICATE" else x for x in citations]
    result = score_investigation(state, label, evidence=store.to_context(state.evidence_ids))
    assert result.citation_validity == 0
    assert result.root_cause == 1


def test_required_evidence_must_be_observed_not_merely_asserted_in_report():
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    state.report.summary = "CUDA assert and memory samples"
    assert score_investigation(state, label, evidence=[]).required_evidence == 0


@pytest.mark.parametrize("root,unknowns,want", [("insufficient_evidence", ["Missing worker logs"], 1),
    ("gpu_device_assert", ["Missing worker logs"], 0), ("insufficient_evidence", [], 0)])
def test_abstention_requires_explicit_uncertainty_and_no_specific_cause(root, unknowns, want):
    from ailab_ops.evals import score_investigation
    state, _, _ = fixture()
    state.report.root_cause = root
    state.report.unknowns = unknowns
    result = score_investigation(state, {"root_cause": None, "expected_status": "insufficient_evidence"})
    assert result.abstention == want
    assert result.root_cause == (1 if root == "insufficient_evidence" else 0)


def test_missing_and_mutated_outputs_fail_explicitly_and_unconfigured_layers_are_not_perfect():
    from ailab_ops.evals import score_investigation
    state, _, _ = fixture()
    state.report = None
    result = score_investigation(state, {"root_cause": "gpu_device_assert"})
    assert result.root_cause == result.citation_validity == result.abstention == 0
    assert result.tool_choice is None and result.required_evidence is None
    assert result.policy_compliance is None and result.latency is None and result.token_use is None
    assert "missing_report" in result.issues
    state.report = InvestigationReport("gpu_device_assert", .8, "", [])
    state.report.claims = "malformed"
    result = score_investigation(state, {"root_cause": "gpu_device_assert"})
    assert result.citation_validity == 0 and "invalid_report" in result.issues


def test_policy_requires_approval_before_execution_and_observed_costs_must_be_finite():
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    executed = {"kind": "approval", "event": "executed", "approval_id": "a1"}
    bad = score_investigation(state, label, events=[executed], latency_ms=math.nan)
    assert bad.policy_compliance == 0 and bad.latency == 0
    good = score_investigation(state, label, events=[
        {"kind": "approval", "event": "approved", "approval_id": "a1"}, executed], latency_ms=200)
    assert good.policy_compliance == 1 and good.latency == 0
    state.budget.tokens_used = -1
    assert score_investigation(state, label).token_use == 0


def test_repeated_runs_use_fresh_factory_and_report_mean_spread_and_failures(tmp_path):
    from ailab_ops.evals import EvaluationSample, run_evaluation
    state, store, label = fixture()
    import json
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps(label) + "\n")
    calls = []
    def factory(case_id):
        calls.append(case_id)
        if len(calls) == 3:
            raise RuntimeError("api_key=never-store-this")
        report = replace(state.report, root_cause="wrong" if len(calls) == 2 else "gpu_device_assert")
        return EvaluationSample(replace(state, report=report), store.to_context(state.evidence_ids),
                                [{"kind": "policy", "outcome": "allow_read"}], 10)
    result = run_evaluation(["case-test"], 3, factory, labels_path=path)
    assert calls == ["case-test"] * 3
    stats = result.summary["root_cause"]
    assert stats.mean == pytest.approx(1 / 3) and stats.spread == 1 and stats.count == 3
    assert stats.minimum == 0 and stats.maximum == 1
    assert result.by_case["case-test"]["root_cause"] == stats
    assert result.summary["latency_ms"].mean == 10 and result.summary["latency_ms"].count == 2
    assert result.summary["tokens_used"].mean == 20 and result.summary["tokens_used"].spread == 0
    assert result.runs[-1].issues == ["factory_failed:RuntimeError"]
    assert "never-store-this" not in str(result.to_dict())
    with pytest.raises(ValueError, match="positive"):
        run_evaluation(["case-test"], 0, factory, labels_path=path)
    with pytest.raises(KeyError, match="label"):
        run_evaluation(["case-missing"], 1, factory, labels_path=path)


def test_only_evals_exposes_hidden_label_loading():
    from ailab_ops import cases
    from ailab_ops.cases import loader
    from ailab_ops.evals import load_eval_labels
    assert not hasattr(cases, "load_eval_labels")
    assert not hasattr(loader, "load_eval_labels")
    labels = load_eval_labels(Path(__file__).resolve().parents[1] / "data/v2/evals/labels.jsonl")
    assert labels["case-gpu-assert"]["root_cause"] == "gpu_device_assert"


def test_malformed_events_cannot_erase_valid_evidence_or_fail_open_policy():
    from ailab_ops.evals import score_investigation
    state, store, label = fixture()
    result = score_investigation(state, label, evidence=store.to_context(state.evidence_ids), events=[object()])
    assert result.required_evidence == .5 and result.tool_choice == .5
    assert result.policy_compliance == 0 and "invalid_events" in result.issues
    result = score_investigation(state, label, events=[{"kind": "policy"}])
    assert result.policy_compliance == 0


def test_other_session_approval_cannot_authorize_current_execution():
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    result = score_investigation(state, label, events=[
        {"session_id": "other", "kind": "approval", "event": "approved", "approval_id": "a1"},
        {"session_id": "s1", "kind": "approval", "event": "executed", "approval_id": "a1"}])
    assert result.policy_compliance == 0


@pytest.mark.parametrize("unknowns", ["Missing logs", [""], [42]])
def test_malformed_uncertainty_does_not_count_as_abstention(unknowns):
    from ailab_ops.evals import score_investigation
    state, _, _ = fixture()
    state.report.root_cause = "insufficient_evidence"
    state.report.unknowns = unknowns
    assert score_investigation(state, {"root_cause": None}).abstention == 0


def test_static_dependency_boundary_keeps_label_reader_out_of_runtime_packages():
    import ast
    root = Path(__file__).resolve().parents[1] / "src/ailab_ops"
    for package in ("cases", "models", "investigation", "tools", "policy", "approvals"):
        for path in (root / package).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("ailab_ops.evals"), path
                    assert all(alias.name != "load_eval_labels" for alias in node.names), path
                elif isinstance(node, ast.Import):
                    assert all(not alias.name.startswith("ailab_ops.evals") for alias in node.names), path


def test_empty_root_is_invalid_rather_than_an_abstention():
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    state.report.root_cause = " "
    state.report.unknowns = ["Missing logs"]
    result = score_investigation(state, {"root_cause": None})
    assert result.root_cause == result.abstention == 0


def test_malformed_tool_metadata_scores_zero_instead_of_crashing():
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    result = score_investigation(state, label, events=[{"kind": "tool", "tool": ["read_logs"]}])
    assert result.tool_choice == 0 and "invalid_tool_metadata" in result.issues


@pytest.mark.parametrize("event", [
    {"kind": "approval"}, {"kind": "approval", "event": "invented", "approval_id": "a1"},
    {"kind": "approval", "event": "approved", "approval_id": None},
    {"kind": "approval", "event": "approved", "approval_id": " "},
    {"kind": "approval", "event": "approved", "approval_id": 42},
    {"kind": "approval", "event": "executed", "approval_id": False},
    {"kind": "approval", "event": "created", "approval_id": []},
])
def test_malformed_approval_schema_scores_zero_with_issue(event):
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    result = score_investigation(state, label, events=[event])
    assert result.policy_compliance == 0
    assert "invalid_approval_event" in result.issues


@pytest.mark.parametrize("name", ["tracing_failed", "notification_failed"])
def test_diagnostic_approval_events_alone_cannot_prove_policy_compliance(name):
    from ailab_ops.evals import score_investigation
    state, _, label = fixture()
    result = score_investigation(state, label, events=[{"kind": "approval", "event": name}])
    assert result.policy_compliance is None


@pytest.mark.parametrize("fault", ["budget", "evidence", "event", "latency"])
def test_malformed_scoring_inputs_preserve_other_verifiable_dimensions(fault):
    from ailab_ops.evals import score_investigation
    state, store, label = fixture()
    class BadRecord:
        def to_dict(self):
            return None
    evidence = store.to_context(state.evidence_ids)
    events = [{"kind": "policy", "outcome": "allow_read"}]
    latency = 25
    if fault == "budget":
        state.budget = None
    elif fault == "evidence":
        evidence = [BadRecord()]
    elif fault == "event":
        events = [BadRecord()]
    else:
        latency = 10 ** 1000
    result = score_investigation(state, label, evidence=evidence, events=events, latency_ms=latency)
    assert result.root_cause == result.abstention == 1
    if fault != "evidence":
        assert result.required_evidence == .5 and result.citation_validity == 1
    if fault != "event":
        assert result.policy_compliance == 1
    if fault == "budget":
        assert result.token_use == 0 and "missing_or_invalid_token_use" in result.issues
    if fault == "latency":
        assert result.latency == 0 and "missing_or_invalid_latency" in result.issues


def test_repeats_do_not_report_factory_failure_for_invalid_scoring_inputs(tmp_path):
    import json
    from ailab_ops.evals import EvaluationSample, run_evaluation
    state, store, label = fixture()
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps(label) + "\n")
    evidence = store.to_context(state.evidence_ids)
    state.budget = None
    result = run_evaluation(["case-test"], 2, lambda case_id: EvaluationSample(state, evidence,
        [{"kind": "policy", "outcome": "allow_read"}], 10 ** 1000), labels_path=path)
    assert result.summary["root_cause"].mean == 1 and result.summary["citation_validity"].mean == 1
    assert result.summary["token_use"].mean == result.summary["latency"].mean == 0
    assert all(not any(issue.startswith("factory_failed") for issue in run.issues) for run in result.runs)


def test_unexpected_scoring_failure_is_distinct_from_factory_failure(tmp_path, monkeypatch):
    import json
    from ailab_ops.evals import runner
    state, _, label = fixture()
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps(label) + "\n")
    def broken_scoring(*args, **kwargs):
        raise RuntimeError("api_key=do-not-record")
    monkeypatch.setattr(runner, "score_investigation", broken_scoring)
    result = runner.run_evaluation(["case-test"], 1, lambda case_id: state, labels_path=path)
    assert result.runs[0].issues == ["scoring_failed:RuntimeError"]
    assert "do-not-record" not in str(result.to_dict())
