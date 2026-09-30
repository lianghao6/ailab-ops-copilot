"""The fault playbook: loading and querying `faults.yaml`.

This module is deliberately tiny but load-bearing — the playbook is the
project's single source of truth, and every other component (generator,
knowledge base, mock reasoner, evaluator) consumes it through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PLAYBOOK_PATH = Path(__file__).with_name("faults.yaml")

# The label a correct system should emit when the retained telemetry does not
# support any single root cause. Scored as correct, not as a miss.
INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class LogPattern:
    pattern: str
    weight: float


@dataclass(frozen=True)
class MetricShape:
    name: str
    shape: str
    weight: float


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    category: str
    difficulty: str
    prevalence: int
    distractors: tuple[str, ...]
    log_patterns: tuple[LogPattern, ...]
    metric_shapes: tuple[MetricShape, ...]
    exit_codes: tuple[int, ...]
    terminal: bool
    remediation: str
    runbook_title: str
    runbook_body: str

    @property
    def root_cause(self) -> str:
        """Ground-truth label. Equal to the id by construction."""
        return self.id

    @property
    def is_unknown(self) -> bool:
        return self.id == INSUFFICIENT_EVIDENCE

    @property
    def signature(self) -> float:
        """Total evidence weight.

        Used to sanity-check the playbook: a scenario whose patterns carry
        almost no weight is one the generator will produce look-alike copies
        of, which is intentional for `insufficient_evidence` and a smell
        anywhere else.
        """
        return sum(p.weight for p in self.log_patterns) + sum(m.weight for m in self.metric_shapes)


@dataclass(frozen=True)
class Playbook:
    schema_version: int
    domain: str
    split_ratios: dict[str, float]
    insufficient_evidence_ratio: float
    scenarios: dict[str, Scenario]

    def get(self, scenario_id: str) -> Scenario:
        return self.scenarios[scenario_id]

    @property
    def ids(self) -> list[str]:
        return list(self.scenarios)

    def by_category(self) -> dict[str, list[Scenario]]:
        out: dict[str, list[Scenario]] = {}
        for s in self.scenarios.values():
            out.setdefault(s.category, []).append(s)
        return out

    def by_difficulty(self, difficulty: str) -> list[Scenario]:
        return [s for s in self.scenarios.values() if s.difficulty == difficulty]

    def diagnosable(self) -> list[Scenario]:
        """Scenarios that should always resolve to a definite root cause."""
        return [s for s in self.scenarios.values() if not s.is_unknown]


def _parse_scenario(raw: dict[str, Any]) -> Scenario:
    runbook = raw.get("runbook") or {}
    return Scenario(
        id=raw["id"],
        name=raw["name"],
        category=raw["category"],
        difficulty=raw.get("difficulty", "medium"),
        prevalence=int(raw.get("prevalence", 1)),
        distractors=tuple(raw.get("distractors") or ()),
        log_patterns=tuple(
            LogPattern(pattern=p["pattern"], weight=float(p.get("weight", 1.0)))
            for p in ((raw.get("signals") or {}).get("log_patterns") or [])
        ),
        metric_shapes=tuple(
            MetricShape(name=m["name"], shape=m["shape"], weight=float(m.get("weight", 1.0)))
            for m in ((raw.get("signals") or {}).get("metric_shapes") or [])
        ),
        exit_codes=tuple(int(c) for c in ((raw.get("signals") or {}).get("exit_codes") or ())),
        terminal=bool(raw.get("terminal", True)),
        remediation=" ".join((raw.get("remediation") or "").split()),
        runbook_title=(runbook.get("title") or raw["name"]).strip(),
        runbook_body=" ".join((runbook.get("body") or "").split()),
    )


@lru_cache(maxsize=1)
def load_playbook(path: str | None = None) -> Playbook:
    """Load and validate the playbook.

    Raises ValueError on internal inconsistencies (a distractor naming an
    unknown scenario, duplicate ids) so the failure surfaces at import time
    rather than as a confusing downstream bug.
    """
    p = Path(path) if path else PLAYBOOK_PATH
    with p.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    meta = doc.get("meta") or {}
    scenarios = [_parse_scenario(s) for s in doc.get("scenarios") or []]
    if not scenarios:
        raise ValueError(f"playbook at {p} defines no scenarios")

    ids = [s.id for s in scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")

    known = set(ids)
    for s in scenarios:
        bad = [d for d in s.distractors if d not in known]
        if bad:
            raise ValueError(f"scenario {s.id} lists unknown distractors {bad}")

    return Playbook(
        schema_version=int(meta.get("schema_version", 1)),
        domain=str(meta.get("domain", "")),
        split_ratios={k: float(v) for k, v in (meta.get("split_ratios") or {}).items()},
        insufficient_evidence_ratio=float(meta.get("insufficient_evidence_ratio", 0.0)),
        scenarios={s.id: s for s in scenarios},
    )
