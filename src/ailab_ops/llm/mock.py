"""Unsupported V1 deterministic playbook simulator.

Kept for explicit legacy commands, the legacy HTTP stub and regression tests.
It uses the same authored fault signatures as the V1 telemetry generator;
its diagnosis scores do not establish model capability or generalization.
V2 never loads this module. See docs/legacy-v1.md for the historical version.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from ..datagen.taxonomy import INSUFFICIENT_EVIDENCE, Playbook, load_playbook
from ..signals import (
    LogEvidence,
    decide,
    extract_log_evidence,
    score_hypotheses,
)
from .base import (
    ChatMessage,
    FinishReason,
    LLMResponse,
    ToolCall,
    ToolSpec,
    Usage,
    approx_tokens,
)

JOB_ID_RE = re.compile(r"\bjob-[0-9a-f]{6,12}-\d{3,6}\b")

# The mock follows a plan, but the plan is *adaptive*: a step is skipped or
# added in response to what the previous step returned. That adaptivity is the
# difference between an agent and a fixed pipeline, and it is worth being able
# to point at.
PLAN_FULL = ("get_job", "search_logs", "get_metrics", "search_runbooks")
# No metrics exist for a job that never ran, so the plan omits that step -- but
# it must still read logs, because for a scheduling or startup failure the
# scheduler's own output IS the evidence. Skipping the log search here is a
# concrete example of a plan that "looks reasonable" and quietly destroys
# accuracy on a whole category.
PLAN_NO_METRICS = ("get_job", "search_logs", "search_runbooks")

SYSTEM_PROMPT = """You are AILab Ops Copilot, a read-only assistant that diagnoses failed
training and evaluation jobs on the AILab platform.

Method, in order:
1. Fetch the job record. Status and duration are diagnostic: NEVER_STARTED, or a duration of
   only a few seconds, means there is no runtime telemetry and the cause is in scheduling,
   startup or configuration.
2. Read the error log. Prefer the FIRST meaningful error over the last line. When several
   ranks report the same message, that message is usually a cascade, not the cause.
3. Read the metrics and classify each series: cliff means crash, flat means hang, ramp means
   resource exhaustion, staircase means retry backoff.
4. Retrieve the matching runbook and verify that the discriminator it names is present in the
   evidence you actually have.
5. Answer with a root cause, the evidence for it, and the rival explanation you ruled out.
   If the evidence does not discriminate, answer "insufficient_evidence" and name what would
   resolve it. Never guess: a confident wrong answer costs more than an honest gap.

