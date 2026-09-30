"""Load generator.

Written with `asyncio` and `httpx` rather than with Locust or k6, for two
reasons: the load pattern needed here is not HTTP-shaped, it is *pipeline*
shaped (a mix of interactive and batch callers, with different priorities and
different limits), and the benchmark needs to read the server's own counters
afterwards to correlate what it sent with what the server did.

What this measures, and what it deliberately does not:

* It measures **end-to-end behaviour under concurrency**: completion rate,
  latency percentiles, rejection rate by kind, and whether the upstream gate
  actually bounded concurrency.
* It does **not** measure the speed of the language model. The backend here is
  the deterministic mock; what is being measured is the architecture --
  admission control, queueing, caching, degradation -- not inference
  throughput. The README states this so the numbers are not misread.

Three scenarios, each of which demonstrates one claim:

    steady     N clients at a moderate rate -> most requests succeed, latencies
               stable, essentially no rejections: the system is not fragile.
    burst      a step increase well past capacity -> the gate queues, excess is
               rejected with a clear reason rather than the upstream failing.
    hotkey     many clients asking the same question -> the cache absorbs it and
               the gate never saturates: the cheapest capacity is the one you
               do not use.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx


@dataclass
class ClientResult:
    ok: bool
    status: int
    latency_ms: float
    served_by: str = ""
    error: str = ""
    queued_ms: float = 0.0
    tokens: int = 0
    usd: float = 0.0


@dataclass
class BenchReport:
    scenario: str
    concurrency: int
    n_requests: int
    duration_s: float
    throughput_rps: float
    n_ok: int
    n_failed: int
    n_rejected: int
    status_counts: dict[str, int]
    served_by_counts: dict[str, int]
    error_counts: dict[str, int]
    latency: dict[str, float]
    tokens_total: int
    usd_total: float
    server_before: dict[str, Any] = field(default_factory=dict)
    server_after: dict[str, Any] = field(default_factory=dict)
    gate_would_have_exceeded: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_lines(self) -> list[str]:
        L = [
            "=" * 78,
            f"benchmark: {self.scenario}",
            "=" * 78,
            f"clients={self.concurrency}  requests={self.n_requests}  wall={self.duration_s:.2f}s  "
            f"throughput={self.throughput_rps:.1f} req/s",
            f"ok={self.n_ok}  failed={self.n_failed}  rejected={self.n_rejected}",
            f"status codes    {self.status_counts}",
            f"served by       {self.served_by_counts}",
        ]
        if self.error_counts:
            L.append(f"errors          {self.error_counts}")
        L += [
            f"latency         p50={self.latency['p50']:.0f}ms  p90={self.latency['p90']:.0f}ms  "
            f"p99={self.latency['p99']:.0f}ms  max={self.latency['max']:.0f}ms  mean={self.latency['mean']:.0f}ms",
            f"tokens          {self.tokens_total}  (${self.usd_total:.4f})",
        ]
        g_after = (self.server_after.get("gate") or {})
        if g_after:
            L.append(
                f"gate after      in_flight={g_after.get('in_flight')} queued={g_after.get('queued')} "
                f"admitted={g_after.get('admitted')} rejected_full={g_after.get('rejected_queue_full')} "
                f"rejected_timeout={g_after.get('rejected_timeout')}"
            )
        cache_after = (self.server_after.get("cache") or {})
        if cache_after:
            L.append(f"cache after     hit_rate={cache_after.get('hit_rate')} entries={cache_after.get('entries')}")
        for n in self.notes:
            L.append(f"note            {n}")
        L.append("=" * 78)
        return L


def percentiles(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    s = sorted(values)
    return {
        "p50": _pct(s, 0.50),
        "p90": _pct(s, 0.90),
        "p99": _pct(s, 0.99),
        "max": s[-1],
        "mean": statistics.fmean(s),
    }


def _pct(sorted_values: list[float], q: float) -> float:
    idx = min(int(round(q * (len(sorted_values) - 1))), len(sorted_values) - 1)
    return sorted_values[idx]


# --------------------------------------------------------------------------
# Request mix
# --------------------------------------------------------------------------


def build_question_mix(
    job_ids: Sequence[str], hotkey_ratio: float = 0.0, unique: bool = False, n: int = 0
) -> list[str]:
    """Question templates over the given job ids.

    `hotkey_ratio` models the real traffic shape of an operations assistant:
    during an incident, everybody asks about the same job. That is why the
    semantic cache matters more here than in a general-purpose chat product.

    `unique=True` defeats the cache on purpose by appending a per-request
    nonce. Without it, any scenario that repeats its question list is measuring
    the cache, not the component it claims to be measuring -- a mistake that is
    easy to make and produces a very flattering, entirely meaningless result.
    """
    if not job_ids:
        return ["Why did the last job fail?"]
    qs: list[str] = []
    for jid in job_ids:
        qs.append(f"Why did {jid} fail? Give me the root cause and what to do about it.")
        qs.append(f"Diagnose {jid}. What is the root cause?")
    if hotkey_ratio > 0:
        hot = job_ids[0]
        n_hot = int(len(qs) * hotkey_ratio / max(1 - hotkey_ratio, 1e-9))
        qs.extend([f"Diagnose {hot}. What is the root cause?"] * max(n_hot, 0))
    if unique:
        want = max(n, len(qs))
        out: list[str] = []
        for i in range(want):
            base = qs[i % len(qs)]
            out.append(f"{base} (investigation {i})")
        return out
    return qs


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------


async def _one_request(
    client: httpx.AsyncClient,
    url: str,
    question: str,
    tenant: str,
    user: str,
    batch_id: str | None = None,
) -> ClientResult:
    t0 = time.perf_counter()
    try:
        r = await client.post(
            url,
            json={"question": question, "tenant_id": tenant, "user_id": user, "batch_id": batch_id},
        )
        dt = (time.perf_counter() - t0) * 1000.0
        if r.status_code == 200:
            body = r.json()
            usage = body.get("usage") or {}
            return ClientResult(
                ok=True,
                status=200,
                latency_ms=dt,
                served_by=body.get("served_by", ""),
                queued_ms=body.get("gate_wait_ms", 0.0),
                tokens=int((usage.get("in") or 0) + (usage.get("out") or 0)),
                usd=float(usage.get("usd") or 0.0),
            )
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text[:120]}
        return ClientResult(
            ok=False, status=r.status_code, latency_ms=dt,
            error=err.get("error", f"http_{r.status_code}"),
        )
    except Exception as exc:
        return ClientResult(
            ok=False, status=0, latency_ms=(time.perf_counter() - t0) * 1000.0,
            error=f"{type(exc).__name__}",
        )


def _is_local(url: str) -> bool:
    """True for loopback targets.

    A machine with `http_proxy` exported will otherwise route 127.0.0.1 through
    the corporate proxy, and the benchmark fails with a 503 from the proxy
    rather than anything to do with the service. Ignoring the environment for
    loopback is not a hack -- a proxy is definitionally not involved in talking
    to localhost -- but it is worth being explicit about, because the symptom
    (503 from an unrelated server) is genuinely confusing the first time.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


