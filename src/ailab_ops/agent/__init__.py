"""The agent loop: the thing that is actually 200 lines.

Everything else in this project exists so that this file can be small. The
loop is a bounded cycle of model call -> tool dispatch -> observation, with
four things that a tutorial implementation usually omits and that a production
one cannot:

* **A step budget.** An unbounded loop is an unbounded bill. The budget is
  enforced by counting, not by trusting the model to stop.
* **Per-call timeouts and a total deadline.** A model call can hang; so can a
  tool. Both need a ceiling, and the ceiling for the whole request has to be
  lower than the client's, or the client gives up first and the work is wasted.
* **Tool-error feedback.** A failed tool call becomes a message the model can
  act on, rather than an exception that ends the run. This is the mechanism
  that makes the agent self-correcting.
* **Full traceability.** Every step is a span with the tool, arguments, result
  size and duration; the trace is the artefact you debug with.

The loop is synchronous on purpose. Concurrency belongs one layer up, in the
server, where it can be bounded and measured -- mixing the two is how you get
a system that is concurrently wrong instead of serially correct.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from ..config import Settings, get_settings
from ..llm.base import ChatMessage, FinishReason, LLMClient, LLMResponse, ToolCall, approx_tokens
from ..obs import Tracer
from ..tools import ToolRegistry

DEFAULT_SYSTEM = """You are AILab Ops Copilot, a read-only diagnostic assistant for an
AI training and evaluation platform.

You help engineers work out why a job failed. You have tools to read job records, logs, metric
series, incident history and platform runbooks. You cannot change anything: you cannot restart
a job, edit a queue, raise a quota, or delete anything.

How to work:
- Fetch the job record first. Its status, exit code and duration decide what evidence can exist.
- Read the error log, preferring the FIRST meaningful error over the last line.
- When several ranks report the same message, treat it as a cascade until proven otherwise:
  diff by rank and find the unique, earliest error.
- Classify metric series rather than eyeballing them: cliff = crash, flat = hang, ramp =
  resource exhaustion, staircase = backoff.
- Retrieve the runbook for the signals you observed and check that its discriminator is
  actually present in the evidence.
- Answer with a root cause, the evidence for it, the rival you ruled out, and the remediation.
- If the evidence does not discriminate, answer "insufficient_evidence" and say what would
  resolve it. A confident wrong answer is worse than an honest gap.

