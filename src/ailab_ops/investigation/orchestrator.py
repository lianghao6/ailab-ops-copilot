"""Model-directed investigation with state, budget and evidence guardrails.

One step is one model decision (potentially a batch of read calls). Deadlines
are checked before/after each synchronous operation; in-flight IO cancellation
belongs to gateway/tool timeouts. Token usage includes both input and output.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from typing import Any, Callable
from uuid import uuid4

from ailab_ops.approvals import ApprovalError, ApprovalRequest, ApprovalService, ApprovalStore
from ailab_ops.evidence import Evidence, EvidenceStore, ValidationIssue, validate_report
from ailab_ops.llm.base import ChatMessage, FinishReason, ToolCall
from ailab_ops.models.protocol import ModelBackendError, ModelGateway
from ailab_ops.observability.events import TraceEvent
from ailab_ops.policy import PolicyContext, PolicyEngine
from ailab_ops.tools.registry import ToolRegistry, ToolResult

from .models import Budget, Hypothesis, InvestigationPhase, InvestigationReport, InvestigationState
from .parsing import (ActionControl, ControlError, HypothesesControl, PlanControl, ReportControl,
                      parse_arguments, parse_control)
from .prompts import SYSTEM_PROMPT


class InvestigationSessionError(ValueError):
    """State does not belong to a live session of this orchestrator."""


@dataclass
class InvestigationSession:
    state: InvestigationState
    evidence_store: EvidenceStore
    messages: list[ChatMessage]
    events: list[dict[str, Any]] = field(default_factory=list)
    completed_calls: dict[str, list[str]] = field(default_factory=dict)
    report_failures: int = 0
    proposed_action: dict[str, Any] | None = None
    approval_request_id: str | None = None


class InvestigationOrchestrator:
    def __init__(self, gateway: ModelGateway, registry: ToolRegistry,
                 evidence_store: EvidenceStore | None = None, *,
                 persist: Callable[[InvestigationState], None] | None = None,
                 now: Callable[[], datetime] | None = None,
                 approval_store: ApprovalStore | None = None,
                 event_sink: Callable[[TraceEvent], None] | None = None):
        self.gateway = gateway
        self.registry = registry
        # The legacy injection is an empty, single-use test seam, not historical
        # context. A nonempty store can replace current observations by identity.
        if evidence_store is not None and evidence_store._evidence:
            raise ValueError("Injected evidence_store must be empty; historical evidence cannot seed an investigation")
        self._initial_store = deepcopy(evidence_store) if evidence_store is not None else None
        self.evidence_store = EvidenceStore()
        self._persist = persist
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sessions: dict[str, InvestigationSession] = {}
        # Latest immutable-by-copy state snapshots. No terminal chat transcripts
        # or events are retained here; durable storage can use the callback.
        self.states: dict[str, InvestigationState] = {}
        self._evidence_archive: dict[str, dict[str, Evidence]] = {}
        self._persistence_errors: list[dict[str, Any]] = []
        self._event_sink = event_sink
        self._tracing_errors: list[dict[str, Any]] = []
        self.policy = PolicyEngine(registry, evidence_lookup=self.get_evidence)
        self.approval_service = ApprovalService(self.policy, approval_store, now=self._now, event_sink=event_sink)

    def start(self, question: str, *, case_id: str | None, budget: Budget) -> InvestigationState:
        """Create and persist intake for callers advancing a session one step at a time."""
        state = InvestigationState(uuid4().hex, question, deepcopy(budget), case_id=case_id,
                                   mode=self.gateway.mode)
        store = self._initial_store if self._initial_store is not None else EvidenceStore()
        self._initial_store = None
        self.evidence_store = store
        actions = []
        for name in self.registry.names:
            tool = self.registry.get(name)
            if tool.kind == "action":
                actions.append({"name": tool.name, "description": tool.description,
                    "parameters": tool.parameters, "sensitivity": tool.sensitivity,
                    "idempotent": tool.idempotent})
        session = InvestigationSession(state, store, [ChatMessage("system", SYSTEM_PROMPT),
            ChatMessage("user", json.dumps({"question": question, "case_id": case_id,
                                            "available_actions": actions}, ensure_ascii=False))])
        self.sessions[state.session_id] = session
        self._save(session, "intake")
        return state

    def run(self, question: str, *, case_id: str | None, budget: Budget) -> InvestigationState:
        state = self.start(question, case_id=case_id, budget=budget)
        while state.phase not in {InvestigationPhase.COMPLETED, InvestigationPhase.STOPPED,
                                  InvestigationPhase.AWAITING_APPROVAL}:
            state = self.advance(state)
        return state

    def advance(self, state: InvestigationState) -> InvestigationState:
        session = self.sessions.get(state.session_id)
        if session is None or state.to_dict() != session.state.to_dict():
            raise InvestigationSessionError("Unknown, released or stale investigation session")
        state = session.state
        if state.phase == InvestigationPhase.AWAITING_APPROVAL:
            # Approval is owned by the action workflow, never this read loop.
            return state
        if self._stop_if_exhausted(session):
            return state
        state.phase = InvestigationPhase.INVESTIGATING
        state.budget.steps_used += 1
        self._save(session, "model_requested")
        self._emit(session, "model", "requested", model=self.gateway.model, retry=session.report_failures)
        try:
            response = self.gateway.complete(deepcopy(session.messages),
                [self.registry.get(name).spec() for name in self.registry.names
                 if self.registry.get(name).kind == "read"],
                max_tokens=min(4096, state.budget.max_tokens - state.budget.tokens_used))
        except ModelBackendError as exc:
            self._emit(session, "error", "model_failed", model=self.gateway.model,
                       error=exc.kind, payload={"exception_type": type(exc).__name__})
            return self._stop(session, f"model_backend:{exc.kind}")
        state.budget.tokens_used += response.usage.total
        self._emit(session, "model", "received", model=response.model or self.gateway.model,
            usage={"tokens_in": response.usage.tokens_in, "tokens_out": response.usage.tokens_out},
            latency_ms=response.latency_s * 1000, retry=session.report_failures,
            payload={"finish_reason": response.finish_reason.value})
        self._save(session, "model_received")
        if self._stop_if_exhausted(session, include_steps=False):
            return state
        if response.finish_reason in {FinishReason.ERROR, FinishReason.LENGTH}:
            if session.report_failures:
                return self._stop(session, "report_validation_failed")
            # Incomplete calls are neither executed nor replayed to the model:
            # retaining them would require matching tool results in the transcript.
            self._feedback(session, "incomplete_response", "Return a complete control object or tool call.")
            self._stop_if_exhausted(session)
            return state
        session.messages.append(ChatMessage("assistant", response.content, tool_calls=deepcopy(response.tool_calls)))
        if response.tool_calls:
            if session.report_failures:
                return self._stop(session, "report_validation_failed")
            for call in response.tool_calls:
                if self._stop_if_exhausted(session, include_steps=False):
                    return state
                self._read_tool(session, call)
                if self._stop_if_exhausted(session, include_steps=False):
                    return state
        else:
            self._control(session, response.content)
        if state.phase not in {InvestigationPhase.COMPLETED, InvestigationPhase.STOPPED,
                               InvestigationPhase.AWAITING_APPROVAL}:
            self._stop_if_exhausted(session)
        return state

    def get_evidence(self, session_id: str, evidence_id: str) -> Evidence | None:
        """Read an isolated observation from an active or archived session.

        Results are independent copies. Unknown evidence returns None; unknown
        sessions raise a typed error. Terminal archives retain observations only,
        never the model conversation or events.
        """
        if session_id in self._evidence_archive:
            return deepcopy(self._evidence_archive[session_id].get(evidence_id))
        session = self.sessions.get(session_id)
        if session is None:
            raise InvestigationSessionError("Unknown investigation session")
        if evidence_id not in session.state.evidence_ids:
            return None
        return session.evidence_store.get(evidence_id)

    def request_action(self, session_id: str, proposal: dict[str, Any]) -> ApprovalRequest:
        """Request approval against a completed, cited diagnosis.

        Report generation terminates the read loop. This explicit boundary lets
        callers propose an action after inspecting its retained diagnosis.
        """
        with self.approval_service.store._lock:
            state = self.states.get(session_id)
            if (state is None or state.phase != InvestigationPhase.COMPLETED or state.report is None
                    or session_id in self.sessions):
                raise ApprovalError("A completed diagnosis is required before proposing an action")
            store = EvidenceStore()
            for evidence in self._evidence_archive.get(session_id, {}).values():
                store.add(deepcopy(evidence))
            if (validate_report(state.report, store) or not any(c.material for c in state.report.claims)
                    or any(eid not in state.evidence_ids for c in state.report.claims for eid in c.evidence_ids)):
                raise ApprovalError("A valid cited diagnosis is required")
            try:
                control = parse_control(json.dumps({"type": "proposed_action", "action": proposal}, allow_nan=False))
            except (ControlError, TypeError, ValueError) as exc:
                raise ApprovalError(str(exc)) from None
            session = InvestigationSession(deepcopy(state), store, [])
            return self._propose_action(session, control.action.model_dump())

    def _propose_action(self, session: InvestigationSession, action: dict[str, Any]) -> ApprovalRequest:
        if session.state.report is None:
            raise ApprovalError("Complete a valid diagnosis before proposing an action")
        context = PolicyContext(session.state.session_id, action["reason"], action["risk"],
                                action["rollback"], tuple(action["evidence_ids"]))
        request = self.approval_service.create(context, action["tool"], action["arguments"])
        session.proposed_action = deepcopy(action)
        session.approval_request_id = request.request_id
        session.state.phase = InvestigationPhase.AWAITING_APPROVAL
        session.state.stop_reason = None
        self.approval_service.watch(request.request_id,
                                    lambda changed: self._approval_changed(session, changed))
        self.sessions[session.state.session_id] = session
        self._save(session, "action_proposed")
        return request

    def _approval_changed(self, session: InvestigationSession, request: ApprovalRequest) -> None:
        if request.status not in {"executed", "rejected", "expired", "failed"}:
            return
        session.state.phase = InvestigationPhase.COMPLETED
        session.state.stop_reason = None if request.status == "executed" else "approval_" + request.status
        self._save(session, "action_" + request.status)

    @property
    def persistence_errors(self) -> tuple[dict[str, Any], ...]:
        """Isolated failure records for best-effort state persistence callbacks."""
        return tuple(deepcopy(self._persistence_errors))

    @property
    def tracing_errors(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self._tracing_errors))

    def _emit(self, session: InvestigationSession, kind: str, event: str, **metadata) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(TraceEvent(session.state.session_id, kind, session.state.phase.value,
                event=event, timestamp=self._now().isoformat(), **metadata))
        except Exception as exc:
            # No messages, recursive callbacks or transcripts in failure records.
            self._tracing_errors.append({"session_id": session.state.session_id, "event": event,
                "phase": session.state.phase.value, "error": type(exc).__name__})
            session.events.append({"event": "tracing_failed", "error": type(exc).__name__})

    def _save(self, session: InvestigationSession, event: str) -> None:
        session.events.append({"event": event, "step": session.state.budget.steps_used})
        self._emit(session, "state", event, evidence_ids=list(session.state.evidence_ids),
                   approval_id=session.approval_request_id,
                   payload={"budget": session.state.budget.to_dict(), "stop_reason": session.state.stop_reason})
        self.states[session.state.session_id] = deepcopy(session.state)
        terminal = session.state.phase in {InvestigationPhase.COMPLETED, InvestigationPhase.STOPPED}
        if terminal:
            archive = {
                evidence_id: session.evidence_store.get(evidence_id)
                for evidence_id in session.state.evidence_ids
            }
            self._evidence_archive[session.state.session_id] = archive
        try:
            if self._persist is not None:
                self._persist(deepcopy(session.state))
        except Exception as exc:
            self._persistence_errors.append({
                "session_id": session.state.session_id, "event": event,
                "phase": session.state.phase.value, "error": type(exc).__name__,
                "occurred_at": self._now().isoformat(),
            })
        finally:
            if terminal:
                self._evidence_archive[session.state.session_id] = archive
                self.sessions.pop(session.state.session_id, None)

    def _stop(self, session: InvestigationSession, reason: str) -> InvestigationState:
        session.state.phase = InvestigationPhase.STOPPED
        session.state.stop_reason = reason
        self._save(session, "stopped")
        return session.state

    def _stop_if_exhausted(self, session: InvestigationSession, *, include_steps: bool = True) -> bool:
        limits = session.state.budget.exhausted(self._now())
        if not include_steps:
            limits = [limit for limit in limits if limit != "steps"]
        if limits:
            self._stop(session, "budget_exhausted:" + ",".join(limits))
        return bool(limits)

    def _feedback(self, session: InvestigationSession, error: str, hint: str, **extra: Any) -> None:
        session.messages.append(ChatMessage("user", json.dumps(
            {"error": error, "hint": hint, **extra}, ensure_ascii=False)))
        self._emit(session, "error", error, error=error, retry=session.report_failures)
        self._save(session, error)

    def _tool_feedback(self, session: InvestigationSession, call: ToolCall, payload: dict) -> None:
        session.messages.append(ChatMessage("tool", json.dumps(payload, ensure_ascii=False, default=str),
                                            name=call.name, tool_call_id=call.id))
        self._save(session, "tool_result")

    def _read_tool(self, session: InvestigationSession, call: ToolCall) -> None:
        tool = self.registry.get(call.name)
        try:
            arguments = parse_arguments(call)
            decision = self.policy.authorize(PolicyContext(session.state.session_id), tool, arguments)
            self._emit(session, "policy", "authorized", tool=call.name, payload={"outcome": decision.outcome})
            if decision.outcome != "allow_read":
                if tool is not None and tool.kind == "action":
                    raise ControlError("action tools require a proposed_action and approval")
                raise ControlError(decision.reason)
        except ControlError as exc:
            self._emit(session, "policy", "denied", tool=call.name, payload={"outcome": "deny"})
            self._tool_feedback(session, call, {"ok": False, "error": str(exc),
                "hint": "Repair the arguments using the tool schema; actions must be proposed."})
            return
        key = json.dumps([call.name, arguments], sort_keys=True, ensure_ascii=False, allow_nan=False)
        if key in session.completed_calls:
            self._tool_feedback(session, call, {"ok": False, "error": "duplicate_call",
                "hint": "Use the existing evidence or change the query.",
                "evidence_ids": session.completed_calls[key]})
            return
        result = self.registry.call(call.name, deepcopy(arguments), step=session.state.budget.steps_used)
        self._emit(session, "tool", "result", tool=call.name, latency_ms=result.latency_ms,
                   error=None if result.ok else "tool_failed", payload={"ok": result.ok})
        try:
            payload = result.to_dict()
            # Runtime latency is telemetry, not evidence or replay input.
            payload.pop("latency_ms", None)
            if result.ok:
                items = self._normalize(call.name, arguments, result)
                # Validate the entire batch before changing the evidence store.
                json.dumps([item.to_dict() for item in items], allow_nan=False)
                json.dumps(payload, default=str, allow_nan=False)
                captured = [session.evidence_store.add(item) for item in items]
                ids = [item.evidence_id for item in captured]
                session.state.evidence_ids = list(dict.fromkeys(session.state.evidence_ids + ids))
                session.completed_calls[key] = ids
                payload["evidence_items"] = [item.to_dict() for item in captured]
                self._emit(session, "evidence", "captured", tool=call.name, evidence_ids=ids)
        except (ValueError, TypeError, AttributeError):
            payload = {"ok": False, "error": "invalid_evidence_result",
                       "hint": "The tool returned an invalid observation; use another read or retry."}
        self._tool_feedback(session, call, payload)

    @staticmethod
    def _normalize(name: str, arguments: dict, result: ToolResult) -> list[Evidence]:
        if result.evidence_items:
            return [replace(deepcopy(item), source_tool=name, arguments=deepcopy(arguments), evidence_id="",
                            truncated=item.truncated or result.truncated) for item in result.evidence_items]
        return [Evidence(name, deepcopy(arguments), f"Observation from {name}",
            json.dumps(result.data, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False),
            truncated=result.truncated)]

    def _control(self, session: InvestigationSession, content: str) -> None:
        try:
            control = parse_control(content)
        except ControlError as exc:
            try:
                raw = json.loads(content)
            except ValueError:
                raw = None
            if session.report_failures:
                self._stop(session, "report_validation_failed")
            elif isinstance(raw, dict) and raw.get("type") == "report":
                self._report_issues(session, [ValidationIssue("invalid_report", "report", str(exc))])
            elif isinstance(raw, dict) and raw.get("type") == "proposed_action":
                self._feedback(session, "invalid_action", str(exc))
            else:
                self._feedback(session, "invalid_control", str(exc))
            return
        if session.report_failures and not isinstance(control, ReportControl):
            self._stop(session, "report_validation_failed")
            return
        state = session.state
        if isinstance(control, PlanControl):
            state.plan = control.plan
            self._save(session, "plan_updated")
        elif isinstance(control, HypothesesControl):
            hypotheses = [Hypothesis.from_dict(h.model_dump()) for h in control.hypotheses]
            references = {eid for h in hypotheses for eid in h.supporting_evidence_ids + h.contradicting_evidence_ids}
            if references.difference(state.evidence_ids):
                self._feedback(session, "unknown_evidence", "Use only this session's evidence in hypotheses.")
            elif len({h.hypothesis_id for h in hypotheses}) != len(hypotheses):
                self._feedback(session, "duplicate_hypothesis", "Each hypothesis needs a unique ID.")
            else:
                state.hypotheses = hypotheses
                self._save(session, "hypotheses_updated")
        elif isinstance(control, ReportControl):
            self._report(session, InvestigationReport.from_dict(control.report.model_dump()))
        elif isinstance(control, ActionControl):
            action = control.action.model_dump()
            try:
                self._propose_action(session, action)
            except ApprovalError as exc:
                self._feedback(session, "invalid_action", str(exc))

    def _report(self, session: InvestigationSession, report: InvestigationReport) -> None:
        state = session.state
        state.phase = InvestigationPhase.VALIDATING
        self._save(session, "validating_report")
        issues = validate_report(report, session.evidence_store)
        if not any(claim.material for claim in report.claims):
            issues.append(ValidationIssue("missing_material_claim", "claims", "Report requires a material cited claim."))
        for i, claim in enumerate(report.claims):
            for j, evidence_id in enumerate(claim.evidence_ids):
                if evidence_id not in state.evidence_ids and session.evidence_store.get(evidence_id) is not None:
                    issues.append(ValidationIssue("out_of_session_evidence", f"claims[{i}].evidence_ids[{j}]",
                                                  "Citation is not evidence observed in this session."))
        if issues:
            self._report_issues(session, issues)
            return
        state.report = report
        state.phase = InvestigationPhase.COMPLETED
        self._save(session, "completed")

    def _report_issues(self, session: InvestigationSession, issues: list[ValidationIssue]) -> None:
        session.report_failures += 1
        if session.report_failures > 1:
            self._stop(session, "report_validation_failed")
        else:
            session.state.phase = InvestigationPhase.VALIDATING
            self._feedback(session, "report_validation", "Regenerate the report once, correcting these issues.",
                           issues=[{"code": i.code, "path": i.path, "message": i.message} for i in issues])
