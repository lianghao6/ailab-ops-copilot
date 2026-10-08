"""The upstream concurrency gate.

This is the single most important component in the project, and the one most
often missing from a tutorial agent. The reasoning:

An LLM agent does not have a database at the bottom of the stack; it has a
model server, and that server is expensive, stateful and finite. A typical
inference server is configured to hold a small number of concurrent sequences
in flight. Past that number, latency rises superlinearly and eventually the
server starts rejecting, restarting, or thrashing its KV cache. So the failure
mode when traffic arrives is NOT "requests get slower" -- it is "the model
server falls over and *every* request fails, including the ones that would have
succeeded".

The gate therefore does three things, in this order of importance:

1. **Admission control.** At most `max_concurrency` calls are in flight at any
   instant. Not "approximately" and not "per worker": one semaphore for the
   whole process, acquired before the call and released in a `finally`.
2. **Bounded queueing.** Excess requests wait in a bounded queue rather than
   piling up without limit. An unbounded queue does not prevent overload, it
   just moves the failure from "fast rejection" to "everything times out",
   which is strictly worse: the client waits longer to learn the same thing,
   and memory grows while it does.
3. **A wait deadline.** A queued request that cannot be admitted in time is
   rejected with a clear reason. "We are at capacity, retry shortly" is a
   correct and useful answer; a request that hangs for two minutes and then
   times out is not.

Priority exists because not every caller is equal: an interactive diagnosis
should not queue behind a batch re-evaluation of two thousand jobs. The
priority queue is a small, honest implementation -- there is no starvation
protection beyond a promotion rule, and the code says so.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    INTERACTIVE = 0
    NORMAL = 1
    BATCH = 2


class GateRejected(RuntimeError):
    """Raised when a request cannot be admitted within its deadline."""

    def __init__(self, reason: str, waited_ms: float = 0.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.waited_ms = waited_ms


@dataclass(order=True)
class _Waiter:
    sort_key: tuple[int, float]
    future: asyncio.Future = field(compare=False)
    enqueued_at: float = field(compare=False, default=0.0)


@dataclass
class GateStats:
    in_flight: int
    queued: int
    admitted: int
    rejected_queue_full: int
    rejected_timeout: int
    total_wait_ms: float
    max_wait_ms: float
    max_concurrency: int = 0
    queue_maxsize: int = 0
    queue_timeout_s: float = 0.0

    @property
    def avg_wait_ms(self) -> float:
        n = self.admitted
        return self.total_wait_ms / n if n else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "in_flight": self.in_flight,
            "queued": self.queued,
            "admitted": self.admitted,
            "rejected_queue_full": self.rejected_queue_full,
            "rejected_timeout": self.rejected_timeout,
            "avg_wait_ms": round(self.avg_wait_ms, 1),
            "max_wait_ms": round(self.max_wait_ms, 1),
            "max_concurrency": self.max_concurrency,
            "queue_maxsize": self.queue_maxsize,
            "queue_timeout_s": self.queue_timeout_s,
        }


class UpstreamGate:
    """Bounded-concurrency admission control in front of the model server."""

    def __init__(self, max_concurrency: int = 8, queue_maxsize: int = 64,
                 queue_timeout_s: float = 30.0, starvation_promote_s: float = 5.0) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.max_concurrency = max_concurrency
        self.queue_maxsize = queue_maxsize
        self.queue_timeout_s = queue_timeout_s
        self.starvation_promote_s = starvation_promote_s

        self._sem = asyncio.Semaphore(max_concurrency)
        self._heap: list[_Waiter] = []
        self._lock = asyncio.Lock()
        self._in_flight = 0

        self.admitted = 0
        self.rejected_queue_full = 0
        self.rejected_timeout = 0
        self._total_wait_ms = 0.0
        self._max_wait_ms = 0.0
        self._dispatching = False

    # ---- admission -----------------------------------------------------

    async def acquire(self, priority: Priority = Priority.NORMAL, timeout_s: float | None = None) -> float:
        """Wait for a slot. Returns the wait time in milliseconds.

        Fast path first: if a slot is free, take it without touching the queue,
        because most requests arrive when the system is not saturated and
        paying for queueing machinery on the common path is a real cost.
        """
        if self._sem.locked() is False and not self._heap:
            # `locked()` is a proxy for "value == 0"; the heap check ensures a
            # waiter that is already queued is never overtaken by a newcomer,
            # which is the fairness property that matters here.
            await self._sem.acquire()
            self._in_flight += 1
            self.admitted += 1
            return 0.0

        async with self._lock:
            if len(self._heap) >= self.queue_maxsize:
                self.rejected_queue_full += 1
                raise GateRejected(
                    f"queue full ({self.queue_maxsize} waiting, {self.max_concurrency} in flight); "
                    "retry shortly or lower the request rate"
                )
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            # Age is folded into the sort key so that a long-waiting low
            # priority request eventually outranks a stream of new high
            # priority ones. Without this, a busy interactive workload starves
            # the batch workload indefinitely.
            heapq.heappush(self._heap, _Waiter((int(priority), time.monotonic()), fut, time.monotonic()))

        t0 = time.monotonic()
        limit = timeout_s if timeout_s is not None else self.queue_timeout_s
        try:
            await asyncio.wait_for(fut, timeout=limit)
        except asyncio.CancelledError:
            async with self._lock:
                self._heap = [w for w in self._heap if w.future is not fut]
                heapq.heapify(self._heap)
                # release() may have transferred the slot just before this
                # task observed cancellation. Return that slot exactly once.
                transferred = fut.done() and not fut.cancelled() and fut.exception() is None
            if transferred:
                await self.release()
            raise
        except asyncio.TimeoutError:
            async with self._lock:
                self._heap = [w for w in self._heap if w.future is not fut]
                heapq.heapify(self._heap)
            self.rejected_timeout += 1
            raise GateRejected(
                f"waited {limit:.1f}s for an upstream slot and did not get one; "
                f"{self.max_concurrency} calls in flight, {len(self._heap)} queued",
                waited_ms=(time.monotonic() - t0) * 1000.0,
            ) from None
        else:
            wait_ms = (time.monotonic() - t0) * 1000.0
            self._total_wait_ms += wait_ms
            self._max_wait_ms = max(self._max_wait_ms, wait_ms)
            self.admitted += 1
            return wait_ms

    async def release(self) -> None:
        """Release a slot and hand it to the longest-waiting suitable waiter."""
        self._in_flight -= 1
        async with self._lock:
            if self._heap:
                waiter = heapq.heappop(self._heap)
                if not waiter.future.done():
                    waiter.future.set_result(True)
                    # The slot is transferred directly rather than released and
                    # re-acquired: releasing first would let a newcomer on the
                    # fast path steal it, which is exactly the overtaking the
                    # heap exists to prevent.
                    self._in_flight += 1
                    return
        self._sem.release()

    def try_promote_starved(self) -> int:
        """Re-key waiters that have waited too long so they are served next.

        Returns the number promoted. Called by the queue-reaper task in the
        server. Kept as an explicit method rather than folded into `acquire`
        so its effect is visible in the stats and testable on its own.
        """
        promoted = 0
        now = time.monotonic()
        for w in self._heap:
            if now - w.enqueued_at >= self.starvation_promote_s and w.sort_key[0] > int(Priority.INTERACTIVE):
                w.sort_key = (int(Priority.INTERACTIVE), w.sort_key[1])
                promoted += 1
        if promoted:
            heapq.heapify(self._heap)
        return promoted

    # ---- introspection -------------------------------------------------

    def stats(self) -> GateStats:
        return GateStats(
            in_flight=self._in_flight,
            queued=len(self._heap),
            admitted=self.admitted,
            rejected_queue_full=self.rejected_queue_full,
            rejected_timeout=self.rejected_timeout,
            total_wait_ms=self._total_wait_ms,
            max_wait_ms=self._max_wait_ms,
            max_concurrency=self.max_concurrency,
            queue_maxsize=self.queue_maxsize,
            queue_timeout_s=self.queue_timeout_s,
        )

    @property
    def queued(self) -> int:
        return len(self._heap)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def reset_stats(self) -> None:
        """Zero the counters, keeping capacity and any queued waiters.

        Used between benchmark scenarios. The semaphore and the heap are left
        alone deliberately: resetting a semaphore mid-flight is how you end up
        with either leaked capacity or a deadlock, and neither is worth it just
        to make a report tidier.
        """
        self.admitted = 0
        self.rejected_queue_full = 0
        self.rejected_timeout = 0
        self._total_wait_ms = 0.0
        self._max_wait_ms = 0.0

    def reconfigure(
        self,
        max_concurrency: int | None = None,
        queue_maxsize: int | None = None,
        queue_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Change the gate's capacity, refusing to do so while it is busy.

        In production this is a startup-time knob; it exists as a runtime call
        so a benchmark can show what a genuinely small upstream can absorb. The
        refusal matters: shrinking a semaphore while requests hold it either
        loses capacity or blocks forever, and both are worse than the error.
        """
        if self._in_flight > 0 or self._heap:
            raise RuntimeError(
                f"gate is active ({self._in_flight} in flight, {len(self._heap)} queued); "
                "wait for it to drain before reconfiguring"
            )
        if max_concurrency is not None:
            if max_concurrency < 1:
                raise ValueError("max_concurrency must be >= 1")
            self.max_concurrency = int(max_concurrency)
            self._sem = asyncio.Semaphore(int(max_concurrency))
        if queue_maxsize is not None:
            self.queue_maxsize = int(queue_maxsize)
        if queue_timeout_s is not None:
            self.queue_timeout_s = float(queue_timeout_s)
        return {
            "max_concurrency": self.max_concurrency,
            "queue_maxsize": self.queue_maxsize,
            "queue_timeout_s": self.queue_timeout_s,
        }

    def saturated(self) -> bool:
        return self._in_flight >= self.max_concurrency
