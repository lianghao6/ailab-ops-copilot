"""Explicit approval and exactly-once simulated execution within one store.

The handler is declarative data, never an arbitrary callable or Tool.fn.
Identity is supplied by trusted callers; authentication belongs to the API.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable
from uuid import uuid4

from ailab_ops.policy import ApprovalRequest, AuditEvent, PolicyContext, PolicyEngine
from ailab_ops.observability.events import TraceEvent
from .store import ApprovalStore


class ApprovalError(ValueError):
    """A request cannot satisfy policy or its current lifecycle state."""


@dataclass(frozen=True)
class SimulatedActionHandler:
    result: dict[str, Any]

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {**deepcopy(self.result), "simulated": True}


class ApprovalService:
    def __init__(self, policy: PolicyEngine, store: ApprovalStore | None = None, *,
                 now: Callable[[], datetime] | None = None,
                 expires_in: timedelta = timedelta(minutes=15),
                 event_sink: Callable[[TraceEvent], None] | None = None):
        if expires_in <= timedelta(0):
            raise ValueError("Approval lifetime must be positive")
        self.policy = policy
        self.store = store if store is not None else ApprovalStore()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._expires_in = expires_in
        self._handlers: dict[str, SimulatedActionHandler] = {}
        self._observers: dict[str, Callable[[ApprovalRequest], None]] = {}
        self._event_sink = event_sink
        self._tracing_errors: list[dict[str, Any]] = []

    def register_simulated(self, tool_name: str, handler: SimulatedActionHandler) -> None:
        with self.store._lock:
            tool = self.policy.registry.get(tool_name)
            if tool is None or tool.kind != "action" or type(handler) is not SimulatedActionHandler:
                raise ApprovalError("Only declarative simulated handlers for registered actions are accepted")
            if tool_name in self._handlers:
                raise ApprovalError("A simulated handler cannot be replaced")
            if not isinstance(handler.result, dict):
                raise ApprovalError("Simulated result must be an object")
            try:
                json.dumps(handler.result, allow_nan=False)
            except (ValueError, TypeError) as exc:
                raise ApprovalError("Simulated result must be finite JSON") from exc
            self._handlers[tool_name] = deepcopy(handler)

    def watch(self, request_id: str, observer: Callable[[ApprovalRequest], None]) -> None:
        """Attach the owning investigation's state persistence callback."""
        with self.store._lock:
            self.store.get(request_id)
            self._observers[request_id] = observer

    def _audit(self, event: str, actor: str, request: ApprovalRequest | None = None,
               *, request_id: str | None = None, session_id: str | None = None, **details) -> None:
        audit = AuditEvent(uuid4().hex,
            request.request_id if request else request_id,
            request.session_id if request else session_id, event, actor, self._now(), deepcopy(details))
        self.store._append(audit)
        if self._event_sink is not None:
            try:
                self._event_sink(TraceEvent(audit.session_id, "approval", request.status if request else None,
                    event=event, timestamp=audit.occurred_at.isoformat(),
                    approval_id=audit.request_id, tool=request.tool if request else details.get("tool"),
                    evidence_ids=list(request.evidence_ids) if request else [],
                    error=details.get("error"), payload={"actor": actor, **deepcopy(details)}))
            except Exception as exc:
                failure = {"session_id": audit.session_id, "approval_id": audit.request_id,
                           "event": event, "error": type(exc).__name__}
                self._tracing_errors.append(failure)
                # Append directly: tracing failure must never invoke its own sink.
                self.store._append(AuditEvent(uuid4().hex, audit.request_id, audit.session_id,
                    "tracing_failed", "system", audit.occurred_at, {"event": event, "error": type(exc).__name__}))

    @property
    def tracing_errors(self) -> tuple[dict[str, Any], ...]:
        return tuple(deepcopy(self._tracing_errors))

    def _save(self, request: ApprovalRequest) -> ApprovalRequest:
        # Commit the lifecycle before best-effort observers. Notification must
        # not strand execution or invalidate a stored idempotent result.
        self.store._save(request)
        self._notify(request)
        return deepcopy(request)

    def _notify(self, request: ApprovalRequest) -> None:
        observer = self._observers.get(request.request_id)
        try:
            if observer is not None:
                observer(deepcopy(request))
        except Exception as exc:
            # Exception messages may contain credentials or private backend
            # payloads. Status, request identity and exception type are enough
            # to make the failed extension observable without exposing them.
            self._audit("notification_failed", "system", request,
                        status=request.status, error=type(exc).__name__)
        finally:
            if request.status in {"executed", "rejected", "expired", "failed"}:
                self._observers.pop(request.request_id, None)

    def _get(self, request_id: str, actor: str, operation: str) -> ApprovalRequest:
        if not isinstance(actor, str) or not actor.strip():
            self._audit(operation + "_denied", "anonymous", request_id=request_id, reason="Actor required")
            raise ApprovalError("An explicit actor is required")
        try:
            request = self.store.get(request_id)
        except KeyError:
            self._audit(operation + "_denied", actor, request_id=request_id, reason="Unknown request")
            raise ApprovalError("Unknown approval request") from None
        if request.status in {"pending", "approved"} and self._now() >= request.expires_at:
            request = replace(request, status="expired")
            self._audit("expired", "system", request)
            self._save(request)
        return request

    def create(self, context: PolicyContext, tool: str, arguments: dict, *, actor: str = "system") -> ApprovalRequest:
        with self.store._lock:
            decision = self.policy.authorize(context, tool, arguments)
            if decision.outcome != "require_approval":
                self._audit("policy_denied", actor, session_id=context.session_id, tool=tool, reason=decision.reason)
                raise ApprovalError(decision.reason)
            metadata = self.policy.registry.get(tool)
            created_at = self._now()
            request = ApprovalRequest(uuid4().hex, context.session_id, tool, deepcopy(arguments),
                context.reason, context.risk, context.rollback, tuple(dict.fromkeys(context.evidence_ids)),
                metadata.sensitivity, metadata.idempotent, created_at, created_at + self._expires_in)
            self._audit("created", actor, request, tool=tool, arguments=arguments,
                        reason=context.reason, risk=context.risk, rollback=context.rollback,
                        evidence_ids=list(context.evidence_ids))
            return self._save(request)

    def approve(self, request_id: str, *, actor: str) -> ApprovalRequest:
        with self.store._lock:
            request = self._get(request_id, actor, "approval")
            if request.status != "pending":
                self._audit("approval_denied", actor, request, status=request.status)
                raise ApprovalError("Only pending requests can be approved")
            request = replace(request, status="approved", approved_by=actor)
            self._audit("approved", actor, request)
            return self._save(request)

    def reject(self, request_id: str, *, actor: str, reason: str) -> ApprovalRequest:
        with self.store._lock:
            request = self._get(request_id, actor, "rejection")
            if request.status != "pending" or not isinstance(reason, str) or not reason.strip():
                self._audit("rejection_denied", actor, request, status=request.status)
                raise ApprovalError("Rejection requires a pending request and a reason")
            request = replace(request, status="rejected", rejection_reason=reason)
            self._audit("rejected", actor, request, reason=reason)
            return self._save(request)

    def execute(self, request_id: str, *, actor: str) -> dict[str, Any]:
        with self.store._lock:
            request = self._get(request_id, actor, "execution")
            if request.status == "executed":
                self._audit("execution_replayed", actor, request)
                return deepcopy(request.result)
            if request.status != "approved":
                self._audit("execution_denied", actor, request, status=request.status)
                raise ApprovalError("Explicit unexpired approval is required")
            context = PolicyContext(request.session_id, request.reason, request.risk,
                                    request.rollback, request.evidence_ids)
            tool = self.policy.registry.get(request.tool)
            decision = self.policy.authorize(context, tool, request.arguments)
            handler = self._handlers.get(request.tool)
            if (decision.outcome != "require_approval" or handler is None or tool is None
                    or tool.sensitivity != request.sensitivity or tool.idempotent != request.idempotent):
                self._audit("execution_denied", actor, request, reason="Policy or simulated handler unavailable")
                raise ApprovalError("Current policy and a simulated handler are required")
            request = replace(request, status="executing")
            self._audit("execution_started", actor, request)
            self._save(request)
            try:
                result = handler.execute(deepcopy(request.arguments))
            except Exception as exc:
                request = replace(request, status="failed")
                self._audit("execution_failed", actor, request, error=type(exc).__name__)
                self._save(request)
                raise ApprovalError("Simulated execution failed") from exc
            request = replace(request, status="executed", result=deepcopy(result))
            self._audit("executed", actor, request, result=result)
            self._save(request)
            return deepcopy(result)

    def expire(self) -> tuple[ApprovalRequest, ...]:
        """Sweep overdue approvals even when no user attempts a transition."""
        with self.store._lock:
            expired = []
            for request in self.store.requests:
                if request.status in {"pending", "approved"} and self._now() >= request.expires_at:
                    expired.append(self._get(request.request_id, "system", "expiry"))
            return tuple(expired)
