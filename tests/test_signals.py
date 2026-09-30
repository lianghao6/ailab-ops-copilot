"""Signals: shape classification, log evidence extraction and the cascade rule.

This is the reasoning core, and the cascade rule in particular is the single
idea in the project most worth having an executable test for. It is easy to
state ("the timeout is a symptom, not the cause") and easy to get wrong.
"""

from __future__ import annotations

import pytest

from ailab_ops.signals import (
    INSUFFICIENT_EVIDENCE,
    classify_series,
    decide,
    extract_log_evidence,
    score_hypotheses,
)


# --------------------------------------------------------------------------
# Shape classification
# --------------------------------------------------------------------------


def test_shapes_round_trip(world):
    """Every shape the generator can emit must be classifiable as itself.

    This is the test that keeps the generator and the classifier from drifting
    apart. Without it the two can both change and the dataset quietly becomes
    mislabelled: a series generated as a leak might be classified as capacity
    exhaustion, and the resulting accuracy drop would be blamed on the model.
    """
    import random

    from ailab_ops.datagen.world import _shape_series
    from ailab_ops.signals import SHAPES

    mismatches: list[tuple[str, str, int]] = []
    for shape in SHAPES:
        for seed in range(12):
            vals = _shape_series(shape, 60, random.Random(seed))
            got = classify_series(vals).shape
            if got != shape:
                mismatches.append((shape, got, seed))
    assert not mismatches, (
        f"{len(mismatches)} shape(s) are classified as something else: "
        f"{sorted({(a, b) for a, b, _ in mismatches})}"
    )


def test_flat_series():
    v = classify_series([50.0] * 40)
    assert v.shape == "flat"


def test_cliff_to_zero():
    v = classify_series([70.0, 72.0, 68.0, 71.0, 69.0, 70.0, 0.0, 0.0, 0.0, 0.0])
    assert v.shape == "cliff_to_zero"


def test_ramp_to_ceiling():
    vals = [20 + 80 * (i / 59) ** 1.9 for i in range(60)]
    v = classify_series(vals, "gpu_mem_used_pct")
    assert v.shape == "ramp_to_ceiling"


def test_sawtooth_rising():
    vals = []
    for i in range(60):
        vals.append(20 + 40 * (i / 59) + 15 * ((i % 7) / 7))
    v = classify_series(vals, "host_mem_used_pct")
    assert v.shape == "sawtooth_rising"
    assert "floor" in v.detail


