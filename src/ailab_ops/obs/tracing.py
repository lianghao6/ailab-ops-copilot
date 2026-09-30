"""链路追踪：trace / span / 事件。

接口沿用 OpenTelemetry 的概念命名，因此换成真实 SDK 是机械替换，而不是重写。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Iterator
import json
import time
import uuid

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

def write_traces(traces: list[Trace], path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([t.to_dict() for t in traces], fh, ensure_ascii=False, indent=2)
    return path
