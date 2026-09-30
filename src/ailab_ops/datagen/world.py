"""The synthetic world: entities, telemetry synthesis, and serialization.

Design notes that matter for teaching:

* A `World` is a self-consistent fictional platform. Teams own jobs, jobs run
  on nodes in queues, failures produce incidents, incidents carry a ground-
  truth root cause. Because the ground truth is attached at generation time,
  the evaluation harness never has to guess what the right answer is.
* Telemetry is synthesized *from* the fault, not the other way round. The
  generator asks "what does this failure look like in logs and metrics?" and
  emits exactly that, which is why the dataset is diagnosable at all.
* Determinism is per-entity: each job derives its own RNG seed from
  (master_seed, job_index). Adding a job at the end does not perturb earlier
  jobs, so datasets remain comparable across generator versions.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .taxonomy import INSUFFICIENT_EVIDENCE, Playbook, Scenario, load_playbook

# --------------------------------------------------------------------------
# Vocabulary for the fictional platform. Nothing here is drawn from reality.
# --------------------------------------------------------------------------

COMPANY = "AILab"

CLUSTERS = ["ailab-prod-a", "ailab-prod-b", "ailab-research", "ailab-eval"]

QUEUES = [
    "gpu-p0-training",
    "gpu-p1-training",
    "gpu-preemptible",
    "npu-training",
    "eval-batch",
]

JOB_KINDS = [
    "pretrain",
    "sft",
    "dpo",
    "rlhf-ppo",
    "lora-finetune",
    "eval-suite",
    "distill",
    "quantize-eval",
]

TEAM_NAMES = [
    "foundation-models",
    "post-training",
    "multimodal",
    "agent-platform",
    "inference-optim",
    "data-engineering",
    "eval-platform",
    "speech",
]

FIRST_NAMES = [
    "wei", "jing", "hao", "lei", "nan", "yue", "chen", "xin", "rui", "dan",
    "kai", "mei", "peng", "qi", "shan", "tao", "wen", "yao", "zhi", "bo",
]

LEADERS = [
    "yunjian", "zhixuan", "haoran", "linfeng", "sichen", "muhan", "ruoxi",
]

WORKDIRS = ["/workspace/run", "/opt/train", "/home/worker/job", "/srv/ailab/job"]

# Plausible operating ranges per metric, used to map a normalized 0..1 shape
# onto realistic-looking numbers. Ranges are deliberately wide.
METRIC_RANGES: dict[str, tuple[float, float, str]] = {
    "gpu_mem_used_pct": (12.0, 99.5, "%"),
    "gpu_util_pct": (0.0, 99.0, "%"),
    "host_mem_used_pct": (18.0, 98.0, "%"),
    "host_cpu_pct": (5.0, 96.0, "%"),
    "disk_used_pct": (20.0, 100.0, "%"),
    "disk_read_mbps": (0.0, 2400.0, "MB/s"),
    "disk_write_mbps": (0.0, 1800.0, "MB/s"),
    "net_rx_mbps": (0.0, 9500.0, "Mb/s"),
    "net_tx_retrans_pct": (0.0, 14.0, "%"),
    "step_time_s": (0.4, 90.0, "s"),
    "loss": (0.4, 12.0, "nats"),
    "upstream_error_rate": (0.0, 62.0, "%"),
    "upstream_qps": (0.0, 900.0, "qps"),
}

# Metric ranges used for os-level names emitted by specific scenarios.
METRIC_SHAPES = {
    "ramp_to_ceiling",
    "spike_then_zero",
    "flat",
    "noisy_high",
    "cliff_to_zero",
    "staircase",
    "sawtooth_rising",
    "periodic_gap",
}

# Shape -> a short human phrase, used in the README-adjacent console output.
SHAPE_DESCRIPTIONS = {
    "ramp_to_ceiling": "monotone climb into the ceiling",
    "spike_then_zero": "sharp spike, then dead",
    "flat": "plateau with no movement",
    "noisy_high": "elevated and erratic",
    "cliff_to_zero": "healthy, then an instant drop to zero",
    "staircase": "descending steps, not a crash",
    "sawtooth_rising": "rising sawtooth, floor creeps up",
    "periodic_gap": "intermittent gaps in throughput",
}


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------


@dataclass
class Team:
    team_id: str
    name: str
    owner: str
    cost_center: str


@dataclass
class Node:
    node_id: str
    cluster: str
    pool: str
    gpus: int
    host_mem_gib: int
    local_disk_gib: int
    healthy: bool


@dataclass
class LogRecord:
    job_id: str
    ts: str
    level: str
    rank: int
    source: str
    message: str


@dataclass
class MetricSeries:
    job_id: str
    metric: str
    unit: str
    ts_start: str
    step_s: int
    values: list[float]


@dataclass
class Job:
    job_id: str
    name: str
    team_id: str
    submitter: str
    kind: str
    cluster: str
    queue: str
    node_ids: list[str]
    world_size: int
    status: str  # SUCCEEDED | FAILED | NEVER_STARTED
    exit_code: int | None
    submitted_at: str
    started_at: str | None
    finished_at: str | None
    duration_s: int
    image: str
    workdir: str
    # ground truth -- never exposed through the agent's tools
    root_cause: str | None = None
    root_cause_name: str | None = None
    category: str | None = None
    difficulty: str | None = None
    confounders: list[str] = field(default_factory=list)
    incident_id: str | None = None
    is_insufficient_evidence: bool = False
    summary: str = ""


@dataclass
class Incident:
    incident_id: str
    job_id: str
    team_id: str
    opened_at: str
    closed_at: str | None
    severity: str
    root_cause: str
    root_cause_name: str
    category: str
    difficulty: str
    resolution: str
    timeline: list[dict[str, str]]


@dataclass
class WorldSummary:
    n_jobs: int
    n_failed: int
    n_succeeded: int
    n_never_started: int
    n_incidents: int
    n_log_records: int
    n_metric_series: int
    seed: int
    scenario_counts: dict[str, int]
    category_counts: dict[str, int]
    difficulty_counts: dict[str, int]


# --------------------------------------------------------------------------
# Time helpers. All timestamps are synthetic but internally consistent.
# --------------------------------------------------------------------------

DAY = 86_400
BASE_EPOCH = 1_756_512_000  # a fixed instant; nothing depends on the real clock


def _iso(ts: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_dur(s: int) -> str:
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# --------------------------------------------------------------------------
# Metric shape synthesis
# --------------------------------------------------------------------------


def _shape_series(shape: str, n: int, rng: random.Random) -> list[float]:
    """Return a normalized 0..1 series for the named shape.

    These are written to have *clean, discriminating* signatures rather than to
    look maximally organic, because they are the evidence a classifier is
    scored on: a ramp with heavy noise has local maxima, and a classifier that
    calls it a sawtooth is not wrong -- the data really does contain both
    features. `tests/test_signals.py::test_shapes_round_trip` asserts that the
    generator and the classifier agree, so the two cannot drift apart silently.
    """
    if shape == "flat":
        base = rng.uniform(0.30, 0.65)
        return [_clip01(base + rng.gauss(0, 0.008)) for _ in range(n)]

    if shape == "noisy_high":
        # No trend at all: high mean, wide scatter, and a fixed floor. The
        # absence of drift is what distinguishes it from a leak.
        base = rng.uniform(0.70, 0.88)
        return [_clip01(base + rng.gauss(0, 0.11)) for _ in range(n)]

    if shape == "cliff_to_zero":
        cut = int(n * rng.uniform(0.5, 0.7))
        healthy = rng.uniform(0.55, 0.80)
        return [_clip01(healthy + rng.gauss(0, 0.04)) if i < cut else 0.0 for i in range(n)]

    if shape == "spike_then_zero":
        # Healthy, then a single excursion to the ceiling, then dead. The dead
        # tail must be at least a fifth of the series (the classifier's window)
        # so the two agree on where the series stops.
        tail = max(int(n * rng.uniform(0.22, 0.30)), 3)
        live = n - tail
        out: list[float] = []
        for i in range(live - 1):
            out.append(_clip01(0.35 + rng.gauss(0, 0.05)))
        out.append(1.0)
        out.extend([0.0] * tail)
        return out

    if shape == "ramp_to_ceiling":
        # Smooth by construction: a gentle curve with tiny noise. The exponent
        # makes the approach to the ceiling accelerate, which is what
        # distinguishes "slow accumulation, then sudden exhaustion" from a
        # straight line.
        out = []
        for i in range(n):
            t = i / max(n - 1, 1)
            out.append(_clip01(0.28 + 0.70 * (t**2.1) + rng.gauss(0, 0.006)))
        return out

    if shape == "sawtooth_rising":
        # Repeated peaks AND a floor that climbs: the two features together are
        # what "this is a leak, not jitter" means.
        period = max(int(n / rng.uniform(5, 8)), 4)
        out = []
        for i in range(n):
            phase = (i % period) / period
            creep = 0.50 * (i / max(n - 1, 1))
            out.append(_clip01(0.22 + creep + 0.26 * phase + rng.gauss(0, 0.008)))
        return out

    if shape == "staircase":
        # Plateaus with low within-plate noise, so the level changes dominate.
        steps = rng.randint(4, 6)
        out = []
        for i in range(n):
            step = min(int(i / max(n / steps, 1)), steps - 1)
            out.append(_clip01(0.90 - step * (0.80 / max(steps - 1, 1)) + rng.gauss(0, 0.006)))
        return out

    if shape == "periodic_gap":
        # Regular gaps rather than random drops: a network path that
        # intermittently disappears looks like this, an overloaded disk does not.
        period = max(int(n / rng.uniform(5, 8)), 4)
        out = []
        for i in range(n):
            if (i % period) in (0, 1):
                out.append(0.0)
            else:
                out.append(_clip01(0.62 + rng.gauss(0, 0.05)))
        return out

    # Unknown shape name: fail loudly rather than emitting plausible noise that
    # would silently weaken the dataset.
    raise ValueError(f"unknown metric shape: {shape!r}")


def _clip01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _scale(shape_values: Sequence[float], metric: str) -> list[float]:
    lo, hi, _unit = METRIC_RANGES.get(metric, (0.0, 1.0, ""))
    return [round(lo + v * (hi - lo), 2) for v in shape_values]


# --------------------------------------------------------------------------
# Log synthesis
# --------------------------------------------------------------------------

LOG_LEVELS = ["INFO", "WARNING", "ERROR", "CRITICAL"]

# Ambient framework chatter used to pad a job's log so the failure is not the
# only thing in it. Realism matters here: a dataset where line 1 is always the
# error teaches the model a shortcut that does not survive contact with prod.
AMBIENT_INFO = [
    "initializing distributed process group: backend=nccl world_size={world}",
    "rank {rank} bound to device cuda:{dev}",
    "dataloader workers started: {workers}",
    "loaded dataset shard {shard} ({rows} rows)",
    "model built: params={params}M layers={layers} dtype=bf16",
    "optimizer ready: adamw lr={lr} betas=(0.9,0.95) wd=0.1",
    "step {step}: loss={loss} lr={lr} grad_norm={gnorm} step_time={st}s",
    "gradient accumulation steps={accum} micro_batch={mb}",
    "checkpoint written to {workdir}/ckpt-step{step}",
    "reducing loss over {world} ranks",
    "learning rate warmup complete at step {step}",
    "throughput {tput} tokens/s across {world} ranks",
]

AMBIENT_WARN = [
    "rank {rank} step time {st}s exceeds p99 budget",
    "dataloader falling behind: prefetch queue at {q}/{qmax}",
    "allreduce took {st}s on rank {rank}",
    "gradient norm unusually large: {gnorm}",
    "retrying metadata lookup for shard {shard}",
    # Environment noise. These lines name symptoms that belong to OTHER faults
    # and appear, harmlessly, in healthy runs: a transient allreduce hiccup, a
    # dataloader that briefly starved, a prefetcher that hit its cache limit.
    #
    # They exist because a dataset in which only the true fault appears is a
    # dataset where the answer can be read off the first matching substring.
    # Real logs are full of near-misses, and a system that treats every
    # timeout-shaped string as evidence will diagnose transient noise as an
    # outage. Keeping them here is what makes precision a real property of the
    # agent rather than an artefact of a clean corpus.
    "rank {rank} slow allreduce: 1 collective took {st}s, retrying",
    "transient NCCL retry on rank {rank} succeeded after 1 attempt",
    "temporary failure in name resolution for shard {shard}, retrying",
    "WARNING: disk usage at {du}% on local scratch {workdir}",
    "prefetch queue hit cache limit {qmax} on rank {rank}, dropping to {q}",
    "connection reset by peer while fetching shard {shard}, retrying",
]


def _render(tpl: str, rng: random.Random, world_size: int) -> str:
    return tpl.format(
        world=world_size,
        rank=rng.randrange(world_size),
        dev=rng.randrange(8),
        workers=rng.choice([4, 8, 12, 16]),
        shard=rng.randrange(64, 4096),
        rows=rng.randrange(10_000, 2_000_000),
        params=rng.choice([0.5, 1.3, 3, 7, 13, 34, 70]),
        layers=rng.choice([12, 24, 32, 48, 64]),
        lr=round(rng.uniform(1e-5, 3e-4), 7),
        loss=round(rng.uniform(0.6, 9.0), 4),
        gnorm=round(rng.uniform(0.2, 4.5), 3),
        st=round(rng.uniform(0.4, 14.0), 2),
        step=rng.randrange(10, 4000),
        accum=rng.choice([1, 2, 4, 8]),
        mb=rng.choice([1, 2, 4, 8]),
        workdir=rng.choice(WORKDIRS),
        tput=rng.randrange(2_000, 90_000),
        q=rng.randrange(1, 32),
        qmax=rng.choice([32, 64, 128]),
        du=rng.randrange(40, 96),
    )


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------


def _job_seed(master: int, index: int) -> int:
    return (master * 1_000_003 + index * 9_176) & 0xFFFFFFFF


def _sample_scenario(
    playbook: Playbook, rng: random.Random, forced: str | None = None
) -> Scenario:
    """Sample a failure.

    Difficulty is drawn FIRST, according to the playbook's declared split, and
    only then is a scenario chosen within that difficulty by prevalence. Drawing
    directly by prevalence would let the easy scenarios dominate the dataset
    (there are more of them and they are individually common), which would make
    the evaluation report look far better than the system really is.
    """
    if forced:
        return playbook.get(forced)

    # The "unknowable" slice is controlled by an explicit ratio rather than by
    # prevalence, because its share is a deliberate design choice: the dataset
    # must contain enough of them to measure calibration, but not so many that
    # refusing to answer becomes a winning strategy.
    if playbook.insufficient_evidence_ratio > 0 and rng.random() < playbook.insufficient_evidence_ratio:
        return playbook.get(INSUFFICIENT_EVIDENCE)

    ratios = playbook.split_ratios or {"easy": 0.6, "medium": 0.3, "hard": 0.1}
    levels = [lv for lv in ("easy", "medium", "hard") if ratios.get(lv, 0) > 0]
    if not levels:
        levels = ["easy", "medium", "hard"]
    difficulty = rng.choices(levels, weights=[ratios.get(lv, 0.1) for lv in levels], k=1)[0]

    pool = playbook.by_difficulty(difficulty)
    if not pool:
        pool = list(playbook.scenarios.values())
    weights = [max(s.prevalence, 0) for s in pool]
    if sum(weights) <= 0:
        weights = [1.0] * len(pool)
    return rng.choices(pool, weights=weights, k=1)[0]


# Scenarios that fail before the workload produces any runtime telemetry.
# Either the container never started, or it died during startup/import.
_STARTUP_CATEGORIES = {"scheduling"}
_STARTUP_SCENARIOS = {"compile_error", "missing_dependency", "config_parse_error"}


def generate_world(
    seed: int = 20260929,
    n_jobs: int = 400,
    playbook: Playbook | None = None,
    force_scenarios: list[str] | None = None,
) -> World:
    """Build a complete synthetic world.

    `force_scenarios` pins the failure mix (one scenario per job, cycled) which
    is what the test suite and the smoke demo use to get full coverage of the
    playbook without a large `n_jobs`.
    """
    pb = playbook or load_playbook()
    rng = random.Random(seed)

    teams = _build_teams(rng)
    nodes = _build_nodes(rng)

    jobs: list[Job] = []
    logs: list[LogRecord] = []
    metrics: list[MetricSeries] = []
    incidents: list[Incident] = []

    for i in range(n_jobs):
        jrng = random.Random(_job_seed(seed, i))
        forced = None
        if force_scenarios:
            forced = force_scenarios[i % len(force_scenarios)]

        # ~18% of jobs succeed. A dataset of nothing but failures teaches a
        # model that "there is always something wrong", which is a bias worth
        # avoiding in the teaching material.
        if forced is None and jrng.random() < 0.18:
            job = _build_job(i, jrng, teams, nodes, pb, scenario=None)
            jobs.append(job)
            logs.extend(_synth_success_logs(job, jrng))
            metrics.extend(_synth_success_metrics(job, jrng))
            continue

        scenario = _sample_scenario(pb, jrng, forced=forced)
        job = _build_job(i, jrng, teams, nodes, pb, scenario=scenario)
        jobs.append(job)
        logs.extend(_synth_failure_logs(job, jrng, scenario, pb))
        metrics.extend(_synth_failure_metrics(job, jrng, scenario))
        if scenario.terminal:
            incidents.append(_build_incident(job, jrng, scenario))

    # Order logs and metrics as a log store would present them.
    logs.sort(key=lambda r: (r.job_id, r.ts))
    metrics.sort(key=lambda m: (m.job_id, m.metric))

    return World(
        company=COMPANY,
        seed=seed,
        teams=teams,
        nodes=nodes,
        jobs=jobs,
        logs=logs,
        metrics=metrics,
        incidents=incidents,
        playbook=pb,
    )


def _build_teams(rng: random.Random) -> list[Team]:
    teams = []
    for idx, name in enumerate(TEAM_NAMES):
        teams.append(
            Team(
                team_id=f"team-{idx + 1:02d}",
                name=name,
                owner=rng.choice(LEADERS),
                cost_center=f"CC-{rng.randrange(1000, 9999)}",
            )
        )
    return teams


def _build_nodes(rng: random.Random) -> list[Node]:
    nodes: list[Node] = []
    n = 0
    for cluster in CLUSTERS:
        for pool, gpus, dgpu in [
            ("gpu-h800", 8, 0),
            ("gpu-a100", 8, 0),
            ("npu-910b", 8, 0),
            ("cpu-batch", 0, 0),
        ]:
            for k in range(3):
                n += 1
                nodes.append(
                    Node(
                        node_id=f"{cluster}-{pool}-{k:02d}",
                        cluster=cluster,
                        pool=pool,
                        gpus=gpus,
                        host_mem_gib=rng.choice([512, 1024, 2048]),
                        local_disk_gib=rng.choice([2048, 4096, 8192]),
                        healthy=rng.random() > 0.03,
                    )
                )
    return nodes


def _build_job(
    index: int,
    rng: random.Random,
    teams: list[Team],
    nodes: list[Node],
    pb: Playbook,
    scenario: Scenario | None,
) -> Job:
    team = rng.choice(teams)
    kind = rng.choice(JOB_KINDS)
    cluster = rng.choice(CLUSTERS)
    queue = rng.choice(QUEUES)
    world_size = rng.choice([1, 2, 4, 8, 16, 32, 64])
    gpu_nodes = [n for n in nodes if n.cluster == cluster and n.gpus > 0]
    world_size = min(world_size, max(len(gpu_nodes), 1))
    node_ids = [n.node_id for n in rng.sample(gpu_nodes, world_size)]

    submitted = BASE_EPOCH + rng.randrange(0, 14 * DAY)
    job_id = f"job-{submitted:x}-{index:04d}"
    name = f"{kind}-{team.name}-{rng.randrange(100, 999)}"
    workdir = f"{rng.choice(WORKDIRS)}/{job_id}"

    if scenario is None:
        # Success: the queues are real, so jobs do start, and the duration is
        # plausible rather than instant.
        duration = rng.randrange(600, 6 * 3600)
        started = submitted + rng.randrange(5, 900)
        return Job(
            job_id=job_id,
            name=name,
            team_id=team.team_id,
            submitter=f"{rng.choice(FIRST_NAMES)}.{rng.randrange(10, 99)}",
            kind=kind,
            cluster=cluster,
            queue=queue,
            node_ids=node_ids,
            world_size=world_size,
            status="SUCCEEDED",
            exit_code=0,
            submitted_at=_iso(submitted),
            started_at=_iso(started),
            finished_at=_iso(started + duration),
            duration_s=duration,
            image=f"registry.ailab.internal/train/{kind}:v{rng.randrange(1, 40)}",
            workdir=workdir,
            summary="completed without error",
        )

    # Two ways a failure produces no runtime telemetry: the container never
    # started (scheduling) or it died during startup (build/import/config).
    # Both must be modelled, because a dataset where every failure has a full
    # metric series teaches the model that telemetry is always available --
    # which is exactly the assumption the `insufficient_evidence` case punishes.
    never_started = scenario.category in _STARTUP_CATEGORIES and scenario.id in {
        "insufficient_resources",
        "image_pull_failed",
    }
    startup_failure = scenario.id in _STARTUP_SCENARIOS

    if never_started:
        status = "NEVER_STARTED"
        started = None
        duration = rng.randrange(30, 900)
        finished = submitted + duration
    elif startup_failure:
        status = "FAILED"
        started = submitted + rng.randrange(3, 60)
        duration = rng.randrange(4, 150)
        finished = started + duration
    else:
        status = "FAILED"
        started = submitted + rng.randrange(5, 600)
        duration = rng.randrange(120, 5 * 3600)
        if scenario.category == "storage" and rng.random() < 0.4:
            duration = rng.randrange(1800, 7 * 3600)  # slow accumulation
        finished = started + duration

    confounders: list[str] = []
    if scenario.difficulty == "hard" and scenario.distractors and scenario.id != INSUFFICIENT_EVIDENCE:
        # Hard cases carry a genuine second signal from a plausible-but-wrong
        # scenario, so the agent has to discriminate rather than pattern-match.
        # The undecidable case is excluded on purpose: its difficulty comes from
        # the *absence* of evidence, not from the presence of a rival signal. If
        # it carried a rival signature it would simply be a different diagnosable
        # failure, and the "correctly declined to guess" metric would never fire.
        confounders = [rng.choice(scenario.distractors)]

    return Job(
        job_id=job_id,
        name=name,
        team_id=team.team_id,
        submitter=f"{rng.choice(FIRST_NAMES)}.{rng.randrange(10, 99)}",
        kind=kind,
        cluster=cluster,
        queue=queue,
        node_ids=node_ids,
        world_size=world_size,
        status=status,
        exit_code=(rng.choice(list(scenario.exit_codes)) if scenario.exit_codes else 1),
        submitted_at=_iso(submitted),
        started_at=_iso(started) if started else None,
        finished_at=_iso(finished),
        duration_s=duration,
        image=f"registry.ailab.internal/train/{kind}:v{rng.randrange(1, 40)}",
        workdir=workdir,
        root_cause=scenario.root_cause,
        root_cause_name=scenario.name,
        category=scenario.category,
        difficulty=scenario.difficulty,
        confounders=confounders,
        is_insufficient_evidence=(scenario.id == INSUFFICIENT_EVIDENCE),
        summary=f"failed: {scenario.name}",
    )


# --------------------------------------------------------------------------
# Log synthesis
# --------------------------------------------------------------------------


def _synth_success_logs(job: Job, rng: random.Random) -> list[LogRecord]:
    import datetime as _dt

    start = int(_dt.datetime.strptime(job.started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp())
    n = rng.randrange(18, 40)
    out: list[LogRecord] = []
    for k in range(n):
        ts = start + int(job.duration_s * k / n)
        is_warn = rng.random() < 0.12
        tpl = rng.choice(AMBIENT_WARN if is_warn else AMBIENT_INFO)
        out.append(
            LogRecord(
                job_id=job.job_id,
                ts=_iso(ts),
                level="WARNING" if is_warn else "INFO",
                rank=rng.randrange(job.world_size),
                source="trainer",
                message=_render(tpl, rng, job.world_size),
            )
        )
    out.append(
        LogRecord(
            job_id=job.job_id,
            ts=job.finished_at,
            level="INFO",
            rank=0,
            source="trainer",
            message=f"training finished: steps={rng.randrange(200, 9000)} final_loss={round(rng.uniform(0.4, 3.0), 4)} elapsed={_fmt_dur(job.duration_s)}",
        )
    )
    return out


def _synth_failure_logs(
    job: Job, rng: random.Random, scenario: Scenario, pb: Playbook
) -> list[LogRecord]:
    """Emit a plausible log for a failure.

    Structure: ambient history -> (optional confounding error) -> the real
    error, deliberately placed on a non-zero rank for cascade-prone scenarios,
    because "rank 0 says the loudest thing" is the exact trap the runbook warns
    about.
    """
    import datetime as _dt

    def _ts_of(iso: str) -> int:
        return int(
            _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )

    begin = _ts_of(job.started_at) if job.started_at else _ts_of(job.submitted_at)
    end = _ts_of(job.finished_at)
    span = max(end - begin, 1)

    out: list[LogRecord] = []

    # 1. ambient history, truncated when the job failed early
    if job.started_at:
        n_ambient = rng.randrange(6, 26) if job.duration_s > 180 else rng.randrange(1, 5)
        for k in range(n_ambient):
            ts = begin + int(span * 0.85 * k / max(n_ambient, 1))
            is_warn = rng.random() < 0.15
            tpl = rng.choice(AMBIENT_WARN if is_warn else AMBIENT_INFO)
            out.append(
                LogRecord(
                    job_id=job.job_id,
                    ts=_iso(ts),
                    level="WARNING" if is_warn else "INFO",
                    rank=rng.randrange(job.world_size),
                    source="trainer",
                    message=_render(tpl, rng, job.world_size),
                )
            )

    # 2. a confounder, earlier than the real error, from a plausible rival
    #    scenario. This is what makes `hard` genuinely hard.
    if job.confounders:
        other = pb.get(job.confounders[0])
        if other.log_patterns:
            ts = begin + int(span * rng.uniform(0.80, 0.90))
            pat = rng.choice(list(other.log_patterns))
            out.append(
                LogRecord(
                    job_id=job.job_id,
                    ts=_iso(ts),
                    level="ERROR",
                    rank=rng.randrange(max(job.world_size - 1, 1)),
                    source="trainer",
                    message=_decorate(pat.pattern, rng, job),
                )
            )

    # 3. the real error
    for pat in scenario.log_patterns:
        if rng.random() > min(0.35 + pat.weight, 0.98):
            continue
        ts = begin + int(span * rng.uniform(0.90, 1.0))
        rank = rng.randrange(max(job.world_size - 1, 1)) if scenario.category == "network" and job.world_size > 1 else 0
        level = "ERROR" if scenario.id != "insufficient_evidence" else "WARNING"
        msg = _decorate(pat.pattern, rng, job)
        if scenario.id == "insufficient_evidence":
            msg = f"[log level raised to ERROR mid-run; verbose output rotated out] {msg}" if rng.random() < 0.3 else msg
        out.append(
            LogRecord(
                job_id=job.job_id,
                ts=_iso(ts),
                level=level,
                rank=rank,
                source="trainer" if scenario.category != "scheduling" else "scheduler",
                message=msg,
            )
        )

    # 4. cascade noise for cascade-prone scenarios: peers time out AFTER the
    #    first rank dies. Rank 0 is deliberately among the loud ones.
    if scenario.id in {"collective_timeout", "rank_crash_assert"} and job.world_size > 1:
        for rank in range(job.world_size):
            ts = end - rng.randrange(1, 6)
            out.append(
                LogRecord(
                    job_id=job.job_id,
                    ts=_iso(ts),
                    level="ERROR",
                    rank=rank,
                    source="trainer",
                    message=f"Watchdog caught collective operation timeout: WorkNCCL(SeqNum={rng.randrange(100, 9999)}, OpType=ALLREDUCE, Timeout(ms)={rng.choice([600000, 1800000])}) ran for {rng.randrange(600, 1800)} milliseconds before timing out.",
                )
            )

    # 5. a terminal line naming the exit, as a framework would print
    out.append(
        LogRecord(
            job_id=job.job_id,
            ts=_iso(end),
            level="ERROR" if scenario.id != "insufficient_evidence" else "WARNING",
            rank=0,
            source="scheduler" if scenario.category in {"scheduling", "code"} else "trainer",
            message=(
                f"process exited with code {job.exit_code}"
                + (f" after {_fmt_dur(job.duration_s)}" if job.started_at else " before the workload started")
            ),
        )
    )
    out.sort(key=lambda r: r.ts)
    return out


def _decorate(pattern: str, rng: random.Random, job: Job) -> str:
    """Wrap a raw pattern in a realistic surrounding message.

    The pattern itself is always present verbatim, because both the retrieval
    step and the mock reasoner match on it. The wrapper text is what makes the
    log look like output from a real process instead of a labelled fixture.
    """
    prefixes = [
        "Traceback (most recent call last): ",
        "",
        f"[rank {rng.randrange(max(job.world_size, 1))}] ",
        f"{job.workdir}: ",
        "RuntimeError: ",
        "ERROR: ",
    ]
    suffixes = [
        "",
        f" (job={job.job_id})",
        f" at {job.workdir}/train.py:{rng.randrange(40, 900)}",
        f" [elapsed={_fmt_dur(job.duration_s)}]",
    ]
    return f"{rng.choice(prefixes)}{pattern}{rng.choice(suffixes)}".strip()


# --------------------------------------------------------------------------
# Metric synthesis
# --------------------------------------------------------------------------


def _synth_success_metrics(job: Job, rng: random.Random) -> list[MetricSeries]:
    if not job.started_at:
        return []
    n = max(int(job.duration_s / 60), 6)
    n = min(n, 240)
    out = [
        _series(job, "gpu_util_pct", rng, n, "noisy_high", scale_override=(35.0, 92.0)),
        _series(job, "gpu_mem_used_pct", rng, n, "flat", scale_override=(60.0, 88.0)),
        _series(job, "step_time_s", rng, n, "flat", scale_override=(0.5, 4.0)),
        _series(job, "loss", rng, n, "staircase", scale_override=(0.5, 6.0)),
        _series(job, "host_mem_used_pct", rng, n, "flat", scale_override=(30.0, 70.0)),
    ]
    return out


def _synth_failure_metrics(
    job: Job, rng: random.Random, scenario: Scenario
) -> list[MetricSeries]:
    if not job.started_at:
        return []
    # The "no decisive evidence" case is deliberately telemetry-poor: the job
    # ran, died, and nobody had metrics export switched on. Emitting a fallback
    # series here would quietly hand the agent the evidence the scenario is
    # designed to withhold.
    if scenario.is_unknown:
        return []

    n = max(int(job.duration_s / 60), 6)
    n = min(n, 240)

    out: list[MetricSeries] = []
    for ms in scenario.metric_shapes:
        if ms.name not in METRIC_RANGES:
            continue
        out.append(_series(job, ms.name, rng, n, ms.shape))

    # Hard cases also carry the confounder's metric signature.
    if job.confounders:
        for cid in job.confounders:
            other = _playbook_singleton().get(cid)
            if other.metric_shapes:
                ms = other.metric_shapes[0]
                if ms.name in METRIC_RANGES and ms.name not in {s.metric for s in out}:
                    out.append(_series(job, ms.name, rng, n, ms.shape))

    # A couple of always-on series so a dashboard is never empty.
    have = {s.metric for s in out}
    if "step_time_s" not in have:
        out.append(_series(job, "step_time_s", rng, n, "noisy_high" if scenario.category == "runtime" else "flat"))

    return out


def _series(
    job: Job,
    metric: str,
    rng: random.Random,
    n: int,
    shape: str,
    scale_override: tuple[float, float] | None = None,
) -> MetricSeries:
    raw = _shape_series(shape, n, rng)
    lo, hi, unit = METRIC_RANGES.get(metric, (0.0, 1.0, ""))
    if scale_override is not None:
        lo, hi = scale_override
    values = [round(lo + v * (hi - lo), 2) for v in raw]
    return MetricSeries(
        job_id=job.job_id,
        metric=metric,
        unit=unit,
        ts_start=job.started_at or job.submitted_at,
        step_s=max(job.duration_s // max(n, 1), 1),
        values=values,
    )


_PLAYBOOK_SINGLETON: Playbook | None = None


def _playbook_singleton() -> Playbook:
    global _PLAYBOOK_SINGLETON
    if _PLAYBOOK_SINGLETON is None:
        _PLAYBOOK_SINGLETON = load_playbook()
    return _PLAYBOOK_SINGLETON


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


def _build_incident(job: Job, rng: random.Random, scenario: Scenario) -> Incident:
    import datetime as _dt

    def _ts_of(iso: str) -> int:
        return int(
            _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )

    end = _ts_of(job.finished_at)
    opened = end + rng.randrange(30, 1200)
    closed = opened + rng.randrange(300, 2 * DAY)

    timeline = [
        {"ts": _iso(opened), "actor": "alerts", "event": f"incident opened for {job.job_id} ({job.name})"},
        {"ts": _iso(opened + 60), "actor": job.submitter, "event": "acknowledged; reproducing locally"},
        {
            "ts": _iso(opened + rng.randrange(300, 3600)),
            "actor": job.submitter,
            "event": f"root cause identified: {scenario.name}",
        },
        {"ts": _iso(closed), "actor": job.submitter, "event": "resolved and verified on re-run"},
    ]
    if job.confounders:
        timeline.insert(
            2,
            {
                "ts": _iso(opened + 120),
                "actor": job.submitter,
                "event": f"initially suspected {_playbook_singleton().get(job.confounders[0]).name}; ruled out",
            },
        )

    severity = "SEV3"
    if scenario.difficulty == "hard" and rng.random() < 0.5:
        severity = "SEV2"
    if job.duration_s > 4 * 3600:
        severity = "SEV2"

    return Incident(
        incident_id=f"INC-{opened % 1_000_000:06d}",
        job_id=job.job_id,
        team_id=job.team_id,
        opened_at=_iso(opened),
        closed_at=_iso(closed),
        severity=severity,
        root_cause=scenario.root_cause,
        root_cause_name=scenario.name,
        category=scenario.category,
        difficulty=scenario.difficulty,
        resolution=scenario.remediation,
        timeline=timeline,
    )


# --------------------------------------------------------------------------
# World container + serialization
# --------------------------------------------------------------------------


@dataclass
class World:
    company: str
    seed: int
    teams: list[Team]
    nodes: list[Node]
    jobs: list[Job]
    logs: list[LogRecord]
    metrics: list[MetricSeries]
    incidents: list[Incident]
    playbook: Playbook

    # ---- lookups -------------------------------------------------------
    def job(self, job_id: str) -> Job | None:
        for j in self.jobs:
            if j.job_id == job_id:
                return j
        return None

    def team(self, team_id: str) -> Team | None:
        for t in self.teams:
            if t.team_id == team_id:
                return t
        return None

    def logs_for(self, job_id: str) -> list[LogRecord]:
        return [r for r in self.logs if r.job_id == job_id]

    def metrics_for(self, job_id: str) -> list[MetricSeries]:
        return [m for m in self.metrics if m.job_id == job_id]

    def incident_for(self, job_id: str) -> Incident | None:
        for inc in self.incidents:
            if inc.job_id == job_id:
                return inc
        return None

    def failed_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.status == "FAILED"]

    def summary(self) -> WorldSummary:
        sc: dict[str, int] = {}
        cc: dict[str, int] = {}
        dc: dict[str, int] = {}
        for j in self.jobs:
            if not j.root_cause:
                continue
            sc[j.root_cause] = sc.get(j.root_cause, 0) + 1
            cc[j.category or "?"] = cc.get(j.category or "?", 0) + 1
            dc[j.difficulty or "?"] = dc.get(j.difficulty or "?", 0) + 1
        return WorldSummary(
            n_jobs=len(self.jobs),
            n_failed=sum(1 for j in self.jobs if j.status == "FAILED"),
            n_succeeded=sum(1 for j in self.jobs if j.status == "SUCCEEDED"),
            n_never_started=sum(1 for j in self.jobs if j.status == "NEVER_STARTED"),
            n_incidents=len(self.incidents),
            n_log_records=len(self.logs),
            n_metric_series=len(self.metrics),
            seed=self.seed,
            scenario_counts=dict(sorted(sc.items())),
            category_counts=dict(sorted(cc.items())),
            difficulty_counts=dict(sorted(dc.items())),
        )


# ---- serialization -------------------------------------------------------


def write_world(world: World, out_dir: str | Path) -> dict[str, str]:
    """Write the world to disk as the artifacts the rest of the system reads."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def _dump(name: str, objs: Iterable[Any]) -> str:
        p = out / name
        with p.open("w", encoding="utf-8") as fh:
            for o in objs:
                fh.write(json.dumps(asdict(o) if not isinstance(o, dict) else o, ensure_ascii=False) + "\n")
        return str(p)

    written: dict[str, str] = {}
    written["jobs"] = _dump("jobs.jsonl", world.jobs)
    written["logs"] = _dump("logs.jsonl", world.logs)
    written["metrics"] = _dump("metrics.jsonl", world.metrics)
    written["incidents"] = _dump("incidents.jsonl", world.incidents)
    written["teams"] = _dump("teams.jsonl", world.teams)
    written["nodes"] = _dump("nodes.jsonl", world.nodes)

    # A small flat failure table: handy for pandas, charts, and eyeballing.
    p = out / "failures.csv"
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["job_id", "name", "team_id", "kind", "cluster", "queue", "status",
             "exit_code", "duration_s", "root_cause", "root_cause_name",
             "category", "difficulty", "confounders", "insufficient_evidence"]
        )
        for j in world.jobs:
            if j.status == "SUCCEEDED":
                continue
            w.writerow(
                [j.job_id, j.name, j.team_id, j.kind, j.cluster, j.queue, j.status,
                 j.exit_code, j.duration_s, j.root_cause or "", j.root_cause_name or "",
                 j.category or "", j.difficulty or "", "|".join(j.confounders),
                 int(j.is_insufficient_evidence)]
            )
    written["failures_csv"] = str(p)

    # Ground truth as its own file, kept separate on purpose: the agent never
    # reads `ground_truth.jsonl`, only the evaluator does.
    written["ground_truth"] = _dump(
        "ground_truth.jsonl",
        (
            {
                "job_id": j.job_id,
                "root_cause": j.root_cause,
                "root_cause_name": j.root_cause_name,
                "category": j.category,
                "difficulty": j.difficulty,
                "confounders": j.confounders,
                "insufficient_evidence": j.is_insufficient_evidence,
                "exit_code": j.exit_code,
                "status": j.status,
            }
            for j in world.jobs
            if j.status != "SUCCEEDED"
        ),
    )

    (out / "meta.json").write_text(
        json.dumps(
            {
                "company": world.company,
                "seed": world.seed,
                "playbook_schema_version": world.playbook.schema_version,
                "domain": world.playbook.domain,
                "summary": asdict(world.summary()),
                "notice": (
                    "Entirely synthetic. Generated locally by ailab_ops.datagen from "
                    "faults.yaml. No production data was read, copied, or derived."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    written["meta"] = str(out / "meta.json")
    return written


def load_world(data_dir: str | Path) -> World:
    """Read a written world back from disk.

    Only the evaluator and the tools need this; the agent sees the world
    exclusively through tool calls.
    """
    d = Path(data_dir)

    def _read(name: str) -> list[dict]:
        p = d / name
        if not p.exists():
            return []
        with p.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    teams = [Team(**r) for r in _read("teams.jsonl")]
    nodes = [Node(**r) for r in _read("nodes.jsonl")]
    jobs = [Job(**r) for r in _read("jobs.jsonl")]
    logs = [LogRecord(**r) for r in _read("logs.jsonl")]
    metrics = [MetricSeries(**r) for r in _read("metrics.jsonl")]
    incidents = [Incident(**r) for r in _read("incidents.jsonl")]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
    pb = load_playbook()
    return World(
        company=meta.get("company", COMPANY),
        seed=int(meta.get("seed", 0)),
        teams=teams,
        nodes=nodes,
        jobs=jobs,
        logs=logs,
        metrics=metrics,
        incidents=incidents,
        playbook=pb,
    )


def write_timeseries_csv(world: World, path: str | Path, max_jobs: int = 40) -> str:
    """Long-format CSV for the metrics of a subset of jobs.

    Kept separate from the main artifacts because it can get large; the JSONL
    form is the primary one.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["job_id", "metric", "unit", "ts_start", "step_s", "idx", "value"])
        for s in world.metrics[: max_jobs * 8]:
            for idx, v in enumerate(s.values):
                w.writerow([s.job_id, s.metric, s.unit, s.ts_start, s.step_s, idx, v])
    return str(p)
