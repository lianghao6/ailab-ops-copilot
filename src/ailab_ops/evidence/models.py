"""Evidence records preserve observations without assigning a root cause."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EvidenceRelation(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    RELATED = "related"


@dataclass
class Evidence:
    source_tool: str
    arguments: dict[str, Any]
    summary: str
    excerpt: str
    evidence_id: str = ""
    observed_time_range: tuple[str, str] | None = None
    truncated: bool = False
    relation: EvidenceRelation = EvidenceRelation.RELATED
    hypothesis_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["relation"] = self.relation.value
        if self.observed_time_range is not None:
            data["observed_time_range"] = list(self.observed_time_range)
        return data


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
