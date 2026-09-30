"""压测执行器。

两种施压模式，回答不同的问题：闭环（拿到响应立刻发下一个）暴露系统自身的控制
是否能顶住，因为客户端不会自我限流；定速开环则展示缓存和闸门在吸收负载，
而不是客户端在限自己。
"""

from __future__ import annotations

from .mix import build_question_mix
from .models import BenchReport
from .models import ClientResult
from .models import percentiles
from .reporting import _slim
from .scenarios import DEFAULT_GATE_CONCURRENCY
from .scenarios import DEFAULT_GATE_QUEUE
from .scenarios import SCENARIO_ACTIVE_CONTROL
from .scenarios import SCENARIO_GATE
from .scenarios import SCENARIO_LIMITS
from .scenarios import _is_local
from .scenarios import _scenario_clients
from typing import Any
from typing import Sequence
import asyncio
import httpx
import time

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
