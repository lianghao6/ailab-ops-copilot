"""AILab Ops Copilot 的平台数据生成层。

对外接口：

    generate_world(...)   -> World   在内存中构建
    write_world(w, dir)   -> Paths   落盘为 jsonl / csv
    load_world(dir)       -> World   读回

所有标识符、主机名、路径和日志行都由种子 RNG 生成，同一个种子始终给出
同一份数据集，因此结果可复现、可对比。
"""

from .taxonomy import INSUFFICIENT_EVIDENCE, Playbook, Scenario, load_playbook
from .world import (
    Incident,
    Job,
    LogRecord,
    MetricSeries,
    Node,
    Team,
    World,
    WorldSummary,
    generate_world,
    load_world,
    write_timeseries_csv,
    write_world,
)

__all__ = [
    "INSUFFICIENT_EVIDENCE",
    "Incident",
    "Job",
    "LogRecord",
    "MetricSeries",
    "Node",
    "Playbook",
    "Scenario",
    "Team",
    "World",
    "WorldSummary",
    "generate_world",
    "load_world",
    "load_playbook",
    "write_timeseries_csv",
    "write_world",
]
