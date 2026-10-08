"""串行执行评测并聚合结果。

刻意串行：这是一次测量，在自造负载下测出来的数字测的是负载，不是系统。
并发行为由 bench 负责度量。
"""

from __future__ import annotations

from ..agent import AgentResult
from ..datagen.taxonomy import INSUFFICIENT_EVIDENCE
from ..legacy.runtime import Runtime
from .models import CaseResult
from .models import EvalReport
from .models import HIGH_CONFIDENCE
from collections import Counter
from collections import defaultdict
from typing import Any
from typing import Sequence
import time

def run_eval(
    runtime: Runtime,
    cases: Sequence[dict[str, Any]],
    max_steps: int | None = None,
    progress: bool = True,
) -> EvalReport:
    """Run each case through a fresh agent and score it.

    Sequential rather than concurrent on purpose: this is a measurement, and a
    measurement taken while the system is under self-inflicted load measures
    the load, not the system. The benchmark in `bench.py` is the component that
    measures behaviour under concurrency.
    """
    t0 = time.perf_counter()
    results: list[CaseResult] = []
    s = runtime.settings

    for i, case in enumerate(cases, 1):
        agent = runtime.new_agent()
        tracer = runtime.new_tracer()
        try:
            res: AgentResult = agent.run(
                case["question"], deadline_s=max(s.llm_timeout_s * 3, 60.0), tracer=tracer,
                max_steps=max_steps,
            )
        except Exception as exc:
            res = AgentResult(
                question=case["question"], answer="", steps=[], stop_reason="exception",
                error=f"{type(exc).__name__}: {exc}",
            )

        pred = (res.parsed or {}).get("root_cause") if res.parsed else None
        conf = (res.parsed or {}).get("confidence") if res.parsed else None
        usd = (res.usage_in / 1e6 * s.price_input_per_mtok) + (res.usage_out / 1e6 * s.price_output_per_mtok)
        refused = pred == INSUFFICIENT_EVIDENCE

        results.append(
            CaseResult(
                job_id=case["job_id"],
                question=case["question"],
                truth=case["truth"],
                truth_name=case["truth_name"] or "",
                truth_category=case["truth_category"] or "",
                truth_difficulty=case["truth_difficulty"] or "",
                is_unknown_case=bool(case["is_unknown_case"]),
                predicted=pred,
                predicted_name=(res.parsed or {}).get("root_cause_label") if res.parsed else None,
                confidence=conf,
                correct=bool(pred) and pred == case["truth"],
                refused=refused,
                parsed_ok=res.parsed is not None,
                confounders=list(case.get("confounders") or []),
                n_tool_calls=res.n_tool_calls,
                n_llm_calls=res.n_llm_calls,
                tokens=res.tokens_total,
                usd=usd,
                elapsed_ms=res.elapsed_ms,
                stop_reason=res.stop_reason,
                error=res.error,
            )
        )

        if progress and (i % 10 == 0 or i == len(cases)):
            n_ok = sum(1 for r in results if r.correct)
            print(
                f"  [{i:>4}/{len(cases)}] running accuracy {n_ok / i:.1%} "
                f"({n_ok} correct, {sum(1 for r in results if r.refused)} abstained)",
                flush=True,
            )

    return _aggregate(results, config=_config_fingerprint(runtime), elapsed_s=time.perf_counter() - t0)

def _config_fingerprint(runtime: Runtime) -> dict[str, Any]:
    """Record the configuration alongside the result.

    An accuracy number without the configuration that produced it is not a
    measurement, it is an anecdote: it cannot be compared with anything, and it
    cannot be reproduced.
    """
    s = runtime.settings
    return {
        "seed": runtime.world.seed,
        "n_jobs": len(runtime.world.jobs),
        "llm_backend": s.llm_backend,
        "llm_model": s.llm_model,
        "max_steps": s.max_steps,
        "retrieval": {
            "fusion": getattr(runtime.kb.retriever, "fusion", None),
            "embedder": type(getattr(runtime.kb.retriever, "embedder", None)).__name__,
            "n_docs": len(runtime.kb.docs),
            "n_chunks": getattr(runtime.kb.retriever, "n_chunks", None),
        },
    }

