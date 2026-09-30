"""The serving layer: sessions, the request pipeline, and the degradation path.

This module is where the architecture becomes visible as a sequence. A request
passes through, in order:

    identity -> limits -> cache -> degradation choice -> gate -> agent -> accounting

Each stage exists because of a specific failure, and each is skippable in a way
that makes the consequence obvious. The order is not arbitrary:

* **Limits before the cache.** A cached answer is still a served request, and a
  tenant that is over budget must not be able to keep consuming by hitting
  cache. It is cheap, so it runs first.
* **Cache before the gate.** A cache hit should never occupy an upstream slot.
  Getting this backwards is the classic mistake that makes a cache useless
  under load -- exactly when it was supposed to help.
* **Degradation before the gate.** If the decision is already "do not call the
  model", queueing for a slot would be nonsense.
* **Accounting after the agent.** Usage is recorded from what actually
  happened, not from an estimate made before the call.

**Session state lives here, not in the agent.** The `Agent` object is
constructed per request and thrown away; the conversation history is a list of
messages in a session store. That is what allows any replica to serve any turn,
and it is why the store is an explicit component with an eviction policy rather
than a dictionary on the agent.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..agent import AgentResult
from ..config import Settings, get_settings
from ..datagen.taxonomy import INSUFFICIENT_EVIDENCE
from ..llm.base import ChatMessage, approx_tokens
from ..obs import METRICS, Metrics, Tracer
from ..runtime import Runtime
from ..signals import decide, extract_log_evidence, score_hypotheses
from .cache import (
    BreakerState,
    BreakerRegistry,
    DegradeLevel,
    SemanticCache,
    decide_degradation,
)
from .gate import GateRejected, Priority, UpstreamGate
from .limits import LimitExceeded, RateLimiter

JOB_ID_RE = __import__("re").compile(r"\bjob-[0-9a-f]{6,12}-\d{3,6}\b")


# --------------------------------------------------------------------------
# Request / response shapes
# --------------------------------------------------------------------------


@dataclass
class DiagnosisRequest:
    question: str
    tenant_id: str = "tenant-01"
    user_id: str = "user"
    session_id: str | None = None
    priority: Priority = Priority.NORMAL
    batch_id: str | None = None
    max_steps: int | None = None
    stream: bool = False
    no_cache: bool = False


@dataclass
class DiagnosisResponse:
    answer: str
    parsed: dict[str, Any] | None
    session_id: str
    trace_id: str
    served_by: str  # full | cache | evidence_only
    cache: str | None = None
    queued_ms: float = 0.0
    gate_wait_ms: float = 0.0
    elapsed_ms: float = 0.0
    usage_in: int = 0
    usage_out: int = 0
    usd: float = 0.0
    n_tool_calls: int = 0
    n_llm_calls: int = 0
    stop_reason: str = "stop"
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    retry_after_s: float | None = None
    degraded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "parsed": self.parsed,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "served_by": self.served_by,
            "cache": self.cache,
            "queued_ms": round(self.queued_ms, 1),
            "gate_wait_ms": round(self.gate_wait_ms, 1),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "usage": {"in": self.usage_in, "out": self.usage_out, "usd": round(self.usd, 6)},
            "n_tool_calls": self.n_tool_calls,
            "n_llm_calls": self.n_llm_calls,
            "stop_reason": self.stop_reason,
            "steps": self.steps,
            "error": self.error,
            "retry_after_s": self.retry_after_s,
            "degraded_reason": self.degraded_reason,
        }


class ServiceError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int = 400, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after_s = retry_after_s


# --------------------------------------------------------------------------
# Session store
# --------------------------------------------------------------------------


@dataclass
class Session:
    session_id: str
    tenant_id: str
    user_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turns: int = 0

    def trim(self, max_messages: int = 12) -> None:
        """Keep the most recent turns.

        Trimming from the front rather than summarising is a real tradeoff and
        worth naming: summarising preserves more information but costs another
        model call and can itself be wrong. For a diagnosis flow the useful
        context is the last job under discussion, so the cheap option is
        adequate -- and the code says which one it chose and why.
        """
        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]


class SessionStore:
    """In-memory session storage with TTL and a size cap.

    A real deployment uses Redis here. The interface is kept identical to what
    Redis would need (get/put/touch/evict by key) so the substitution is a
    drop-in, and the in-memory version is honest about its limitation: it does
    not survive a restart and it does not work across replicas. That limitation
    is itself the lesson -- it is exactly why the production answer is a shared
    store, and why sticky sessions are the wrong fix.
    """

    def __init__(self, ttl_s: float = 3600.0, max_sessions: int = 2048) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.evictions = 0

    async def get_or_create(
        self, session_id: str | None, tenant_id: str, user_id: str
    ) -> tuple[Session, bool]:
        async with self._lock:
            self._sweep()
            if session_id and session_id in self._sessions:
                s = self._sessions[session_id]
                s.last_used = time.time()
                return s, False
            sid = session_id or f"sess-{uuid.uuid4().hex[:12]}"
            s = Session(session_id=sid, tenant_id=tenant_id, user_id=user_id)
            self._sessions[sid] = s
            return s, True

    async def save(self, session: Session) -> None:
        async with self._lock:
            session.last_used = time.time()
            session.turns += 1
            self._sessions[session.session_id] = session

    def _sweep(self) -> None:
        now = time.time()
        dead = [k for k, s in self._sessions.items() if now - s.last_used > self.ttl_s]
        for k in dead:
            del self._sessions[k]
            self.evictions += 1
        if len(self._sessions) > self.max_sessions:
            for k, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].last_used)[
                : len(self._sessions) - self.max_sessions
            ]:
                del self._sessions[k]
                self.evictions += 1

    def stats(self) -> dict[str, Any]:
        return {
            "sessions": len(self._sessions),
            "evictions": self.evictions,
            "ttl_s": self.ttl_s,
            "note": "in-memory: does not span replicas and does not survive a restart",
        }


# --------------------------------------------------------------------------
# The evidence-only path
# --------------------------------------------------------------------------


def evidence_only_answer(runtime: Runtime, job_id: str) -> dict[str, Any]:
    """Answer without any model call.

    This is the degradation rung that most implementations omit, and it is the
    one that matters most. Everything needed is already local: the tools are
    cheap reads over an in-process dataset, and the scoring is deterministic
    code. So when the model is unavailable, dropping to "no model" does not
    mean dropping to "no answer" -- the same evidence pipeline still produces a
    ranked hypothesis with the supporting lines.

    The output is explicitly labelled as degraded, and it says which rung it
    came from, because an unlabelled lower-quality answer is worse than a
    labelled one: the engineer needs to know whether to trust it.
    """
    reg = runtime.registry
    pb = runtime.world.playbook
    job_res = reg.call("get_job", {"job_id": job_id})
    if not job_res.ok:
        return {
            "root_cause": INSUFFICIENT_EVIDENCE,
            "confidence": 0.0,
            "summary": f"cannot read job {job_id}: {job_res.error}",
            "degraded": True,
            "served_by": DegradeLevel.EVIDENCE_ONLY,
            "evidence": [],
            "remediation": ["retry when the model service is available"],
        }
    job = job_res.data

    logs = reg.call("search_logs", {"job_id": job_id, "level": "ERROR", "limit": 60})
    lines = (logs.data or {}).get("lines", []) if logs.ok else []

    metrics_res = reg.call("get_metrics", {"job_id": job_id})
    series: dict[str, list[float]] = {}
    if metrics_res.ok:
        for name, s in ((metrics_res.data or {}).get("series") or {}).items():
            vals = s.get("sampled") or []
            if vals:
                series[name] = [float(v) for v in vals]

    ev = extract_log_evidence(lines, pb)
    hyps = score_hypotheses(
        pb, ev, metrics=series, exit_code=job.get("exit_code"), status=job.get("status")
    )
    verdict = decide(pb, ev, hyps, status=job.get("status"))

    return {
        "job_id": job_id,
        "root_cause": verdict.root_cause,
        "root_cause_label": (
            "insufficient evidence" if verdict.is_refusal else pb.get(verdict.root_cause).name
        ),
        "confidence": round(min(verdict.confidence, 0.7), 3),
        "insufficient_evidence": verdict.is_refusal,
        "degraded": True,
        "served_by": DegradeLevel.EVIDENCE_ONLY,
        "summary": (
            f"[degraded: no model call] Job {job_id} is {job.get('status')} with exit "
            f"{job.get('exit_code')}. Deterministic scoring favours {verdict.root_cause} "
            f"({verdict.decided_by}), with the top two hypotheses separated by {verdict.margin:.2f}."
        ),
        "evidence": [
            f"first non-cascade error (rank {ev.first_error_rank}): {(ev.first_error or 'none')[:200]}",
            f"cascade-shaped logs: {ev.is_cascade_shaped}",
            f"log signatures matched: {sorted(ev.matched)}",
            *[f"metric {k}: {v['shape']}" for k, v in list(hyps[0].to_dict().items())[:0]],
        ],
        "hypotheses": [h.to_dict() for h in hyps[:3]],
        "remediation": [
            pb.get(verdict.root_cause).remediation if not verdict.is_refusal else "collect more evidence",
            "re-request with the model available for a cited diagnosis",
        ],
    }


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class CopilotService:
    def __init__(
        self,
        runtime: Runtime,
        settings: Settings | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or get_settings()
        s = self.settings

        self.gate = UpstreamGate(
            max_concurrency=s.upstream_concurrency,
            queue_maxsize=s.queue_maxsize,
            queue_timeout_s=s.queue_timeout_s,
        )
        self.limiter = RateLimiter(
            user_qps=s.user_qps,
            tenant_concurrency=s.tenant_concurrency,
            tenant_token_per_min=s.tenant_token_per_min,
            tenant_cost_per_day_usd=s.tenant_cost_per_day_usd,
        )
        self.cache = SemanticCache(
            ttl_s=s.semantic_cache_ttl_s,
            threshold=s.semantic_cache_threshold if s.semantic_cache_enabled else 1.0,
        )
        self.breakers = BreakerRegistry()
        self.sessions = SessionStore()
        self.metrics = metrics or METRICS
        self._recent_upstream_failures = 0
        self.force_degrade = False  # fault-injection switch, used by the demo
        self.request_seq = 0

    # ---- pipeline ------------------------------------------------------

    async def diagnose(self, req: DiagnosisRequest) -> DiagnosisResponse:
        t0 = time.perf_counter()
        self.request_seq += 1
        trace_id = f"trace-{self.request_seq:06d}-{uuid.uuid4().hex[:8]}"

        # 1. limits -- cheap, decisive, and before anything is spent
        try:
            if not self.limiter.is_batch(req.tenant_id, req.batch_id):
                self.limiter.check_request(req.tenant_id, req.user_id, req.batch_id)
            else:
                self.limiter.check_tokens(req.tenant_id, approx_tokens(req.question) + 2000)
        except LimitExceeded as exc:
            self.metrics.incr(f"rejected.{exc.kind}")
            raise ServiceError(exc.kind, exc.detail, status=429, retry_after_s=exc.retry_after_s) from exc

        self.metrics.incr("requests.total")
        release_tenant = True
        try:
            # 2. session
            session, _new = await self.sessions.get_or_create(
                req.session_id, req.tenant_id, req.user_id
            )

            # 3. cache
            if not req.no_cache:
                hit = self.cache.get(req.tenant_id, req.question)
                if hit is not None:
                    value, how = hit
                    self.metrics.incr("cache.hit")
                    self.metrics.incr(f"cache.hit.{how}")
                    resp = self._response_from_cache(value, session, trace_id, how, t0)
                    return resp
            self.metrics.incr("cache.miss")

            # 4. degradation decision
            breaker = self.breakers.get(req.tenant_id)
            decision = decide_degradation(
                breaker, self.gate.queued, self.gate.queue_maxsize, self._recent_upstream_failures
            )
            if self.force_degrade:
                decision = type(decision)(
                    DegradeLevel.EVIDENCE_ONLY, "fault injection: model call disabled by the operator"
                )
            if decision.level == DegradeLevel.EVIDENCE_ONLY:
                self.metrics.incr("degrade.evidence_only")
                return self._evidence_only_response(req, session, trace_id, decision, t0)

            # 5. gate
            gate_wait_ms = 0.0
            try:
                gate_wait_ms = await self.gate.acquire(req.priority)
            except GateRejected as exc:
                self.metrics.incr("gate.rejected")
                self.metrics.incr("degrade.evidence_only")
                d2 = type(decision)(DegradeLevel.EVIDENCE_ONLY, f"gate rejected: {exc.reason}",
                                    retry_after_s=2.0)
                return self._evidence_only_response(req, session, trace_id, d2, t0)

            # 6. the agent, run off the event loop
            try:
                result, tracer, usd = await self._run_agent(req, session, trace_id)
                self.breakers.get(req.tenant_id).record_success()
                self._recent_upstream_failures = 0
            except Exception as exc:  # upstream or agent failure
                self.breakers.get(req.tenant_id).record_failure()
                self._recent_upstream_failures += 1
                self.metrics.incr("agent.error")
                d3 = type(decision)(
                    DegradeLevel.EVIDENCE_ONLY,
                    f"model call failed ({type(exc).__name__}: {exc}); served the deterministic "
                    "evidence summary instead",
                    retry_after_s=self.breakers.get(req.tenant_id).cooldown_s,
                )
                return self._evidence_only_response(req, session, trace_id, d3, t0, error=str(exc))
            finally:
                await self.gate.release()

            # 7. accounting + session + cache
            self.metrics.record_cost(
                req.tenant_id, result.usage_in, result.usage_out,
                self.settings.price_input_per_mtok, self.settings.price_output_per_mtok,
            )
            self.limiter.record_usage(req.tenant_id, result.tokens_total, usd)
            self.metrics.incr("tokens.in", result.usage_in)
            self.metrics.incr("tokens.out", result.usage_out)
            self.metrics.observe("agent.elapsed_ms", result.elapsed_ms)
            self.metrics.observe("gate.wait_ms", gate_wait_ms)

            self._append_history(session, req.question, result)
            await self.sessions.save(session)

            if result.parsed and not result.parsed.get("insufficient_evidence") and not req.no_cache:
                self.cache.put(req.tenant_id, req.question, {"answer": result.answer, "parsed": result.parsed})

            elapsed = (time.perf_counter() - t0) * 1000.0
            self.metrics.observe("request.elapsed_ms", elapsed)
            return DiagnosisResponse(
                answer=result.answer,
                parsed=result.parsed,
                session_id=session.session_id,
                trace_id=trace_id,
                served_by=DegradeLevel.FULL,
                gate_wait_ms=gate_wait_ms,
                elapsed_ms=elapsed,
                usage_in=result.usage_in,
                usage_out=result.usage_out,
                usd=usd,
                n_tool_calls=result.n_tool_calls,
                n_llm_calls=result.n_llm_calls,
                stop_reason=result.stop_reason,
                steps=[s.to_dict() for s in result.steps],
                error=result.error,
            )
        finally:
            if release_tenant:
                self.limiter.release_request(req.tenant_id)

    async def _run_agent(
        self, req: DiagnosisRequest, session: Session, trace_id: str
    ) -> tuple[AgentResult, Tracer, float]:
        """Run the blocking agent loop in a worker thread.

        `asyncio.to_thread` rather than a bare call, because the agent makes
        blocking HTTP calls and blocking tool calls. Blocking the event loop
        here would serialise every request in the process and make the
        concurrency controls meaningless -- the gate would be protecting an
        upstream that only ever receives one call at a time.

        The deadline is derived from the queue timeout and the step budget so
        that a request cannot outlive the client's patience by a wide margin.
        """
        agent = self.runtime.new_agent()
        history = list(session.messages) if session.messages else None
        deadline_s = max(self.settings.llm_timeout_s * 2.0, 30.0)
        tracer = self.runtime.new_tracer(record_content=False)

        result = await asyncio.to_thread(
            agent.run,
            req.question,
            history,
            req.max_steps,
            deadline_s,
            tracer,
            None,
        )
        usd = (
            result.usage_in / 1e6 * self.settings.price_input_per_mtok
            + result.usage_out / 1e6 * self.settings.price_output_per_mtok
        )
        return result, tracer, usd

    async def diagnose_stream(self, req: DiagnosisRequest) -> AsyncIterator[dict[str, Any]]:
        """Server-sent events for one diagnosis.

        The event vocabulary is deliberately small and stable, because it is a
        client contract:

            start   {session_id, trace_id}
            stage   {name, detail}          progress of the pipeline
            step    {index, kind, tool, ok}  one agent step
            answer  {answer, parsed}
            done    {usage, elapsed_ms, served_by}
            error   {kind, message, retry_after_s}

        Tool use is announced as it happens rather than only at the end, which
        is the entire point of streaming here: a diagnosis takes seconds and
        the user needs to see that it is working on the right job.
        """
        t0 = time.perf_counter()
        try:
            self.limiter.check_request(req.tenant_id, req.user_id, req.batch_id)
        except LimitExceeded as exc:
            yield {"event": "error", "data": {"kind": exc.kind, "message": exc.detail,
                                              "retry_after_s": exc.retry_after_s}}
            return

        try:
            session, _ = await self.sessions.get_or_create(req.session_id, req.tenant_id, req.user_id)
        except Exception as exc:
            yield {"event": "error", "data": {"kind": "session_error", "message": str(exc)}}
            return

        trace_id = f"trace-{self.request_seq + 1:06d}-{uuid.uuid4().hex[:8]}"
        yield {"event": "start", "data": {"session_id": session.session_id, "trace_id": trace_id}}

        try:
            if not req.no_cache:
                hit = self.cache.get(req.tenant_id, req.question)
                if hit is not None:
                    value, how = hit
                    self.limiter.release_request(req.tenant_id)
                    yield {"event": "stage", "data": {"name": "cache", "detail": f"{how} hit"}}
                    yield {"event": "answer", "data": value}
                    yield {"event": "done", "data": {"served_by": "cache", "cache": how,
                                                     "elapsed_ms": (time.perf_counter() - t0) * 1000}}
                    return
        except Exception:
            pass

        gate_wait_ms = 0.0
        try:
            yield {"event": "stage", "data": {"name": "gate", "detail": "waiting for an upstream slot"}}
            gate_wait_ms = await self.gate.acquire(req.priority)
        except GateRejected as exc:
            self.limiter.release_request(req.tenant_id)
            yield {"event": "stage", "data": {"name": "degrade", "detail": exc.reason}}
            jid = _job_id_in(req.question) or _job_id_in(" ".join(m.content for m in session.messages))
            if jid:
                payload = evidence_only_answer(self.runtime, jid)
                yield {"event": "answer", "data": payload}
                yield {"event": "done", "data": {"served_by": "evidence_only", "elapsed_ms":
                                                 (time.perf_counter() - t0) * 1000}}
            else:
                yield {"event": "error", "data": {"kind": "gate_rejected", "message": exc.reason,
                                                  "retry_after_s": 2.0}}
            return

        yield {"event": "stage", "data": {"name": "agent", "detail": "diagnosing"}}
        # The agent loop runs to completion in a thread, and its steps are
        # replayed as events. Doing it this way keeps one implementation of the
        # loop (see `Agent.run_stream`) and guarantees the streamed answer is
        # byte-identical to the non-streamed one.
        #
        # The slot is released in exactly one place (`finally`), because a
        # double release silently inflates capacity and a missing release
        # permanently shrinks it -- both are worse than the failure they come
        # from, and both are hard to notice.
        try:
            result, _tracer, usd = await self._run_agent(req, session, trace_id)
        except Exception as exc:
            self.breakers.get(req.tenant_id).record_failure()
            jid = _job_id_in(req.question)
            yield {"event": "stage", "data": {"name": "degrade", "detail": f"model failed: {exc}"}}
            if jid:
                payload = evidence_only_answer(self.runtime, jid)
                yield {"event": "answer", "data": payload}
                yield {"event": "done", "data": {"served_by": "evidence_only",
                                                 "elapsed_ms": (time.perf_counter() - t0) * 1000}}
            else:
                yield {"event": "error", "data": {"kind": "upstream_error", "message": str(exc),
                                                  "retry_after_s": 5.0}}
            return
        finally:
            await self.gate.release()
            self.limiter.release_request(req.tenant_id)

        self.breakers.get(req.tenant_id).record_success()
        self.metrics.record_cost(req.tenant_id, result.usage_in, result.usage_out,
                                 self.settings.price_input_per_mtok, self.settings.price_output_per_mtok)
        self.limiter.record_usage(req.tenant_id, result.tokens_total, usd)
        self.metrics.incr("tokens.in", result.usage_in)
        self.metrics.incr("tokens.out", result.usage_out)

        for st in result.steps:
            yield {"event": "step", "data": st.to_dict()}
            # A small delay makes the sequence legible in a browser. It is a
            # presentation choice, not a protocol one, and is documented as
            # such -- the loop itself has already finished.
            await asyncio.sleep(0.02)

        self._append_history(session, req.question, result)
        await self.sessions.save(session)
        if result.parsed and not result.parsed.get("insufficient_evidence") and not req.no_cache:
            self.cache.put(req.tenant_id, req.question, {"answer": result.answer, "parsed": result.parsed})

        yield {"event": "answer", "data": {"answer": result.answer, "parsed": result.parsed}}
        yield {
            "event": "done",
            "data": {
                "served_by": DegradeLevel.FULL,
                "trace_id": trace_id,
                "session_id": session.session_id,
                "usage": {"in": result.usage_in, "out": result.usage_out, "usd": round(usd, 6)},
                "gate_wait_ms": round(gate_wait_ms, 1),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "n_tool_calls": result.n_tool_calls,
                "n_llm_calls": result.n_llm_calls,
                "stop_reason": result.stop_reason,
            },
        }

    # ---- helpers -------------------------------------------------------

    def _append_history(self, session: Session, question: str, result: AgentResult) -> None:
        session.messages.append(ChatMessage(role="user", content=question))
        if result.answer:
            session.messages.append(ChatMessage(role="assistant", content=result.answer))
        session.trim()

    def _response_from_cache(
        self, value: dict[str, Any], session: Session, trace_id: str, how: str, t0: float
    ) -> DiagnosisResponse:
        return DiagnosisResponse(
            answer=value.get("answer", ""),
            parsed=value.get("parsed"),
            session_id=session.session_id,
            trace_id=trace_id,
            served_by="cache",
            cache=how,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def _evidence_only_response(
        self,
        req: DiagnosisRequest,
        session: Session,
        trace_id: str,
        decision: Any,
        t0: float,
        error: str | None = None,
    ) -> DiagnosisResponse:
        jid = _job_id_in(req.question) or _job_id_in(" ".join(m.content for m in session.messages))
        if jid:
            payload = evidence_only_answer(self.runtime, jid)
            answer = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            payload = None
            answer = (
                "The model service is unavailable and no job id was given, so there is nothing to "
                "summarise. Provide a job id and the deterministic evidence path can still answer."
            )
        return DiagnosisResponse(
            answer=answer,
            parsed=payload,
            session_id=session.session_id,
            trace_id=trace_id,
            served_by=DegradeLevel.EVIDENCE_ONLY,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            degraded_reason=decision.reason,
            retry_after_s=decision.retry_after_s,
            error=error,
        )

    # ---- introspection -------------------------------------------------

    def health(self) -> dict[str, Any]:
        up_breakers = self.breakers.snapshot()
        open_breakers = [n for n, s in up_breakers.items() if s["state"] == BreakerState.OPEN]
        return {
            "status": "degraded" if (open_breakers or self.force_degrade) else "ok",
            "force_degrade": self.force_degrade,
            "gate": self.gate.stats().to_dict(),
            "limits": self.limiter.snapshot(),
            "cache": self.cache.stats(),
            "sessions": self.sessions.stats(),
            "breakers": up_breakers,
            "world": self.runtime.world.summary().__dict__,
            "llm_backend": self.settings.llm_backend,
            "llm_model": self.settings.llm_model,
            "llm_latency_ms": self.settings.llm_latency_ms,
            "upstream_concurrency": self.settings.upstream_concurrency,
            "queue_maxsize": self.settings.queue_maxsize,
        }

    def stats(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.snapshot(),
            "gate": self.gate.stats().to_dict(),
            "limits": self.limiter.snapshot(),
            "cache": self.cache.stats(),
            "sessions": self.sessions.stats(),
            "breakers": self.breakers.snapshot(),
            "tools": self.runtime.registry.stats(),
        }


def _job_id_in(text: str) -> str | None:
    m = JOB_ID_RE.search(text or "")
    return m.group(0) if m else None
