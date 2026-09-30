"""Observability: tracing, metrics, and cost accounting.

Kept dependency-free and in-process on purpose. The teaching point is not
"install Jaeger" -- it is *what you must record so that a production question
can be answered afterwards*. Those questions are concrete:

* Why was this one request slow? -> per-step spans with durations.
* What did the model actually receive? -> recorded prompt/response token counts
  and step kinds; optional content capture, off by default because it is both
  large and sensitive.
* How much is a tenant costing us right now? -> token and USD accounting keyed
  by tenant, so it can be rate-limited on.
* Why did the answer change? -> the tool-call trace, which is the equivalent of
  a query plan: the same question can produce a different answer only if a
  different set of evidence was read.

Interfaces are named after OpenTelemetry concepts (trace, span, attribute) so
that swapping this for a real SDK is a mechanical change rather than a rewrite.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Span:
    name: str
    span_id: str
    parent_id: str | None
    trace_id: str
    start_ms: float
    end_ms: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "OK"
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.end_ms if self.end_ms is not None else time.perf_counter() * 1000.0) - self.start_ms

    def set(self, **attrs: Any) -> "Span":
        self.attributes.update(attrs)
        return self

    def add_event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "t": time.perf_counter() * 1000.0, **attrs})

    def finish(self, error: str | None = None) -> None:
        self.end_ms = time.perf_counter() * 1000.0
        if error:
            self.status = "ERROR"
            self.error = error

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration_ms"] = round(self.duration_ms, 3)
        return d


@dataclass
class Trace:
    trace_id: str
    question: str = ""
    tenant_id: str = ""
    user_id: str = ""
    spans: list[Span] = field(default_factory=list)
    created_at_ms: float = field(default_factory=lambda: time.perf_counter() * 1000.0)
    outcome: str = ""
    cache_hit: bool = False
    queued_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "outcome": self.outcome,
            "cache_hit": self.cache_hit,
            "queued_ms": round(self.queued_ms, 2),
            "error": self.error,
            "spans": [s.to_dict() for s in self.spans],
        }

    def step_summary(self) -> list[dict]:
        """One row per span, for a terminal table or a report."""
        return [
            {
                "name": s.name,
                "ms": round(s.duration_ms, 2),
                "status": s.status,
                "tokens_in": s.attributes.get("tokens_in"),
                "tokens_out": s.attributes.get("tokens_out"),
            }
            for s in self.spans
        ]


class Tracer:
    """Collects spans for one logical operation and exports them."""

    def __init__(self, trace_id: str | None = None, record_content: bool = False) -> None:
        self.trace = Trace(trace_id=trace_id or new_trace_id())
        self.record_content = record_content
        self._stack: list[str] = []

    def start_span(self, name: str, **attrs: Any) -> Span:
        sp = Span(
            name=name,
            span_id=new_span_id(),
            parent_id=self._stack[-1] if self._stack else None,
            trace_id=self.trace.trace_id,
            start_ms=time.perf_counter() * 1000.0,
            attributes=dict(attrs),
        )
        self.trace.spans.append(sp)
        self._stack.append(sp.span_id)
        return sp

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        sp = self.start_span(name, **attrs)
        try:
            yield sp
        except Exception as exc:  # record and re-raise; never swallow
            sp.finish(error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            sp.finish()
        finally:
            if self._stack and self._stack[-1] == sp.span_id:
                self._stack.pop()

    def record_content(self, name: str, content: str) -> None:
        if self.record_content and self.trace.spans:
            self.trace.spans[-1].add_event(name, content=content[:4000])


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
    the reporter invented between two observations."""
    if not sorted_values:
        return 0.0
    idx = min(int(round(q * (len(sorted_values) - 1))), len(sorted_values) - 1)
    return sorted_values[idx]


def write_traces(traces: list[Trace], path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([t.to_dict() for t in traces], fh, ensure_ascii=False, indent=2)
    return path


# A process-wide metrics registry. The server and the CLI share it, which is
# what lets the benchmark read the same counters the server was writing.
METRICS = Metrics()
