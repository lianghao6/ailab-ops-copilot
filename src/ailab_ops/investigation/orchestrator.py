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

from ailab_ops.evidence import Evidence, EvidenceStore, ValidationIssue, validate_report
from ailab_ops.llm.base import ChatMessage, FinishReason, ToolCall
from ailab_ops.models.protocol import ModelBackendError, ModelGateway
from ailab_ops.tools.registry import ToolRegistry, ToolResult

from .models import Budget, Hypothesis, InvestigationPhase, InvestigationReport, InvestigationState
from .parsing import (ActionControl, ControlError, HypothesesControl, PlanControl, ReportControl,
                      parse_arguments, parse_control, validate_arguments)
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


class InvestigationOrchestrator:
    def __init__(self, gateway: ModelGateway, registry: ToolRegistry,
                 evidence_store: EvidenceStore | None = None, *,
                 persist: Callable[[InvestigationState], None] | None = None,
                 now: Callable[[], datetime] | None = None):
        self.gateway = gateway
        self.registry = registry
        self._injected_store = evidence_store
        self.evidence_store = evidence_store if evidence_store is not None else EvidenceStore()
        self._persist = persist
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.sessions: dict[str, InvestigationSession] = {}
        # Latest immutable-by-copy state snapshots. No terminal chat transcripts
        # or events are retained here; durable storage can use the callback.
        self.states: dict[str, InvestigationState] = {}

    def start(self, question: str, *, case_id: str | None, budget: Budget) -> InvestigationState:
        """Create and persist intake for callers advancing a session one step at a time."""
        state = InvestigationState(uuid4().hex, question, deepcopy(budget), case_id=case_id,
                                   mode=self.gateway.mode)
        store = self._injected_store if self._injected_store is not None else EvidenceStore()
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
        try:
            response = self.gateway.complete(deepcopy(session.messages),
                [self.registry.get(name).spec() for name in self.registry.names
                 if self.registry.get(name).kind == "read"],
                max_tokens=min(4096, state.budget.max_tokens - state.budget.tokens_used))
        except ModelBackendError as exc:
            return self._stop(session, f"model_backend:{exc.kind}")
        state.budget.tokens_used += response.usage.total
        self._save(session, "model_received")
        if self._stop_if_exhausted(session, include_steps=False):
            return state
        session.messages.append(ChatMessage("assistant", response.content, tool_calls=deepcopy(response.tool_calls)))
        if response.finish_reason in {FinishReason.ERROR, FinishReason.LENGTH}:
            if session.report_failures:
                return self._stop(session, "report_validation_failed")
            self._feedback(session, "incomplete_response", "Return a complete control object or tool call.")
        elif response.tool_calls:
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

    def _save(self, session: InvestigationSession, event: str) -> None:
        session.events.append({"event": event, "step": session.state.budget.steps_used})
        self.states[session.state.session_id] = deepcopy(session.state)
        if self._persist is not None:
            self._persist(deepcopy(session.state))

    def _stop(self, session: InvestigationSession, reason: str) -> InvestigationState:
        session.state.phase = InvestigationPhase.STOPPED
        session.state.stop_reason = reason
        self._save(session, "stopped")
        self.sessions.pop(session.state.session_id, None)
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
        self._save(session, error)

    def _tool_feedback(self, session: InvestigationSession, call: ToolCall, payload: dict) -> None:
        session.messages.append(ChatMessage("tool", json.dumps(payload, ensure_ascii=False, default=str),
                                            name=call.name, tool_call_id=call.id))
        self._save(session, "tool_result")

    def _read_tool(self, session: InvestigationSession, call: ToolCall) -> None:
        tool = self.registry.get(call.name)
        try:
            arguments = parse_arguments(call)
            if tool is not None:
                if tool.kind != "read":
                    raise ControlError("action tools require a proposed_action and approval")
                validate_arguments(arguments, tool.parameters)
        except ControlError as exc:
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
            tool = self.registry.get(action["tool"])
            try:
                if tool is None or tool.kind != "action":
                    raise ControlError("Proposed action must name a registered action tool")
                validate_arguments(action["arguments"], tool.parameters)
            except ControlError as exc:
                self._feedback(session, "invalid_action", str(exc))
                return
            session.proposed_action = action
            state.phase = InvestigationPhase.AWAITING_APPROVAL
            self._save(session, "action_proposed")

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
        self.sessions.pop(state.session_id, None)

    def _report_issues(self, session: InvestigationSession, issues: list[ValidationIssue]) -> None:
        session.report_failures += 1
        if session.report_failures > 1:
            self._stop(session, "report_validation_failed")
        else:
            session.state.phase = InvestigationPhase.VALIDATING
            self._feedback(session, "report_validation", "Regenerate the report once, correcting these issues.",
                           issues=[{"code": i.code, "path": i.path, "message": i.message} for i in issues])
