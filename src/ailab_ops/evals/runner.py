"""The only V2 hidden-label reader and repeat aggregation boundary."""

import json
from pathlib import Path
from statistics import fmean, pstdev
from time import perf_counter
from typing import Callable, Iterable

from ailab_ops.investigation.models import InvestigationState

from .models import EvaluationRun, EvaluationSample, MetricSummary, SCORE_NAMES
from .scoring import score_investigation


def load_eval_labels(path: Path) -> dict[str, dict]:
    labels = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row["case_id"]
        if case_id in labels:
            raise ValueError(f"Duplicate evaluation case ID: {case_id}")
        labels[case_id] = row
    return labels


def _summarize(runs) -> dict[str, MetricSummary]:
    summaries = {}
    for name in (*SCORE_NAMES, "latency_ms", "tokens_used"):
        values = [getattr(run, name) for run in runs if getattr(run, name) is not None]
        summaries[name] = (MetricSummary(len(values), fmean(values), max(values) - min(values),
                                        min(values), max(values), pstdev(values)) if values else
                           MetricSummary(0, None, None, None, None, None))
    return summaries


def run_evaluation(case_ids: Iterable[str], repeats: int,
                   factory: Callable[[str], InvestigationState | EvaluationSample], *,
                   labels_path: Path | None = None) -> EvaluationRun:
    """Call a fresh investigation factory per case/run, passing only case IDs.

    Spread is max-minus-min; stddev is population deviation. Failed factories
    contribute explicit zeros, and exception messages are never retained.
    """
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats <= 0:
        raise ValueError("repeats must be a positive integer")
    case_ids = list(case_ids)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("Duplicate evaluation case IDs")
    path = labels_path if labels_path is not None else Path(__file__).resolve().parents[3] / "data/v2/evals/labels.jsonl"
    labels = load_eval_labels(path)
    for case_id in case_ids:
        if case_id not in labels:
            raise KeyError(f"Missing evaluation label for {case_id}")
    runs = []
    by_case = {}
    for case_id in case_ids:
        current = []
        for _ in range(repeats):
            started = perf_counter()
            try:
                sample = factory(case_id)
                elapsed = (perf_counter() - started) * 1000
                if isinstance(sample, InvestigationState):
                    result = score_investigation(sample, labels[case_id], latency_ms=elapsed)
                elif isinstance(sample, EvaluationSample):
                    result = score_investigation(sample.state, labels[case_id], evidence=sample.evidence,
                        events=sample.events, latency_ms=sample.latency_ms if sample.latency_ms is not None else elapsed)
                else:
                    result = score_investigation(None, labels[case_id])
            except Exception as exc:
                result = score_investigation(None, labels[case_id])
                result.issues = ["factory_failed:" + type(exc).__name__]
            current.append(result)
        runs.extend(current)
        by_case[case_id] = _summarize(current)
    return EvaluationRun(runs, _summarize(runs), by_case)