You are read-only. You cannot restart jobs, change quotas, or modify anything."""

ANSWER_SCHEMA_HINT = (
    "Answer with a single JSON object: "
    '{"root_cause": "<id or insufficient_evidence>", "confidence": 0.0-1.0, '
    '"summary": "...", "evidence": ["..."], "ruled_out": [{"cause": "...", "why": "..."}], '
    '"remediation": ["..."], "citations": ["<doc_id>"]}'
)


@dataclass
class _State:
    """Per-conversation state, reconstructed from the message history.

    Reconstructed rather than stored, because that is what a stateless serving
    layer requires: any replica must be able to continue any conversation.
    """

    job_id: str | None = None
    question: str = ""
    tool_calls_done: list[str] = field(default_factory=list)
    job_record: dict[str, Any] | None = None
    log_lines: list[dict[str, Any]] = field(default_factory=list)
    log_meta: dict[str, Any] = field(default_factory=dict)
    metric_series: dict[str, list[float]] = field(default_factory=dict)
    metric_meta: dict[str, Any] = field(default_factory=dict)
    runbook_hits: list[dict[str, Any]] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)


class MockLLMClient:
    """Offline reasoner implementing the `LLMClient` protocol."""

    def __init__(
        self,
        model: str = "mock-diagnoser-v1",
        playbook: Playbook | None = None,
        max_log_lines: int = 40,
        verbose: bool = False,
        latency_s: float = 0.0,
    ) -> None:
        self.model = model
        self.playbook = playbook or load_playbook()
        self.max_log_lines = max_log_lines
        self.verbose = verbose
        # Simulated per-call think time. Off by default (the test suite should
        # not sleep), but essential for the benchmark: without it every call
        # returns instantly, the gate is never contended, and the concurrency
        # controls become unobservable.
        self.latency_s = latency_s

    # ---- protocol ------------------------------------------------------

    def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        state = self._read_state(messages)

        if state.job_id is None:
            return self._final(
                content=json.dumps(
                    {
                        "root_cause": INSUFFICIENT_EVIDENCE,
                        "confidence": 0.0,
                        "summary": (
                            "No job id was found in the request. Give me a job id (for example "
                            "job-68bdf963-0000) or a team and time window, and I will investigate."
                        ),
                        "evidence": [],
                        "ruled_out": [],
                        "remediation": [],
                        "citations": [],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                state=state,
                t0=t0,
                messages=messages,
            )

        nxt = self._next_action(state)
        if nxt is not None:
            name, args = nxt
            return self._tool_call_response(name, args, state, t0, messages)

        return self._final(content=self._answer(state), state=state, t0=t0, messages=messages)

    def chat_stream(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield `{"type": "delta", "text": ...}` chunks, then a final
        `{"type": "done", "response": LLMResponse}`.

        Implemented as "compute, then chunk", which is honest for an offline
        reasoner and is the same contract a real streaming backend fulfils.
        The agent loop only relies on the events, so a slow real model streams
        progressively without changing any caller.
        """
        resp = self.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
        if not resp.tool_calls:
            text = resp.content
            step = 48
            for i in range(0, len(text), step):
                yield {"type": "delta", "text": text[i : i + step]}
        yield {"type": "done", "response": resp}

    # ---- planning ------------------------------------------------------

    def _next_action(self, st: _State) -> tuple[str, dict[str, Any]] | None:
        done = set(st.tool_calls_done)

        plan: tuple[str, ...]
        if "get_job" not in done:
            plan = ("get_job",)
        elif st.job_record is None:
            # The job lookup failed outright; nothing else can be fetched.
            return None
        elif st.job_record.get("status") == "NEVER_STARTED" or (st.job_record.get("duration_s") or 0) <= 90:
            # No runtime telemetry exists: the container never ran, or died
            # during startup. Metrics would be empty and asking for them wastes
            # a step, but the scheduler's own log IS the evidence.
            plan = PLAN_NO_METRICS + ("get_exit_code_meaning",)
        else:
            plan = PLAN_FULL + ("get_exit_code_meaning",)

        for step in plan:
            if step in done:
                continue
            if step == "get_job":
                return "get_job", {"job_id": st.job_id}
            if step == "search_logs":
                return "search_logs", {"job_id": st.job_id, "level": "ERROR", "limit": self.max_log_lines}
            if step == "get_metrics":
                return "get_metrics", {"job_id": st.job_id}
            if step == "search_runbooks":
                q = self._retrieval_query(st)
                if not q:
                    continue
                return "search_runbooks", {"query": q, "top_k": 4}
            if step == "get_exit_code_meaning":
                code = st.job_record.get("exit_code")
                if code is None:
                    continue
                return "get_exit_code_meaning", {"code": int(code)}
        return None

    def _retrieval_query(self, st: _State) -> str:
        """Build a retrieval query from observed signals.

        The realistic behaviour: search with the symptom text you actually saw,
        not with the user's question. Searching the question is the single most
        common mistake in a first RAG implementation, and it retrieves nothing
        useful because the question is generic.
        """
        bits: list[str] = []
        ev = self._log_evidence(st)
        if ev.first_error:
            bits.append(_clean_log_line(ev.first_error))
        if ev.is_cascade_shaped:
            bits.append("Watchdog caught collective operation timeout")
        for metric, values in st.metric_series.items():
            from ..signals import classify_series

            verdict = classify_series(values, metric)
            if verdict.shape != "flat":
                bits.append(f"{metric} {verdict.shape}")
        if st.job_record:
            if st.job_record.get("status") == "NEVER_STARTED":
                bits.append("job never started insufficient resources unschedulable")
            code = st.job_record.get("exit_code")
            if code in (137, 143) and not ev.has_error:
                bits.append("exit 137 no evidence SIGKILL unattributed")
        return " ".join(bits)[:400]

    # ---- final answer --------------------------------------------------

    def _answer(self, st: _State) -> str:
        ev = self._log_evidence(st)
        status = (st.job_record or {}).get("status")
        exit_code = (st.job_record or {}).get("exit_code")
        hyps = score_hypotheses(
            self.playbook,
            ev,
            metrics=st.metric_series,
            exit_code=exit_code,
            status=status,
        )
        verdict = decide(
            self.playbook,
            ev,
            hyps,
            status=status,
            exit_code=exit_code,
            telemetry_gap=self._telemetry_gap(st),
        )

        citations = [h["doc_id"] for h in st.runbook_hits[:3]]
        remediation = self._remediation(verdict, st)
        ruled_out = [
            {
                "cause": h.scenario_id,
                "why": (
                    "scored lower: "
                    + (h.rationale or "less evidence")
                    + ("; cascade-shaped logs but no unique signature" if h.penalty else "")
                ),
            }
            for h in hyps[1:3]
            if h.score > 0
        ]

        if verdict.is_refusal:
            summary = (
                f"Job {st.job_id} ({status}, exit {exit_code}) cannot be attributed to a single "
                f"root cause from the evidence available. {verdict.decided_by}: "
                + ("; ".join(verdict.notes) if verdict.notes else "insufficient discriminating evidence")
                + ". "
                + self._what_would_resolve(st)
            )
        else:
            summary = (
                f"Job {st.job_id} ({status}, exit {exit_code}) — "
                f"{verdict.root_cause}: {self.playbook.get(verdict.root_cause).name}. "
                f"Decided by {verdict.decided_by}. "
                + (verdict.notes[0] if verdict.notes else "")
            ).strip()

        payload = {
            "job_id": st.job_id,
            "root_cause": verdict.root_cause,
            "root_cause_label": (
                "insufficient evidence"
                if verdict.is_refusal
                else self.playbook.get(verdict.root_cause).name
            ),
            "category": ("unknown" if verdict.is_refusal else self.playbook.get(verdict.root_cause).category),
            "confidence": round(verdict.confidence, 3),
            "insufficient_evidence": verdict.is_refusal,
            "summary": summary,
            "evidence": self._evidence_lines(st, ev, verdict),
            "ruled_out": ruled_out,
            "hypotheses": [h.to_dict() for h in hyps[:4]],
            "remediation": remediation,
            "next_steps": self._next_steps(st, verdict),
            "citations": citations,
            "answer_schema": ANSWER_SCHEMA_HINT,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _evidence_lines(self, st: _State, ev: LogEvidence, verdict: Any) -> list[str]:
        out: list[str] = []
        if st.job_record:
            out.append(
                f"status={st.job_record.get('status')} exit={st.job_record.get('exit_code')} "
                f"duration={st.job_record.get('duration_s')}s cluster={st.job_record.get('cluster')} "
                f"queue={st.job_record.get('queue')}"
            )
        if ev.first_error:
            out.append(
                f"first non-cascade error (rank {ev.first_error_rank}, {ev.first_error_ts}): "
                f"{_clean_log_line(ev.first_error)[:220]}"
            )
        if ev.is_cascade_shaped:
            out.append(
                f"cascade signature: {len(ev.cascade_lines)} timeout lines across ranks "
                f"{ev.ranks_reporting_cascade}; treated as a symptom, not the cause"
            )
        if ev.matched:
            out.append("log signatures matched: " + ", ".join(sorted(ev.matched)))
        for name, values in list(st.metric_series.items())[:4]:
            from ..signals import classify_series

            v = classify_series(values, name)
            out.append(f"metric {name}: {v.shape} ({v.detail})")
        if ev.truncation_hints:
            out.append(f"log retention warning: {ev.truncation_hints}")
        if st.tool_errors:
            out.append(f"tool errors encountered: {st.tool_errors}")
        if verdict.hypotheses:
            top = verdict.hypotheses[0]
            out.append(
                f"hypothesis score: {top.scenario_id}={top.score:.2f}, margin over next={verdict.margin:.2f}"
            )
        return out

    def _remediation(self, verdict: Any, st: _State) -> list[str]:
        if verdict.is_refusal:
            return [
                "re-run with debug logging enabled so the causal error is retained",
                "enable metrics export for this job so the shape of the failure is visible",
                "check the node's kernel log for an OOM-kill line, and the scheduler for eviction events",
            ]
        steps = [self.playbook.get(verdict.root_cause).remediation]
        # Cite the retrieved runbook too, since that is the document a human
        # would actually open.
        for hit in st.runbook_hits[:1]:
            if hit.get("doc_id"):
                steps.append(f"see {hit['doc_id']} ({hit.get('title', '')})")
        return steps

    def _next_steps(self, st: _State, verdict: Any) -> list[str]:
        if verdict.is_refusal:
            return ["collect more evidence before acting", "re-open the incident once telemetry is available"]
        if st.job_record and st.job_record.get("status") == "NEVER_STARTED":
            return ["fix the scheduling or image problem, then resubmit"]
        return ["apply the remediation and resubmit", "if it recurs, group the incident by cluster and queue"]

    def _what_would_resolve(self, st: _State) -> str:
        missing: list[str] = []
        if not st.log_lines:
            missing.append("a retained log with error-level output")
        if not st.metric_series:
            missing.append("metric export enabled for the job")
        c = (st.job_record or {}).get("exit_code")
        if c in (137, 143):
            missing.append(f"a kernel OOM line, eviction event or preemption event to explain exit {c}")
        if not missing:
            missing = ["a distinctive error line, since the evidence available does not discriminate"]
        return "Would be resolved by: " + "; ".join(missing) + "."

    # ---- message history parsing --------------------------------------

    def _read_state(self, messages: Sequence[ChatMessage]) -> _State:
        st = _State()
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "user":
                st.question += ("\n" if st.question else "") + m.content
                found = JOB_ID_RE.search(m.content)
                if found and st.job_id is None:
                    st.job_id = found.group(0)
            elif m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    st.tool_calls_done.append(tc.name)
            elif m.role == "tool" and m.name:
                self._absorb_tool_result(st, m.name, m.content)

        # A job id may also appear in a tool result, e.g. when the user named a
        # team instead of a job and the agent had to resolve it.
        if st.job_id is None and st.job_record:
            st.job_id = st.job_record.get("job_id")
        return st

    def _absorb_tool_result(self, st: _State, tool: str, content: str) -> None:
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return
        if not payload.get("ok"):
            err = payload.get("error")
            if err:
                st.tool_errors.append(f"{tool}: {err}")
            return
        data = payload.get("data") or {}

        if tool == "get_job":
            st.job_record = data
            if st.job_id is None:
                st.job_id = data.get("job_id")
        elif tool == "search_logs":
            st.log_lines.extend(data.get("lines") or [])
            st.log_meta = {k: v for k, v in data.items() if k != "lines"}
        elif tool == "get_metrics":
            for name, series in (data.get("series") or {}).items():
                # Prefer the full series when present; the tool returns a
                # `sampled` subset for display, which is enough to classify.
                values = series.get("sampled") or []
                if values:
                    st.metric_series[name] = [float(v) for v in values]
                st.metric_meta[name] = {k: v for k, v in series.items() if k != "sampled"}
        elif tool == "search_runbooks":
            st.runbook_hits.extend(data.get("hits") or [])

    def _log_evidence(self, st: _State) -> LogEvidence:
        return extract_log_evidence(st.log_lines, self.playbook)

    def _telemetry_gap(self, st: _State) -> bool:
        """True when a job ran long enough to have metrics but has none.

        Distinguishing this from "the job was too short to produce metrics" is
        the difference between an honest refusal and a blind guess: the
        generator withholds metric series for the deliberately-undiagnosable
        cases, and this is how the reasoner notices.
        """
        if st.metric_series:
            return False
        dur = (st.job_record or {}).get("duration_s") or 0
        status = (st.job_record or {}).get("status")
        return status == "FAILED" and dur > 600 and bool(st.metric_meta is not None)

    # ---- plumbing ------------------------------------------------------

    def _tool_call_response(
        self, name: str, args: dict[str, Any], st: _State, t0: float, messages: Sequence[ChatMessage]
    ) -> LLMResponse:
        call = ToolCall(
            id=f"call_{len(st.tool_calls_done) + 1}_{name}",
            name=name,
            arguments=args,
            raw_arguments=json.dumps(args, ensure_ascii=False),
        )
        content = f"Calling {name} with {json.dumps(args, ensure_ascii=False)}"
        return self._final(content=content, state=st, t0=t0, messages=messages, tool_calls=[call],
                           finish=FinishReason.TOOL_CALLS)

    def _final(
        self,
        content: str,
        state: _State,
        t0: float,
        messages: Sequence[ChatMessage],
        tool_calls: list[ToolCall] | None = None,
        finish: FinishReason = FinishReason.STOP,
    ) -> LLMResponse:
        # Token accounting is injected locally, but reported for real: the
        # server's rate limiting and cost ledger consume these numbers, and
        # skipping them here would mean the offline mode teaches nothing about
        # the cost controls.
        tok_in = sum(approx_tokens(m.text_for_prompt()) for m in messages)
        tokens_in = tok_in
        tokens_out = approx_tokens(content)
        calls = tool_calls or []
        if self.latency_s > 0:
            time.sleep(self.latency_s)
        resp = LLMResponse(
            content=content,
            tool_calls=calls,
            finish_reason=finish if not calls else FinishReason.TOOL_CALLS,
            usage=Usage(tokens_in=tokens_in, tokens_out=tokens_out),
            model=self.model,
            latency_s=time.perf_counter() - t0,
        )
        return resp


def _clean_log_line(message: str) -> str:
    """剥掉生成器附加的装饰性前缀/后缀。

    A real model would not need this, but the mock must not benefit from
    wrapper text that a real deployment would not have. Keeping the extractor
    honest keeps the comparison between mock and model fair.
    """
    msg = message
    for prefix in ("Traceback (most recent call last): ", "RuntimeError: ", "ERROR: "):
        if msg.startswith(prefix):
            msg = msg[len(prefix) :]
    msg = re.sub(r"^\[rank \d+\]\s*", "", msg)
    msg = re.sub(r"\s*\(job=job-[0-9a-f-]+\)\s*$", "", msg)
    msg = re.sub(r"\s*at /[^\s]*train\.py:\d+\s*$", "", msg)
    msg = re.sub(r"\s*\[elapsed=[^\]]+\]\s*$", "", msg)
    return msg.strip()
