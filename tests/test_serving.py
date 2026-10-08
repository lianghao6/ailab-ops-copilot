"""Concurrency, limits, cache and the service pipeline.

Async tests are written as `def test_x(): asyncio.run(_body())` rather than with
`pytest-asyncio`. That is a deliberate choice: the project's whole promise is
that it runs with no installation step beyond `pip install -e .`, and a plugin
dependency for six tests is a poor trade.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ailab_ops.serving.cache import (
    BreakerState,
    CircuitBreaker,
    DegradeLevel,
    SemanticCache,
    cache_key,
    decide_degradation,
    normalise_question,
    tenant_scoped,
)
from ailab_ops.serving.gate import GateRejected, Priority, UpstreamGate
from ailab_ops.serving.limits import LimitExceeded, RateLimiter, TokenBucket


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------


def test_gate_bounds_concurrency():
    async def body():
        gate = UpstreamGate(max_concurrency=3, queue_maxsize=20, queue_timeout_s=5)
        peak = {"v": 0}
        in_flight = {"v": 0}

        async def job():
            await gate.acquire()
            in_flight["v"] += 1
            peak["v"] = max(peak["v"], in_flight["v"])
            await asyncio.sleep(0.02)
            in_flight["v"] -= 1
            await gate.release()

        await asyncio.gather(*(job() for _ in range(15)))
        assert peak["v"] <= 3, f"concurrency reached {peak['v']} with a limit of 3"
        assert gate.stats().admitted == 15

    asyncio.run(body())


def test_gate_rejects_when_the_queue_is_full():
    async def body():
        gate = UpstreamGate(max_concurrency=2, queue_maxsize=3, queue_timeout_s=5)
        started = []

        async def holder():
            await gate.acquire()
            started.append(1)
            await asyncio.sleep(0.3)
            await gate.release()

        tasks = [asyncio.create_task(holder()) for _ in range(2)]
        await asyncio.sleep(0.02)  # let the holders take both slots

        rejections = 0
        for _ in range(10):
            try:
                await gate.acquire(timeout_s=0.05)
            except GateRejected:
                rejections += 1
        assert rejections > 0, "a full queue should reject rather than block forever"
        assert gate.stats().rejected_queue_full + gate.stats().rejected_timeout > 0
        for t in tasks:
            await t

    asyncio.run(body())


def test_gate_queue_timeout_is_reported_as_a_timeout_not_a_full_queue():
    async def body():
        gate = UpstreamGate(max_concurrency=1, queue_maxsize=10, queue_timeout_s=0.05)
        await gate.acquire()
        with pytest.raises(GateRejected) as ei:
            await gate.acquire(timeout_s=0.05)
        assert "waited" in str(ei.value)
        assert gate.stats().rejected_timeout == 1
        assert gate.stats().rejected_queue_full == 0
        await gate.release()

    asyncio.run(body())


def test_higher_priority_is_served_first():
    async def body():
        gate = UpstreamGate(max_concurrency=1, queue_maxsize=10, queue_timeout_s=5)
        await gate.acquire()  # occupy the only slot
        order: list[str] = []

        async def waiter(name: str, prio: Priority):
            await gate.acquire(prio)
            order.append(name)
            await gate.release()

        tasks = [
            asyncio.create_task(waiter("batch", Priority.BATCH)),
            asyncio.create_task(waiter("normal", Priority.NORMAL)),
            asyncio.create_task(waiter("interactive", Priority.INTERACTIVE)),
        ]
        await asyncio.sleep(0.05)
        await gate.release()
        await asyncio.gather(*tasks)
        assert order[0] == "interactive", f"priority order was {order}"

    asyncio.run(body())


def test_gate_drains_completely():
    """A leaked slot is worse than a rejection: capacity shrinks silently and
    the service degrades for everyone."""

    async def body():
        gate = UpstreamGate(max_concurrency=4, queue_maxsize=50, queue_timeout_s=5)

        async def job(ok: bool):
            await gate.acquire()
            try:
                await asyncio.sleep(0.005)
                if not ok:
                    raise RuntimeError("boom")
            finally:
                await gate.release()

        results = await asyncio.gather(*(job(i % 3 != 0) for i in range(30)), return_exceptions=True)
        assert sum(1 for r in results if isinstance(r, Exception)) > 0
        assert gate.stats().in_flight == 0, "a slot leaked after an exception"

    asyncio.run(body())


def test_gate_release_skips_cancelled_head_before_its_cleanup_resumes():
    async def body():
        gate = UpstreamGate(max_concurrency=1, queue_maxsize=2, queue_timeout_s=1)
        await gate.acquire()
        cancelled = asyncio.create_task(gate.acquire(Priority.INTERACTIVE))
        following = asyncio.create_task(gate.acquire(Priority.NORMAL))
        try:
            while gate.queued < 2:
                await asyncio.sleep(0)
            cancelled.cancel()
            # Reverse the prior regression's ordering: release must skip this
            # cancelled Future before acquire() gets to remove its heap item.
            await gate.release()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            await asyncio.wait_for(following, timeout=0.1)
            assert gate.in_flight == 1 and gate.queued == 0
            assert gate.stats().admitted == 2
            assert gate.stats().rejected_timeout == 0
            await gate.release()
            assert gate.in_flight == 0
            # Neither cancelled-waiter cleanup nor handoff inflated capacity.
            await gate.acquire()
            with pytest.raises(GateRejected):
                await gate.acquire(timeout_s=0.01)
            await gate.release()
            assert gate.in_flight == gate.queued == 0
        finally:
            for task in (cancelled, following):
                if not task.done():
                    task.cancel()
            await asyncio.gather(cancelled, following, return_exceptions=True)
    asyncio.run(body())


def test_reconfigure_refuses_while_busy():
    async def body():
        gate = UpstreamGate(max_concurrency=2)
        await gate.acquire()
        with pytest.raises(RuntimeError):
            gate.reconfigure(max_concurrency=1)
        await gate.release()
        cfg = gate.reconfigure(max_concurrency=1)
        assert cfg["max_concurrency"] == 1

    asyncio.run(body())


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


def test_user_rate_limit_rejects_a_burst():
    rl = RateLimiter(user_qps=5, tenant_concurrency=100)
    accepted = 0
    for _ in range(20):
        try:
            rl.check_request("t", "u")
            accepted += 1
        except LimitExceeded as exc:
            assert exc.kind == "user_rate_exceeded"
    assert accepted <= 12, f"a 5/s limit accepted {accepted} back-to-back requests"
    assert rl.rejections["user_rate_exceeded"] > 0


def test_tenant_concurrency_is_shared_across_users():
    """The point of a tenant limit: one user is not the threat, a whole
    organisation is."""
    rl = RateLimiter(user_qps=0, tenant_concurrency=3)
    for i in range(3):
        rl.check_request("t", f"user-{i}")
    with pytest.raises(LimitExceeded) as ei:
        rl.check_request("t", "user-9")
    assert ei.value.kind == "tenant_concurrency_exceeded"


def test_cost_budget_is_not_recoverable_by_waiting():
    rl = RateLimiter(user_qps=0, tenant_concurrency=100, tenant_cost_per_day_usd=1.0)
    rl.record_usage("t", tokens=0, usd=1.5)
    with pytest.raises(LimitExceeded) as ei:
        rl.check_request("t", "u")
    assert ei.value.kind == "tenant_cost_exceeded"
    assert ei.value.retry_after_s is None, "a daily budget does not clear on a retry"


def test_token_window_enforces_a_sustained_rate():
    rl = RateLimiter(user_qps=0, tenant_concurrency=100, tenant_token_per_min=1000)
    rl.record_usage("t", tokens=1200, usd=0.0)
    with pytest.raises(LimitExceeded) as ei:
        rl.check_tokens("t", 100)
    assert ei.value.kind == "tenant_token_exceeded"


def test_batch_traffic_bypasses_the_user_limit_but_not_the_tenant_one():
    rl = RateLimiter(user_qps=1, tenant_concurrency=2)
    rl.register_batch("t", "b1")
    for _ in range(10):
        rl.check_tokens("t", 10)  # batch path: the caller skips check_request
    assert rl.is_batch("t", "b1")
    assert not rl.is_batch("t", "other")


def test_release_restores_capacity():
    rl = RateLimiter(user_qps=0, tenant_concurrency=1)
    rl.check_request("t", "u")
    with pytest.raises(LimitExceeded):
        rl.check_request("t", "u2")
    rl.release_request("t")
    rl.check_request("t", "u2")  # should not raise


def test_token_bucket_refills_over_time():
    b = TokenBucket(rate=100.0, capacity=2.0)
    assert b.take()
    assert b.take()
    assert not b.take()
    assert b.take(now=b.last + 0.05)


def test_set_limits_takes_effect_and_reset_clears_usage():
    rl = RateLimiter(user_qps=1, tenant_concurrency=1)
    rl.record_usage("t", tokens=500, usd=0.5)
    rl.set_limits(user_qps=0, tenant_concurrency=500, tenant_token_per_min=0,
                  tenant_cost_per_day_usd=0)
    rl.reset_counters()
    snap = rl.snapshot()
    assert snap["tenants"]["t"]["usd_today"] == 0.0
    assert snap["limits"]["tenant_concurrency"] == 500


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def test_semantic_cache_exact_hit():
    c = SemanticCache(ttl_s=60)
    c.put("t1", "Why did job-abc fail?", {"a": 1})
    assert c.get("t1", "Why did job-abc fail?") == ({"a": 1}, "exact")


def test_semantic_cache_near_hit_on_rephrasing():
    """The reason a semantic cache is worth having: two people investigating the
    same incident rarely type the same sentence. Similarity is computed over the
    normalised question (identifiers and stopwords removed), so what matters is
    that the surviving content words overlap."""
    c = SemanticCache(ttl_s=60, threshold=0.6)
    c.put("t1", "job-abc memory exhaustion kernel kill diagnosis", {"a": 1})
    got = c.get("t1", "job-abc memory exhaustion kernel kill root cause")
    assert got is not None and got[1] == "near", f"no near-match: {got}"


def test_semantic_cache_does_not_near_match_unrelated_questions():
    c = SemanticCache(ttl_s=60, threshold=0.6)
    c.put("t1", "job-abc memory exhaustion kernel kill diagnosis", {"a": 1})
    assert c.get("t1", "job-xyz network timeout collective failure") is None


def test_cache_ignores_question_boilerplate():
    """Two questions about the same job phrased as boilerplate differ only in
    words that carry no information, so they must share a key."""
    c = SemanticCache(ttl_s=60, threshold=1.0)
    c.put("t1", "Please tell me why job-abc failed", {"a": 1})
    got = c.get("t1", "why did job-abc fail")
    assert got is not None and got[1] == "exact"


def test_semantic_cache_is_scoped_per_tenant():
    """A shared cache in an operations assistant leaks one organisation's
    incident content into another's answers."""
    c = SemanticCache(ttl_s=60, threshold=0.5)
    c.put("tenant-a", "Why did job-abc fail?", {"secret": "tenant-a-detail"})
    assert c.get("tenant-b", "Why did job-abc fail?") is None, (
        "a cache hit crossed a tenant boundary"
    )
    assert c.get("tenant-a", "Why did job-abc fail?") is not None


