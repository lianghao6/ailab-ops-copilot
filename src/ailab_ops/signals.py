"""Evidence extraction and hypothesis scoring.

This module is the part of the system that actually *reasons about telemetry*.
It is deliberately separate from both the model adapter and the agent loop, for
two reasons:

1. **The mock LLM needs it.** A mock that hard-codes "if the log contains X
   answer Y" would teach nothing and would make the evaluation meaningless.
   This module does real work — it matches log lines against the playbook,
   classifies metric series into shapes, applies the cascade rule, and scores
   competing hypotheses — so the offline mode exercises the same reasoning
   path a real model would have to perform, with the same evidence.
2. **The evaluator needs it.** Comparing "what the agent concluded" against
   "what the evidence actually supports" is how you separate a retrieval
   failure from a reasoning failure from a classification failure. That
   separation is the single most useful thing in the evaluation report.

Everything here is a pure function of its inputs. No I/O, no model, no state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .datagen.taxonomy import INSUFFICIENT_EVIDENCE, Playbook

# --------------------------------------------------------------------------
# Metric shape classification
# --------------------------------------------------------------------------

SHAPES = (
    "flat",
    "noisy_high",
    "cliff_to_zero",
    "spike_then_zero",
    "ramp_to_ceiling",
    "sawtooth_rising",
    "staircase",
    "periodic_gap",
)


@dataclass
class ShapeVerdict:
    shape: str
    detail: str
    confidence: float

    def to_dict(self) -> dict:
        return {"shape": self.shape, "detail": self.detail, "confidence": round(self.confidence, 3)}


def classify_series(values: Sequence[float], metric: str = "") -> ShapeVerdict:
    """Classify a metric series into one of the shapes the playbook names.

    The order of the checks is the whole design, and it is load-bearing:

    1. degenerate (flat) first, because everything else will match noise;
    2. death-at-the-end shapes (spike, cliff) before trend shapes, because a
       series that ends at zero also "has a decreasing trend";
    3. sawtooth and staircase before ramp, because a rising sawtooth is also
       80%-rising and a descending staircase also looks like a trend;
    4. ramp, then noisy, then a weak fallback.

    Getting this order wrong is not a subtle bug: a sawtooth misread as a ramp
    turns "this is a leak" into "this is capacity exhaustion", which are
    different fixes. That is why each branch records *why* it matched, and why
    the tests pin the ambiguous cases individually.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return ShapeVerdict("flat", "no data points", 0.0)
    if n == 1:
        return ShapeVerdict("flat", "single data point; shape undeterminable", 0.1)

    hi = max(vals)
    lo = min(vals)
    span = hi - lo
    mean = sum(vals) / n
    scale = max(abs(hi), abs(mean), 1e-9)

    # --- 1. degenerate: effectively constant
    if span <= max(0.02 * scale, 1e-6):
        return ShapeVerdict("flat", f"range {lo:.2f}..{hi:.2f} is essentially constant", 0.9)

    tail_n = max(int(n * 0.2), 1)
    tail = vals[-tail_n:]
    head = vals[: n - tail_n] or vals[:1]
    tail_max = max(tail)
    head_max = max(head)
    head_mean = sum(head) / len(head)
    eps = max(0.06 * scale, 1e-6)

    # --- 2. death at the end.
    #
    # Detected by counting *trailing* zero points rather than by looking at the
    # last 20%: for a short series the 20% window reaches back into healthy
    # territory and the death is missed entirely. Spike is distinguished from
    # cliff by the height of the final live value against the level the series
    # had been running at, taken as a median so that one extreme point cannot
    # drag the comparison.
    trailing = 0
    for v in reversed(vals):
        if v <= eps:
            trailing += 1
        else:
            break
    live_vals = vals[: n - trailing]
    running_level = _median(live_vals) if live_vals else 0.0
    if trailing >= 2 and trailing < n - 1 and running_level > 0.2 * scale:
        last_live_val = live_vals[-1]
        if last_live_val >= 0.9 * scale and running_level <= 0.75 * scale:
            return ShapeVerdict(
                "spike_then_zero",
                f"ran near {running_level:.2f}, spiked to {last_live_val:.2f}, then stopped for "
                f"the last {trailing} points",
                0.85,
            )
        return ShapeVerdict(
            "cliff_to_zero",
            f"ran near {running_level:.2f} then collapsed to zero for the last {trailing} points",
            0.85,
        )

    # --- 3a. intermittent zeros, otherwise carrying load
    zeros = sum(1 for v in vals if v <= eps)
    if 0 < zeros < n * 0.5:
        runs_of_zero = _count_zero_runs(vals, eps)
        if runs_of_zero >= 2 and tail_max > 0.2 * scale:
            return ShapeVerdict(
                "periodic_gap",
                f"{zeros} zero points in {runs_of_zero} separate gaps, otherwise carrying load",
                0.7,
            )

    peaks, troughs = _peaks_troughs(vals, prominence=0.08 * scale)
    rising_frac = _fraction_rising(vals)
    rel_std = _rel_std(vals)

    # --- 3b. sawtooth: repeated, *prominent* peaks AND a trough floor that
    # drifts upward. Prominence filtering is what separates this from a smooth
    # ramp, whose tiny point-to-point noise would otherwise register as dozens
    # of peaks; and the floor rise is what separates it from ordinary jitter.
    floor_rise = _floor_rise(vals, troughs)
    if len(peaks) >= 2 and floor_rise > 0.10 * scale:
        return ShapeVerdict(
            "sawtooth_rising",
            f"{len(peaks)} prominent peaks and the trough floor rose by {floor_rise:.2f}",
            0.8,
        )

    # --- 3c. staircase: a descending sequence of a few plateaus. Detected by
    # quantising to a coarse grid and requiring the series to occupy few levels
    # with few transitions between them -- a steadily stepping throughput, as
    # opposed to the fifty-odd alternations of a noisy series.
    if _looks_like_staircase(vals, scale):
        return ShapeVerdict(
            "staircase",
            f"descending steps from {head_max:.2f} to {vals[-1]:.2f} with few distinct levels",
            0.75,
        )

    # --- 4a. monotone-ish rise that ends at the ceiling
    if rising_frac > 0.62 and vals[-1] >= 0.9 * hi and hi >= 0.85 * scale:
        return ShapeVerdict(
            "ramp_to_ceiling",
            f"{rising_frac:.0%} of consecutive steps increase and the series ends at its maximum",
            0.85,
        )

    # --- 4b. noisy and elevated, no trend
    if mean > 0.55 * scale and rel_std > 0.10 and len(peaks) >= 3 and floor_rise <= 0.10 * scale:
        return ShapeVerdict(
            "noisy_high",
            f"elevated mean {mean:.2f} with high relative variation ({rel_std:.0%}) and no floor drift",
            0.65,
        )
    if mean > 0.55 * scale and rel_std > 0.10:
        return ShapeVerdict(
            "noisy_high",
            f"elevated mean {mean:.2f} with high relative variation ({rel_std:.0%})",
            0.6,
        )

    # --- 4c. a flat-ish decline that is not stepped enough to be a staircase
    if rising_frac < 0.3 and vals[-1] < 0.6 * head_max and head_max > 0.4 * scale:
        return ShapeVerdict(
            "staircase",
            f"series declines from {head_max:.2f} to {vals[-1]:.2f} without a crash",
            0.5,
        )

    return ShapeVerdict("flat", f"no distinctive pattern (rel_std {rel_std:.0%})", 0.4)


