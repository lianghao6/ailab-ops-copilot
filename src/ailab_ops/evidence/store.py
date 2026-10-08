"""Insertion-ordered evidence storage with deterministic source identities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from typing import Any, Iterable

from .models import Evidence


class EvidenceStore:
    def __init__(self) -> None:
        self._evidence: dict[str, Evidence] = {}

    def add(self, evidence: Evidence) -> Evidence:
        """Keep the first observation and return an independent copy."""
        source = json.dumps(
            {"source_tool": evidence.source_tool, "arguments": evidence.arguments, "excerpt": evidence.excerpt},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
        evidence_id = "ev-" + sha256(source.encode("utf-8")).hexdigest()
        if evidence_id not in self._evidence:
            self._evidence[evidence_id] = replace(deepcopy(evidence), evidence_id=evidence_id)
        return deepcopy(self._evidence[evidence_id])

    def get(self, evidence_id: str) -> Evidence | None:
        return deepcopy(self._evidence.get(evidence_id))

    def to_context(self, ids: Iterable[str]) -> list[dict[str, Any]]:
        """Return independent JSON-ready records in requested citation order.

        Unknown IDs raise KeyError so an incomplete context is never silently built.
        """
        return [self._evidence[evidence_id].to_dict() for evidence_id in ids]