def test_cache_key_ignores_question_phrasing():
    assert cache_key("Why did job-X fail?") == cache_key("why did job-X fail")
    assert cache_key("Diagnose job-X") != cache_key("Diagnose job-Y")


def test_normalise_question_strips_noise_but_keeps_identifiers():
    n = normalise_question("Please tell me why job-68b290e6-0001 failed?")
    assert "68b290e6" not in n and "job" not in n.split()
    assert "dissimilar" not in n


def test_cache_expiry():
    c = SemanticCache(ttl_s=0.01)
    c.put("t", "q", {"a": 1})
    time.sleep(0.03)
    assert c.get("t", "q") is None
    assert c.stats()["entries"] == 0


def test_cache_eviction_bounds_memory():
    c = SemanticCache(ttl_s=60, max_entries=5, threshold=1.0)
    for i in range(20):
        c.put("t", f"question number {i}", {"i": i})
    assert c.stats()["entries"] <= 5


def test_cache_stats_track_hit_rate():
    c = SemanticCache(ttl_s=60)
    c.put("t", "q", {"a": 1})
    c.get("t", "q")
    c.get("t", "something else entirely")
    s = c.stats()
    assert s["hits"] == 1 and s["misses"] == 1
    assert 0.0 < s["hit_rate"] < 1.0
    assert s["scoped_per_tenant"] is True


