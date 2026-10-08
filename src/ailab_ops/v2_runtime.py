"""V2 investigation ownership and guarded model calls, shared by CLI and API.

State, traces and approvals are process local. Tenant identity must be verified
by a deployment gateway; the example API's identity fields are simulation only.
Semantic answer caching is deliberately disabled: citations belong to a session.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

from .approvals import SimulatedActionHandler
from .cases.loader import load_case
from .config import Settings
from .investigation import Budget, InvestigationOrchestrator, InvestigationPhase
from .investigation.orchestrator import InvestigationInterrupted
from .llm.base import approx_tokens
from .models import ModelBackendError, build_model_gateway
from .observability import TraceRecorder
from .serving.cache import BreakerRegistry
from .serving.gate import GateRejected, UpstreamGate
from .serving.limits import LimitExceeded, RateLimiter
from .tools.cases import build_case_registry


class GuardedGateway:
    """Synchronous model boundary used only after async runtime admission."""

    def __init__(self, runtime, cancelled, deadline_at):
        self.runtime, self.cancelled, self.deadline_at = runtime, cancelled, deadline_at
        self.mode, self.model = runtime.gateway.mode, runtime.gateway.model
        self.error = None
        self.response = None

    def complete(self, messages, tools=None, *, max_tokens=1024):
        if self.cancelled.is_set():
            raise InvestigationInterrupted("cancelled")
        if datetime.now(timezone.utc) >= self.deadline_at:
            raise InvestigationInterrupted("budget_exhausted:deadline")
        breaker = self.runtime.breakers.get("model")
        try:
            if not breaker.allow():
                raise ModelBackendError("Model circuit is open", kind="circuit_open", retryable=True,
                                        retry_after_s=breaker.cooldown_s)
            try:
                self.response = self.runtime.gateway.complete(messages, tools, max_tokens=max_tokens)
            except ModelBackendError as exc:
                if exc.retryable:
                    breaker.record_failure()
                else:
                    breaker.record_success()
                raise
            except Exception:
                breaker.record_failure()
                raise ModelBackendError("Unexpected model backend failure", kind="upstream", retryable=True) from None
            breaker.record_success()
            return self.response
        except ModelBackendError as exc:
            self.error = exc
            raise


@dataclass
class InvestigationRecord:
    tenant_id: str
    orchestrator: InvestigationOrchestrator
    recorder: TraceRecorder
    trace_id: str
    error: ModelBackendError | None = None


class InvestigationRuntime:
    def __init__(self, settings: Settings, *, gateway=None):
        self.settings = settings
        self.gateway = gateway if gateway is not None else build_model_gateway(settings)
        self.model_mode = self.gateway.mode
        self.gate = UpstreamGate(settings.upstream_concurrency, settings.queue_maxsize, settings.queue_timeout_s)
        self.limiter = RateLimiter(user_qps=settings.user_qps, tenant_concurrency=settings.tenant_concurrency,
            tenant_token_per_min=settings.tenant_token_per_min, tenant_cost_per_day_usd=settings.tenant_cost_per_day_usd)
        self.breakers = BreakerRegistry()
        # Only a gate-admitted step enters this pool. There is no whole-run
        # submission or hidden default-executor queue before global admission.
        self._model_workers = ThreadPoolExecutor(max_workers=settings.upstream_concurrency,
                                                thread_name_prefix="v2-model")
        self.records: dict[str, InvestigationRecord] = {}
        self.approval_owners: dict[str, str] = {}

    async def _advance(self, record, state, gateway, cancelled):
        orchestrator, tenant_id = record.orchestrator, record.tenant_id
        acquired = False
        try:
            exhausted = state.budget.exhausted(datetime.now(timezone.utc))
            if exhausted:
                return orchestrator.stop(state, "budget_exhausted:" + ",".join(exhausted))
            session = orchestrator.sessions[state.session_id]
            messages = session.messages
            max_tokens = min(4096, state.budget.max_tokens - state.budget.tokens_used)
            estimate = sum(approx_tokens(message.text_for_prompt()) for message in messages) + max_tokens
            self.limiter.check_tokens(tenant_id, estimate)
            self.limiter.check_cost(tenant_id)
            remaining = (state.budget.deadline_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return orchestrator.stop(state, "budget_exhausted:deadline")
            await self.gate.acquire(timeout_s=min(self.settings.queue_timeout_s, remaining))
            acquired = True
            # Every admission decision is rechecked after waiting, including
            # costs settled by the prior holder before it transfers this slot.
            if datetime.now(timezone.utc) >= state.budget.deadline_at:
                return orchestrator.stop(state, "budget_exhausted:deadline")
            self.limiter.check_tokens(tenant_id, estimate)
            self.limiter.check_cost(tenant_id)
            gateway.response = None
            worker = asyncio.get_running_loop().run_in_executor(self._model_workers, orchestrator.advance, state)
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled.set()
                # Only the current step may finish. The orchestrator checks
                # this Event after accounting and before each next tool.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                worker.result()
                raise
            finally:
                # Settle usage while still holding admission, even on cancel.
                if gateway.response is not None:
                    usage = gateway.response.usage
                    usd = 0.0 if self.model_mode == "replay" else (
                        usage.tokens_in * self.settings.price_input_per_mtok + usage.tokens_out * self.settings.price_output_per_mtok) / 1e6
                    self.limiter.record_usage(tenant_id, usage.total, usd)
                    gateway.response = None
                record.error = None if cancelled.is_set() else gateway.error
        except GateRejected as exc:
            if datetime.now(timezone.utc) >= state.budget.deadline_at:
                return orchestrator.stop(state, "budget_exhausted:deadline")
            kind = "queue_full" if exc.reason.startswith("queue full") else "queue_timeout"
            record.error = ModelBackendError("Model admission rejected", kind=kind, retryable=True, retry_after_s=1)
            return orchestrator.stop(state, "model_backend:" + kind)
        except LimitExceeded as exc:
            record.error = ModelBackendError("Tenant usage limit exceeded", kind=exc.kind,
                retryable=exc.retry_after_s is not None, retry_after_s=exc.retry_after_s)
            return orchestrator.stop(state, "model_backend:" + exc.kind)
        finally:
            if acquired:
                await self.gate.release()

    async def investigate(self, *, case_id, question=None, tenant_id="tenant-01", user_id="analyst",
                          max_steps=None, max_tokens=32768, deadline_s=120):
        world = load_case(case_id)
        self.limiter.check_request(tenant_id, user_id)
        try:
            cancelled = Event()
            budget = Budget(self.settings.max_steps if max_steps is None else max_steps, max_tokens,
                            datetime.now(timezone.utc) + timedelta(seconds=deadline_s))
            gateway = GuardedGateway(self, cancelled, budget.deadline_at)
            recorder = TraceRecorder(secrets=[self.settings.llm_api_key] if self.settings.llm_api_key else [])
            orchestrator = InvestigationOrchestrator(gateway, build_case_registry(world), event_sink=recorder.record,
                                                     cancelled=cancelled.is_set)
            for action in world.actions:
                orchestrator.approval_service.register_simulated(action["action_id"], SimulatedActionHandler({"ok": True}))
            state = orchestrator.start(question if question is not None else f"Diagnose {case_id}.", case_id=case_id, budget=budget)
            record = InvestigationRecord(tenant_id, orchestrator, recorder, "trace-" + uuid4().hex)
            self.records[state.session_id] = record

            try:
                while state.phase not in {InvestigationPhase.COMPLETED, InvestigationPhase.STOPPED, InvestigationPhase.AWAITING_APPROVAL}:
                    state = await self._advance(record, state, gateway, cancelled)
            except asyncio.CancelledError:
                cancelled.set()
                if state.session_id in orchestrator.sessions:
                    orchestrator.stop(state, "cancelled")
                raise
            return self.get_investigation(state.session_id, tenant_id)
        finally:
            self.limiter.release_request(tenant_id)

    def record(self, session_id, tenant_id="tenant-01"):
        record = self.records.get(session_id)
        if record is None or record.tenant_id != tenant_id:
            raise KeyError("Unknown investigation")
        return record

    def get_investigation(self, session_id, tenant_id="tenant-01"):
        record = self.record(session_id, tenant_id)
        orchestrator = record.orchestrator
        orchestrator.approval_service.expire()
        state = orchestrator.states[session_id]
        result = state.to_dict()
        result.update(trace_id=record.trace_id,
            evidence=[orchestrator.get_evidence(session_id, eid).to_dict() for eid in state.evidence_ids],
            approval_state=[request.to_dict() for request in orchestrator.approval_service.store.requests],
            error=None, simulated_actions=True)
        if record.error:
            error = record.error
            result["error"] = {"kind": error.kind, "retryable": error.retryable,
                               "retry_after_s": error.retry_after_s}
        return result

    def request_action(self, session_id, proposal, tenant_id="tenant-01"):
        request = self.record(session_id, tenant_id).orchestrator.request_action(session_id, proposal)
        self.approval_owners[request.request_id] = session_id
        return request.to_dict()

    def approval_service(self, approval_id, tenant_id="tenant-01"):
        session_id = self.approval_owners[approval_id]
        return self.record(session_id, tenant_id).orchestrator.approval_service

    def health(self):
        breakers = self.breakers.snapshot()
        degraded = any(item["state"] == "open" for item in breakers.values())
        return {"status": "degraded" if degraded else "ok", "model_mode": self.model_mode, "model": self.gateway.model,
                "gate": self.gate.stats().to_dict(), "breakers": breakers,
                "cache": "disabled: session-scoped evidence", "storage": "process-local",
                "identity": "simulation; not authenticated"}

    def close(self):
        self._model_workers.shutdown(wait=True)
        close = getattr(self.gateway, "close", None)
        if close:
            close()
