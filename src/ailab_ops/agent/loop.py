"""agent 循环本体：有界的工具调用循环。

循环是「调用模型 → 派发工具 → 回喂观测」的受控重复，配套四件事：步数预算、
单次调用与整体截止时间、把工具报错回喂给模型（而不是抛异常终止）、全链路 span。

循环本身是同步的。并发属于上一层——放在 serving 里才能被限界和度量；把两者
混在一起，只会得到一个「并发地错」的系统，而不是一个「串行地对」的系统。
"""

from __future__ import annotations

from ..config import Settings
from ..config import get_settings
from ..llm.base import ChatMessage
from ..llm.base import FinishReason
from ..llm.base import LLMClient
from ..llm.base import ToolCall
from ..obs import Tracer
from ..tools import ToolRegistry
from .parsing import parse_answer
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Iterator
from typing import Sequence
import re
import time

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
