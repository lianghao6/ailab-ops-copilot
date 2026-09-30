"""Rate limiting, quota and budget control.

Four different ceilings, because conflating them is the mistake this module
exists to prevent. Each answers a different question and fails in a different
way when it is missing:

| limiter              | question                                | symptom if absent                     |
|----------------------|-----------------------------------------|---------------------------------------|
| per-user QPS         | is one caller abusing the endpoint?      | one client starves everyone           |
| per-tenant concurrency | how much of the model is one org using? | a noisy tenant saturates the cluster  |
| per-tenant token/min | what is the sustained generation volume?  | sustained overload, not a burst       |
| per-tenant USD/day   | what will this cost us?                  | a surprise invoice                    |

The QPS limiter is a token bucket, which allows a short burst while enforcing a
sustained rate -- the right shape for a human-driven client that clicks twice.
The token and cost limiters are rolling windows and hard daily counters, which
are the right shape for a non-reversible resource: there is no way to "refund"
a token that has been generated.

Everything is keyed by tenant as well as by user, because the interesting abuse
case is not one user hammering the endpoint; it is a hundred users in one
organisation doing so, and the person who has to answer for it is the org.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class LimitExceeded(RuntimeError):
    """Raised when a request would breach a configured ceiling."""

    def __init__(self, kind: str, detail: str, retry_after_s: float | None = None) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.retry_after_s = retry_after_s


@dataclass
class TokenBucket:
    """Classic token bucket: `rate` tokens per second, bursting up to `capacity`."""

    rate: float
    capacity: float
    tokens: float = 0.0
    last: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens <= 0:
            self.tokens = self.capacity

    def take(self, n: float = 1.0, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        elapsed = max(now - self.last, 0.0)
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def wait_s(self, n: float = 1.0) -> float:
        deficit = n - self.tokens
        return max(deficit, 0.0) / self.rate if self.rate > 0 else float("inf")


@dataclass
class SlidingWindow:
    """Rolling window over a fixed span, in events or in summed weight."""

    span_s: float
    limit: float
    _events: deque = field(default_factory=deque)

    def _evict(self, now: float) -> None:
        cutoff = now - self.span_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def current(self, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        self._evict(now)
        return sum(w for _t, w in self._events)

    def add(self, weight: float, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._evict(now)
        self._events.append((now, weight))

    def allows(self, weight: float, now: float | None = None) -> bool:
        return self.current(now) + weight <= self.limit


@dataclass
class TenantState:
    tenant_id: str
    user_buckets: dict[str, TokenBucket] = field(default_factory=dict)
    token_window: SlidingWindow = field(default_factory=lambda: SlidingWindow(60.0, 0))
    in_flight: int = 0
    usd_today: float = 0.0
    day_key: str = ""
    batch_ids: set[str] = field(default_factory=set)


class RateLimiter:
    """Enforces all four ceilings. Thread-safe enough for the server's usage:
    every mutation happens on the event loop, and cost accounting from worker
    threads goes through `record_usage` which is the only method taking a lock.
    """

    def __init__(
        self,
        user_qps: float = 2.0,
        user_burst: float | None = None,
        tenant_concurrency: int = 16,
        tenant_token_per_min: int = 0,
        tenant_cost_per_day_usd: float = 0.0,
    ) -> None:
        self.user_qps = user_qps
        self.user_burst = user_burst if user_burst is not None else max(user_qps * 2.0, 2.0)
        self.tenant_concurrency = tenant_concurrency
        self.tenant_token_per_min = tenant_token_per_min
        self.tenant_cost_per_day_usd = tenant_cost_per_day_usd
        self._tenants: dict[str, TenantState] = {}
        self.rejections: dict[str, int] = {}
        self._thread_lock = threading.Lock()

    def _tenant(self, tenant_id: str) -> TenantState:
        st = self._tenants.get(tenant_id)
        if st is None:
            st = TenantState(
                tenant_id=tenant_id,
                token_window=SlidingWindow(60.0, float(self.tenant_token_per_min or 10**12)),
                day_key=_today(),
            )
            self._tenants[tenant_id] = st
        if st.day_key != _today():
            # The cost ceiling is daily on purpose: a per-minute cost limit
            # can be evaded by spreading the same spend over the hour, which is
            # precisely the behaviour a budget is supposed to stop.
            st.day_key = _today()
            st.usd_today = 0.0
        return st

    def _reject(self, kind: str, detail: str, retry_after_s: float | None = None) -> None:
        self.rejections[kind] = self.rejections.get(kind, 0) + 1
        raise LimitExceeded(kind, detail, retry_after_s)

    def check_request(self, tenant_id: str, user_id: str, batch_id: str | None = None) -> None:
        """Cheap pre-flight checks, before any work is done.

        Order matters: the cheapest and most decisive check runs first, so a
        request that cannot possibly succeed is rejected in microseconds.
        """
        st = self._tenant(tenant_id)

        if self.tenant_cost_per_day_usd > 0 and st.usd_today >= self.tenant_cost_per_day_usd:
            self._reject(
                "tenant_cost_exceeded",
                f"tenant {tenant_id} has spent ${st.usd_today:.4f} of its "
                f"${self.tenant_cost_per_day_usd:.2f} daily budget; this does not clear on retry",
            )

        st.in_flight += 1
        if st.in_flight > self.tenant_concurrency:
            st.in_flight -= 1
            self._reject(
                "tenant_concurrency_exceeded",
                f"tenant {tenant_id} already has {st.in_flight} requests in flight "
                f"(limit {self.tenant_concurrency})",
                retry_after_s=1.0,
            )

        if self.user_qps > 0:
            bucket = st.user_buckets.get(user_id)
            if bucket is None:
                bucket = TokenBucket(rate=self.user_qps, capacity=self.user_burst)
                st.user_buckets[user_id] = bucket
            if not bucket.take():
                st.in_flight -= 1
                self._reject(
                    "user_rate_exceeded",
                    f"user {user_id} exceeded {self.user_qps:g} req/s "
                    f"(burst {self.user_burst:g}); retry in {bucket.wait_s():.2f}s",
                    retry_after_s=bucket.wait_s(),
                )

        if self.tenant_token_per_min > 0 and not st.token_window.allows(0):
            st.in_flight -= 1
            self._reject(
                "tenant_token_exceeded",
                f"tenant {tenant_id} has used {st.token_window.current():.0f} tokens in the last "
                f"minute (limit {self.tenant_token_per_min})",
                retry_after_s=15.0,
            )

    def check_tokens(self, tenant_id: str, estimated_tokens: int) -> None:
        st = self._tenant(tenant_id)
        if self.tenant_token_per_min > 0 and not st.token_window.allows(estimated_tokens):
            self._reject(
                "tenant_token_exceeded",
                f"tenant {tenant_id} would exceed {self.tenant_token_per_min} tokens/min "
                f"(current {st.token_window.current():.0f}, this request ~{estimated_tokens})",
                retry_after_s=15.0,
            )

    def release_request(self, tenant_id: str) -> None:
        st = self._tenant(tenant_id)
        st.in_flight = max(st.in_flight - 1, 0)

    def record_usage(self, tenant_id: str, tokens: int, usd: float) -> None:
        st = self._tenant(tenant_id)
        if tokens:
            st.token_window.add(float(tokens))
        if usd:
            st.usd_today += usd

    # ---- batch jobs ----------------------------------------------------

    def register_batch(self, tenant_id: str, batch_id: str) -> None:
        """Batch work is exempt from the per-user QPS limit.

        A bulk re-evaluation of two thousand jobs is not abuse: it is the
        intended use, and throttling it to 2/s would make it useless. It is
        still subject to tenant concurrency, token and cost ceilings, which are
        the limits that actually protect the platform.
        """
        st = self._tenant(tenant_id)
        st.batch_ids.add(batch_id)

    def is_batch(self, tenant_id: str, batch_id: str | None) -> bool:
        return bool(batch_id) and batch_id in self._tenant(tenant_id).batch_ids

    # ---- introspection -------------------------------------------------

    def set_limits(
        self,
        user_qps: float | None = None,
        tenant_concurrency: int | None = None,
        tenant_token_per_min: int | None = None,
        tenant_cost_per_day_usd: float | None = None,
    ) -> dict[str, Any]:
        """Adjust the ceilings at runtime.

        Present because a benchmark needs to measure one control at a time. A
        load test that trips four limits at once produces a rejection count and
        no understanding; being able to raise every limit except the one under
        test is what turns the benchmark into a demonstration. Existing
        per-tenant windows keep their accumulated usage, which is deliberate --
        silently resetting the spend counter while changing the ceiling would
        make the budget control untestable.
        """
        if user_qps is not None:
            self.user_qps = float(user_qps)
            self.user_burst = max(self.user_qps * 2.0, 2.0)
        if tenant_concurrency is not None:
            self.tenant_concurrency = int(tenant_concurrency)
        if tenant_token_per_min is not None:
            self.tenant_token_per_min = int(tenant_token_per_min)
            for st in self._tenants.values():
                st.token_window.limit = float(tenant_token_per_min or 10**12)
        if tenant_cost_per_day_usd is not None:
            self.tenant_cost_per_day_usd = float(tenant_cost_per_day_usd)
        return self.snapshot()

    def reset_counters(self) -> None:
        """Clear accumulated usage between benchmark scenarios.

        Without this, scenario two starts with scenario one's token and dollar
        spend already against it, and every scenario after the first measures
        the accumulation rather than its own behaviour.
        """
        with self._thread_lock:
            for st in self._tenants.values():
                st.token_window = SlidingWindow(60.0, float(self.tenant_token_per_min or 10**12))
                st.usd_today = 0.0
                st.user_buckets.clear()
                st.in_flight = 0
        self.rejections.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": {
                "user_qps": self.user_qps,
                "user_burst": self.user_burst,
                "tenant_concurrency": self.tenant_concurrency,
                "tenant_token_per_min": self.tenant_token_per_min,
                "tenant_cost_per_day_usd": self.tenant_cost_per_day_usd,
            },
            "tenants": {
                tid: {
                    "in_flight": st.in_flight,
                    "usd_today": round(st.usd_today, 6),
                    "tokens_last_min": round(st.token_window.current(), 1),
                    "n_users": len(st.user_buckets),
                }
                for tid, st in self._tenants.items()
            },
            "rejections": dict(self.rejections),
        }


def _today() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
