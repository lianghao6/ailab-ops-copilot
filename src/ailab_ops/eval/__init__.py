"""Evaluation against ground truth.

The point of this module is not to produce a single accuracy number. A single
number tells you an agent is wrong somewhere; it does not tell you whether to
change the retrieval, the prompt, the tools, or the data. So the report is
built around the distinctions that map onto real decisions:

| metric                 | what it decides                                        |
|------------------------|--------------------------------------------------------|
| exact accuracy         | is the diagnosis right                                 |
| per-category accuracy  | which family of failure needs work (memory? network?)   |
| per-difficulty accuracy| is the system robust or just good on easy cases         |
| abstention precision   | when it refuses, is refusing correct                    |
| false-confidence rate  | how often is a WRONG answer given with high confidence  |
| confusion pairs        | which two causes are being confused, i.e. the fix       |
| answer-parse rate      | is the output usable, independent of being correct      |
| cost & latency         | what the accuracy costs per diagnosis                   |

Two of these deserve emphasis because they are usually missing and are the most
useful:

* **Abstention precision.** The dataset deliberately contains cases whose
  correct answer is "insufficient evidence". A system that refuses everything
  scores 6% on exact accuracy and 100% on abstention precision; a system that
  refuses nothing scores 94% and 0%. Reporting both makes the tradeoff visible
  instead of hiding it inside one number.
* **False-confidence rate.** A wrong answer delivered at 0.97 confidence costs
  an engineer real time. Accuracy hides this; this metric does not.
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..agent import AgentResult
from ..datagen.taxonomy import INSUFFICIENT_EVIDENCE
from ..runtime import Runtime

# Confidence at or above this is treated as a firm claim for the
# false-confidence metric. 0.7 is a judgement call, stated here so it can be
# argued with rather than being buried in the analysis code.
HIGH_CONFIDENCE = 0.70


@dataclass
class CaseResult:
    job_id: str
    question: str
    truth: str
    truth_name: str
    truth_category: str
    truth_difficulty: str
    is_unknown_case: bool
    predicted: str | None
    predicted_name: str | None
    confidence: float | None
    correct: bool
    refused: bool
    parsed_ok: bool
    confounders: list[str] = field(default_factory=list)
    n_tool_calls: int = 0
    n_llm_calls: int = 0
    tokens: int = 0
    usd: float = 0.0
    elapsed_ms: float = 0.0
    stop_reason: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalReport:
    n: int
    n_evaluated: int
    accuracy: float
    n_correct: int
    confidence_mean: float
    abstentions: int
    abstention_correct: int
    abstention_precision: float | None
    unknown_cases: int
    unknown_cases_refused: int
    unknown_recall: float | None
    false_confident_wrong: int
    false_confidence_rate: float
    parsed_ok: int
    parse_rate: float
    by_category: dict[str, dict[str, float]]
    by_difficulty: dict[str, dict[str, float]]
    confusion: list[dict[str, Any]]
    misses: list[dict[str, Any]]
    cost: dict[str, Any]
    latency: dict[str, Any]
    config: dict[str, Any]
    started_at: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_lines(self) -> list[str]:
        """Terminal report. Fixed-width, ordered by what to look at first."""
        L: list[str] = []
        add = L.append
        add("=" * 78)
        add("AILab Ops Copilot — evaluation report")
        add("=" * 78)
        add(f"cases evaluated      {self.n_evaluated} / {self.n}")
        add(f"exact accuracy       {self.accuracy:.1%}   ({self.n_correct} correct)")
        add(f"  by category        " + "  ".join(
            f"{k}:{v['accuracy']:.0%}(n={int(v['n'])})" for k, v in sorted(self.by_category.items())
        ))
        add(f"  by difficulty      " + "  ".join(
            f"{k}:{v['accuracy']:.0%}(n={int(v['n'])})" for k, v in sorted(self.by_difficulty.items())
        ))
        add("")
        add(f"abstentions          {self.abstentions}")
        add(f"  abstention precision {_fmt_pct(self.abstention_precision)}  "
            f"({self.abstention_correct}/{self.abstentions} refusals were correct refusals)")
        add(f"  unknown-case recall  {_fmt_pct(self.unknown_recall)}  "
            f"({self.unknown_cases_refused}/{self.unknown_cases} unknowable cases correctly refused)")
        add(f"false-confidence rate {self.false_confidence_rate:.1%}  "
            f"({self.false_confident_wrong} wrong answers given at >= {HIGH_CONFIDENCE:.0%} confidence)")
        add(f"answer parse rate    {self.parse_rate:.1%}  ({self.parsed_ok}/{self.n_evaluated} parseable)")
        add("")
        add(f"cost                 ${self.cost['total_usd']:.4f} total, "
            f"${self.cost['usd_per_diagnosis']:.6f} per diagnosis")
        add(f"tokens               {self.cost['total_tokens']} total, "
            f"{self.cost['tokens_per_diagnosis']:.0f} per diagnosis")
        add(f"latency              p50 {self.latency['p50']:.0f}ms  p90 {self.latency['p90']:.0f}ms  "
            f"p99 {self.latency['p99']:.0f}ms  max {self.latency['max']:.0f}ms")
        add(f"wall clock           {self.elapsed_s:.1f}s")
        add("")
        if self.confusion:
            add("most frequent confusions (predicted <- truth):")
            for c in self.confusion[:8]:
                add(f"  {c['predicted']:<28} <- {c['truth']:<28} x{c['n']}")
            add("")
        if self.misses:
            add(f"first {min(len(self.misses), 6)} misses:")
            for m in self.misses[:6]:
                add(f"  {m['job_id']}  truth={m['truth']:<24} pred={str(m['predicted']):<24} "
                    f"conf={m['confidence']}")
            add("")
        add("how to read this:")
        add("  · accuracy alone is not the goal — a system that never abstains looks better on")
        add("    accuracy and worse on abstention precision; watch both together.")
        add("  · a low per-difficulty accuracy with a high easy accuracy means the system")
        add("    pattern-matches rather than reasons; check the confusion pairs.")
        add("  · any non-zero false-confidence rate is a trust problem, not an accuracy problem.")
        add("=" * 78)
        return L


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


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


def write_report(report: EvalReport, path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def write_cases(results_path: Path, cases: Sequence[dict[str, Any]]) -> None:
    """Persist the case list next to the report.

    Without this, a report cannot be re-run or compared, because the sample it
    was computed over is gone.
    """
    with (results_path.parent / "eval_cases.jsonl").open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
