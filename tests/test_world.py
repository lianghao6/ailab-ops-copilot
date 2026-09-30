"""The generated world, and the promise that it is reproducible and synthetic."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict

from ailab_ops.datagen import generate_world, load_world, write_world


def test_generation_is_deterministic_for_a_seed():
    a = generate_world(seed=99, n_jobs=40)
    b = generate_world(seed=99, n_jobs=40)
    assert [j.job_id for j in a.jobs] == [j.job_id for j in b.jobs]
    assert [asdict(j) for j in a.jobs] == [asdict(j) for j in b.jobs]
    assert [asdict(r) for r in a.logs] == [asdict(r) for r in b.logs]


def test_different_seeds_produce_different_worlds():
    a = generate_world(seed=1, n_jobs=40)
    b = generate_world(seed=2, n_jobs=40)
    assert [j.job_id for j in a.jobs] != [j.job_id for j in b.jobs]


def test_world_is_internally_consistent(world):
    for j in world.jobs:
        assert j.finished_at >= j.submitted_at
        if j.started_at:
            assert j.started_at >= j.submitted_at
            assert j.duration_s > 0
        if j.status == "NEVER_STARTED":
            assert j.started_at is None, f"{j.job_id} never started but has a start time"
        if j.status == "SUCCEEDED":
            assert j.exit_code == 0
            assert j.root_cause is None, "a successful job must not carry a root cause"


def test_failed_jobs_carry_ground_truth(world):
    for j in world.jobs:
        if j.status == "SUCCEEDED":
            continue
        assert j.root_cause, f"{j.job_id} failed with no recorded cause"
        assert j.category and j.difficulty
        assert j.root_cause in world.playbook.scenarios


def test_incidents_reference_real_jobs_and_causes(world):
    for inc in world.incidents:
        job = world.job(inc.job_id)
        assert job is not None, f"incident {inc.incident_id} references an unknown job"
        assert inc.root_cause == job.root_cause
        assert inc.closed_at >= inc.opened_at
        assert len(inc.timeline) >= 3


def test_cascade_cases_look_like_cascades(world):
    """The generator must produce logs where several ranks report a timeout AND
    exactly one rank reports the real error -- otherwise the cascade rule the
    runbooks teach would never be exercised."""
    from ailab_ops.signals import extract_log_evidence

    cascades = [j for j in world.jobs if j.root_cause in {"collective_timeout", "rank_crash_assert"}]
    assert cascades, "the world should contain cascade-prone failures"
    seen_multi_rank = 0
    for j in cascades:
        ev = extract_log_evidence(world.logs_for(j.job_id), world.playbook)
        if ev.is_cascade_shaped:
            seen_multi_rank += 1
    assert seen_multi_rank > 0, "no cascade case produced multi-rank timeout lines"


def test_undecidable_cases_withhold_telemetry(world):
    """The `insufficient_evidence` cases must actually be short of evidence."""
    iev = [j for j in world.jobs if j.is_insufficient_evidence]
    assert iev, "the world should contain undecidable cases"
    for j in iev:
        assert not world.metrics_for(j.job_id), (
            f"{j.job_id} is supposed to be undecidable but has metric series; the case is "
            f"diagnosable, so refusing to answer would be scored as an error"
        )
        assert not j.confounders, (
            f"{j.job_id} carries a rival signal, which makes it diagnosable rather than "
            f"undecidable"
        )


def test_no_obviously_real_identifiers(world):
    """A blunt but useful guard: nothing in the generated data should look like a
    real hostname, a real bucket, or a real person."""
    blob = json.dumps([asdict(j) for j in world.jobs] + [asdict(n) for n in world.nodes])
    for forbidden in ("sankuai", "corp.", ".com", "prod.sankuai", "dianping", "meituan"):
        assert forbidden not in blob.lower(), f"generated data contains {forbidden!r}"


def test_distribution_respects_the_declared_split(world):
    counts = Counter(j.difficulty for j in world.jobs if j.root_cause)
    total = sum(counts.values())
    assert total > 0
    hard_share = counts["hard"] / total
    assert 0.05 <= hard_share <= 0.40, (
        f"hard cases are {hard_share:.1%} of failures; the split ratios promise roughly "
        f"{world.playbook.split_ratios.get('hard')}"
    )


def test_round_trip_through_disk(tmp_path):
    w = generate_world(seed=7, n_jobs=30)
    written = write_world(w, tmp_path)
    assert "ground_truth" in written

    back = load_world(tmp_path)
    assert len(back.jobs) == len(w.jobs)
    assert len(back.logs) == len(w.logs)
    assert back.job(w.jobs[0].job_id).root_cause == w.jobs[0].root_cause


def test_ground_truth_file_is_separate_from_the_browsable_data():
    """The ground truth must live in its own artifact, so that "the agent does
    not see the answer" is a property of the data layout rather than of the
    agent's good behaviour."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "ailab_ops" / "tools"
    src = (root / "__init__.py").read_text(encoding="utf-8")
    assert "_job_view" in src
    view_fn = src.split("def _job_view")[1].split("\ndef ")[0]
    for leaked in ("root_cause", "confounders", "is_insufficient_evidence", "difficulty"):
        assert leaked not in view_fn, (
            f"_job_view exposes {leaked!r}, which the agent must not see"
        )
    # And the ground truth is written to its own file, not mixed into jobs.jsonl.
    world_src = (Path(__file__).resolve().parents[1] / "src" / "ailab_ops" / "datagen"
                 / "world.py").read_text(encoding="utf-8")
    assert "ground_truth.jsonl" in world_src