def _aggregate(results: list[CaseResult], config: dict[str, Any], elapsed_s: float) -> EvalReport:
    n = len(results)
    n_eval = sum(1 for r in results if r.predicted is not None or r.refused)
    correct = [r for r in results if r.correct]
    confs = [r.confidence for r in results if r.confidence is not None]

    by_cat: dict[str, list[CaseResult]] = defaultdict(list)
    by_diff: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_cat[r.truth_category or "unknown"].append(r)
        by_diff[r.truth_difficulty or "medium"].append(r)

    def agg(group: list[CaseResult]) -> dict[str, float]:
        return {
            "n": float(len(group)),
            "accuracy": sum(1 for r in group if r.correct) / len(group) if group else 0.0,
            "abstention_rate": sum(1 for r in group if r.refused) / len(group) if group else 0.0,
        }

    abstained = [r for r in results if r.refused]
    # A refusal is "correct" when the case genuinely had no decisive evidence.
    # Being cautious on an easy case is not an error, but it is not a correct
    # answer either, so it is excluded from the precision numerator and counted
    # in the denominator -- which is what makes this metric hard to game.
    abstention_correct = sum(1 for r in abstained if r.is_unknown_case)
    unknown_cases = [r for r in results if r.is_unknown_case]
    unknown_refused = sum(1 for r in unknown_cases if r.refused)

    fc_wrong = sum(
        1 for r in results if (not r.correct) and (r.confidence or 0.0) >= HIGH_CONFIDENCE and not r.refused
    )

    conf_pairs = Counter(
        (r.predicted or "<no answer>", r.truth) for r in results if not r.correct
    )
    confusion = [
        {"predicted": p, "truth": t, "n": c} for (p, t), c in conf_pairs.most_common(12)
    ]
    misses = [
        {
            "job_id": r.job_id,
            "truth": r.truth,
            "predicted": r.predicted,
            "confidence": r.confidence,
            "difficulty": r.truth_difficulty,
            "stop_reason": r.stop_reason,
            "error": r.error,
        }
        for r in results
        if not r.correct
    ]

    lat = sorted(r.elapsed_ms for r in results)
    total_usd = sum(r.usd for r in results)
    total_tokens = sum(r.tokens for r in results)

    import datetime as dt

    return EvalReport(
        n=n,
        n_evaluated=n_eval,
        accuracy=len(correct) / n if n else 0.0,
        n_correct=len(correct),
        confidence_mean=(sum(confs) / len(confs)) if confs else 0.0,
        abstentions=len(abstained),
        abstention_correct=abstention_correct,
        abstention_precision=(abstention_correct / len(abstained)) if abstained else None,
        unknown_cases=len(unknown_cases),
        unknown_cases_refused=unknown_refused,
        unknown_recall=(unknown_refused / len(unknown_cases)) if unknown_cases else None,
        false_confident_wrong=fc_wrong,
        false_confidence_rate=(fc_wrong / n) if n else 0.0,
        parsed_ok=sum(1 for r in results if r.parsed_ok),
        parse_rate=(sum(1 for r in results if r.parsed_ok) / n) if n else 0.0,
        by_category={k: agg(v) for k, v in sorted(by_cat.items())},
        by_difficulty={k: agg(v) for k, v in sorted(by_diff.items())},
        confusion=confusion,
        misses=misses,
        cost={
            "total_usd": round(total_usd, 6),
            "usd_per_diagnosis": round(total_usd / n, 6) if n else 0.0,
            "total_tokens": total_tokens,
            "tokens_per_diagnosis": round(total_tokens / n, 1) if n else 0.0,
        },
        latency={
            "p50": _pct(lat, 0.50),
            "p90": _pct(lat, 0.90),
            "p99": _pct(lat, 0.99),
            "max": lat[-1] if lat else 0.0,
            "mean": sum(lat) / len(lat) if lat else 0.0,
        },
        config=config,
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        elapsed_s=elapsed_s,
    )

def _pct(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(int(round(q * (len(sorted_values) - 1))), len(sorted_values) - 1)
    return sorted_values[idx]
