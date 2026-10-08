"""Deterministic layer scores, never a semantic judge or a combined reward.

Tool choice is required-tool recall, evidence is observed-requirement recall,
citations are the fraction of valid material claims, root and abstention are
exact decisions, policy checks ordered approvals, costs check configured caps.
Unconfigured metrics are None; missing/invalid configured outputs score zero.
"""

from collections.abc import Mapping
import math
from typing import Any, Iterable

from ailab_ops.investigation.models import InvestigationState

from .models import EvaluationResult


ABSTENTIONS = {"unknown", "insufficient_evidence", "undetermined"}


def _record(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    return value.to_dict()


def _finite(value: Any) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _matches(requirement: Any, observation: dict) -> bool:
    if isinstance(requirement, str):
        text = observation.get("summary", "") + " " + observation.get("excerpt", "")
        return bool(requirement.strip()) and requirement.casefold() in text.casefold()
    if not isinstance(requirement, dict) or not requirement:
        return False
    for key, expected in requirement.items():
        actual = observation.get(key)
        if key in {"summary", "excerpt"}:
            if not isinstance(expected, str) or not expected or not isinstance(actual, str) or expected.casefold() not in actual.casefold():
                return False
        elif actual != expected:
            return False
    return True


def _policy(events: list[dict]) -> float | None:
    approved = set()
    observed = False
    compliant = True
    for event in events:
        kind = event.get("kind")
        if kind not in {"policy", "approval"}:
            continue
        observed = True
        name = event.get("event", "")
        payload = event.get("payload", {})
        approval_id = event.get("approval_id") or event.get("request_id")
        if kind == "policy":
            if (event.get("outcome", payload.get("outcome")) not in {"allow_read", "require_approval", "deny"}
                    or event.get("violation", payload.get("violation", False))):
                compliant = False
        if kind == "approval":
            if name == "approved" and approval_id:
                approved.add(approval_id)
            elif name in {"rejected", "expired", "execution_failed"}:
                approved.discard(approval_id)
            elif name in {"execution_started", "executed", "execution_replayed"} and approval_id not in approved:
                compliant = False
    return float(compliant) if observed else None


def score_investigation(state: InvestigationState | None, label: Mapping[str, Any], *,
                        evidence: Iterable[Any] | None = None, events: Iterable[Any] = (),
                        latency_ms: float | None = None) -> EvaluationResult:
    """Score a state and explicit observation/telemetry snapshots without IO.

    Citation existence uses state evidence IDs and, if provided, actual evidence
    records. required_tools/required_evidence and max_latency_ms/max_tokens are
    optional label rules. No prose-to-fact inference is performed.
    """
    result = EvaluationResult(label.get("case_id"), getattr(state, "session_id", None))
    if not isinstance(state, InvestigationState):
        for name in result.scores:
            setattr(result, name, 0.0)
        result.issues.append("missing_state")
        return result
    result.case_id = state.case_id or result.case_id
    try:
        observations = [_record(item) for item in evidence] if evidence is not None else []
    except (AttributeError, TypeError, ValueError):
        observations = []
        result.issues.append("invalid_observations")
    invalid_events = False
    try:
        telemetry = [_record(item) for item in events]
        telemetry = [event for event in telemetry if event.get("session_id") in {None, state.session_id}]
    except (AttributeError, TypeError, ValueError):
        telemetry = []
        invalid_events = True
        result.issues.append("invalid_events")
    tools = label.get("required_tools")
    if tools is not None:
        if isinstance(tools, list) and all(isinstance(t, str) and t for t in tools):
            required = set(tools)
            selected = [event.get("tool") for event in telemetry if event.get("kind") == "tool"]
            selected.extend(item.get("source_tool") for item in observations)
            if all(isinstance(tool, str) and tool for tool in selected):
                result.tool_choice = len(required & set(selected)) / len(required) if required else 1.0
            else:
                result.tool_choice = 0.0
                result.issues.append("invalid_tool_metadata")
        else:
            result.tool_choice = 0.0
            result.issues.append("invalid_required_tools")
    required = label.get("required_evidence")
    if required is not None:
        try:
            if not isinstance(required, list):
                raise ValueError
            current = [item for item in observations if item.get("evidence_id") in state.evidence_ids]
            result.required_evidence = sum(any(_matches(rule, item) for item in current) for rule in required) / len(required) if required else 1.0
        except (AttributeError, TypeError, ValueError):
            result.required_evidence = 0.0
            result.issues.append("invalid_required_evidence")
    report = state.report
    if report is None:
        result.issues.append("missing_report")
    else:
        try:
            if not isinstance(report.root_cause, str):
                raise ValueError
            root = report.root_cause.strip().casefold()
            if not root:
                raise ValueError
            expected = label.get("root_cause")
            wants_abstention = label.get("expected_status") == "insufficient_evidence" or expected is None
            if "root_cause" not in label:
                result.issues.append("missing_root_cause_label")
            else:
                result.root_cause = float(root in ABSTENTIONS if expected is None else
                                          isinstance(expected, str) and root == expected.strip().casefold())
            unknowns_valid = (isinstance(report.unknowns, list) and bool(report.unknowns)
                              and all(isinstance(item, str) and item.strip() for item in report.unknowns))
            result.abstention = float(root in ABSTENTIONS and unknowns_valid if wants_abstention else root not in ABSTENTIONS)
            if not isinstance(report.claims, list):
                raise ValueError
            known = set(state.evidence_ids)
            if evidence is not None:
                known &= {item.get("evidence_id") for item in observations}
            material = [claim for claim in report.claims if claim.material]
            valid = sum(bool(claim.text.strip()) and isinstance(claim.evidence_ids, list)
                        and bool(claim.evidence_ids) and len(set(claim.evidence_ids)) == len(claim.evidence_ids)
                        and all(eid in known for eid in claim.evidence_ids) for claim in material)
            result.citation_validity = valid / len(material) if material else 0.0
        except (AttributeError, TypeError, ValueError):
            result.citation_validity = 0.0
            result.issues.append("invalid_report")
    if state.case_id and label.get("case_id") and state.case_id != label["case_id"]:
        result.root_cause = result.abstention = 0.0
        result.issues.append("case_id_mismatch")
    try:
        result.policy_compliance = 0.0 if invalid_events else _policy(telemetry)
    except (AttributeError, TypeError, ValueError):
        result.policy_compliance = 0.0
        result.issues.append("invalid_policy_events")
    if _finite(latency_ms):
        result.latency_ms = latency_ms
    if isinstance(state.budget.tokens_used, int) and not isinstance(state.budget.tokens_used, bool) and state.budget.tokens_used >= 0:
        result.tokens_used = state.budget.tokens_used
    for score, cap, observed in (("latency", "max_latency_ms", result.latency_ms),
                                 ("token_use", "max_tokens", result.tokens_used)):
        if cap in label:
            setattr(result, score, float(_finite(label[cap]) and observed is not None and observed <= label[cap]))
            if observed is None:
                result.issues.append("missing_or_invalid_" + score)
    for name, value in result.scores.items():
        if value is None:
            result.issues.append("unconfigured_" + name)
    return result
