"""评测用例的选取。

按难度分层采样，且默认包含「证据不足」的案例——否则准确率会被容易的场景
抬上去，而校准指标永远测不到。
"""

from __future__ import annotations

from ..runtime import Runtime
from collections import defaultdict
from typing import Any
import random

def build_cases(
    runtime: Runtime,
    limit: int | None = None,
    include_unknown: bool = True,
    difficulty: str | None = None,
    category: str | None = None,
    seed: int = 7,
) -> list[dict[str, Any]]:
    """Select evaluation cases from the world.

    Stratification by difficulty is deliberate: sampling uniformly over the
    world would over-sample the easy scenarios (they are simply more common)
    and produce a flattering number. The default is to take a bounded number
    from each difficulty stratum so that every stratum is measurable.
    """
    from ..datagen.world import Job

    jobs: list[Job] = [j for j in runtime.world.jobs if j.status != "SUCCEEDED" and j.root_cause]
    if not include_unknown:
        jobs = [j for j in jobs if not j.is_insufficient_evidence]
    if difficulty:
        jobs = [j for j in jobs if j.difficulty == difficulty]
    if category:
        jobs = [j for j in jobs if j.category == category]

    rng = random.Random(seed)
    if limit and limit < len(jobs):
        by_diff: dict[str, list[Job]] = defaultdict(list)
        for j in jobs:
            by_diff[j.difficulty or "medium"].append(j)
        picked: list[Job] = []
        strata = sorted(by_diff)
        per = max(limit // max(len(strata), 1), 1)
        for d in strata:
            pool = sorted(by_diff[d], key=lambda x: x.job_id)
            rng.shuffle(pool)
            picked.extend(pool[:per])
        remaining = [j for j in jobs if j not in picked]
        rng.shuffle(remaining)
        picked.extend(remaining[: max(limit - len(picked), 0)])
        jobs = picked[:limit]

    return [
        {
            "job_id": j.job_id,
            "question": (
                f"Why did {j.job_id} ({j.name}) fail? "
                "State the root cause, the evidence, and what to do about it."
            ),
            "truth": j.root_cause,
            "truth_name": j.root_cause_name,
            "truth_category": j.category,
            "truth_difficulty": j.difficulty,
            "is_unknown_case": j.is_insufficient_evidence,
            "confounders": list(j.confounders),
        }
        for j in sorted(jobs, key=lambda x: x.job_id)
    ]