async def _run_scenario(
    base_url: str,
    questions: Sequence[str],
    concurrency: int,
    n_requests: int,
    rate_per_client: float | None,
    tenant: str,
    users: int,
    batch_id: str | None = None,
    timeout_s: float = 120.0,
) -> tuple[list[ClientResult], float]:
    """Drive `n_requests` across `concurrency` workers.

    Two pacing modes, because they answer different questions:

    * `rate_per_client=None` -> closed loop. Each worker sends the next request
      as soon as the previous returns. This is what an impatient user with a
      script does, and it is the mode that exposes whether the system's own
      controls hold up, since the client never backs off on its own.
    * a number -> open loop with a fixed interval. This is what a well-behaved
      batch client does, and it is the mode that shows the cache and the gate
      absorbing load rather than the client self-limiting.
    """
    results: list[ClientResult] = []
    lock = asyncio.Lock()
    counter = {"i": 0}

    async with httpx.AsyncClient(
        timeout=timeout_s,
        limits=httpx.Limits(max_connections=concurrency * 2),
        trust_env=not _is_local(base_url),
    ) as client:

        async def worker(wid: int) -> None:
            while True:
                async with lock:
                    if counter["i"] >= n_requests:
                        return
                    idx = counter["i"]
                    counter["i"] += 1
                q = questions[idx % len(questions)]
                user = f"{tenant}-user-{wid % max(users, 1)}"
                res = await _one_request(client, f"{base_url}/v1/diagnose", q, tenant, user, batch_id)
                results.append(res)
                if rate_per_client:
                    await asyncio.sleep(1.0 / rate_per_client)

        t0 = time.perf_counter()
        async with asyncio.TaskGroup() as tg:
            for w in range(concurrency):
                tg.create_task(worker(w))
        return results, time.perf_counter() - t0


