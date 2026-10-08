"""Reviewable policy decisions, approval snapshots and audit records."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class PolicyContext:
    session_id: str
    reason: str = ""
    risk: str = ""
    rollback: str = ""
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Literal["allow_read", "require_approval", "deny"]
    reason: str


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    session_id: str
    tool: str
    arguments: dict[str, Any]
    reason: str
    risk: str
    rollback: str
    evidence_ids: tuple[str, ...]
    sensitivity: str
    idempotent: bool
    created_at: datetime
    expires_at: datetime
    status: str = "pending"
    approved_by: str | None = None
    rejection_reason: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["expires_at"] = self.expires_at.isoformat()
        data["evidence_ids"] = list(self.evidence_ids)
        return data


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    request_id: str | None
    session_id: str | None
    event: str
    actor: str
    occurred_at: datetime
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data
