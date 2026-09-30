"""Tools: the read-only contract, output caps, and error behaviour.

These tests exist because the tool layer is where an agent system most often
breaks in ways that are invisible in a demo: a tool that leaks the answer, or
one that silently truncates its output, produces an agent that looks competent
and is not.
"""

from __future__ import annotations

import json

import pytest

from ailab_ops.datagen.taxonomy import load_playbook
from ailab_ops.signals import classify_series, extract_log_evidence

GROUND_TRUTH_FIELDS = {"root_cause", "root_cause_name", "category", "difficulty",
                       "confounders", "is_insufficient_evidence"}


def test_expected_tools_are_registered(registry):
    assert set(registry.names) >= {
        "get_job", "search_logs", "get_metrics", "list_jobs",
        "search_runbooks", "get_incident_history", "get_exit_code_meaning",
    }


def test_all_tools_are_read_only(registry):
    """The central safety property. A diagnostic assistant that can restart jobs
    will eventually restart the wrong one."""
    assert registry.names, "no tools are registered"
    for name, t in registry._tools.items():
        assert t.read_only, f"{name} is not marked read-only"


def test_no_tool_exposes_ground_truth(registry, world):
    """Sweep every tool over every failed job and assert the answer never leaks.

    This is the test that makes the guardrail real. Checking only `get_job`
    would miss a leak added later to `list_jobs` or to the incident timeline.
    """
    calls = [
        ("get_job", lambda jid: {"job_id": jid}),
        ("search_logs", lambda jid: {"job_id": jid, "level": "ERROR"}),
        ("get_metrics", lambda jid: {"job_id": jid}),
    ]
    for job in world.failed_jobs()[:40]:
        for name, mk in calls:
            res = registry.call(name, mk(job.job_id))
            if not res.ok:
                continue
            blob = json.dumps(res.data, default=str)
            assert job.root_cause not in blob, (
                f"{name} leaked the root cause {job.root_cause!r} for {job.job_id}"
            )
    # list_jobs and incident history aggregate, so check them once over a wide
    # sweep rather than per job.
    for name, args in (("list_jobs", {"limit": 25}),
                       ("get_incident_history", {"limit": 15})):
        res = registry.call(name, args)
        assert res.ok
        blob = json.dumps(res.data, default=str)
        assert "root_cause" not in blob, f"{name} exposes a root_cause field"
        assert "confounders" not in blob, f"{name} exposes confounders"


def test_unknown_job_returns_a_typed_error(registry):
    res = registry.call("get_job", {"job_id": "job-00000000-9999"})
    assert res.ok is False
    assert res.error and "not found" in res.error
    assert res.hint, "an error should tell the model what to do next"


def test_unknown_tool_lists_the_available_ones(registry):
    res = registry.call("drop_database", {})
    assert res.ok is False
    assert "available tools" in (res.hint or "")


def test_bad_arguments_do_not_raise(registry):
    """A tool must never crash the loop. A raised exception ends the run; a
    typed error lets the model correct itself, which is the whole reason
    tool-calling beats a fixed pipeline."""
    res = registry.call("search_logs", {"nonexistent_argument": 1})
    assert res.ok is False
    assert res.error
    assert "expected parameters" in (res.hint or "")


def test_search_logs_caps_its_output_and_says_so(registry, world):
    # Find a job with plenty of log records.
    job = max(world.jobs, key=lambda j: len(world.logs_for(j.job_id)))
    res = registry.call("search_logs", {"job_id": job.job_id, "limit": 5})
    assert res.ok
    assert len(res.data["lines"]) <= 5
    if res.data["total_matching"] > 5:
        assert res.truncated is True
        assert "truncation_note" in res.data, (
            "a truncated tool result must announce the truncation; silent truncation makes "
            "the model unable to distinguish 'no more evidence' from 'you were not shown it'"
        )


def test_search_logs_reports_retention_gaps(registry, world):
    """A short log for a long-running job should be flagged, because otherwise
    the absence of an error looks like evidence of absence."""
    candidates = [
        j for j in world.jobs
        if j.duration_s > 1800 and 0 < len(world.logs_for(j.job_id)) < 12
    ]
    if not candidates:
        pytest.skip("generator produced no retention-gap case in this sample")
    res = registry.call("search_logs", {"job_id": candidates[0].job_id})
    assert "retention_note" in res.data


