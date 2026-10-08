"""Exercise orchestration with real tools/evidence and a scripted model boundary."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from ailab_ops.evidence import Evidence, EvidenceStore
from ailab_ops.investigation import (Budget, InvestigationPhase, InvestigationState,
                                     InvestigationOrchestrator, InvestigationSessionError)
from ailab_ops.llm.base import FinishReason, LLMResponse, ToolCall, Usage
from ailab_ops.models.protocol import ModelBackendError
from ailab_ops.tools.registry import Tool, ToolRegistry, ToolResult


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


class ScriptedGateway:
    mode = "replay"
    model = "scripted-test"

    def __init__(self, *steps):
        self.steps = iter(steps)
        self.requests = []

    def complete(self, messages, tools=None, *, max_tokens=1024):
        self.requests.append((deepcopy(messages), deepcopy(tools), max_tokens))
        step = next(self.steps)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def control(kind, **payload):
    return LLMResponse(content=json.dumps({"type": kind, **payload}), usage=Usage(2, 3))


def call(name="read_logs", arguments=None, call_id="c1", raw=""):
    return LLMResponse(tool_calls=[ToolCall(call_id, name, arguments if arguments is not None else {"job_id": "job-1"}, raw)], usage=Usage(3, 2))


def evidence_ids(messages):
    return [item["evidence_id"] for message in messages if message.role == "tool"
            for item in json.loads(message.content).get("evidence_items", [])]


def report(ids, root="Model selected explanation"):
    return control("report", report={"root_cause": root, "confidence": 0.7, "summary": "Observed failure",
                                    "claims": [{"text": "Worker lost heartbeat", "evidence_ids": ids}]})


def cited_report(messages):
    return report(evidence_ids(messages))


def registry():
    tools = ToolRegistry()
    schema = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}
    tools.register(Tool("read_logs", "Read logs", schema,
                        lambda job_id: ToolResult(True, {"line": "Worker lost heartbeat", "job": job_id}, truncated=True)))
    tools.register(Tool("read_metrics", "Read metrics", schema,
                        lambda job_id: ToolResult(True, {"memory": [12, 13, 12]})))
    return tools


def budget(**updates):
    return Budget(**{"max_steps": 12, "max_tokens": 10000, "deadline_at": NOW + timedelta(hours=1), **updates})


def run(gateway, tools=None, **kwargs):
    orchestrator = InvestigationOrchestrator(gateway, tools or registry(), now=lambda: NOW, **kwargs)
    return orchestrator, orchestrator.run("Why did job-1 fail?", case_id="case-example", budget=budget())


@pytest.mark.parametrize("selected", ["read_logs", "read_metrics"])
def test_model_dynamically_selects_tools_and_report_root_without_rule_inference(selected):
    tools = registry()
    snapshots = []
    gateway = ScriptedGateway(control("plan", plan=["Inspect observed telemetry"]), call(selected), cited_report)
    orchestrator, state = run(gateway, tools, persist=snapshots.append)
    assert [entry["tool"] for entry in tools.call_log] == [selected]
    assert state.phase == InvestigationPhase.COMPLETED
    assert state.report.root_cause == "Model selected explanation"
    assert state.mode == "replay"
    assert state.plan == ["Inspect observed telemetry"]
    assert state.report.claims[0].evidence_ids == state.evidence_ids
    assert state.budget.steps_used == 3 and state.budget.tokens_used == 15
    assert snapshots[0].phase == InvestigationPhase.INTAKE
    assert InvestigationPhase.VALIDATING in [s.phase for s in snapshots]
    assert snapshots[-1].phase == InvestigationPhase.COMPLETED
    assert snapshots[0].evidence_ids == []
    observation = orchestrator.evidence_store.get(state.evidence_ids[0])
    assert observation.source_tool == selected and observation.arguments == {"job_id": "job-1"}
    assert observation.truncated is (selected == "read_logs")
    assert state.session_id not in orchestrator.sessions


def test_model_records_competing_hypotheses_with_support_and_contradiction():
    def hypotheses(messages):
        ids = evidence_ids(messages)
        return control("hypotheses", hypotheses=[
            {"hypothesis_id": "h1", "title": "Node lost", "confidence": 0.7, "supporting_evidence_ids": ids},
            {"hypothesis_id": "h2", "title": "Memory exhausted", "confidence": 0.2, "contradicting_evidence_ids": ids},
        ])
    _, state = run(ScriptedGateway(call(), hypotheses, cited_report))
    assert [h.title for h in state.hypotheses] == ["Node lost", "Memory exhausted"]
    assert state.hypotheses[0].supporting_evidence_ids == state.evidence_ids
    assert state.hypotheses[1].contradicting_evidence_ids == state.evidence_ids


@pytest.mark.parametrize("bad", [call(arguments={}), call(arguments={"job_id": 9}), call(raw="{broken"), call(raw='[1]')])
def test_argument_errors_return_to_model_for_repair_without_evidence(bad):
    tools = registry()
    gateway = ScriptedGateway(bad, call(), cited_report)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.COMPLETED and len(state.evidence_ids) == 1
    error = json.loads(gateway.requests[1][0][-1].content)
    assert not error["ok"] and "argument" in error["error"].lower()
    assert tools.get("read_logs").calls == 1


def test_duplicate_calls_are_detected_by_canonical_arguments_not_provider_id():
    tools = registry()
    gateway = ScriptedGateway(call(), call(call_id="c2"), call("read_metrics", call_id="c1"), cited_report)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.COMPLETED
    assert [item["tool"] for item in tools.call_log] == ["read_logs", "read_metrics"]
    duplicate = json.loads(gateway.requests[2][0][-1].content)
    assert "duplicate" in duplicate["error"] and duplicate["evidence_ids"] == state.evidence_ids[:1]


@pytest.mark.parametrize("correct_retry", [True, False])
def test_unknown_citation_is_returned_as_issue_and_retried_only_once(correct_retry):
    gateway = ScriptedGateway(call(), report(["ev-forged"]), cited_report if correct_retry else report(["ev-still-forged"]))
    _, state = run(gateway)
    assert len(gateway.requests) == 3
    feedback = json.loads(gateway.requests[2][0][-1].content)
    assert feedback["error"] == "report_validation" and feedback["issues"][0]["code"] == "unknown_evidence"
    if correct_retry:
        assert state.phase == InvestigationPhase.COMPLETED
        assert state.report.claims[0].evidence_ids == state.evidence_ids
    else:
        assert state.phase == InvestigationPhase.STOPPED and state.stop_reason == "report_validation_failed"
        assert state.report is None


@pytest.mark.parametrize("limits, reason", [({"max_steps": 1}, "steps"), ({"max_tokens": 5}, "tokens")])
def test_budget_stops_with_partial_evidence_and_no_further_model_call(limits, reason):
    gateway = ScriptedGateway(call())
    orchestrator = InvestigationOrchestrator(gateway, registry(), now=lambda: NOW)
    state = orchestrator.run("Investigate", case_id=None, budget=budget(**limits))
    assert state.phase == InvestigationPhase.STOPPED and reason in state.stop_reason
    # Step budget counts model decisions; its last read result is retained.
    assert len(state.evidence_ids) == (1 if reason == "steps" else 0)
    assert len(gateway.requests) == 1


def test_deadline_expiring_during_model_call_prevents_tool_execution():
    clock = [NOW]
    def delayed(messages):
        clock[0] += timedelta(hours=2)
        return call()
    tools = registry()
    orchestrator = InvestigationOrchestrator(ScriptedGateway(delayed), tools, now=lambda: clock[0])
    state = orchestrator.run("Investigate", case_id=None, budget=budget())
    assert state.phase == InvestigationPhase.STOPPED and "deadline" in state.stop_reason
    assert tools.call_log == []


def test_exhausted_budget_does_not_invoke_model():
    gateway = ScriptedGateway()
    orchestrator = InvestigationOrchestrator(gateway, registry(), now=lambda: NOW)
    state = orchestrator.run("Investigate", case_id=None, budget=budget(max_steps=0))
    assert state.phase == InvestigationPhase.STOPPED and not gateway.requests


def test_backend_failure_is_stopped_with_partial_state_and_no_diagnosis_fallback():
    _, state = run(ScriptedGateway(call(), ModelBackendError("Offline", kind="transport")))
    assert state.phase == InvestigationPhase.STOPPED and state.report is None
    assert state.stop_reason == "model_backend:transport" and len(state.evidence_ids) == 1


def test_action_tool_is_never_executed_and_is_excluded_from_model_tools():
    tools = registry()
    tools.register(Tool("restart", "Restart", {}, lambda: pytest.fail("Action executed"), kind="action"))
    gateway = ScriptedGateway(call("restart", {}), call(), cited_report)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.COMPLETED
    assert "restart" not in [spec.name for spec in gateway.requests[0][1]]
    assert "action" in json.loads(gateway.requests[1][0][-1].content)["error"]


def test_proposed_action_is_retained_for_approval_without_execution():
    gateway = ScriptedGateway(control("proposed_action", action={"tool": "restart", "arguments": {"job_id": "job-1"}, "reason": "Recover worker"}))
    tools = registry()
    tools.register(Tool("restart", "Restart", tools.get("read_logs").parameters, lambda **kw: pytest.fail("Action executed"), kind="action"))
    orchestrator, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.AWAITING_APPROVAL and not tools.call_log
    assert orchestrator.sessions[state.session_id].proposed_action["tool"] == "restart"


def test_legacy_read_only_constructor_and_new_kind_enforce_same_permission():
    legacy = Tool("restart", "Restart", {}, lambda: ToolResult(True), read_only=False)
    assert legacy.kind == "action" and legacy.read_only is False and legacy.idempotent is False
    read = Tool("read", "Read", {}, lambda: ToolResult(True), kind="read", sensitivity="sensitive", idempotent=True)
    assert read.read_only and read.sensitivity == "sensitive" and read.idempotent
    with pytest.raises(ValueError):
        Tool("bad", "Bad", {}, lambda: ToolResult(True), kind="action", read_only=True)


def test_tool_evidence_items_keep_observations_and_authoritative_provenance():
    tools = registry()
    tools.get("read_logs").fn = lambda job_id: ToolResult(True, evidence_items=[
        Evidence("forged", {}, "Heartbeat missed", "10:00 heartbeat missed", truncated=True,
                 observed_time_range=("10:00", "10:01"), metadata={"rank": 2}),
    ])
    orchestrator, state = run(ScriptedGateway(call(), cited_report), tools)
    item = orchestrator.evidence_store.get(state.evidence_ids[0])
    assert item.source_tool == "read_logs" and item.arguments == {"job_id": "job-1"}
    assert item.excerpt == "10:00 heartbeat missed" and item.truncated
    assert item.observed_time_range == ("10:00", "10:01") and item.metadata == {"rank": 2}


def test_new_runs_have_isolated_evidence_and_terminal_context_is_released():
    gateway = ScriptedGateway(call(), cited_report, call("read_metrics"), cited_report)
    orchestrator = InvestigationOrchestrator(gateway, registry(), now=lambda: NOW)
    first = orchestrator.run("First", case_id="case-a", budget=budget())
    second = orchestrator.run("Second", case_id="case-b", budget=budget())
    assert first.session_id != second.session_id
    assert orchestrator.evidence_store.get(first.evidence_ids[0]) is None
    assert not orchestrator.sessions
    assert "First" not in str([m.content for m in gateway.requests[2][0]])
    with pytest.raises(InvestigationSessionError):
        orchestrator.advance(first)
    with pytest.raises(InvestigationSessionError):
        orchestrator.advance(InvestigationState("unknown", "Question", budget()))


def test_injected_store_does_not_allow_report_to_cite_another_session():
    store = EvidenceStore()
    foreign = store.add(Evidence("read_secret", {}, "Other session", "Other session"))
    gateway = ScriptedGateway(call(), report([foreign.evidence_id]), report([foreign.evidence_id]))
    _, state = run(gateway, evidence_store=store)
    assert state.phase == InvestigationPhase.STOPPED
    assert json.loads(gateway.requests[2][0][-1].content)["issues"][0]["code"] == "out_of_session_evidence"


@pytest.mark.parametrize("content", ['{}', '{"type":"plan","plan":"bad"}', 'not JSON'])
def test_malformed_control_is_recoverable_and_does_not_become_a_report(content):
    gateway = ScriptedGateway(LLMResponse(content=content), call(), cited_report)
    _, state = run(gateway)
    assert state.phase == InvestigationPhase.COMPLETED
    assert json.loads(gateway.requests[1][0][-1].content)["error"] == "invalid_control"


def test_empty_or_uncited_report_requires_model_repair():
    gateway = ScriptedGateway(call(), control("report", report={"root_cause": "Guess", "confidence": 1.0, "summary": "Guess", "claims": []}), cited_report)
    _, state = run(gateway)
    assert state.report.root_cause == "Model selected explanation"
    assert json.loads(gateway.requests[2][0][-1].content)["error"] == "report_validation"


def test_malformed_report_schema_is_retried_once_then_stopped():
    malformed = control("report", report={"confidence": "high"})
    gateway = ScriptedGateway(malformed, malformed)
    _, state = run(gateway)
    assert state.phase == InvestigationPhase.STOPPED and state.stop_reason == "report_validation_failed"
    assert len(gateway.requests) == 2
    assert json.loads(gateway.requests[1][0][-1].content)["issues"][0]["code"] == "invalid_report"


def test_incomplete_report_retry_does_not_get_an_extra_retry():
    gateway = ScriptedGateway(call(), report(["ev-forged"]), LLMResponse(content="{", finish_reason=FinishReason.LENGTH))
    _, state = run(gateway)
    assert state.phase == InvestigationPhase.STOPPED and state.stop_reason == "report_validation_failed"
    assert len(gateway.requests) == 3


def test_interleaved_sessions_keep_messages_and_evidence_separate_and_advance_one_step():
    gateway = ScriptedGateway(call(), call("read_metrics"), cited_report, cited_report)
    orchestrator = InvestigationOrchestrator(gateway, registry(), now=lambda: NOW)
    a = orchestrator.start("A question", case_id="case-a", budget=budget())
    b = orchestrator.start("B question", case_id="case-b", budget=budget())
    orchestrator.advance(a)
    orchestrator.advance(b)
    assert a.phase == b.phase == InvestigationPhase.INVESTIGATING
    orchestrator.advance(a)
    orchestrator.advance(b)
    assert a.report.claims[0].evidence_ids == a.evidence_ids
    assert b.report.claims[0].evidence_ids == b.evidence_ids
    assert set(a.evidence_ids).isdisjoint(b.evidence_ids)
    assert "B question" not in str([message.content for message in gateway.requests[2][0]])
    assert not orchestrator.sessions


def test_deadline_during_read_keeps_observation_but_stops_remaining_batch():
    clock = [NOW]
    tools = registry()
    def slow_read(job_id):
        clock[0] += timedelta(hours=2)
        return ToolResult(True, {"line": "Last available observation"})
    tools.get("read_logs").fn = slow_read
    gateway = ScriptedGateway(LLMResponse(tool_calls=[ToolCall("a", "read_logs", {"job_id": "job-1"}),
                                                    ToolCall("b", "read_metrics", {"job_id": "job-1"})]))
    orchestrator = InvestigationOrchestrator(gateway, tools, now=lambda: clock[0])
    state = orchestrator.run("Investigate", case_id=None, budget=budget())
    assert state.phase == InvestigationPhase.STOPPED and "deadline" in state.stop_reason
    assert len(state.evidence_ids) == 1
    assert [entry["tool"] for entry in tools.call_log] == ["read_logs"]


def test_unknown_and_failed_tools_produce_no_evidence_and_failed_call_can_retry():
    tools = registry()
    outcomes = iter([ToolResult(False, error="Transient read failure"), ToolResult(True, {"line": "Heartbeat lost"})])
    tools.get("read_logs").fn = lambda job_id: next(outcomes)
    gateway = ScriptedGateway(call("unknown"), call(), call(), cited_report)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.COMPLETED and len(state.evidence_ids) == 1
    assert tools.get("read_logs").calls == 2
    assert json.loads(gateway.requests[1][0][-1].content)["ok"] is False
    assert json.loads(gateway.requests[2][0][-1].content)["ok"] is False


def test_invalid_evidence_result_is_returned_to_model_without_crashing_session():
    tools = registry()
    tools.get("read_logs").fn = lambda job_id: ToolResult(True, {"measurement": float("nan")})
    gateway = ScriptedGateway(call(), call("read_metrics"), cited_report)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.COMPLETED and len(state.evidence_ids) == 1
    assert json.loads(gateway.requests[1][0][-1].content)["error"] == "invalid_evidence_result"


@pytest.mark.parametrize("action", [
    {"tool": "unknown", "arguments": {}, "reason": "Recover"},
    {"tool": "read_logs", "arguments": {"job_id": "job-1"}, "reason": "Recover"},
    {"tool": "restart", "arguments": {}, "reason": "Recover"},
])
def test_invalid_action_is_returned_for_correction(action):
    tools = registry()
    tools.register(Tool("restart", "Restart", tools.get("read_logs").parameters, lambda **kw: pytest.fail("Action executed"), kind="action"))
    gateway = ScriptedGateway(control("proposed_action", action=action), call(), cited_report)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.COMPLETED
    assert json.loads(gateway.requests[1][0][-1].content)["error"] == "invalid_action"


def test_unknown_hypothesis_evidence_is_not_silently_accepted():
    gateway = ScriptedGateway(control("hypotheses", hypotheses=[{
        "hypothesis_id": "h1", "title": "Guess", "supporting_evidence_ids": ["ev-forged"]}]), call(), cited_report)
    _, state = run(gateway)
    assert state.hypotheses == []
    assert json.loads(gateway.requests[1][0][-1].content)["error"] == "unknown_evidence"


def test_model_can_discover_action_schema_for_a_proposal_without_callable_action_tools():
    tools = registry()
    tools.register(Tool("restart", "Restart job", tools.get("read_logs").parameters,
                        lambda **kw: pytest.fail("Action executed"), kind="action", sensitivity="sensitive"))
    def propose(messages):
        intake = json.loads(messages[1].content)
        advertised = intake["available_actions"][0]
        assert advertised["name"] == "restart" and advertised["sensitivity"] == "sensitive"
        assert advertised["parameters"]["required"] == ["job_id"]
        return control("proposed_action", action={"tool": advertised["name"], "arguments": {"job_id": "job-1"}, "reason": "Recover"})
    gateway = ScriptedGateway(propose)
    _, state = run(gateway, tools)
    assert state.phase == InvestigationPhase.AWAITING_APPROVAL
    assert [spec.name for spec in gateway.requests[0][1]] == ["read_logs", "read_metrics"]
