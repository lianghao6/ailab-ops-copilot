"""Code-enforced authorization; model content cannot grant execution rights."""

import json
from typing import Callable

from ailab_ops.investigation.parsing import ControlError, validate_arguments
from ailab_ops.tools.registry import Tool, ToolRegistry

from .models import PolicyContext, PolicyDecision


class PolicyEngine:
    def __init__(self, registry: ToolRegistry, *, evidence_lookup: Callable | None = None):
        self.registry = registry
        self._evidence_lookup = evidence_lookup

    def authorize(self, context: PolicyContext, tool: Tool | str | None,
                  arguments: dict) -> PolicyDecision:
        if isinstance(tool, str):
            tool = self.registry.get(tool)
        if tool is None or self.registry.get(tool.name) is not tool:
            return PolicyDecision("deny", "Unknown or unregistered tool")
        try:
            if not isinstance(arguments, dict):
                raise ControlError("Arguments must be an object")
            json.dumps(arguments, allow_nan=False)
            validate_arguments(arguments, tool.parameters)
        except (ControlError, TypeError, ValueError) as exc:
            return PolicyDecision("deny", str(exc))
        if tool.kind == "read":
            return PolicyDecision("allow_read", "Registered read access is automatic")
        if tool.kind != "action":
            return PolicyDecision("deny", "Unknown permission kind")
        if not all(isinstance(value, str) and value.strip() for value in (
                context.session_id, context.reason, context.risk, context.rollback)):
            return PolicyDecision("deny", "Actions require session, reason, risk and rollback")
        if (not isinstance(context.evidence_ids, (tuple, list)) or not context.evidence_ids
                or not all(isinstance(eid, str) and eid.strip() for eid in context.evidence_ids)):
            return PolicyDecision("deny", "Actions require evidence references")
        try:
            verified = self._evidence_lookup is not None and all(
                self._evidence_lookup(context.session_id, eid) is not None for eid in context.evidence_ids)
        except (KeyError, ValueError):
            verified = False
        if not verified:
            return PolicyDecision("deny", "Evidence must belong to this investigation session")
        return PolicyDecision("require_approval", "Action requires explicit approval")
