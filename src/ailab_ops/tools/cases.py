"""Case-scoped observation tools. No labels, inference rules or real mutations."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from ailab_ops.cases.loader import CaseWorld
from ailab_ops.evidence import Evidence
from .registry import Tool, ToolRegistry, ToolResult
from .runbooks import build_runbook_tool


def _observed_range(rows, kind):
    timestamps = []
    for row in rows:
        raw = row["ts"] if kind == "logs" else row["ts_start"]
        start = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        if kind == "logs":
            timestamps.append(start)
        elif row["values"]:
            timestamps.extend([start, start + timedelta(seconds=row["step_s"] * (len(row["values"]) - 1))])
    if not timestamps:
        return None
    return tuple(value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                 for value in (min(timestamps), max(timestamps)))


def build_case_registry(world: CaseWorld, *, knowledge_root: Path | None = None) -> ToolRegistry:
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

    registry.register(Tool("get_case_snapshot", "Read case jobs, nodes, incidents and telemetry availability",
        deepcopy(schema), lambda case_id: read(snapshot, case_id), simulated_latency_ms=0))

    def observe(kind, case_id):
        if case_id != world.case_id:
            return ToolResult(False, error="case_not_found")
        data = {"rows": deepcopy(getattr(world, kind)), "telemetry": deepcopy(world.telemetry.get(kind, {})),
                "source_note": world.telemetry.get("note", "")}
        evidence = Evidence("get_case_" + kind, {"case_id": case_id},
            f"Retained case {kind}; consult telemetry for source coverage",
            json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False),
            observed_time_range=_observed_range(data["rows"], kind),
            metadata={"telemetry": deepcopy(data["telemetry"]), "source_note": data["source_note"]})
        # Source completeness/availability lives in telemetry; truncated only
        # records clipping by this tool, which currently returns all retained rows.
        return ToolResult(True, data=data, evidence_items=[evidence])

    for kind, description in [
        ("logs", "Read retained timestamped logs with source coverage and missing-data details"),
        ("metrics", "Read retained metric samples with collection availability and coverage"),
    ]:
        registry.register(Tool("get_case_" + kind, description, deepcopy(schema),
            lambda case_id, kind=kind: observe(kind, case_id), simulated_latency_ms=0))
    registry.register(build_runbook_tool(knowledge_root))
    for action in world.actions:
        action_schema = deepcopy(schema)
        action_schema["properties"]["note"] = {"type": "string", "minLength": 1, "maxLength": 4000}
        action_schema["required"].append("note")
        registry.register(Tool(action["action_id"], action["description"], action_schema,
            lambda **kwargs: ToolResult(False, error="approval_required"), kind="action",
            sensitivity="internal", idempotent=True))
    return registry