def _looks_like_staircase(vals: Sequence[float], scale: float) -> bool:
    """A sustained decline: a few descending plateaus, or a steady downward slope.

    Two detectors, because both are "throughput going down without crashing" and
    the distinction between them is not one a diagnosis depends on:

    * **Stepped.** Run-length encode the median-filtered series into plateaus
      and check that their levels step down monotonically. This is the literal
      staircase: a rate limiter throttling, retries backing off.
    * **Sloped.** A monotone decline with few rises, judged by R² against a
      straight line. A long graceful degradation looks like this, and calling it
      "noisy" would be actively misleading -- noise is not the finding.

    Two earlier attempts and why they failed, because the lesson generalises:
    counting quantisation crossings is dominated by within-plateau noise (a
    staircase with realistic jitter crosses its own boundaries constantly), and
    comparing means over a fixed grid mis-splits the series when the step
    positions do not align with the grid.
    """
    n = len(vals)
    if n < 12:
        return False

    smoothed = _median_filter(vals, window=3)
    if smoothed[-1] >= 0.8 * smoothed[0]:
        return False  # not materially lower at the end

    if _is_linear(smoothed) and smoothed[-1] < 0.75 * smoothed[0]:
        return True

    tol = 0.06 * scale
    runs: list[list[float]] = [[smoothed[0]]]
    for x in smoothed[1:]:
        cur = runs[-1]
        if abs(x - (sum(cur) / len(cur))) <= tol:
            cur.append(x)
        else:
            runs.append([x])
    # Single-point runs are transition artefacts, not plateaus.
    plateaus = [r for r in runs if len(r) >= 2]
    if len(plateaus) < 3:
        return False

    levels = [sum(r) / len(r) for r in plateaus]
    drops = sum(1 for a, b in zip(levels, levels[1:]) if b < a - 0.02 * scale)
    rises = sum(1 for a, b in zip(levels, levels[1:]) if b > a + 0.02 * scale)
    return drops >= 2 and rises <= 1 and levels[-1] < 0.8 * levels[0]


