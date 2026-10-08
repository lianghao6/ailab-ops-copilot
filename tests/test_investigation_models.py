"""Contracts for resumable investigation state and deterministic budgets."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from ailab_ops.investigation import (
    Budget,
    Claim,
    Hypothesis,
    InvestigationPhase,
    InvestigationReport,
    InvestigationState,
)


def test_new_investigation_starts_in_intake():
    state = InvestigationState(session_id="session-1", question="Why did job-1 fail?",
                               budget=Budget(20, 100000, None))
    assert state.phase is InvestigationPhase.INTAKE
    assert [phase.value for phase in InvestigationPhase] == [
        "intake", "investigating", "validating", "awaiting_approval", "completed", "stopped"
    ]
    assert state.to_dict()["phase"] == "intake"
    assert state.plan == state.hypotheses == state.evidence_ids == []
    assert state.report is None
    other = InvestigationState(session_id="session-2", question="Another job",
                               budget=Budget(20, 100000, None))
    state.plan.append("Read logs")
    assert other.plan == []
    assert json.loads(json.dumps(state.to_dict()))["session_id"] == "session-1"


def test_budget_reports_each_exhausted_dimension():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    budget = Budget(max_steps=2, max_tokens=10, deadline_at=now)
    assert budget.exhausted(now - timedelta(seconds=1)) == []
    budget.steps_used = 2
    assert budget.exhausted(now - timedelta(seconds=1)) == ["steps"]
    budget.steps_used = 0
    budget.tokens_used = 10
    assert budget.exhausted(now - timedelta(seconds=1)) == ["tokens"]
    budget.tokens_used = 0
    assert budget.exhausted(now) == ["deadline"]
    budget.steps_used = 3
    budget.tokens_used = 11
    assert budget.exhausted(now + timedelta(seconds=1)) == [
        "steps", "tokens", "deadline"
    ]
    assert budget.to_dict() == {
        "max_steps": 2, "max_tokens": 10, "deadline_at": "2026-10-08T00:00:00+00:00",
        "steps_used": 3, "tokens_used": 11,
    }
    assert Budget(2, 10, None).exhausted(now) == []
    assert Budget(0, 0, None).exhausted(now) == ["steps", "tokens"]


def test_hypothesis_tracks_support_and_contradiction_without_duplicates():
    hypothesis = Hypothesis(hypothesis_id="h1", title="GPU memory exhausted")
    for evidence_id in ["e2", "e1", "e2"]:
        hypothesis.add_support(evidence_id)
    for evidence_id in ["e3", "e2", "e3"]:
        hypothesis.add_contradiction(evidence_id)
    assert hypothesis.to_dict() == {
        "hypothesis_id": "h1", "title": "GPU memory exhausted", "confidence": 0.0,
        "supporting_evidence_ids": ["e2", "e1"],
        "contradicting_evidence_ids": ["e3", "e2"],
    }
    assert Hypothesis(hypothesis_id="h2", title="Node failed").supporting_evidence_ids == []


@pytest.mark.parametrize("restore", [False, True], ids=["construct", "restore"])
def test_hypothesis_initial_evidence_ids_are_deduplicated_in_order(restore):
    payload = {
        "hypothesis_id": "h1", "title": "OOM",
        "supporting_evidence_ids": ["e2", "e1", "e2", "e3", "e1"],
        "contradicting_evidence_ids": ["e3", "e2", "e3", "e1", "e2"],
    }
    hypothesis = Hypothesis.from_dict(payload) if restore else Hypothesis(**payload)
    assert hypothesis.supporting_evidence_ids == ["e2", "e1", "e3"]
    assert hypothesis.contradicting_evidence_ids == ["e3", "e2", "e1"]
    assert payload["supporting_evidence_ids"] == ["e2", "e1", "e2", "e3", "e1"]
    assert payload["contradicting_evidence_ids"] == ["e3", "e2", "e3", "e1", "e2"]


def test_state_round_trip_preserves_phase_and_report():
    report = InvestigationReport(
        root_cause="GPU memory exhausted", confidence=0.9, summary="Allocator failed",
        claims=[Claim(text="Allocator reported OOM", evidence_ids=["e1"])],
        ruled_out=["Node failure"],
        unknowns=["Peak allocation"], recommendations=["Reduce batch size"],
    )
    state = InvestigationState(
        session_id="session-1", question="Why did job-1 fail?",
        budget=Budget(max_steps=4, max_tokens=100, steps_used=2, tokens_used=20,
                      deadline_at=datetime(2026, 10, 8, tzinfo=timezone.utc)),
        phase=InvestigationPhase.COMPLETED, plan=["Read logs"],
        hypotheses=[Hypothesis(hypothesis_id="h1", title="OOM", confidence=0.9,
                               supporting_evidence_ids=["e1"])],
        evidence_ids=["e1"], report=report, stop_reason="report_ready", case_id="case-1",
        mode="replay",
    )
    payload = json.loads(json.dumps(state.to_dict()))
    assert payload["report"] == {
        "root_cause": "GPU memory exhausted", "confidence": 0.9, "summary": "Allocator failed",
        "claims": [{"text": "Allocator reported OOM", "evidence_ids": ["e1"], "material": True}],
        "ruled_out": ["Node failure"],
        "unknowns": ["Peak allocation"], "recommendations": ["Reduce batch size"],
    }
    restored = InvestigationState.from_dict(payload)
    assert restored == state
    assert restored.phase is InvestigationPhase.COMPLETED
    assert restored.report.claims[0].evidence_ids == ["e1"]
    restored.plan.append("Review")
    assert state.plan == ["Read logs"]
    empty = InvestigationState.from_dict(json.loads(json.dumps(
        InvestigationState(session_id="empty", question="Investigate",
                           budget=Budget(20, 100000, None)).to_dict()
    )))
    assert empty.phase is InvestigationPhase.INTAKE
    assert empty.report is None
    assert empty.budget.deadline_at is None
    assert empty.case_id is None
    assert empty.mode == "online"
    assert empty.stop_reason is None


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan")])
def test_confidence_rejects_values_outside_unit_interval(confidence):
    with pytest.raises(ValueError, match="confidence"):
        Hypothesis(hypothesis_id="h1", title="OOM", confidence=confidence)
    with pytest.raises(ValueError, match="confidence"):
        InvestigationReport(root_cause="OOM", confidence=confidence, summary="Failure")


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_confidence_accepts_unit_interval_boundaries(confidence):
    assert Hypothesis("h1", "OOM", confidence).confidence == confidence
    assert InvestigationReport("OOM", confidence, "Failure").confidence == confidence
