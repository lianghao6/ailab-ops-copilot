"""Real orchestration/approval events are recorded safely and best effort."""

from datetime import datetime, timezone
import json

import pytest

from test_orchestrator import NOW, ScriptedGateway, budget, call, cited_report, registry
from ailab_ops.investigation import InvestigationOrchestrator, InvestigationPhase
from ailab_ops.models.protocol import ModelBackendError
from ailab_ops.tools.registry import Tool
from ailab_ops.approvals import SimulatedActionHandler


def test_recursive_redaction_snapshots_and_jsonl_keep_usage_but_remove_credentials(tmp_path):
    from ailab_ops.observability import TraceEvent, TraceRecorder
    recorder = TraceRecorder(sensitive_fields=["private_id"], secrets=["raw-credential"],
                             now=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc))
    payload = {"Authorization": "Bearer abc123", "nested": [{"api_key": "key123", "token": "tok123",
        "client_secret": "secret123", "private_id": "private123"}],
        "error": "Failed using raw-credential; Authorization: Bearer embedded456",
        "usage": {"tokens_in": 12, "tokens_out": 4}}
    recorder.record(TraceEvent("s1", "error", "investigating", payload=payload))
    payload["usage"]["tokens_in"] = 999
    captured = recorder.events
    captured[0].payload["usage"]["tokens_in"] = 777
    assert recorder.events[0].payload["usage"] == {"tokens_in": 12, "tokens_out": 4}
    path = tmp_path / "trace.jsonl"
    recorder.write_jsonl(path)
    text = path.read_text()
    for secret in ["abc123", "key123", "tok123", "secret123", "private123", "raw-credential", "embedded456"]:
        assert secret not in text
    assert json.loads(text)["timestamp"] == "2026-10-08T00:00:00+00:00"


def test_orchestrator_records_state_model_tools_evidence_usage_and_validation_retry():
    from ailab_ops.observability import TraceRecorder
    from test_orchestrator import report
    recorder = TraceRecorder()
    orchestrator = InvestigationOrchestrator(ScriptedGateway(call(), report(["ev-forged"]), cited_report),
                                             registry(), event_sink=recorder.record, now=lambda: NOW)
    state = orchestrator.run("Why?", case_id="case-test", budget=budget())
    assert state.phase == InvestigationPhase.COMPLETED
    events = recorder.events
    assert all(event.session_id == state.session_id for event in events)
    assert {event.kind for event in events} >= {"state", "model", "tool", "evidence", "error", "policy"}
    assert any(event.phase == "validating" for event in events)
    assert any(event.retry == 1 for event in events if event.kind == "model")
    assert any(event.model == "scripted-test" for event in events if event.kind == "model")
    assert any(event.tool == "read_logs" for event in events if event.kind == "tool")
    assert any(event.evidence_ids == state.evidence_ids for event in events if event.kind == "evidence")
    assert sum(event.usage.get("tokens_in", 0) for event in events) == 7
    assert sum(event.usage.get("tokens_out", 0) for event in events) == 8


def test_model_error_and_failing_trace_callback_do_not_break_cleanup_or_expose_exception_message():
    def broken(event):
        raise RuntimeError("authorization=secret-should-not-leak")
    orchestrator = InvestigationOrchestrator(ScriptedGateway(ModelBackendError("api_key=backend-secret", kind="timeout")),
                                             registry(), event_sink=broken, now=lambda: NOW)
    state = orchestrator.run("Why?", case_id=None, budget=budget())
    assert state.phase == InvestigationPhase.STOPPED and not orchestrator.sessions
    assert orchestrator.tracing_errors
    assert "secret" not in str(orchestrator.tracing_errors)
    assert all(entry["error"] == "RuntimeError" for entry in orchestrator.tracing_errors)


@pytest.mark.parametrize("broken_sink", [False, True])
def test_approval_events_include_identity_and_sink_failure_never_changes_execution(broken_sink):
    from ailab_ops.observability import TraceRecorder
    recorder = TraceRecorder()
    def sink(event):
        if broken_sink and event.kind == "approval":
            raise RuntimeError("token=approval-secret")
        recorder.record(event)
    tools = registry()
    tools.register(Tool("restart", "Restart", tools.get("read_logs").parameters,
        lambda **kw: pytest.fail("Real action ran"), kind="action", sensitivity="sensitive"))
    orchestrator = InvestigationOrchestrator(ScriptedGateway(call(), cited_report), tools, event_sink=sink, now=lambda: NOW)
    state = orchestrator.run("Why?", case_id=None, budget=budget())
    service = orchestrator.approval_service
    service.register_simulated("restart", SimulatedActionHandler({"ok": True}))
    request = orchestrator.request_action(state.session_id, {"tool": "restart", "arguments": {"job_id": "job-1"},
        "reason": "Recover", "risk": "State lost", "rollback": "Restore", "evidence_ids": state.evidence_ids})
    service.approve(request.request_id, actor="reviewer")
    assert service.execute(request.request_id, actor="operator")["simulated"] is True
    assert orchestrator.states[state.session_id].phase == InvestigationPhase.COMPLETED
    if broken_sink:
        assert service.tracing_errors
        assert "approval-secret" not in str(service.tracing_errors)
        assert any(event.event == "tracing_failed" for event in service.store.audit_events)
    else:
        events = [event for event in recorder.events if event.kind == "approval"]
        assert [event.event for event in events] == ["created", "approved", "execution_started", "executed"]
        assert all(event.approval_id == request.request_id and event.session_id == state.session_id for event in events)


def test_redaction_covers_quoted_text_custom_fields_and_json_inside_strings():
    from ailab_ops.observability import TraceEvent, TraceRecorder
    recorder = TraceRecorder(sensitive_fields=["private_id"])
    recorder.record(TraceEvent("s1", "error", payload={
        "message": 'apiKey="quoted-secret" private_id=custom-secret password=pass-secret',
        "json": '{"nested": {"Authorization": "Bearer json-secret"}}',
        "unknown": object()}))
    encoded = json.dumps(recorder.events[0].to_dict())
    for secret in ["quoted-secret", "custom-secret", "pass-secret", "json-secret"]:
        assert secret not in encoded
    assert recorder.events[0].payload["unknown"] == "[UNSUPPORTED]"


def test_iterable_redaction_configuration_also_applies_to_prefixed_text_keys():
    from ailab_ops.observability import TraceEvent, TraceRecorder
    recorder = TraceRecorder(sensitive_fields=iter(["private_id"]))
    recorder.record(TraceEvent("s1", "error", payload={
        "message": "private_id=hidden-id client_secret=hidden-client"}))
    encoded = json.dumps(recorder.events[0].to_dict())
    assert "hidden-id" not in encoded and "hidden-client" not in encoded