async def _server_snapshot(base_url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=not _is_local(base_url)) as c:
            r = await c.get(f"{base_url}/v1/stats")
            return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


async def run_benchmark(
    base_url: str,
    scenarios: Sequence[str],
    concurrency: int,
    n_requests: int,
    job_ids: Sequence[str],
    rate_per_client: float | None = None,
) -> list[BenchReport]:
    reports: list[BenchReport] = []

    # Captured once, before any scenario mutates anything. This is the value
    # that must be restored between scenarios; the server's *current* value
    # would be the benchmark's own previous override.
    startup = await _server_snapshot(base_url)
    startup_gate = startup.get("gate") or {}

    for scenario in scenarios:
        if scenario == "steady":
            # Unique questions, so this measures the system under load and NOT
            # the cache. One worker per user, so the per-user limit is not the
            # thing being tested either. What is left, and what this scenario
            # is for, is: is the service stable when nothing is wrong?
            questions = build_question_mix(job_ids[: max(len(job_ids) // 4, 1)], unique=True, n=n_requests)
            tenant, users, pacing, batch = "tenant-steady", concurrency, rate_per_client, None
            notes = [
                "unique questions with one worker per user: this measures the service under "
                "load, not the cache. The expected result is near-total success with only "
                "queueing-induced latency, i.e. the controls are not in the way when nothing "
                "is wrong",
            ]
        elif scenario == "burst":
            # Unique questions and the per-user rate limit bypassed. The only
            # thing left to push back is the gate -- which is precisely the
            # component this scenario exists to exercise.
            questions = build_question_mix(job_ids[: max(len(job_ids) // 4, 1)], unique=True, n=n_requests)
            tenant, users, pacing, batch = "tenant-burst", concurrency, None, "batch-burst"
            await _register_batch(base_url, tenant, batch)
            notes = [
                "closed-loop clients asking unique questions, with the per-user rate limit "
                "bypassed (registered as a batch): offered load rises until the GATE pushes "
                "back. Expect queueing and 429s rather than upstream failure, and check that "
                "in_flight stayed at the configured ceiling",
            ]
        elif scenario == "hotkey":
            questions = build_question_mix(job_ids[:1], hotkey_ratio=0.85)
            tenant, users, pacing, batch = "tenant-hotkey", concurrency, rate_per_client, None
            notes = [
                "85% of questions describe the same incident: the cache should absorb most of "
                "it and the gate should barely be touched. Cache hit rate is the metric here, "
                "not latency",
            ]
        else:
            continue

        before = await _server_snapshot(base_url)
        # Configure the server for this scenario and clear last scenario's
        # accumulated spend, so each result can be attributed to one mechanism.
        await _apply_scenario_limits(base_url, scenario, startup_gate)
        before = await _server_snapshot(base_url)
        notes.append(f"active control: {SCENARIO_ACTIVE_CONTROL.get(scenario, 'unknown')}")

        # Each scenario drives its own client count. Handing every scenario the
        # same (large) number is the classic way to make a "steady load" test
        # reject most of its traffic, and then draw the wrong conclusion.
        scen_clients = _scenario_clients(scenario, concurrency, before)
        results, duration = await _run_scenario(
            base_url=base_url,
            questions=questions,
            concurrency=scen_clients,
            n_requests=n_requests,
            rate_per_client=pacing,
            tenant=tenant,
            users=scen_clients,
            batch_id=batch,
        )
        after = await _server_snapshot(base_url)
        # A cache left warm from the previous scenario would make the next one's
        # hit rate meaningless, so the question list for hotkey is the only
        # thing allowed to be repeatable.
        if scenario != "hotkey":
            await _admin(base_url, "/v1/admin/cache-clear", {})

        ok = [r for r in results if r.ok]
        lat = [r.latency_ms for r in ok]
        status_counts: dict[str, int] = {}
        served_by: dict[str, int] = {}
        errors: dict[str, int] = {}
        for r in results:
            status_counts[str(r.status)] = status_counts.get(str(r.status), 0) + 1
            if r.ok:
                served_by[r.served_by] = served_by.get(r.served_by, 0) + 1
            else:
                errors[r.error or "unknown"] = errors.get(r.error or "unknown", 0) + 1

        g_after = (after.get("gate") or {}) if after else {}
        reports.append(
            BenchReport(
                scenario=scenario,
                concurrency=concurrency,
                n_requests=n_requests,
                duration_s=duration,
                throughput_rps=(len(results) / duration) if duration > 0 else 0.0,
                n_ok=len(ok),
                n_failed=len(results) - len(ok),
                n_rejected=sum(1 for r in results if r.status == 429),
                status_counts=dict(sorted(status_counts.items())),
                served_by_counts=dict(sorted(served_by.items())),
                error_counts=dict(sorted(errors.items(), key=lambda kv: -kv[1])[:6]),
                latency=percentiles(lat),
                tokens_total=sum(r.tokens for r in results),
                usd_total=sum(r.usd for r in results),
                server_before=_slim(before),
                server_after=_slim(after),
                gate_would_have_exceeded=int(g_after.get("rejected_queue_full", 0))
                + int(g_after.get("rejected_timeout", 0)),
                notes=notes,
            )
        )
    return reports


def _scenario_clients(scenario: str, requested: int, snapshot: dict[str, Any]) -> int:
    """Client count per scenario.

    Every scenario now runs with its own limit profile, so the client count no
    longer needs clamping to avoid tripping an unrelated ceiling -- that was a
    workaround for a problem the profiles fix properly. The function is kept
    because the clamp is still the right thing for `steady`, which claims the
    service is healthy and would be lying if it drove more concurrency than the
    platform is configured to allow.
    """
    if scenario != "steady":
        return requested
    return max(1, requested)


# 0 means "unlimited" for the token and cost ceilings. Concurrency is the
# exception: 0 there would reject every request, so "off" is expressed as a
# large finite number.
_UNLIMITED_CONCURRENCY = 10**6

# The gate capacity to restore between scenarios. Matches the config defaults;
# read from the server's own health payload when available so a server started
# with a different value is not silently rewritten.
DEFAULT_GATE_CONCURRENCY = 8
DEFAULT_GATE_QUEUE = 64

# Per-scenario limit profiles. Each scenario raises every ceiling except the one
# it exists to demonstrate, so that a rejection count can be attributed to a
# specific mechanism instead of to "the limits, somehow".
SCENARIO_LIMITS: dict[str, dict[str, Any]] = {
    # Nothing should push back: this is the "is the service healthy" scenario.
    "steady": {
        "user_qps": 0,
        "tenant_concurrency": _UNLIMITED_CONCURRENCY,
        "tenant_token_per_min": 0,
        "tenant_cost_per_day_usd": 0,
    },
    # The gate is the ceiling under study, so it is left at its configured
    # value while everything else is raised. The gate lives in its own
    # component (serving/gate.py) and is NOT one of the rate limits, which is
    # why raising the limits does not disable it. Without a simulated model
    # latency the gate never contends and this scenario degenerates into
    # "steady" -- hence the warning printed by `serve --llm-latency-ms 0`.
    "burst": {
        "user_qps": 0,
        "tenant_concurrency": _UNLIMITED_CONCURRENCY,
        "tenant_token_per_min": 0,
        "tenant_cost_per_day_usd": 0,
    },
    # Cache behaviour is the point; no explicit limit should interfere.
    "hotkey": {
        "user_qps": 0,
        "tenant_concurrency": _UNLIMITED_CONCURRENCY,
        "tenant_token_per_min": 0,
        "tenant_cost_per_day_usd": 0,
    },
}

# Gate capacity per scenario.
#
# `steady` and `hotkey` leave the gate at whatever the server was started with,
# because neither is about the gate. `burst` shrinks it deliberately: with a
# generously-sized gate and a queue deep enough to hold the whole burst, every
# request eventually succeeds and nothing is demonstrated -- the run just takes
# longer. Admission control only becomes visible when the queue is genuinely
# smaller than the offered load, which is the real situation in production: a
# bounded queue is what turns "everything times out eventually" into "most
# requests are told to retry immediately".
SCENARIO_GATE: dict[str, dict[str, Any]] = {
    "burst": {"max_concurrency": 4, "queue_maxsize": 16, "queue_timeout_s": 5.0},
}

SCENARIO_ACTIVE_CONTROL = {
    "steady": "nothing — all ceilings raised, so a 429 here would be a bug",
    "burst": "the upstream gate only, shrunk so the queue is smaller than the burst",
    "hotkey": "the semantic cache only",
}


async def _apply_scenario_limits(
    base_url: str, scenario: str, startup_gate: dict[str, Any]
) -> None:
    """Configure the server for one scenario, best-effort.

    Resets the per-tenant usage counters first, because otherwise scenario two
    inherits scenario one's token and dollar spend and every scenario after the
    first measures the accumulation rather than its own behaviour.

    `startup_gate` is captured once before any scenario runs. Reading it back
    from the server instead would be wrong in a way that is easy to miss: the
    value to restore is the one the server was *started* with, and after the
    first scenario the server reports the benchmark's own previous override.
    """
    # Wait for the gate to drain before resizing it; the previous scenario's
    # last requests can still be holding slots.
    for _ in range(40):
        snap = await _server_snapshot(base_url)
        g = snap.get("gate") or {}
        if not g.get("in_flight") and not g.get("queued"):
            break
        await asyncio.sleep(0.25)

    gate_cfg = SCENARIO_GATE.get(scenario)
    if gate_cfg:
        await _admin(base_url, "/v1/admin/gate", gate_cfg)
    else:
        await _admin(
            base_url,
            "/v1/admin/gate",
            {
                "max_concurrency": int(startup_gate.get("max_concurrency") or DEFAULT_GATE_CONCURRENCY),
                "queue_maxsize": int(startup_gate.get("queue_maxsize") or DEFAULT_GATE_QUEUE),
            },
        )

    profile = dict(SCENARIO_LIMITS.get(scenario, {}))
    await _admin(base_url, "/v1/admin/reset-counters", {})
    if profile:
        await _admin(base_url, "/v1/admin/limits", profile)
    await _admin(base_url, "/v1/admin/reset-gate", {})


async def _admin(base_url: str, path: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget admin call.

    Best-effort on purpose: a missing knob should degrade the benchmark's
    explanatory power, not make it fail. The report records what was and was
    not applied, so a silently-ignored profile cannot be mistaken for a
    measurement.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=not _is_local(base_url)) as c:
            await c.post(f"{base_url}{path}", json=payload)
    except Exception:
        pass


async def _register_batch(base_url: str, tenant: str, batch_id: str) -> None:
    """Tell the server this traffic is batch work.

    Best-effort: if the endpoint is unavailable the scenario still runs, it just
    measures a different thing (the per-user limiter instead of the gate), and
    the report's error breakdown makes that evident.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=not _is_local(base_url)) as c:
            await c.post(f"{base_url}/v1/admin/prepare-batch",
                         json={"tenant_id": tenant, "batch_id": batch_id})
    except Exception:
        pass


def _slim(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep only what a report needs from a server snapshot.

    The full stats payload is large and mostly invariant between scenarios;
    embedding it verbatim would make the report unreadable and would hide the
    three or four numbers that actually differ.
    """
    if not snapshot:
        return {}
    return {
        "gate": snapshot.get("gate"),
        "cache": snapshot.get("cache"),
        "limits": {
            "rejections": (snapshot.get("limits") or {}).get("rejections"),
        },
        "sessions": snapshot.get("sessions"),
        "cost": (snapshot.get("metrics") or {}).get("cost"),
        "counters": {
            k: v
            for k, v in ((snapshot.get("metrics") or {}).get("counters") or {}).items()
            if k.startswith(("requests.", "cache.", "gate.", "degrade.", "rejected."))
        },
    }


def write_bench_report(reports: Sequence[BenchReport], path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)
