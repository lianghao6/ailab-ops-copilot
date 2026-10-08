"""Approval tests exercise permissions, immutable snapshots and real lifecycle effects."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from ailab_ops.approvals import ApprovalError, ApprovalService, SimulatedActionHandler
from ailab_ops.policy import PolicyContext, PolicyEngine
from ailab_ops.tools.registry import Tool, ToolResult
from test_orchestrator import ScriptedGateway, budget, call, cited_report, control, registry
from ailab_ops.investigation import InvestigationOrchestrator, InvestigationPhase


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def setup():
    tools = registry()
    tools.register(Tool("restart", "Restart worker", tools.get("read_logs").parameters,
                        lambda **kw: pytest.fail("Real action handler executed"), kind="action",
                        sensitivity="sensitive", idempotent=False))
    context = PolicyContext("session-a", reason="Recover lost worker", risk="Worker state may be lost",
                            rollback="Restore saved checkpoint", evidence_ids=("ev-current",))
    clock = [NOW]
    policy = PolicyEngine(tools, evidence_lookup=lambda sid, eid:
                          object() if (sid, eid) == ("session-a", "ev-current") else None)
    service = ApprovalService(policy, now=lambda: clock[0])
    service.register_simulated("restart", SimulatedActionHandler({"worker": "restarted"}))
    return tools, context, clock, service


def create(service, context):
    return service.create(context, "restart", {"job_id": "job-1"})


def test_policy_automatically_allows_read_but_never_directly_allows_action():
    tools, context, _, service = setup()
    read = service.policy.authorize(PolicyContext("session-a"), tools.get("read_logs"), {"job_id": "job-1"})
    action = service.policy.authorize(context, tools.get("restart"), {"job_id": "job-1"})
    assert read.outcome == "allow_read"
    assert action.outcome == "require_approval"
    assert tools.get("restart").calls == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_direct_and_registry_action_calls_are_guarded_before_side_effects(legacy):
    from ailab_ops.tools.registry import ToolRegistry
    effects = []
    def action():
        effects.append("mutated")
        return ToolResult(True, {"changed": True})
    tool = Tool("restart", "Restart", {}, action,
                **({"read_only": False} if legacy else {"kind": "action"}))
    tools = ToolRegistry()
    tools.register(tool)
    direct = tool()
    dispatched = tools.call("restart", {}, step=3)
    assert not direct.ok and direct.error == "approval_required"
    assert not dispatched.ok and dispatched.error == "approval_required"
    assert effects == []
    assert tools.call_log[-1]["error"] == "approval_required" and tools.call_log[-1]["step"] == 3


def test_registry_guard_does_not_delegate_permission_enforcement_to_action_subclass():
    from ailab_ops.tools.registry import ToolRegistry
    effects = []
    class CustomTool(Tool):
        def __call__(self, **kwargs):
            effects.append("mutated")
            return ToolResult(True)
    tools = ToolRegistry()
    tools.register(CustomTool("restart", "Restart", {}, lambda: ToolResult(True), kind="action"))
    result = tools.call("restart", {})
    assert not result.ok and result.error == "approval_required"
    assert effects == [] and tools.call_log[-1]["ok"] is False


def test_legacy_agent_loop_cannot_execute_action_even_when_model_requests_it():
    from ailab_ops.agent import Agent
    from ailab_ops.tools.registry import ToolRegistry
    from test_agent import ScriptedClient, _tool_turn
    effects = []
    def action():
        effects.append("mutated")
        return ToolResult(True)
    tools = ToolRegistry()
    tools.register(Tool("restart", "Restart", {}, action, read_only=False))
    agent = Agent(ScriptedClient([_tool_turn("restart", {})]), tools)
    result = agent.run("Restart the job")
    assert effects == []
    assert result.steps[0].error == "approval_required" and result.steps[0].ok is False
    assert tools.call_log[0]["error"] == "approval_required"


@pytest.mark.parametrize("field,value", [("reason", " "), ("risk", ""), ("rollback", ""),
                                          ("evidence_ids", ()), ("evidence_ids", ("ev-forged",)),
                                          ("session_id", "session-b")])
def test_policy_denies_missing_safety_context_or_out_of_session_evidence(field, value):
    tools, context, _, service = setup()
    invalid = replace(context, **{field: value})
    decision = service.policy.authorize(invalid, tools.get("restart"), {"job_id": "job-1"})
    assert decision.outcome == "deny"
    with pytest.raises(ApprovalError):
        create(service, invalid)
    assert service.store.audit_events[-1].event == "policy_denied"
    assert tools.get("restart").calls == 0


@pytest.mark.parametrize("arguments", [{}, {"job_id": 7}, {"job_id": "job-1", "extra": True},
                                      {"job_id": float("nan")}, []])
def test_invalid_action_arguments_cannot_enter_approval(arguments):
    _, context, _, service = setup()
    with pytest.raises(ApprovalError):
        service.create(context, "restart", arguments)
    assert service.store.requests == ()


def test_unknown_action_and_forged_tool_are_denied_and_read_cannot_create_approval():
    tools, context, _, service = setup()
    with pytest.raises(ApprovalError):
        service.create(context, "unknown", {})
    forged = Tool("restart", "Forged read", {}, lambda: ToolResult(True))
    assert service.policy.authorize(context, forged, {}).outcome == "deny"
    with pytest.raises(ApprovalError):
        service.create(context, "read_logs", {"job_id": "job-1"})
    assert [e.event for e in service.store.audit_events] == ["policy_denied", "policy_denied"]


def test_request_contains_review_context_and_defaults_to_fifteen_minute_expiry():
    _, context, _, service = setup()
    request = create(service, context)
    assert request.status == "pending"
    assert request.expires_at == NOW + timedelta(minutes=15)
    assert (request.reason, request.risk, request.rollback, request.evidence_ids) == (
        "Recover lost worker", "Worker state may be lost", "Restore saved checkpoint", ("ev-current",))
    assert request.sensitivity == "sensitive" and not request.idempotent
    assert request.to_dict()["expires_at"] == "2026-10-08T00:15:00+00:00"
    assert service.store.audit_events[0].event == "created"


def test_pending_execution_is_denied_then_explicit_approval_executes_simulation_once():
    tools, context, _, service = setup()
    request = create(service, context)
    with pytest.raises(ApprovalError):
        service.execute(request.request_id, actor="operator")
    service.approve(request.request_id, actor="operator")
    result = service.execute(request.request_id, actor="operator")
    assert result == {"simulated": True, "worker": "restarted"}
    assert service.store.get(request.request_id).status == "executed"
    assert service.store.get(request.request_id).approved_by == "operator"
    assert service.execute(request.request_id, actor="operator") == result
    assert tools.get("restart").calls == 0 and not tools.call_log
    assert [e.event for e in service.store.audit_events] == [
        "created", "execution_denied", "approved", "execution_started", "executed", "execution_replayed"]


def test_rejected_request_cannot_be_approved_or_executed():
    _, context, _, service = setup()
    request = create(service, context)
    rejected = service.reject(request.request_id, actor="operator", reason="Do not restart")
    assert rejected.status == "rejected" and rejected.rejection_reason == "Do not restart"
    for operation in (service.approve, service.execute):
        with pytest.raises(ApprovalError):
            operation(request.request_id, actor="operator")
    assert [e.event for e in service.store.audit_events] == [
        "created", "rejected", "approval_denied", "execution_denied"]


@pytest.mark.parametrize("approved", [False, True])
def test_expiry_blocks_both_approval_and_execution_at_deadline(approved):
    _, context, clock, service = setup()
    request = create(service, context)
    if approved:
        service.approve(request.request_id, actor="operator")
    clock[0] += timedelta(minutes=15)
    with pytest.raises(ApprovalError):
        service.execute(request.request_id, actor="operator")
    with pytest.raises(ApprovalError):
        service.approve(request.request_id, actor="operator")
    assert service.store.get(request.request_id).status == "expired"
    assert [e.event for e in service.store.audit_events].count("expired") == 1


def test_handlers_are_declarative_simulations_and_cannot_be_replaced_after_registration():
    _, _, _, service = setup()
    with pytest.raises(ApprovalError):
        service.register_simulated("restart", lambda **kw: {"simulated": True})
    class Impostor(SimulatedActionHandler):
        def execute(self, arguments):
            pytest.fail("Overridden handler executed")
    with pytest.raises(ApprovalError):
        service.register_simulated("restart", Impostor({}))
    with pytest.raises(ApprovalError):
        service.register_simulated("unknown", SimulatedActionHandler({}))
    with pytest.raises(ApprovalError):
        service.register_simulated("restart", SimulatedActionHandler({"changed": True}))


def test_external_mutation_cannot_edit_requests_results_or_append_only_audit_records():
    _, context, _, service = setup()
    request = create(service, context)
    request.arguments["job_id"] = "tampered"
    service.approve(request.request_id, actor="operator")
    result = service.execute(request.request_id, actor="operator")
    result["worker"] = "tampered"
    events = service.store.audit_events
    events[0].details["tool"] = "tampered"
    assert service.store.get(request.request_id).arguments == {"job_id": "job-1"}
    assert service.execute(request.request_id, actor="operator")["worker"] == "restarted"
    assert service.store.audit_events[0].details["tool"] == "restart"
    assert isinstance(events, tuple)


def test_concurrent_duplicate_execution_reuses_one_stored_result():
    _, context, _, service = setup()
    request = create(service, context)
    service.approve(request.request_id, actor="operator")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.execute(request.request_id, actor="operator"), range(8)))
    assert results == [{"simulated": True, "worker": "restarted"}] * 8
    assert [e.event for e in service.store.audit_events].count("execution_started") == 1
    assert [e.event for e in service.store.audit_events].count("execution_replayed") == 7


def test_action_is_reauthorized_before_execution_if_evidence_or_tool_changes():
    tools, context, _, service = setup()
    request = create(service, context)
    service.approve(request.request_id, actor="operator")
    tools.get("restart").kind = "read"
    with pytest.raises(ApprovalError):
        service.execute(request.request_id, actor="operator")
    assert service.store.get(request.request_id).result is None


def test_evidence_removal_after_approval_prevents_execution():
    tools, context, _, _ = setup()
    evidence = {("session-a", "ev-current"): object()}
    service = ApprovalService(PolicyEngine(tools, evidence_lookup=lambda sid, eid: evidence.get((sid, eid))), now=lambda: NOW)
    service.register_simulated("restart", SimulatedActionHandler({}))
    request = create(service, context)
    service.approve(request.request_id, actor="operator")
    evidence.clear()
    with pytest.raises(ApprovalError):
        service.execute(request.request_id, actor="operator")
    assert service.store.get(request.request_id).result is None


def test_unregistered_handler_and_missing_actor_cannot_execute():
    tools, context, _, _ = setup()
    service = ApprovalService(PolicyEngine(tools, evidence_lookup=lambda sid, eid: object()), now=lambda: NOW)
    request = create(service, context)
    with pytest.raises(ApprovalError):
        service.approve(request.request_id, actor=" ")
    service.approve(request.request_id, actor="operator")
    with pytest.raises(ApprovalError):
        service.execute(request.request_id, actor="operator")
    assert service.store.get(request.request_id).status == "approved"


def diagnosis():
    tools, _, _, _ = setup()
    orchestrator = InvestigationOrchestrator(ScriptedGateway(call(), cited_report), tools, now=lambda: NOW)
    state = orchestrator.run("Failure", case_id=None, budget=budget())
    proposal = {"tool": "restart", "arguments": {"job_id": "job-1"}, "reason": "Recover worker",
                "risk": "Lose worker state", "rollback": "Restore checkpoint", "evidence_ids": state.evidence_ids}
    orchestrator.approval_service.register_simulated("restart", SimulatedActionHandler({"worker": "restarted"}))
    return orchestrator, state, proposal


def test_completed_diagnosis_can_enter_approval_and_execution_completes_with_result():
    orchestrator, state, proposal = diagnosis()
    report = deepcopy(state.report)
    request = orchestrator.request_action(state.session_id, proposal)
    assert orchestrator.states[state.session_id].phase == InvestigationPhase.AWAITING_APPROVAL
    assert orchestrator.sessions[state.session_id].approval_request_id == request.request_id
    orchestrator.approval_service.approve(request.request_id, actor="operator")
    assert orchestrator.states[state.session_id].phase == InvestigationPhase.AWAITING_APPROVAL
    result = orchestrator.approval_service.execute(request.request_id, actor="operator")
    assert result["simulated"] is True
    assert orchestrator.states[state.session_id].phase == InvestigationPhase.COMPLETED
    assert orchestrator.states[state.session_id].report == report
    assert state.session_id not in orchestrator.sessions
    assert orchestrator.get_evidence(state.session_id, state.evidence_ids[0]) is not None


def test_rejection_keeps_diagnosis_and_evidence_in_completed_investigation():
    orchestrator, state, proposal = diagnosis()
    request = orchestrator.request_action(state.session_id, proposal)
    orchestrator.approval_service.reject(request.request_id, actor="operator", reason="Too risky")
    saved = orchestrator.states[state.session_id]
    assert saved.phase == InvestigationPhase.COMPLETED and saved.report == state.report
    assert saved.stop_reason == "approval_rejected"
    assert orchestrator.get_evidence(state.session_id, state.evidence_ids[0]) is not None


def test_revised_proposal_after_rejection_clears_previous_terminal_reason():
    orchestrator, state, proposal = diagnosis()
    first = orchestrator.request_action(state.session_id, proposal)
    orchestrator.approval_service.reject(first.request_id, actor="operator", reason="Too risky")
    proposal["risk"] = "Worker checkpoint has been verified"
    second = orchestrator.request_action(state.session_id, proposal)
    saved = orchestrator.states[state.session_id]
    assert second.request_id != first.request_id
    assert saved.phase == InvestigationPhase.AWAITING_APPROVAL and saved.stop_reason is None
    assert saved.report == state.report


def test_expiry_sweep_completes_awaiting_investigation_and_preserves_report():
    orchestrator, state, proposal = diagnosis()
    clock = [NOW]
    orchestrator.approval_service._now = lambda: clock[0]
    request = orchestrator.request_action(state.session_id, proposal)
    clock[0] += timedelta(minutes=15)
    expired = orchestrator.approval_service.expire()
    assert [r.request_id for r in expired] == [request.request_id]
    saved = orchestrator.states[state.session_id]
    assert saved.phase == InvestigationPhase.COMPLETED and saved.report == state.report
    assert saved.stop_reason == "approval_expired"
    assert orchestrator.approval_service.expire() == ()


def test_awaiting_persist_can_resolve_live_approval_and_terminal_persist_can_resolve_result():
    orchestrator, state, proposal = diagnosis()
    observed = []
    def persist(saved):
        if saved.phase == InvestigationPhase.AWAITING_APPROVAL:
            session = orchestrator.sessions[saved.session_id]
            observed.append(orchestrator.approval_service.store.get(session.approval_request_id).status)
        elif saved.phase == InvestigationPhase.COMPLETED:
            observed.append(orchestrator.approval_service.store.get(request.request_id).result)
    orchestrator._persist = persist
    request = orchestrator.request_action(state.session_id, proposal)
    orchestrator.approval_service.approve(request.request_id, actor="operator")
    orchestrator.approval_service.execute(request.request_id, actor="operator")
    assert observed == ["pending", {"simulated": True, "worker": "restarted"}]


def test_proposal_without_diagnosis_and_unknown_session_evidence_are_rejected():
    orchestrator, state, proposal = diagnosis()
    proposal["evidence_ids"] = ["ev-forged"]
    with pytest.raises(ApprovalError):
        orchestrator.request_action(state.session_id, proposal)
    assert orchestrator.states[state.session_id].phase == InvestigationPhase.COMPLETED
    no_report = InvestigationOrchestrator(ScriptedGateway(), orchestrator.registry, now=lambda: NOW)
    intake = no_report.start("Undiagnosed", case_id=None, budget=budget())
    with pytest.raises(ApprovalError):
        no_report.request_action(intake.session_id, proposal)


@pytest.mark.parametrize("field", ["tool", "arguments", "reason", "risk", "rollback", "evidence_ids"])
def test_explicit_action_protocol_requires_complete_review_context(field):
    orchestrator, state, proposal = diagnosis()
    del proposal[field]
    with pytest.raises(ApprovalError):
        orchestrator.request_action(state.session_id, proposal)
    assert orchestrator.states[state.session_id].phase == InvestigationPhase.COMPLETED
    assert orchestrator.approval_service.store.requests == ()


def test_incomplete_model_proposal_returns_invalid_action_for_repair():
    gateway = ScriptedGateway(control("proposed_action", action={"tool": "restart", "arguments": {}, "reason": "Recover"}), call(), cited_report)
    tools, _, _, _ = setup()
    orchestrator = InvestigationOrchestrator(gateway, tools, now=lambda: NOW)
    state = orchestrator.run("Failure", case_id=None, budget=budget())
    assert json.loads(gateway.requests[1][0][-1].content)["error"] == "invalid_action"
    assert state.phase == InvestigationPhase.COMPLETED


def test_complete_model_proposal_before_diagnosis_is_rejected():
    def proposal(messages):
        from test_orchestrator import evidence_ids
        return control("proposed_action", action={"tool": "restart", "arguments": {"job_id": "job-1"},
            "reason": "Recover", "risk": "Lose state", "rollback": "Restore", "evidence_ids": evidence_ids(messages)})
    tools, _, _, _ = setup()
    gateway = ScriptedGateway(call(), proposal, cited_report)
    orchestrator = InvestigationOrchestrator(gateway, tools, now=lambda: NOW)
    state = orchestrator.run("Failure", case_id=None, budget=budget())
    assert state.phase == InvestigationPhase.COMPLETED
    assert json.loads(gateway.requests[2][0][-1].content)["error"] == "invalid_action"
    assert orchestrator.approval_service.store.requests == ()
