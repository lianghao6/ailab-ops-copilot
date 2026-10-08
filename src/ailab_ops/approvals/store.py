"""In-memory approval snapshots with an append-only audit history."""

from copy import deepcopy
from threading import RLock

from ailab_ops.policy.models import ApprovalRequest, AuditEvent


class ApprovalStore:
    def __init__(self):
        self._requests: dict[str, ApprovalRequest] = {}
        self._audit: list[AuditEvent] = []
        self._lock = RLock()

    def get(self, request_id: str) -> ApprovalRequest:
        with self._lock:
            return deepcopy(self._requests[request_id])

    @property
    def requests(self) -> tuple[ApprovalRequest, ...]:
        with self._lock:
            return tuple(deepcopy(list(self._requests.values())))

    @property
    def audit_events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(deepcopy(self._audit))

    def _save(self, request: ApprovalRequest) -> None:
        self._requests[request.request_id] = deepcopy(request)

    def _append(self, event: AuditEvent) -> None:
        self._audit.append(deepcopy(event))
