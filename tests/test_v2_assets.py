"""Independent V2 assets: observable facts and hidden labels stay separate."""

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / "data" / "v2"
CASE_IDS = ["case-collective-timeout", "case-gpu-assert", "case-insufficient-evidence"]


def _utc_timestamp(value):
    # Python 3.10 accepts an explicit UTC offset, but not the ISO Z suffix.
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def _loader():
    # Import inside tests so the first red run reports a missing feature.
    from ailab_ops.cases import loader
    return loader


def _assert_no_labels(value):
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in {"root_cause", "ground_truth"}
            assert not key.startswith("expected_")
            _assert_no_labels(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_labels(item)


def test_exact_case_catalog_and_deterministic_independent_loads():
    loader = _loader()
    assert loader.list_cases() == CASE_IDS
    first = loader.load_case("case-gpu-assert")
    second = loader.load_case("case-gpu-assert")
    assert first == second
    first.jobs[0]["status"] = "MODIFIED"
    assert second.jobs[0]["status"] == "FAILED"


def test_missing_case_has_actionable_error():
    with pytest.raises(FileNotFoundError, match="Case 'case-absent' not found"):
        _loader().load_case("case-absent")


@pytest.mark.parametrize("case_id", ["../evals/labels", "/etc/passwd", "case-gpu-assert.json"])
def test_case_identifier_cannot_escape_case_directory(case_id):
    with pytest.raises(ValueError, match="Invalid case ID"):
        _loader().load_case(case_id)


def test_explicit_root_is_authoritative_and_does_not_read_labels(tmp_path):
    (tmp_path / "cases").mkdir()
    data = json.loads((ROOT / "cases" / "case-gpu-assert.json").read_text())
    data["title"] = "Isolated fixture"
    (tmp_path / "cases" / "case-gpu-assert.json").write_text(json.dumps(data))
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "labels.jsonl").write_text("not valid JSON")
    assert _loader().load_case("case-gpu-assert", root=tmp_path).title == "Isolated fixture"
    with pytest.raises(FileNotFoundError):
        _loader().load_case("case-collective-timeout", root=tmp_path)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_facts_and_lookup_results_have_no_recursive_labels(case_id):
    world = _loader().load_case(case_id)
    _assert_no_labels(asdict(world))
    job = world.jobs[0]
    assert world.get_job(job["job_id"]) == job
    _assert_no_labels(world.get_job(job["job_id"]))
    _assert_no_labels(world.logs_for(job["job_id"]))
    _assert_no_labels(world.metrics_for(job["job_id"]))
    assert world.get_job("absent") is None
    assert world.logs_for("absent") == []
    assert world.metrics_for("absent") == []
    assert world.actions
    assert all(action["simulated"] is True for action in world.actions)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_timestamps_and_references_are_consistent(case_id):
    world = _loader().load_case(case_id)
    jobs = {job["job_id"]: job for job in world.jobs}
    node_ids = {node["node_id"] for node in world.nodes}
    for job in jobs.values():
        assert job["submitted_at"] <= job["started_at"] < job["finished_at"]
        assert set(job["node_ids"]) <= node_ids
    assert world.logs == sorted(world.logs, key=lambda log: log["ts"])
    for log in world.logs:
        job = jobs[log["job_id"]]
        assert job["started_at"] <= log["ts"] <= job["finished_at"]
        assert 0 <= log["rank"] < job["world_size"]
        assert log["node_id"] in job["node_ids"]
    for metric in world.metrics:
        job = jobs[metric["job_id"]]
        assert metric["step_s"] > 0 and metric["values"]
        last = _utc_timestamp(metric["ts_start"]) + timedelta(
            seconds=metric["step_s"] * (len(metric["values"]) - 1)
        )
        assert _utc_timestamp(job["started_at"]) <= _utc_timestamp(metric["ts_start"])
        assert last <= _utc_timestamp(job["finished_at"])
    for incident in world.incidents:
        assert incident["job_id"] in jobs
        assert incident["opened_at"] >= jobs[incident["job_id"]]["finished_at"]
        assert incident["timeline"] == sorted(incident["timeline"], key=lambda event: event["ts"])


def test_hidden_labels_are_separate_and_cover_cases():
    labels = _loader().load_eval_labels(ROOT / "evals" / "labels.jsonl")
    assert sorted(labels) == CASE_IDS
    assert labels["case-gpu-assert"]["root_cause"] == "gpu_device_assert"
    assert labels["case-collective-timeout"]["root_cause"] == "collective_transport_failure"
    assert labels["case-insufficient-evidence"]["root_cause"] is None
    assert labels["case-insufficient-evidence"]["expected_status"] == "insufficient_evidence"
    assert all("ground_truth" in row for row in labels.values())


def test_duplicate_eval_labels_fail_instead_of_silently_overwriting(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"case_id": "case-a"}\n{"case_id": "case-a"}\n')
    with pytest.raises(ValueError, match="Duplicate evaluation case ID: case-a"):
        _loader().load_eval_labels(path)


def test_insufficient_case_exposes_missing_observations():
    world = _loader().load_case("case-insufficient-evidence")
    assert world.metrics == []
    assert world.telemetry["logs"]["complete"] is False
    assert world.telemetry["metrics"]["available"] is False
    assert all("device-side assert" not in log["message"] for log in world.logs)
    assert all("Watchdog caught" not in log["message"] for log in world.logs)


def test_runbooks_do_not_identify_cases_or_embed_hidden_label_keys():
    runbooks = sorted((ROOT / "knowledge" / "runbooks").glob("*.md"))
    assert {path.name for path in runbooks} == {"gpu-assert.md", "collective-timeout.md"}
    for path in runbooks:
        text = path.read_text()
        assert all(case_id not in text for case_id in CASE_IDS)
        assert all(key not in text for key in ("root_cause", "ground_truth", "expected_"))
