"""压测的数据结构与分位数统计。

分位数取「实际观测到的值」，不做插值：延迟报告里，一个真的被观测到的值，
比一个在两次观测之间被算出来的值更站得住脚。
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Sequence
import statistics

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