def _median_filter(vals: Sequence[float], window: int = 3) -> list[float]:
    """Sliding median. Removes single-point noise without shifting step edges the
    way a moving average does -- which matters here, because the step edges are
    exactly the feature being looked for."""
    half = window // 2
    out: list[float] = []
    for i in range(len(vals)):
        lo = max(0, i - half)
        hi = min(len(vals), i + half + 1)
        s = sorted(vals[lo:hi])
        out.append(s[len(s) // 2])
    return out


def _is_linear(vals: Sequence[float], r2_threshold: float = 0.97) -> bool:
    """Least-squares fit against a straight line, judged by R².

    A crude but honest test for "is this a trend": if a line explains 97% of the
    variance, the series is a slope, not a series of plateaus. Computed directly
    rather than with numpy so the module keeps its zero-dependency property,
    which is what lets the whole reasoning core be imported anywhere.
    """
    n = len(vals)
    if n < 4:
        return False
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(vals) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return False
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, vals))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, vals))
    ss_tot = sum((y - my) ** 2 for y in vals)
    if ss_tot <= 0:
        return True
    return (1.0 - ss_res / ss_tot) >= r2_threshold


def _median(values: Sequence[float]) -> float:
    """Median, without pulling in numpy for one call.

    Used instead of the mean where a series has a small number of extreme
    points: a single spike drags a mean far enough to hide the very feature the
    comparison is trying to find.
    """
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _fraction_rising(vals: Sequence[float]) -> float:
    if len(vals) < 2:
        return 0.0
    ups = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
    return ups / (len(vals) - 1)


def _rel_std(vals: Sequence[float]) -> float:
    n = len(vals)
    mean = sum(vals) / n
    if mean == 0:
        return 0.0
    var = sum((v - mean) ** 2 for v in vals) / n
    return (var**0.5) / abs(mean)


def _count_zero_runs(vals: Sequence[float], eps: float) -> int:
    runs = 0
    prev_zero = False
    for v in vals:
        z = v <= eps
        if z and not prev_zero:
            runs += 1
        prev_zero = z
    return runs


