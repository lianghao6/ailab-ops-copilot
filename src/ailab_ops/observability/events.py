"""Typed telemetry shared by investigation, approvals and offline evaluation."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    session_id: str | None
    kind: str
    phase: str | None = None
    event: str = ""
    timestamp: str | None = None
    model: str | None = None
    tool: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float | None = None
    retry: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    approval_id: str | None = None
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
