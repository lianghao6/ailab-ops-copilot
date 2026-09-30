"""Synthetic data generation for the AILab Ops Copilot teaching platform.

Public surface:

    generate_world(...)   -> World   (in memory)
    World.write(dir)      -> Paths   (jsonl/csv artifacts on disk)
    load_world(dir)       -> World   (read back)

Nothing here touches a real system. Every identifier, hostname, path and log
line is produced from a seeded RNG, so a given seed always yields the same
dataset and every learner gets a private, self-consistent copy.
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