def _peaks_troughs(vals: Sequence[float], prominence: float = 0.0) -> tuple[list[int], list[int]]:
    """Local maxima and minima, with an optional prominence filter.

    Prominence matters more than it looks. A smooth ramp with a little noise has
    a local maximum on nearly every other point, so without a filter a ramp and
    a sawtooth are indistinguishable -- and they mean different things (capacity
    exhaustion versus a leak). Requiring a peak to stand out from its
    neighbourhood by `prominence` is the cheapest fix that works here.
    """
    peaks: list[int] = []
    troughs: list[int] = []

    def _stands_out(i: int, is_peak: bool) -> bool:
        if prominence <= 0:
            return True
        lo = max(0, i - 3)
        hi = min(len(vals), i + 4)
        neighbourhood = list(vals[lo:i]) + list(vals[i + 1 : hi])
        if not neighbourhood:
            return True
        if is_peak:
            return vals[i] - min(neighbourhood) >= prominence
        return max(neighbourhood) - vals[i] >= prominence

    for i in range(1, len(vals) - 1):
        if vals[i] > vals[i - 1] and vals[i] >= vals[i + 1] and _stands_out(i, True):
            peaks.append(i)
        if vals[i] < vals[i - 1] and vals[i] <= vals[i + 1] and _stands_out(i, False):
            troughs.append(i)
    return peaks, troughs


def _floor_rise(vals: Sequence[float], troughs: Sequence[int]) -> float:
    if len(troughs) < 2:
        return 0.0
    half = len(troughs) // 2
    early = sum(vals[i] for i in troughs[:half]) / max(half, 1)
    late = sum(vals[i] for i in troughs[half:]) / max(len(troughs) - half, 1)
    return late - early


# --------------------------------------------------------------------------
# Log evidence extraction
# --------------------------------------------------------------------------

# The cascade rule, expressed as data. A line matching any of these is treated
# as a *symptom of a peer's death* rather than as causal evidence, and is
# excluded from hypothesis scoring for any scenario it does not uniquely
# identify. Without this, the mock reasoner (and a real model) will confidently
# report "collective timeout" for every multi-rank crash, because that string
# is the one that appears most often.
#
# The list is deliberately the *timeout* wording only. A genuine communication
# failure also emits low-level diagnostics -- an NCCL error, a rank that never
# joined, an explicit abort -- and those are causal evidence rather than
# cascade noise. Including them here was a real bug in an earlier revision: it
# made the `collective_timeout` scenario unfalsifiable, because every one of its
# signatures was classified as the symptom it exists to distinguish itself from.
CASCADE_PATTERNS = (
    r"watchdog caught collective operation timeout",
    r"watchdog.*timeout",
    r"ran \d+ milliseconds before timing out",
)

# Low-level diagnostics that indicate the collective itself failed, as opposed
# to a watchdog firing because a peer had already died. These identify a
# communication fault, so they must be allowed to support a hypothesis.
CAUSAL_COMM_PATTERNS = (
    r"nccl (?:error|timeout|unhandled)",
    r"some ranks did not join",
    r"aborted due to timeout",
    r"connection (?:refused|reset) during allreduce",
)

# Lines a supervisor or framework prints *about* the process rather than from
# it. Every failed job ends with one of these, so a signature that happens to
# occur inside them ("exit code 137") matches universally and therefore carries
# no information. Excluding them from signature matching is a small change that
# removes a whole class of false positives -- and it is worth pointing out that
# this is the same insight as IDF, applied to a hand-written pattern list rather
# than to a corpus.
WRAPPER_PATTERNS = (
    r"\bprocess exited with code\s+\d+",
    r"\bexit code\s+\d+\s*(?:\[|$)",
)


@dataclass
class LogEvidence:
    first_error: str | None
    first_error_rank: int | None
    first_error_ts: str | None
    matched: dict[str, list[str]] = field(default_factory=dict)
    cascade_lines: list[str] = field(default_factory=list)
    is_cascade_shaped: bool = False
    n_error_lines: int = 0
    ranks_reporting_cascade: list[int] = field(default_factory=list)
    truncation_hints: list[str] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return self.n_error_lines > 0

    def match_is_cascade_only(self, scenario_id: str) -> bool:
        """True if every pattern this scenario matched is a cascade pattern.

        The distinction this draws is the crux of the whole cascade rule. A
        scenario whose only support is "Watchdog caught collective operation
        timeout" has been matched *by* the cascade, not identified by it -- and
        so has every other scenario that lists the same timeout string. Saying
        "collective timeout" there is reporting the symptom with extra
        confidence, which is exactly the mistake the runbooks warn against.

        The nuance that makes this subtle: for the `collective_timeout` scenario
        itself, a timeout IS the cause. So being cascade-shaped is not on its own
        grounds for refusal -- what matters is whether the evidence is *only*
        the shared timeout wording, or whether something unique corroborates it.
        That corroboration check lives in `decide`, which looks at the metric
        signature too; this method answers the narrower question of whether the
        log evidence alone is generic.
        """
        pats = self.matched.get(scenario_id)
        if not pats:
            return False
        return all(_is_cascade_pattern(p) for p in pats)

    def to_dict(self) -> dict:
        return {
            "first_error": self.first_error,
            "first_error_rank": self.first_error_rank,
            "is_cascade_shaped": self.is_cascade_shaped,
            "n_error_lines": self.n_error_lines,
            "ranks_reporting_cascade": self.ranks_reporting_cascade,
            "matched_scenarios": {k: v for k, v in self.matched.items()},
            "truncation_hints": self.truncation_hints,
        }