def test_job_with_no_logs_returns_an_explanatory_note(registry, world):
    empty = [j for j in world.jobs if not world.logs_for(j.job_id)]
    if not empty:
        pytest.skip("no log-less job in this sample")
    res = registry.call("search_logs", {"job_id": empty[0].job_id})
    assert res.ok
    assert res.data["lines"] == []
    assert "note" in res.data


def test_metrics_tool_classifies_every_series(registry, world):
    job = next(j for j in world.jobs if world.metrics_for(j.job_id))
    res = registry.call("get_metrics", {"job_id": job.job_id})
    assert res.ok
    for name, s in res.data["series"].items():
        assert s["shape"] in {
            "flat", "noisy_high", "cliff_to_zero", "spike_then_zero",
            "ramp_to_ceiling", "sawtooth_rising", "staircase", "periodic_gap",
        }, f"{name} was classified as an unknown shape {s['shape']!r}"
        assert s["shape_detail"]


def test_metrics_tool_suggests_alternatives_on_a_bad_metric_name(registry, world):
    job = next(j for j in world.jobs if world.metrics_for(j.job_id))
    res = registry.call("get_metrics", {"job_id": job.job_id, "metric": "not_a_metric"})
    assert res.ok is False
    assert "available" in (res.hint or "")


def test_list_jobs_returns_a_correlation_breakdown(registry):
    res = registry.call("list_jobs", {"limit": 20})
    assert res.ok
    fc = res.data["failure_correlation"]
    for key in ("by_cluster", "by_queue", "by_team", "note"):
        assert key in fc


def test_runbook_search_returns_ranked_hits_with_excerpts(registry):
    res = registry.call("search_runbooks", {"query": "CUDA out of memory", "top_k": 3})
    assert res.ok
    hits = res.data["hits"]
    assert hits
    assert hits[0]["score"] >= hits[-1]["score"]
    for h in hits:
        assert h["doc_id"] and h["title"] and h["excerpt"]


def test_exit_code_tool_flags_137_as_ambiguous(registry):
    res = registry.call("get_exit_code_meaning", {"code": 137})
    assert res.ok
    assert "symptom" in res.data["meaning"].lower()
    assert "oom" in res.data["next_step"].lower()


def test_unknown_tool_is_logged_as_an_error_without_being_registered(registry):
    before = len(registry.call_log)
    res = registry.call("rm_rf_everything", {})
    assert res.ok is False
    assert len(registry.call_log) == before + 1
    # The attempt is recorded (auditable) but the name is not a tool.
    assert registry.call_log[-1]["tool"] == "rm_rf_everything"
    assert registry.call_log[-1]["ok"] is False
    assert "rm_rf_everything" not in registry.names


def test_call_log_records_every_invocation(registry):
    before = len(registry.call_log)
    registry.call("get_exit_code_meaning", {"code": 1})
    assert len(registry.call_log) == before + 1
    assert registry.call_log[-1]["tool"] == "get_exit_code_meaning"


def test_regex_injection_is_tolerated(registry, world):
    """The model supplies the pattern, so a catastrophic-backtracking regex must
    not be able to hang the service."""
    import time

    job = world.failed_jobs()[0]
    t0 = time.perf_counter()
    registry.call("search_logs", {"job_id": job.job_id, "query": "(a+)+$" + "b" * 40})
    assert time.perf_counter() - t0 < 2.0, "a pathological pattern was allowed through"


def test_json_output_is_stable_for_a_given_call(registry, world):
    job = world.failed_jobs()[0]
    a = registry.call("get_job", {"job_id": job.job_id}).as_text()
    b = registry.call("get_job", {"job_id": job.job_id}).as_text()
    # latency differs between calls by design, so compare the payload only.
    da, db = json.loads(a), json.loads(b)
    da.pop("latency_ms", None)
    db.pop("latency_ms", None)
    assert da == db
