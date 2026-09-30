"""可观测性：链路追踪、指标与成本核算。

刻意不引入外部依赖、就放在进程内。重点不是「装一个 Jaeger」，而是必须记录哪些
东西，才能在事后回答一个真实的生产问题。接口沿用 OpenTelemetry 的概念命名，
换成真实 SDK 是机械替换。
"""

from .metrics import METRICS, CostRecord, Metrics
from .tracing import Span, Trace, Tracer, new_span_id, new_trace_id, write_traces

__all__ = [
    "METRICS",
    "CostRecord",
    "Metrics",
    "Span",
    "Trace",
    "Tracer",
    "new_span_id",
    "new_trace_id",
    "write_traces",
]