_COMPILED: dict[str, re.Pattern] = {}


def _compile(pattern: str) -> re.Pattern:
    if pattern not in _COMPILED:
        _COMPILED[pattern] = re.compile(pattern, re.IGNORECASE)
    return _COMPILED[pattern]


def _is_cascade_pattern(pattern: str) -> bool:
    return any(_compile(p).search(pattern) for p in CASCADE_PATTERNS)


def extract_log_evidence(
    records: Iterable[Any], playbook: Playbook, max_lines: int | None = None
) -> LogEvidence:
    """Reduce a log to the evidence a human would actually use.

    `records` is anything with `.ts`, `.level`, `.rank`, `.message` — the
    datagen `LogRecord`, a dict, or a plain object from a tool result.
    """
    rows: list[tuple[str, str, int, str]] = []
    for r in records:
        ts = _attr(r, "ts", "")
        level = (_attr(r, "level", "INFO") or "INFO").upper()
        rank = int(_attr(r, "rank", 0) or 0)
        msg = _attr(r, "message", "") or ""
        rows.append((ts, level, rank, msg))
    rows.sort(key=lambda t: t[0])
    if max_lines:
        rows = rows[-max_lines:]

    ev = LogEvidence(first_error=None, first_error_rank=None, first_error_ts=None)
    errors = [(ts, rank, msg) for ts, level, rank, msg in rows if level in {"ERROR", "CRITICAL", "FATAL"}]
    ev.n_error_lines = len(errors)

    cascade_re = [_compile(p) for p in CASCADE_PATTERNS]
    for ts, rank, msg in errors:
        if any(rx.search(msg) for rx in cascade_re):
            ev.cascade_lines.append(msg)
            if rank not in ev.ranks_reporting_cascade:
                ev.ranks_reporting_cascade.append(rank)

    # First error that is NOT pure cascade noise. This is the "find the rank
    # whose error is earliest and unique" step from the runbook, implemented.
    for ts, rank, msg in errors:
        if any(rx.search(msg) for rx in cascade_re):
            continue
        ev.first_error, ev.first_error_rank, ev.first_error_ts = msg, rank, ts
        break
    if ev.first_error is None and errors:
        ev.first_error, ev.first_error_rank, ev.first_error_ts = errors[0][2], errors[0][1], errors[0][0]

    ev.is_cascade_shaped = len(ev.cascade_lines) >= 2 or len(ev.ranks_reporting_cascade) >= 2

    # Warnings from the whole log play a role too: a memory ramp warning is
    # evidence even when the fatal line is uninformative. Wrapper lines are
    # stripped first so that a supervisor message cannot impersonate a
    # signature.
    wrapper_re = [_compile(p) for p in WRAPPER_PATTERNS]
    substantive = [
        msg for _ts, _lv, _rk, msg in rows if not any(rx.search(msg) for rx in wrapper_re)
    ]
    haystack = "\n".join(msg for _ts, _lv, _rk, msg in rows)
    substantive_haystack = "\n".join(substantive)
    for s in playbook.scenarios.values():
        for pat in s.log_patterns:
            if pat.weight <= 0:
                continue
            rx = _compile(pat.pattern)
            # Match against substantive lines when possible. The fallback to the
            # full log covers signatures that legitimately only appear inside a
            # wrapper (none today, but the pattern list is data, not code).
            if rx.search(substantive_haystack) or (
                not substantive and rx.search(haystack)
            ):
                ev.matched.setdefault(s.id, []).append(pat.pattern)

    for hint in ("log level raised", "verbose output rotated out", "rotated out", "no metrics", "level=ERROR only"):
        if hint.lower() in haystack.lower():
            ev.truncation_hints.append(hint)

    return ev