Be concise and concrete. Cite the runbook ids you used."""

# Tools whose arguments may legitimately contain a job id, used for the
# rewrite guard below.
_JOB_ID_RE = re.compile(r"\bjob-[0-9a-f]{6,12}-\d{3,6}\b")


@dataclass
class AgentStep:
    index: int
    kind: str  # "llm" | "tool"
    duration_ms: float
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    error: str | None = None
    note: str = ""
    content_chars: int = 0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "kind": self.kind,
            "duration_ms": round(self.duration_ms, 1),
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "error": self.error,
            "note": self.note,
            "content_chars": self.content_chars,
        }


@dataclass
class AgentResult:
    question: str
    answer: str
    steps: list[AgentStep]
    usage_in: int = 0
    usage_out: int = 0
    n_llm_calls: int = 0
    n_tool_calls: int = 0
    elapsed_ms: float = 0.0
    stop_reason: str = "stop"
    error: str | None = None
    trace_id: str = ""
    parsed: dict[str, Any] | None = None

    @property
    def tokens_total(self) -> int:
        return self.usage_in + self.usage_out

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "parsed": self.parsed,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "trace_id": self.trace_id,
            "n_llm_calls": self.n_llm_calls,
            "n_tool_calls": self.n_tool_calls,
            "usage_in": self.usage_in,
            "usage_out": self.usage_out,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "steps": [s.to_dict() for s in self.steps],
        }


class AgentTimeout(RuntimeError):
    """Raised when the total deadline for a request is exceeded."""


class Agent:
    """Bounded tool-calling loop.

    Stateless with respect to conversation: it is constructed per request. That
    is a deliberate choice, not an oversight. A stateful agent object cannot be
    load-balanced without session affinity, and session affinity is the first
    thing that breaks when you scale -- so the multi-turn state lives in the
    message list the caller supplies, and any replica can serve any turn.
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        settings: Settings | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.settings = settings or get_settings()
        self.system_prompt = system_prompt or DEFAULT_SYSTEM

    def run(
        self,
        question: str,
        history: Sequence[ChatMessage] | None = None,
        max_steps: int | None = None,
        deadline_s: float | None = None,
        tracer: Tracer | None = None,
        on_step: Any = None,
    ) -> AgentResult:
        """Run one diagnosis to completion.

        `on_step`, if given, is called with each `AgentStep` as it finishes,
        which is how the streaming endpoint reports progress without the loop
        knowing anything about HTTP.
        """
        max_steps = max_steps or self.settings.max_steps
        deadline = time.perf_counter() + (deadline_s if deadline_s is not None else self.settings.llm_timeout_s * max_steps)
        tracer = tracer or Tracer()
        tracer.trace.question = question

        messages: list[ChatMessage] = [ChatMessage(role="system", content=self.system_prompt)]
        if history:
            messages.extend(history)
        messages.append(ChatMessage(role="user", content=question))

        result = AgentResult(question=question, answer="", steps=[], trace_id=tracer.trace.trace_id)
        t_start = time.perf_counter()
        step_index = 0

        specs = self.tools.specs()

        while step_index < max_steps:
            if time.perf_counter() > deadline:
                result.stop_reason = "deadline"
                result.error = f"total deadline exceeded after {step_index} steps"
                break

            step_index += 1
            with tracer.span("llm.call", step=step_index, model=self.llm.model) as sp:
                try:
                    resp = self.llm.chat(
                        messages,
                        tools=specs,
                        temperature=0.0,
                        max_tokens=self.settings.llm_max_tokens,
                    )
                except Exception as exc:
                    sp.finish(error=f"{type(exc).__name__}: {exc}")
                    result.stop_reason = "llm_error"
                    result.error = f"{type(exc).__name__}: {exc}"
                    result.steps.append(
                        AgentStep(index=step_index, kind="llm", duration_ms=sp.duration_ms, error=result.error)
                    )
                    if on_step:
                        on_step(result.steps[-1])
                    break
                sp.set(tokens_in=resp.usage.tokens_in, tokens_out=resp.usage.tokens_out,
                       finish=str(resp.finish_reason))
                if tracer.record_content:
                    tracer.record_content("assistant", resp.content)

            result.n_llm_calls += 1
            result.usage_in += resp.usage.tokens_in
            result.usage_out += resp.usage.tokens_out

            if not resp.tool_calls:
                result.answer = resp.content
                result.stop_reason = "stop" if resp.finish_reason != FinishReason.LENGTH else "length"
                st = AgentStep(
                    index=step_index, kind="llm", duration_ms=sp.duration_ms,
                    content_chars=len(resp.content), note="final answer",
                )
                result.steps.append(st)
                if on_step:
                    on_step(st)
                break

            # Record the assistant turn *including* its tool calls: dropping the
            # tool_calls here is the classic bug that makes the model repeat
            # itself, because from its point of view it never asked anything.
            messages.append(
                ChatMessage(role="assistant", content=resp.content, tool_calls=list(resp.tool_calls))
            )

            for call in resp.tool_calls:
                if time.perf_counter() > deadline:
                    result.stop_reason = "deadline"
                    result.error = "deadline exceeded while dispatching tools"
                    break
                fixed_args, note = self._repair_arguments(call, messages)
                with tracer.span("tool.call", tool=call.name, arguments=fixed_args) as tsp:
                    tr = self.tools.call(call.name, fixed_args, step=step_index)
                    tsp.set(ok=tr.ok, truncated=tr.truncated)
                    if not tr.ok:
                        tsp.finish(error=tr.error or "tool error")
                result.n_tool_calls += 1
                payload = tr.as_text()
                # Cap what goes back into the context. The tool already caps
                # its own payload; this is the second line of defence, because
                # a single oversized tool result can evict the entire useful
                # history from a modest context window.
                if len(payload) > 12_000:
                    payload = payload[:12_000] + '", "__truncated__": true}'
                messages.append(
                    ChatMessage(role="tool", content=payload, name=call.name, tool_call_id=call.id)
                )
                st = AgentStep(
                    index=step_index, kind="tool", duration_ms=tsp.duration_ms, tool=call.name,
                    arguments=fixed_args, ok=tr.ok, error=tr.error,
                    content_chars=len(payload), note=note,
                )
                result.steps.append(st)
                if on_step:
                    on_step(st)
        else:
            result.stop_reason = "max_steps"
            result.error = f"reached max_steps={max_steps} without a final answer"

        result.elapsed_ms = (time.perf_counter() - t_start) * 1000.0
        if result.answer:
            result.parsed = parse_answer(result.answer)
        tracer.trace.outcome = result.stop_reason
        if result.error:
            tracer.trace.error = result.error
        return result

    def run_stream(self, question: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Same loop, emitting events.

        Implemented by running the loop and yielding its step callbacks rather
        than by re-implementing the loop around a streaming client. The reason
        is that a streamed answer and a non-streamed one must be *identical* in
        content; keeping one implementation is how you guarantee that. The
        server adds a short delay between events so the behaviour is visible in
        a browser, which is a presentation choice, not a protocol one.
        """
        events: list[dict[str, Any]] = []
        hold: list[AgentStep] = []

        def _collect(step: AgentStep) -> None:
            hold.append(step)

        # The loop runs to completion, then the recorded steps are replayed as
        # a stream. This trades memory for determinism, and the trace is saved
        # either way.
        result = self.run(question, on_step=_collect, **kwargs)
        for st in result.steps:
            yield {"event": "step", "data": st.to_dict()}
        yield {"event": "answer", "data": {"answer": result.answer, "parsed": result.parsed}}
        yield {"event": "done", "data": result.to_dict()}

    # ---- guardrails ----------------------------------------------------

    def _repair_arguments(
        self, call: ToolCall, messages: Sequence[ChatMessage]
    ) -> tuple[dict[str, Any], str]:
        """Repair the two argument failures that actually happen in practice.

        1. **Wrong / missing job id.** The model hallucinates an id or omits it.
           Rewriting it from the most recent job id in the conversation is safe
           here because every tool is read-only: the worst outcome is that the
           model gets an honest "not found".
        2. **Unknown keys.** Some backends emit `{"__raw__": ...}` or extra
           fields. Stripping them is better than failing, since the alternative
           is an error the model cannot fix.
        """
        args = dict(call.arguments)
        notes: list[str] = []
        tool = self.tools.get(call.name)
        allowed = set(((tool.parameters.get("properties") if tool else {}) or {}).keys())

        unknown = [k for k in args if k not in allowed]
        for k in unknown:
            args.pop(k, None)
        if unknown:
            notes.append(f"dropped unknown arguments {unknown}")

        if tool and "job_id" in allowed:
            jid = args.get("job_id")
            # "Well-formed" is not the same as "correct": a hallucinated id is
            # perfectly well-formed, and a syntactic check would pass it
            # straight through. The only reliable test available here is whether
            # the id has actually been mentioned in the conversation. If it has
            # not, it is an invention, and the most recent real id is used
            # instead. Read-only tools make the substitution safe: the worst
            # outcome is an honest "not found".
            seen = self._mentioned_job_ids(messages)
            if not isinstance(jid, str) or jid not in seen:
                if seen:
                    recovered = seen[-1]
                    args["job_id"] = recovered
                    notes.append(
                        f"replaced unmentioned job_id {jid!r} with {recovered} from context"
                    )
        return args, "; ".join(notes)

    @staticmethod
    def _mentioned_job_ids(messages: Sequence[ChatMessage]) -> list[str]:
        """Every job id mentioned in an authoritative (user or tool) message, in
        order. Mutable state is never involved: this is a pure scan of the
        conversation, so any replica can reproduce it."""
        out: list[str] = []
        for m in messages:
            if m.role not in ("user", "tool"):
                continue
            out.extend(_JOB_ID_RE.findall(m.content or ""))
        return out


_PARSE_FAILED = object()


def parse_answer(answer: str) -> dict[str, Any] | None:
    """Extract the structured diagnosis from a model's final message.

    Tolerant by design: models wrap JSON in prose, in a fenced block, or emit a
    JSON object followed by commentary. Failing to parse is not a hard error --
    the caller still has the prose -- but a run that cannot be parsed is
    counted separately in the evaluation, because "the model was right but the
    output was unusable" is a real and fixable failure mode.

    Returned keys are normalised so a model that says `cause` and one that says
    `root_cause` are scored the same.
    """
    if not answer:
        return None
    text = answer.strip()

    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])

    for chunk in candidates:
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return _normalise(obj)

    # Last resort: a bare label, which models do emit when a schema is implied.
    m = re.search(r'"?(root_cause|cause|diagnosis)"?\s*[:=]\s*"?([a-z_0-9]+)"?', text)
    if m:
        return {"root_cause": m.group(2).lower(), "confidence": None, "summary": text[:400],
                "_parsed_from": "regex"}
    return None


def _normalise(obj: dict[str, Any]) -> dict[str, Any]:
    def pick(*keys: str) -> Any:
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        return None

    rc = pick("root_cause", "cause", "diagnosis", "label", "rootCause")
    if isinstance(rc, str):
        rc = rc.strip().lower().replace(" ", "_").replace("-", "_")
    conf = pick("confidence", "certainty", "score")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    if conf is not None and conf > 1.0:  # some models answer 0-100
        conf = conf / 100.0

    def as_list(v: Any) -> list[Any]:
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return [v]

    return {
        "root_cause": rc,
        "confidence": conf,
        "summary": pick("summary", "explanation", "analysis") or "",
        "evidence": as_list(pick("evidence", "evidence_lines", "signals")),
        "ruled_out": as_list(pick("ruled_out", "ruledOut", "alternatives", "differential")),
        "remediation": as_list(pick("remediation", "fix", "actions", "recommendation")),
        "citations": as_list(pick("citations", "sources", "references")),
        "job_id": pick("job_id", "jobId"),
        "_extra": {k: v for k, v in obj.items() if k not in {
            "root_cause", "cause", "diagnosis", "label", "rootCause", "confidence", "certainty",
            "score", "summary", "explanation", "analysis", "evidence", "evidence_lines", "signals",
            "ruled_out", "ruledOut", "alternatives", "differential", "remediation", "fix",
            "actions", "recommendation", "citations", "sources", "references", "job_id", "jobId",
        }},
    }