# --------------------------------------------------------------------------
# Breaker and degradation
# --------------------------------------------------------------------------


def test_breaker_opens_after_consecutive_failures_and_recovers():
    b = CircuitBreaker(name="upstream", failure_threshold=3, cooldown_s=0.05)
    assert b.state == BreakerState.CLOSED
    for _ in range(3):
        b.record_failure()
    assert b.state == BreakerState.OPEN
    assert not b.allow(), "an open breaker should stop sending traffic"
    assert b.trips == 1
    time.sleep(0.07)
    assert b.allow(), "the breaker should half-open after its cooldown"
    assert b.state == BreakerState.HALF_OPEN
    b.record_success()
    assert b.state == BreakerState.CLOSED


def test_breaker_reopens_if_the_probe_fails():
    b = CircuitBreaker(name="upstream", failure_threshold=2, cooldown_s=0.02)
    b.record_failure()
    b.record_failure()
    time.sleep(0.03)
    assert b.allow()
    b.record_failure()
    assert b.state == BreakerState.OPEN
    assert b.trips == 2


def test_degradation_ladder_descends_only_when_unhealthy():
    healthy = CircuitBreaker(failure_threshold=5)
    d = decide_degradation(healthy, queued=0, queue_maxsize=64)
    assert d.level == DegradeLevel.FULL, "a healthy system must not serve degraded answers"

    # Queueing alone is not degradation.
    d = decide_degradation(healthy, queued=40, queue_maxsize=64)
    assert d.level == DegradeLevel.FULL

    # A full queue is.
    d = decide_degradation(healthy, queued=200, queue_maxsize=64)
    assert d.level == DegradeLevel.EVIDENCE_ONLY

    # And an open breaker is.
    sick = CircuitBreaker(failure_threshold=1, cooldown_s=60)
    sick.record_failure()
    d = decide_degradation(sick, queued=0, queue_maxsize=64)
    assert d.level == DegradeLevel.EVIDENCE_ONLY
    assert d.retry_after_s is not None