def _attr(obj: Any, name: str, default: Any) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# --------------------------------------------------------------------------
# Hypothesis scoring
# --------------------------------------------------------------------------


@dataclass
class Hypothesis:
    scenario_id: str
    name: str
    score: float
    log_support: list[str] = field(default_factory=list)
    metric_support: list[str] = field(default_factory=list)
    exit_support: bool = False
    # How many scenarios in the playbook declare this same exit code. Used to
    # stop a shared code (137, 143, 1) from being mistaken for a signature.
    exit_codes_shared: int = 0
    penalty: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "score": round(self.score, 4),
            "log_support": self.log_support,
            "metric_support": self.metric_support,
            "exit_support": self.exit_support,
            "exit_codes_shared": self.exit_codes_shared,
            "penalty": round(self.penalty, 4),
            "rationale": self.rationale,
        }


@dataclass
class Verdict:
    root_cause: str
    confidence: float
    hypotheses: list[Hypothesis]
    evidence_strength: float
    decided_by: str
    notes: list[str] = field(default_factory=list)

    @property
    def is_refusal(self) -> bool:
        return self.root_cause == INSUFFICIENT_EVIDENCE

    @property
    def margin(self) -> float:
        if len(self.hypotheses) < 2:
            return self.hypotheses[0].score if self.hypotheses else 0.0
        return self.hypotheses[0].score - self.hypotheses[1].score

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "confidence": round(self.confidence, 3),
            "evidence_strength": round(self.evidence_strength, 3),
            "margin": round(self.margin, 4),
            "decided_by": self.decided_by,
            "notes": self.notes,
            "hypotheses": [h.to_dict() for h in self.hypotheses[:6]],
        }


# Decision thresholds. Exposed as module constants (rather than inlined) so a
# class can move them and watch the evaluation's precision/recall tradeoff
# change -- which is the point of having a calibration metric at all.
MIN_EVIDENCE_TO_DECIDE = 0.55
MIN_MARGIN = 0.18
STRONG_EVIDENCE = 1.6


