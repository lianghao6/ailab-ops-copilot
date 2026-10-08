"""Case-scoped observation tools. No labels, inference rules or real mutations."""

from copy import deepcopy
from dataclasses import asdict

from ailab_ops.cases.loader import CaseWorld
from .registry import Tool, ToolRegistry, ToolResult


def build_case_registry(world: CaseWorld) -> ToolRegistry:
    registry = ToolRegistry()
    schema = {"type": "object", "properties": {"case_id": {"type": "string", "enum": [world.case_id]}},
              "required": ["case_id"], "additionalProperties": False}
    snapshot = asdict(world)
    snapshot.pop("logs")
    snapshot.pop("metrics")

    def read(value, case_id):
        if case_id != world.case_id:
            return ToolResult(False, error="case_not_found")
        return ToolResult(True, data=deepcopy(value))

    for name, description, value in [
        ("get_case_snapshot", "Read case jobs, nodes, incidents and telemetry availability", snapshot),
        ("get_case_logs", "Read retained timestamped logs across all workers", world.logs),
        ("get_case_metrics", "Read retained resource and interface metric samples", world.metrics),
    ]:
        registry.register(Tool(name, description, deepcopy(schema),
            lambda case_id, value=value: read(value, case_id), simulated_latency_ms=0))
    for action in world.actions:
        action_schema = deepcopy(schema)
        action_schema["properties"]["note"] = {"type": "string", "minLength": 1, "maxLength": 4000}
        action_schema["required"].append("note")
        registry.register(Tool(action["action_id"], action["description"], action_schema,
            lambda **kwargs: ToolResult(False, error="approval_required"), kind="action",
            sensitivity="internal", idempotent=True))
    return registry
