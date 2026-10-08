"""Validate citation structure, never the semantic strength of a diagnosis."""

from __future__ import annotations

from ailab_ops.investigation.models import InvestigationReport

from .models import ValidationIssue
from .store import EvidenceStore


def validate_report(report: InvestigationReport, store: EvidenceStore) -> list[ValidationIssue]:
    issues = []
    for claim_index, claim in enumerate(report.claims):
        citations_path = f"claims[{claim_index}].evidence_ids"
        if claim.material and not claim.evidence_ids:
            issues.append(ValidationIssue("missing_citation", citations_path,
                                          "Material claim requires at least one evidence citation."))
        seen = set()
        for citation_index, evidence_id in enumerate(claim.evidence_ids):
            path = f"{citations_path}[{citation_index}]"
            if evidence_id in seen:
                issues.append(ValidationIssue("duplicate_citation", path,
                                              f"Evidence {evidence_id!r} is cited more than once in this claim."))
            seen.add(evidence_id)
            if store.get(evidence_id) is None:
                issues.append(ValidationIssue("unknown_evidence", path,
                                              f"Evidence {evidence_id!r} does not exist in the store."))
    return issues