def score_hypotheses(
    playbook: Playbook,
    log_evidence: LogEvidence,
    metrics: dict[str, list[float]] | None = None,
    exit_code: int | None = None,
    status: str | None = None,
) -> list[Hypothesis]:
    """Score every scenario in the playbook against the available evidence.

    Scoring is additive and unweighted in a deliberately boring way, so that
    every contribution can be pointed at in class:

        score = sum(matched log pattern weights)
              + sum(matched metric shape weights)
              + small exit-code agreement bonus
              - cascade penalty
              - status / telemetry penalties
    """
    shape_hits: dict[str, list[str]] = {}
    shape_verdicts: dict[str, ShapeVerdict] = {}
    for name, values in (metrics or {}).items():
        v = classify_series(values, name)
        shape_verdicts[name] = v
        shape_hits.setdefault(v.shape, []).append(name)

    # How many scenarios share each exit code, computed from the playbook so
    # that adding a scenario automatically weakens the wrong signals.
    exit_sharing: dict[int, int] = {}
    for s in playbook.scenarios.values():
        for c in s.exit_codes:
            exit_sharing[c] = exit_sharing.get(c, 0) + 1

    out: list[Hypothesis] = []
    for s in playbook.scenarios.values():
        if s.is_unknown:
            continue
        h = Hypothesis(scenario_id=s.id, name=s.name, score=0.0)

        for pat in s.log_patterns:
            if pat.weight <= 0:
                continue
            if s.id in log_evidence.matched and pat.pattern in log_evidence.matched[s.id]:
                h.log_support.append(pat.pattern)
                h.score += pat.weight

        for ms in s.metric_shapes:
            if ms.shape in shape_hits and ms.name in shape_hits[ms.shape]:
                h.metric_support.append(f"{ms.name}={ms.shape}")
                h.score += ms.weight

        if exit_code is not None and exit_code in s.exit_codes:
            h.exit_support = True
            h.exit_codes_shared = exit_sharing.get(exit_code, 1)
            # The bonus shrinks as more scenarios share the code. A code unique
            # to one scenario (28 -> disk_full) is real evidence; 137 is nearly
            # worthless, and the score should say so rather than treating both
            # as equal.
            h.score += 0.35 / max(h.exit_codes_shared, 1)

        # Cascade penalty: if the evidence is cascade-shaped and this scenario
        # is NOT the one whose unique signature was found, discount it. This is
        # the mechanical form of "do not report the timeout as the cause."
        if log_evidence.is_cascade_shaped and not h.log_support:
            h.penalty += 0.25
            h.rationale = "cascade-shaped logs but no unique signature for this scenario"

        # Status gating: a job that never ran cannot have a runtime cause.
        if status == "NEVER_STARTED" and s.category not in {"scheduling", "code"}:
            h.penalty += 0.6
            h.rationale = "job never started, so a runtime cause is not possible"

        h.score = max(h.score - h.penalty, 0.0)
        if not h.rationale:
            bits = []
            if h.log_support:
                bits.append(f"{len(h.log_support)} log pattern(s)")
            if h.metric_support:
                bits.append(f"metric {', '.join(h.metric_support)}")
            if h.exit_support:
                bits.append(
                    f"exit {exit_code} (shared by {h.exit_codes_shared} scenarios)"
                    if h.exit_codes_shared > 1
                    else f"exit {exit_code}"
                )
            h.rationale = "evidence: " + (", ".join(bits) if bits else "none")
        out.append(h)

    out.sort(key=lambda x: x.score, reverse=True)
    return out


