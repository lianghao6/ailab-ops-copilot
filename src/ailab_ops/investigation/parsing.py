"""Strict model control and tool-argument parsing; no diagnostic inference."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ailab_ops.llm.base import ToolCall


class ControlError(ValueError):
    """The model must repair its structured response."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PlanControl(_Strict):
    type: Literal["plan"]
    plan: list[str]


class _Hypothesis(_Strict):
    hypothesis_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    confidence: float = Field(default=0.0, ge=0, le=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)


class HypothesesControl(_Strict):
    type: Literal["hypotheses"]
    hypotheses: list[_Hypothesis]


class _Claim(_Strict):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    material: bool = True


class _Report(_Strict):
    root_cause: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1)
    claims: list[_Claim] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class ReportControl(_Strict):
    type: Literal["report"]
    report: _Report


class _Action(_Strict):
    tool: str = Field(min_length=1)
    arguments: dict[str, Any]
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    rollback: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ActionControl(_Strict):
    type: Literal["proposed_action"]
    action: _Action


Control = Union[PlanControl, HypothesesControl, ReportControl, ActionControl]
_CONTROL = TypeAdapter(Annotated[Control, Field(discriminator="type")])


def parse_control(content: str) -> Control:
    try:
        # Round-trip rejects non-finite JSON even in untyped action arguments.
        data = json.loads(content)
        json.dumps(data, allow_nan=False)
        return _CONTROL.validate_python(data)
    except ValidationError as exc:
        # Pydantic's string rendering includes input_value/input_type and may
        # expose credentials, private reasoning or labels. Retain only field
        # locations and stable error types at the source, before any logging or
        # wrapping as a public approval error. Messages/context can echo input.
        issues = exc.errors(include_url=False, include_context=False, include_input=False)
        summary = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or 'control'} ({issue['type']})"
            for issue in issues)
        raise ControlError("invalid control: " + summary) from None
    except json.JSONDecodeError:
        raise ControlError("invalid control: invalid_json") from None
    except (ValueError, TypeError):
        raise ControlError("invalid control: invalid_json_value") from None


def parse_arguments(call: ToolCall) -> dict[str, Any]:
    try:
        arguments = json.loads(call.raw_arguments) if call.raw_arguments else call.arguments
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        json.dumps(arguments, allow_nan=False)
        return arguments
    except (ValueError, TypeError) as exc:
        raise ControlError(f"invalid arguments: {exc}") from None


def validate_arguments(value: Any, schema: dict, path: str = "arguments") -> None:
    """Validate the JSON-schema vocabulary used by registered tool specs.

    Function invocation still handles signature errors. Unknown schema keywords
    do not imply unsupported diagnostic rules or alter argument values.
    """
    kinds = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
             "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
             "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
             "null": lambda v: v is None}
    kind = schema.get("type")
    allowed = kind if isinstance(kind, list) else [kind] if kind else []
    if allowed and not any(k in kinds and kinds[k](value) for k in allowed):
        raise ControlError(f"invalid arguments: {path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ControlError(f"invalid arguments: {path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise ControlError(f"invalid arguments: {path}.{name} is required")
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                validate_arguments(item, properties[name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise ControlError(f"invalid arguments: unexpected {path}.{name}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_arguments(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ControlError(f"invalid arguments: {path} is below minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ControlError(f"invalid arguments: {path} is above maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ControlError(f"invalid arguments: {path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ControlError(f"invalid arguments: {path} is above maximum")
