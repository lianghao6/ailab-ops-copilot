"""压测：并发负载生成。

测的是**架构在并发下的行为**——准入控制、排队、缓存、降级——不是推理吞吐。
三个场景各自只打开一个受试机制，其余天花板全部抬起，这样拒绝计数才有归因。
"""

from .mix import build_question_mix
from .models import BenchReport, ClientResult, percentiles
from .reporting import write_bench_report
from .runner import run_benchmark
from .scenarios import (
    DEFAULT_GATE_CONCURRENCY,
    DEFAULT_GATE_QUEUE,
    SCENARIO_ACTIVE_CONTROL,
    SCENARIO_GATE,
    SCENARIO_LIMITS,
)

__all__ = [
    "BenchReport",
    "ClientResult",
    "DEFAULT_GATE_CONCURRENCY",
    "DEFAULT_GATE_QUEUE",
    "SCENARIO_ACTIVE_CONTROL",
    "SCENARIO_GATE",
    "SCENARIO_LIMITS",
    "build_question_mix",
    "percentiles",
    "run_benchmark",
    "write_bench_report",
]