def decide(
    playbook: Playbook,
    log_evidence: LogEvidence,
    hypotheses: list[Hypothesis],
    status: str | None = None,
    exit_code: int | None = None,
    telemetry_gap: bool = False,
) -> Verdict:
    """Turn scores into a decision, including the decision to refuse.

    `telemetry_gap` is the caller telling us that a job which ran for a
    substantial time has produced NO metric series at all. That absence is
    itself evidence -- it means telemetry was not being collected -- and a
    system that ignores it will confidently guess at a cause from a log that
    was never complete. It is passed in rather than inferred because "there
    were no metrics" is knowledge the tool layer has and this module does not.
    """
    notes: list[str] = []
    top = hypotheses[0] if hypotheses else None

    if top is None:
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, [], 0.0, "no candidates", ["playbook produced no candidates"])

    second = hypotheses[1].score if len(hypotheses) > 1 else 0.0
    margin = top.score - second
    strength = top.score
    by = "signature match"

    # The refusal path. Each condition is a distinct, teachable reason.
    if not log_evidence.has_error and status in {"NEVER_STARTED", None}:
        notes.append("no error lines retained in the log")
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "no error evidence", notes)

    if log_evidence.truncation_hints:
        notes.append(f"log truncation signals present: {log_evidence.truncation_hints}")

    generic_exits = {137, 143, 1, None}
    if (
        top.score < MIN_EVIDENCE_TO_DECIDE
        and strength < STRONG_EVIDENCE
        and margin < MIN_MARGIN
    ):
        notes.append(
            f"top hypothesis {top.scenario_id} scores {top.score:.2f} with a margin of {margin:.2f}; "
            "that is below the threshold for a confident answer"
        )
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "below decision threshold", notes)

    if margin < MIN_MARGIN and top.score < STRONG_EVIDENCE:
        notes.append(
            f"two hypotheses are nearly tied ({top.scenario_id}={top.score:.2f} vs "
            f"{hypotheses[1].scenario_id}={second:.2f}); the evidence does not discriminate"
        )
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "ambiguous evidence", notes)

    if log_evidence.is_cascade_shaped and not top.log_support:
        notes.append(
            "logs are cascade-shaped (several ranks report a collective timeout) but the leading "
            "hypothesis has no signature of its own; the causal rank's error is missing from the "
            "retained evidence"
        )
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "cascade without a unique signature", notes)

    # A hypothesis supported ONLY by a cascade pattern is supported by the
    # symptom. Reporting it as the cause is the single most common
    # misdiagnosis in distributed training, and it is the reason the cascade
    # rule exists at all.
    if log_evidence.is_cascade_shaped and log_evidence.match_is_cascade_only(top.scenario_id):
        notes.append(
            f"every signature for {top.scenario_id} is a collective-timeout line, which several "
            "scenarios share; that identifies the symptom, not the cause"
        )
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "cascade without a unique signature", notes)

    # A long-running job with no telemetry at all: the remaining signal is a
    # generic exit code, which is shared by a dozen different causes. Guessing
    # here would produce a confident wrong answer -- exactly the failure this
    # metric exists to catch.
    if telemetry_gap and top.score < STRONG_EVIDENCE and top.exit_support and top.score <= 1.0:
        notes.append(
            "the job ran long enough to have produced metrics, but no metric series exist for it; "
            "the only remaining signal is a shared exit code, which does not discriminate"
        )
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "telemetry absent, exit code ambiguous", notes)

    # A single generic exit-code match, with nothing corroborating it, is not a
    # diagnosis even when it is the highest score.
    if top.exit_support and not top.log_support and not top.metric_support:
        notes.append(
            f"the only evidence for {top.scenario_id} is that exit code {exit_code_hint(log_evidence)} "
            f"is among its listed codes, and {top.exit_codes_shared} scenarios share that code; "
            "no log or metric signature corroborates it"
        )
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "exit code alone", notes)

    if not log_evidence.has_error and top.score < MIN_EVIDENCE_TO_DECIDE:
        notes.append("no decisive log evidence and no strong metric signature")
        return Verdict(INSUFFICIENT_EVIDENCE, 0.0, hypotheses, strength, "no decisive evidence", notes)

    confidence = _confidence(top.score, margin, log_evidence)
    if log_evidence.is_cascade_shaped and top.log_support:
        notes.append(
            "cascade-shaped logs, but a unique signature was found; reporting the unique signature "
            "rather than the more frequent timeout"
        )
        by = "unique signature under cascade"
    if top.exit_support and top.exit_codes_shared:
        notes.append(
            f"exit code matched, but it is shared by {top.exit_codes_shared} scenarios, so it was "
            "used only as corroboration"
        )
    return Verdict(top.scenario_id, confidence, hypotheses, strength, by, notes)


def _confidence(score: float, margin: float, ev: LogEvidence) -> float:
    """A bounded, monotone confidence. Not a calibrated probability, and the
    README says so: it is a communication device for ranking answers, and
    treating it as a probability is exactly the mistake the calibration metric
    exists to expose."""
    import math

    raw = 1.0 - math.exp(-0.85 * (score + 1.5 * margin))
    if ev.is_cascade_shaped and not ev.matched:
        raw *= 0.7
    return max(0.05, min(0.97, raw))


# --------------------------------------------------------------------------
# Metric support helpers used by tools and by the mock reasoner
# --------------------------------------------------------------------------


def exit_code_hint(ev: LogEvidence) -> str:
    """Best-effort exit code for a message, taken from the first error line."""
    for text in (ev.first_error or "", *(ev.cascade_lines or [])):
        m = re.search(r"exit code\s+(\d+)", text or "")
        if m:
            return m.group(1)
    return "the observed"


def summarise_metrics(series: dict[str, list[float]], limit: int = 8) -> dict[str, dict]:
    """Per-metric summary: shape verdict plus first/last/min/max."""
    out: dict[str, dict] = {}
    for i, (name, values) in enumerate(series.items()):
        if i >= limit:
            break
        v = classify_series(values, name)
        out[name] = {
            "shape": v.shape,
            "shape_detail": v.detail,
            "n": len(values),
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }
    return out