def test_staircase():
    vals = [90.0 - 20.0 * (i // 12) for i in range(60)]
    v = classify_series(vals, "disk_read_mbps")
    assert v.shape == "staircase"


def test_periodic_gap():
    vals = [0.0 if (i % 8) in (0, 1) else 60.0 for i in range(64)]
    v = classify_series(vals, "disk_read_mbps")
    assert v.shape == "periodic_gap"


def test_spike_then_zero():
    vals = [40.0] * 40 + [99.0, 0.0, 0.0, 0.0]
    v = classify_series(vals, "gpu_util_pct")
    assert v.shape == "spike_then_zero"


def test_smooth_decline_is_a_decline_not_noise():
    """A steady downward slope is *degradation*, not noise. Reporting it as
    `noisy_high` would be actively misleading -- noise is not the finding --
    so the `staircase` shape covers both stepped and smooth declines. The name
    is historical (a rate limiter produces steps); the semantics are "a
    sustained decline with no crash"."""
    vals = [90.0 - i * 1.2 for i in range(60)]
    assert classify_series(vals, "disk_read_mbps").shape == "staircase"


def test_a_flat_series_is_not_a_decline():
    assert classify_series([60.0] * 40, "disk_read_mbps").shape == "flat"


def test_empty_and_single_point_series_do_not_crash():
    assert classify_series([]).shape == "flat"
    assert classify_series([42.0]).shape == "flat"


# --------------------------------------------------------------------------
# Log evidence
# --------------------------------------------------------------------------


class _R:
    """Minimal LogRecord stand-in; the extractor works on any object with these
    attributes, which is what lets it be reused on tool results."""

    def __init__(self, ts, level, rank, message):
        self.ts, self.level, self.rank, self.message = ts, level, rank, message


def test_extract_finds_the_first_non_cascade_error(playbook):
    rows = [
        _R("2025-01-01T00:00:00Z", "INFO", 0, "starting"),
        _R("2025-01-01T00:00:05Z", "ERROR", 3, "CUDA error: device-side assert triggered"),
        _R("2025-01-01T00:00:30Z", "ERROR", 0, "Watchdog caught collective operation timeout: WorkNCCL"),
        _R("2025-01-01T00:00:30Z", "ERROR", 1, "Watchdog caught collective operation timeout: WorkNCCL"),
        _R("2025-01-01T00:00:30Z", "ERROR", 2, "Watchdog caught collective operation timeout: WorkNCCL"),
    ]
    ev = extract_log_evidence(rows, playbook)
    assert ev.first_error is not None
    assert "device-side assert" in ev.first_error, (
        "the extractor returned the cascade line instead of the causal one"
    )
    assert ev.first_error_rank == 3
    assert ev.is_cascade_shaped is True
    assert set(ev.ranks_reporting_cascade) == {0, 1, 2}


def test_extract_ranks_the_earliest_error_by_timestamp(playbook):
    rows = [
        _R("2025-01-01T00:00:30Z", "ERROR", 0, "Watchdog caught collective operation timeout"),
        _R("2025-01-01T00:00:05Z", "ERROR", 5, "RuntimeError: something specific broke"),
        _R("2025-01-01T00:00:31Z", "ERROR", 1, "Watchdog caught collective operation timeout"),
    ]
    ev = extract_log_evidence(rows, playbook)
    assert "something specific" in (ev.first_error or "")


def test_wrapper_lines_do_not_impersonate_a_signature(playbook):
    """`process exited with code 137` appears in every failed job's log. If it
    were allowed to match, every failure would look like a SIGKILL."""
    rows = [
        _R("2025-01-01T00:00:10Z", "WARNING", 0, "process exited with code 137 after 2h"),
        _R("2025-01-01T00:00:10Z", "ERROR", 0, "exit code 143 [elapsed=3h]"),
    ]
    ev = extract_log_evidence(rows, playbook)
    assert "process_killed_sigkill" not in ev.matched, (
        "a supervisor wrapper line was treated as a signature"
    )


def test_truncation_hints_are_detected(playbook):
    rows = [_R("2025-01-01T00:00:00Z", "WARNING", 0, "[log level raised to ERROR mid-run; verbose output rotated out]")]
    ev = extract_log_evidence(rows, playbook)
    assert ev.truncation_hints


# --------------------------------------------------------------------------
# Hypothesis scoring and the decision
# --------------------------------------------------------------------------


def test_cascade_rule_picks_the_unique_signature_over_the_frequent_one(playbook):
    rows = [
        _R("2025-01-01T00:00:01Z", "ERROR", 7, "CUDA out of memory. Tried to allocate 2.00 GiB"),
        _R("2025-01-01T00:00:02Z", "ERROR", 3, "GPU 0 has a total capacity of 79.15 GiB"),
        _R("2025-01-01T00:00:30Z", "ERROR", 0, "Watchdog caught collective operation timeout"),
        _R("2025-01-01T00:00:30Z", "ERROR", 1, "Watchdog caught collective operation timeout"),
        _R("2025-01-01T00:00:30Z", "ERROR", 2, "Watchdog caught collective operation timeout"),
    ]
    ev = extract_log_evidence(rows, playbook)
    hyps = score_hypotheses(playbook, ev, exit_code=137, status="FAILED")
    v = decide(playbook, ev, hyps, status="FAILED", exit_code=137)
    assert v.root_cause in {"oom_gpu", "oom_host"}, (
        f"the cascade rule should surface the OOM, got {v.root_cause}"
    )
    assert "cascade" in v.decided_by


def test_cascade_without_a_unique_signature_refuses(playbook):
    """Nothing but timeouts in the log: the causal rank's error rotated away.
    The correct answer is to say so."""
    rows = [
        _R("2025-01-01T00:00:30Z", "ERROR", r, "Watchdog caught collective operation timeout")
        for r in range(8)
    ]
    ev = extract_log_evidence(rows, playbook)
    hyps = score_hypotheses(playbook, ev, exit_code=137, status="FAILED")
    v = decide(playbook, ev, hyps, status="FAILED", exit_code=137)
    assert v.is_refusal
    assert "cascade" in v.decided_by


def test_exit_code_alone_is_not_enough(playbook):
    rows = [_R("2025-01-01T00:00:10Z", "ERROR", 0, "process exited with code 137")]
    ev = extract_log_evidence(rows, playbook)
    hyps = score_hypotheses(playbook, ev, exit_code=137, status="FAILED")
    v = decide(playbook, ev, hyps, status="FAILED", exit_code=137)
    assert v.is_refusal, "a bare exit code should never be reported as a diagnosis"


def test_telemetry_gap_triggers_a_refusal(playbook):
    """A job that ran for hours with no metrics at all: telemetry collection was
    broken, so the log is not the whole story."""
    rows = [
        _R("2025-01-01T00:00:10Z", "WARNING", 0, "process exited with code 137 after 4h"),
    ]
    ev = extract_log_evidence(rows, playbook)
    hyps = score_hypotheses(playbook, ev, exit_code=137, status="FAILED")
    v = decide(playbook, ev, hyps, status="FAILED", exit_code=137, telemetry_gap=True)
    assert v.is_refusal


def test_never_started_job_is_attributed_to_a_scheduling_cause(playbook):
    rows = [
        _R("2025-01-01T00:00:05Z", "ERROR", 0, "0/64 nodes are available: Insufficient nvidia.com/gpu"),
        _R("2025-01-01T00:00:06Z", "ERROR", 0, "Unschedulable"),
    ]
    ev = extract_log_evidence(rows, playbook)
    hyps = score_hypotheses(playbook, ev, exit_code=0, status="NEVER_STARTED")
    v = decide(playbook, ev, hyps, status="NEVER_STARTED", exit_code=0)
    assert v.root_cause == "insufficient_resources"


def test_never_started_job_cannot_have_a_runtime_cause(playbook):
    rows = [_R("2025-01-01T00:00:05Z", "ERROR", 0, "ErrImagePull")]
    ev = extract_log_evidence(rows, playbook)
    hyps = {h.scenario_id: h for h in score_hypotheses(playbook, ev, exit_code=1, status="NEVER_STARTED")}
    assert hyps["oom_gpu"].penalty > 0, "a runtime cause was not penalised for a job that never ran"
    assert hyps["image_pull_failed"].score > hyps["oom_gpu"].score


def test_metric_shape_participates_in_scoring(playbook):
    """A memory ramp plus a CUDA traceback should beat the traceback alone."""
    rows = [{"ts": "2025-01-01T00:00:09Z", "level": "ERROR", "rank": 0,
             "message": "torch.cuda.OutOfMemoryError"}]
    ev = extract_log_evidence(rows, playbook)
    ramp = [20 + 79 * (i / 39) ** 1.9 for i in range(40)]
    without = {h.scenario_id: h.score for h in score_hypotheses(playbook, ev)}
    with_metric = {h.scenario_id: h.score for h in
                   score_hypotheses(playbook, ev, metrics={"gpu_mem_used_pct": ramp})}
    assert with_metric["oom_gpu"] > without["oom_gpu"]


def test_conflicting_metrics_reduce_the_margin(playbook):
    """Two rival signatures present at once should make the decision harder,
    which is what "hard" cases in the dataset are built from."""
    rows = [
        _R("2025-01-01T00:00:09Z", "ERROR", 1, "No space left on device"),
        _R("2025-01-01T00:00:09Z", "ERROR", 0, "Out of memory: Kill process 1234"),
    ]
    ev = extract_log_evidence(rows, playbook)
    hyps = score_hypotheses(playbook, ev, exit_code=137, status="FAILED")
    v = decide(playbook, ev, hyps, status="FAILED", exit_code=137)
    # Either it picks one with a modest margin, or it declines. What it must not
    # do is claim certainty.
    assert v.is_refusal or v.confidence < 0.9


def test_decision_is_deterministic(playbook):
    rows = [
        _R("2025-01-01T00:00:01Z", "ERROR", 2, "Error(s) in loading state_dict for Model"),
        _R("2025-01-01T00:00:02Z", "ERROR", 2, "size mismatch for lm_head.weight"),
    ]
    results = []
    for _ in range(5):
        ev = extract_log_evidence(rows, playbook)
        hyps = score_hypotheses(playbook, ev, exit_code=1, status="FAILED")
        v = decide(playbook, ev, hyps, status="FAILED", exit_code=1)
        results.append((v.root_cause, round(v.confidence, 6)))
    assert len(set(results)) == 1


def test_no_hypotheses_at_all_is_handled(playbook):
    ev = extract_log_evidence([], playbook)
    v = decide(playbook, ev, [], status=None)
    assert v.is_refusal
