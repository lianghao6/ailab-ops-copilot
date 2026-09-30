"""指标与成本核算。

按租户记账的 token 与美元，可直接用于限流。加锁是必要的：服务端在事件循环里
服务请求，而 agent 循环跑在工作线程里，一把锁比它防住的 bug 便宜得多。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any
import threading
import time

@dataclass
class CostRecord:
    tenant_id: str
    tokens_in: int
    tokens_out: int
    usd: float
    ts: float

class Metrics:
    """In-process counters, histograms and cost ledger.

    Thread-safe because the server runs the agent loop in a worker thread while
    the event loop serves other requests; a lock here is cheaper than the bugs
    it prevents.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self.histograms: dict[str, list[float]] = defaultdict(list)
        self.costs: list[CostRecord] = []
        self._tenant_tokens_min: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self._tenant_usd_day: dict[str, float] = defaultdict(float)

    # ---- plain metrics -------------------------------------------------

    def incr(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self.histograms[name].append(value)

    def counter(self, name: str) -> float:
        with self._lock:
            return self.counters.get(name, 0.0)

    def histogram_stats(self, name: str) -> dict[str, float]:
        with self._lock:
            values = sorted(self.histograms.get(name, []))
        if not values:
            return {"count": 0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
        return {
            "count": len(values),
            "p50": _pct(values, 0.50),
            "p90": _pct(values, 0.90),
            "p99": _pct(values, 0.99),
            "max": values[-1],
            "mean": sum(values) / len(values),
        }

    # ---- cost ----------------------------------------------------------

    def record_cost(
        self, tenant_id: str, tokens_in: int, tokens_out: int, price_in_per_mtok: float, price_out_per_mtok: float
    ) -> float:
        usd = tokens_in / 1e6 * price_in_per_mtok + tokens_out / 1e6 * price_out_per_mtok
        with self._lock:
            self.costs.append(CostRecord(tenant_id, tokens_in, tokens_out, usd, time.time()))
            self._tenant_usd_day[tenant_id] += usd
            self._tenant_tokens_min[tenant_id].append((time.time(), tokens_in + tokens_out))
        return usd

    def tenant_usd_today(self, tenant_id: str) -> float:
        with self._lock:
            return self._tenant_usd_day.get(tenant_id, 0.0)

    def tenant_tokens_last_minute(self, tenant_id: str, now: float | None = None) -> int:
        now = now or time.time()
        cutoff = now - 60.0
        with self._lock:
            entries = self._tenant_tokens_min.get(tenant_id, [])
            kept = [(t, n) for t, n in entries if t >= cutoff]
            self._tenant_tokens_min[tenant_id] = kept
        return sum(n for _, n in kept)

    def total_usd(self) -> float:
        with self._lock:
            return sum(c.usd for c in self.costs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self.counters)
            hist_names = list(self.histograms)
            usd = sum(c.usd for c in self.costs)
            n_calls = len(self.costs)
        return {
            "counters": counters,
            "histograms": {n: self.histogram_stats(n) for n in hist_names},
            "cost": {"total_usd": round(usd, 6), "n_records": n_calls},
        }

def _pct(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile. Deliberately not interpolated: for latency
    reporting, a value that was actually observed is more defensible than one
    the reporter interpolated between two observations."""
    if not sorted_values:
        return 0.0
    idx = min(int(round(q * (len(sorted_values) - 1))), len(sorted_values) - 1)
    return sorted_values[idx]

# A process-wide metrics registry. The server and the CLI share it, which is
# what lets the benchmark read the same counters the server was writing.
METRICS = Metrics()
