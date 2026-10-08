"""Contracts for stable evidence identity and mechanical citation validation."""

import json

from ailab_ops.evidence import Evidence, EvidenceRelation, EvidenceStore, validate_report
from ailab_ops.investigation import Claim, InvestigationReport


def test_store_assigns_stable_ids_and_deduplicates_identical_sources():
    store = EvidenceStore()
    first = store.add(Evidence("read_logs", {"job_id": "job-1", "filters": {"rank": 0, "level": "error"}},
                               "Allocator failed", "CUDA out of memory"))
    duplicate = store.add(Evidence("read_logs", {"filters": {"level": "error", "rank": 0}, "job_id": "job-1"},
                                   "Another summary", "CUDA out of memory"))
    assert first.evidence_id.startswith("ev-")
    assert duplicate is first
    assert store.get(first.evidence_id) is first
    assert store.get("unknown") is None
    independent = EvidenceStore().add(Evidence("read_logs", first.arguments, "New summary", first.excerpt))
    assert independent.evidence_id == first.evidence_id
    second = store.add(Evidence("read_logs", first.arguments, "Worker exited", "exit code 1"))
    third = store.add(Evidence("read_events", first.arguments, "Allocator failed", first.excerpt))
    fourth = store.add(Evidence("read_logs", {"job_id": "job-2"}, "Allocator failed", first.excerpt))
    assert len({first.evidence_id, second.evidence_id, third.evidence_id, fourth.evidence_id}) == 4
    assert [item["evidence_id"] for item in store.to_context([first.evidence_id, second.evidence_id])] == [
        first.evidence_id, second.evidence_id,
    ]


def test_context_marks_truncated_evidence():
    store = EvidenceStore()
    evidence = store.add(Evidence(
        source_tool="read_logs", arguments={"job_id": "job-1"}, summary="Allocator failed",
        excerpt="CUDA out of memory", observed_time_range=("2026-10-08T00:00:00Z", "2026-10-08T00:01:00Z"),
        truncated=True, relation=EvidenceRelation.SUPPORTS, hypothesis_id="h1",
        metadata={"line": 42},
    ))
    context = json.loads(json.dumps(store.to_context([evidence.evidence_id])))
    assert context == [{
        "evidence_id": evidence.evidence_id, "source_tool": "read_logs", "arguments": {"job_id": "job-1"},
        "summary": "Allocator failed", "excerpt": "CUDA out of memory",
        "observed_time_range": ["2026-10-08T00:00:00Z", "2026-10-08T00:01:00Z"],
        "truncated": True, "relation": "supports", "hypothesis_id": "h1", "metadata": {"line": 42},
    }]


def test_report_rejects_unknown_evidence_id():
    report = InvestigationReport("OOM", 0.9, "Failed", claims=[Claim("Allocator failed", ["ev-unknown"])])
    issues = validate_report(report, EvidenceStore())
    assert [(issue.code, issue.path) for issue in issues] == [
        ("unknown_evidence", "claims[0].evidence_ids[0]"),
    ]
    assert "ev-unknown" in issues[0].message


def test_report_rejects_material_claim_without_citation():
    report = InvestigationReport("OOM", 0.9, "Failed", claims=[Claim("Allocator failed")])
    issues = validate_report(report, EvidenceStore())
    assert [(issue.code, issue.path) for issue in issues] == [
        ("missing_citation", "claims[0].evidence_ids"),
    ]
    assert issues[0].message


def test_report_accepts_existing_citations():
    store = EvidenceStore()
    evidence = store.add(Evidence("read_logs", {}, "Node restarted", "Node restarted",
                                  relation=EvidenceRelation.CONTRADICTS, hypothesis_id="h1"))
    report = InvestigationReport("OOM", 0.9, "Failed", claims=[
        Claim("Allocator failed", [evidence.evidence_id]), Claim("Investigation complete", material=False),
    ])
    assert validate_report(report, store) == []


def test_report_rejects_duplicate_citations_in_deterministic_order():
    store = EvidenceStore()
    evidence = store.add(Evidence("read_logs", {}, "OOM", "OOM"))
    report = InvestigationReport("OOM", 0.9, "Failed", claims=[
        Claim("Allocator failed", [evidence.evidence_id, evidence.evidence_id, "ev-missing", "ev-missing"]),
        Claim("Worker exited"),
    ])
    issues = validate_report(report, store)
    assert [(issue.code, issue.path) for issue in issues] == [
        ("duplicate_citation", "claims[0].evidence_ids[1]"),
        ("unknown_evidence", "claims[0].evidence_ids[2]"),
        ("duplicate_citation", "claims[0].evidence_ids[3]"),
        ("unknown_evidence", "claims[0].evidence_ids[3]"),
        ("missing_citation", "claims[1].evidence_ids"),
    ]
    assert validate_report(report, store) == issues
    assert report.claims[0].evidence_ids == [evidence.evidence_id, evidence.evidence_id, "ev-missing", "ev-missing"]
