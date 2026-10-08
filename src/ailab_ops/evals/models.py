"""Independent evaluation dimensions; None means an unconfigured measurement."""

from dataclasses import asdict, dataclass, field
from typing import Any

from ailab_ops.investigation.models import InvestigationState


SCORE_NAMES = ("tool_choice", "required_evidence", "citation_validity", "root_cause", "abstention",
               "policy_compliance", "latency", "token_use")


@dataclass
class EvaluationResult:
    case_id: str | None
    session_id: str | None
    tool_choice: float | None = None
    required_evidence: float | None = None
    citation_validity: float = 0.0
    root_cause: float = 0.0
    abstention: float = 0.0
    policy_compliance: float | None = None
    latency: float | None = None
    token_use: float | None = None
    latency_ms: float | None = None
    tokens_used: int | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def scores(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in SCORE_NAMES}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationSample:
    state: InvestigationState
    evidence: list[Any] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)
    latency_ms: float | None = None


@dataclass(frozen=True)
class MetricSummary:
    count: int
    mean: float | None
    spread: float | None
    minimum: float | None
    maximum: float | None
    stddev: float | None


@dataclass
class EvaluationRun:
    runs: list[EvaluationResult]
    summary: dict[str, MetricSummary]
    by_case: dict[str, dict[str, MetricSummary]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
