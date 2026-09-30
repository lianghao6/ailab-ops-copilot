"""评测的数据结构与展示。

`EvalReport` 围绕那些真正对应决策的区分来组织：准确率之外还有拒答精确率、
假自信率、混淆对和成本。只报一个准确率，只能告诉你系统错了；它不会告诉你
该改检索、改提示、改工具还是改数据。
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

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
