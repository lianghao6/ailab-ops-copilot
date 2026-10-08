"""Typed, JSON-serializable state for an evidence-based investigation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class InvestigationPhase(str, Enum):
    INTAKE = "intake"
    INVESTIGATING = "investigating"
    VALIDATING = "validating"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    STOPPED = "stopped"


@dataclass
class Budget:
    max_steps: int
    max_tokens: int
    deadline_at: datetime | None
    steps_used: int = 0
    tokens_used: int = 0

    def exhausted(self, now: datetime) -> list[str]:
        """Return every reached limit in steps, tokens, deadline order."""
        limits = []
        if self.steps_used >= self.max_steps:
            limits.append("steps")
        if self.tokens_used >= self.max_tokens:
            limits.append("tokens")
        if self.deadline_at is not None and now >= self.deadline_at:
            limits.append("deadline")
        return limits

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "steps_used": self.steps_used,
            "tokens_used": self.tokens_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        return cls(
            max_steps=data["max_steps"], max_tokens=data["max_tokens"],
            deadline_at=datetime.fromisoformat(data["deadline_at"]) if data.get("deadline_at") else None,
            steps_used=data.get("steps_used", 0), tokens_used=data.get("tokens_used", 0),
        )


@dataclass
class Hypothesis:
    hypothesis_id: str
    title: str
    confidence: float = 0.0
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        self.supporting_evidence_ids = list(dict.fromkeys(self.supporting_evidence_ids))
        self.contradicting_evidence_ids = list(dict.fromkeys(self.contradicting_evidence_ids))

    def add_support(self, evidence_id: str) -> None:
        if evidence_id not in self.supporting_evidence_ids:
            self.supporting_evidence_ids.append(evidence_id)

    def add_contradiction(self, evidence_id: str) -> None:
        if evidence_id not in self.contradicting_evidence_ids:
            self.contradicting_evidence_ids.append(evidence_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Hypothesis:
        return cls(
            hypothesis_id=data["hypothesis_id"], title=data["title"],
            confidence=data.get("confidence", 0.0),
            supporting_evidence_ids=list(data.get("supporting_evidence_ids", [])),
            contradicting_evidence_ids=list(data.get("contradicting_evidence_ids", [])),
        )


@dataclass
class Claim:
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    material: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Claim:
        return cls(text=data["text"], evidence_ids=list(data.get("evidence_ids", [])),
                   material=data.get("material", True))


@dataclass
class InvestigationReport:
    root_cause: str
    confidence: float
    summary: str
    claims: list[Claim] = field(default_factory=list)
    ruled_out: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvestigationReport:
        return cls(
            root_cause=data["root_cause"], confidence=data["confidence"], summary=data["summary"],
            claims=[Claim.from_dict(claim) for claim in data.get("claims", [])],
            ruled_out=list(data.get("ruled_out", [])), unknowns=list(data.get("unknowns", [])),
            recommendations=list(data.get("recommendations", [])),
        )


@dataclass
class InvestigationState:
    session_id: str
    question: str
    budget: Budget
    case_id: str | None = None
    mode: str = "online"
    phase: InvestigationPhase = InvestigationPhase.INTAKE
    plan: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    report: InvestigationReport | None = None
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "question": self.question,
            "budget": self.budget.to_dict(), "case_id": self.case_id, "mode": self.mode,
            "phase": self.phase.value, "plan": list(self.plan),
            "hypotheses": [hypothesis.to_dict() for hypothesis in self.hypotheses],
            "evidence_ids": list(self.evidence_ids),
            "report": self.report.to_dict() if self.report is not None else None,
            "stop_reason": self.stop_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InvestigationState:
        return cls(
            session_id=data["session_id"], question=data["question"],
            budget=Budget.from_dict(data["budget"]), case_id=data.get("case_id"),
            mode=data.get("mode", "online"),
            phase=InvestigationPhase(data.get("phase", "intake")),
            plan=list(data.get("plan", [])),
            hypotheses=[Hypothesis.from_dict(hypothesis) for hypothesis in data.get("hypotheses", [])],
            evidence_ids=list(data.get("evidence_ids", [])),
            report=InvestigationReport.from_dict(data["report"]) if data.get("report") is not None else None,
            stop_reason=data.get("stop_reason"),
        )
