"""Presentation contracts use real replay sessions and offline online transport."""

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ailab_ops.models import OpenAIModelGateway
from ailab_ops.observability import TraceEvent
from ailab_ops.runtime import build_runtime
from ailab_ops.serving.app import create_app
from test_v2_api import create, proposal
from test_v2_runtime import replay_settings


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Presentation tests must never contact a live model")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.fixture
def client():
    with TestClient(create_app(build_runtime(replay_settings()))) as client:
        yield client


def test_detail_and_evidence_preserve_plan_hypotheses_sources_and_citations(client):
    result = create(client)
    path = "/v2/investigations/" + result["session_id"]
    detail = client.get(path).json()
    evidence = client.get(path + "/evidence")
    assert evidence.status_code == 200
    assert detail["mode"] == "replay"
    assert detail["phase"] == "completed"
    assert detail["plan"] and detail["hypotheses"]
    assert detail["approvals"] == detail["approval_state"] == []
    assert evidence.json() == {"session_id": result["session_id"], "evidence": detail["evidence"]}
    ids = {item["evidence_id"] for item in detail["evidence"]}
    assert ids == set(detail["evidence_ids"])
    for item in detail["evidence"]:
        assert item["source_tool"] and item["arguments"]
        assert "metadata" in item and "observed_time_range" in item
    for claim in detail["report"]["claims"]:
        assert set(claim["evidence_ids"]) <= ids
    assert detail == client.get(path).json()  # Browser reload, same retained session.
    assert detail["timeline"] == client.get(path + "/timeline").json()


def test_online_detail_retains_mode_on_upstream_failure():
    gateway = OpenAIModelGateway(base_url="https://model.invalid/v1", model="offline-online",
        api_key="test-only", max_retries=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "down"})))
    with TestClient(create_app(build_runtime(replay_settings(), gateway=gateway))) as client:
        response = client.post("/v2/investigations", json={"case_id": "case-gpu-assert"})
        assert response.status_code == 503
        path = "/v2/investigations/" + response.json()["session_id"]
        detail = client.get(path).json()
        assert detail["mode"] == "online" and detail["phase"] == "stopped"
        assert detail["timeline"]["events"]
        assert client.get(path + "/evidence").json()["evidence"] == []


def test_timeline_is_bounded_chronological_redacted_and_session_scoped(client):
    result = create(client)
    session_id = result["session_id"]
    rt = client.app.state.rt
    recorder = rt.record(session_id).recorder
    for index in range(520):
        recorder.record(TraceEvent(session_id, "model", event=f"event-{index}", payload={
            "api_key": "do-not-disclose", "authorization": "Bearer never-public",
            "reasoning_content": "private-model-thought", "hidden_labels": {"answer": "eval-secret"},
            "outcome": "public-status"}))
    recorder.record(TraceEvent("another-session", "state", event="foreign-private-event"))
    path = "/v2/investigations/" + session_id + "/timeline"
    response = client.get(path)
    assert response.status_code == 200
    timeline = response.json()
    assert timeline["session_id"] == session_id and timeline["trace_id"] == result["trace_id"]
    assert timeline["returned_events"] == len(timeline["events"]) == 100
    assert timeline["total_events"] > 520 and timeline["truncated"] is True
    assert timeline["events"][0]["event"] == "event-420"
    assert timeline["events"][-1]["event"] == "event-519"
    assert timeline["events"][-1]["payload"]["outcome"] == "public-status"
    public = response.text
    for secret in ("do-not-disclose", "never-public", "private-model-thought", "eval-secret", "foreign-private-event"):
        assert secret not in public
    assert len(client.get(path, params={"limit": 500}).json()["events"]) == 500
    for limit in (0, 501):
        assert client.get(path, params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("decision,status", [("reject", "rejected"), ("execute", "executed"), ("expire", "expired")])
def test_approval_collection_tracks_pending_and_terminal_states(client, decision, status):
    result = create(client)
    path = "/v2/investigations/" + result["session_id"]
    request = client.post(path + "/approvals", json={"proposal": proposal(result)}).json()
    approval_path = "/v2/approvals/" + request["request_id"]
    pending = client.get(path + "/approvals")
    assert pending.status_code == 200
    assert pending.json() == {"session_id": result["session_id"], "approvals": [request]}
    if decision == "expire":
        service = client.app.state.rt.approval_service(request["request_id"])
        service._now = lambda: service.store.get(request["request_id"]).expires_at + timedelta(seconds=1)
    elif decision == "execute":
        assert client.post(approval_path + "/approve", json={"actor": "reviewer"}).status_code == 200
        assert client.get(path + "/approvals").json()["approvals"][0]["status"] == "approved"
        assert client.post(approval_path + "/execute", json={"actor": "operator"}).status_code == 200
    else:
        assert client.post(approval_path + "/reject", json={"actor": "reviewer", "reason": "Reviewed risk"}).status_code == 200
    terminal = client.get(path + "/approvals").json()["approvals"][0]
    assert terminal["status"] == status
    detail = client.get(path).json()
    assert detail["approvals"] == detail["approval_state"] == [terminal]
    assert detail["phase"] == "completed"
    assert client.get(approval_path).json() == terminal


@pytest.mark.parametrize("suffix", ["", "/evidence", "/timeline", "/approvals"])
def test_every_presentation_route_hides_unknown_and_other_tenant_sessions(client, suffix):
    result = create(client)
    path = "/v2/investigations/" + result["session_id"] + suffix
    assert client.get(path, params={"tenant_id": "other"}).status_code == 404
    assert client.get("/v2/investigations/missing" + suffix).status_code == 404


def test_presentation_sanitizes_evidence_approvals_and_returns_independent_copies(client):
    result = create(client)
    session_id = result["session_id"]
    rt = client.app.state.rt
    orchestrator = rt.record(session_id).orchestrator
    archived = orchestrator._evidence_archive[session_id][result["evidence_ids"][0]]
    archived.metadata.update(api_key="evidence-secret", hidden_labels={"answer": "label-secret"},
                             reasoning_content="private-evidence-thought")
    action = proposal(result)
    action["arguments"]["note"] = "authorization=Bearer approval-secret"
    # Represents an approval created by the orchestrator itself, not the POST API.
    request = orchestrator.request_action(session_id, action)
    path = "/v2/investigations/" + session_id
    detail = rt.get_investigation(session_id)
    assert detail["approvals"]
    assert client.get("/v2/approvals/" + request.request_id).status_code == 200
    assert client.get("/v2/approvals/" + request.request_id, params={"tenant_id": "other"}).status_code == 404
    for secret in ("evidence-secret", "label-secret", "private-evidence-thought", "approval-secret"):
        assert secret not in json.dumps(detail)
        assert secret not in client.get(path + "/evidence").text
        assert secret not in client.get(path + "/approvals").text
        assert secret not in client.get("/v2/approvals/" + request.request_id).text
    detail["plan"].clear()
    detail["evidence"][0]["metadata"]["api_key"] = "tampered"
    detail["approvals"][0]["arguments"]["note"] = "tampered"
    detail["timeline"]["events"][0]["payload"].clear()
    fresh = rt.get_investigation(session_id)
    assert fresh["plan"]
    assert fresh["evidence"][0]["metadata"]["api_key"] != "tampered"
    assert fresh["approvals"][0]["arguments"]["note"] != "tampered"
    assert fresh["timeline"]["events"][0]["payload"]
    assert archived.metadata["api_key"] == "evidence-secret"


def test_presentation_redacts_nested_json_evaluation_fields_and_thinking(client):
    result = create(client)
    record = client.app.state.rt.record(result["session_id"])
    record.recorder.record(TraceEvent(result["session_id"], "model", event="received", payload={
        "debug": json.dumps({"expected_findings": ["hidden-finding"],
            "expected_actions": ["hidden-action"], "thinking": "private-thinking",
            "public": "visible"})}))
    path = "/v2/investigations/" + result["session_id"]
    for suffix in ("", "/timeline"):
        response = client.get(path + suffix)
        assert response.status_code == 200
        assert "visible" in response.text
        for hidden in ("hidden-finding", "hidden-action", "private-thinking"):
            assert hidden not in response.text


@pytest.mark.parametrize("operation", ["approve", "reject", "execute"])
def test_approval_operations_hide_unknown_and_other_tenant_requests(client, operation):
    result = create(client)
    request = client.post("/v2/investigations/" + result["session_id"] + "/approvals",
        json={"proposal": proposal(result)}).json()
    body = {"actor": "reviewer", "reason": "reviewed", "tenant_id": "other"}
    assert client.post("/v2/approvals/" + request["request_id"] + "/" + operation, json=body).status_code == 404
    assert client.post("/v2/approvals/missing/" + operation, json=body).status_code == 404
    assert client.get("/v2/approvals/" + request["request_id"]).json()["status"] == "pending"


@pytest.mark.parametrize("field,value", [
    ("reason", {"nested": "configured-approval-key"}),
    ("api_key", "extra-api-key-value"),
    ("private_reasoning", "private-approval-thought"),
    ("hidden_labels", {"expected_findings": ["private-expected-finding"], "label": "private-label-value"}),
])
def test_invalid_proposal_conflict_does_not_echo_validation_input(field, value):
    settings = replace(replay_settings(), llm_api_key="configured-approval-key")
    with TestClient(create_app(build_runtime(settings))) as client:
        result = create(client)
        action = proposal(result)
        action[field] = value
        response = client.post("/v2/investigations/" + result["session_id"] + "/approvals",
            json={"proposal": action})
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["kind"] == "approval_conflict" and error["retryable"] is False
        assert "action." + field in error["message"]
        assert ("string_type" if field == "reason" else "extra_forbidden") in error["message"]
        for secret in ("configured-approval-key", "extra-api-key-value", "private-approval-thought",
                       "private-expected-finding", "private-label-value", "input_value", "input_type"):
            assert secret not in response.text
        # The original exception also must be safe; HTTP redaction alone cannot
        # protect logs or another caller of the source parsing boundary.
        from ailab_ops.approvals import ApprovalError
        with pytest.raises(ApprovalError) as caught:
            client.app.state.rt.request_action(result["session_id"], action)
        for secret in ("configured-approval-key", "extra-api-key-value", "private-approval-thought",
                       "private-expected-finding", "private-label-value", "input_value", "input_type"):
            assert secret not in str(caught.value)


def test_invalid_note_length_conflict_retains_useful_argument_path(client):
    result = create(client)
    action = proposal(result)
    action["arguments"]["note"] = ""
    response = client.post("/v2/investigations/" + result["session_id"] + "/approvals",
        json={"proposal": action})
    assert response.status_code == 409
    assert response.json()["error"]["kind"] == "approval_conflict"
    assert "arguments.note" in response.json()["error"]["message"]
    assert "minLength" in response.json()["error"]["message"]


def test_conflict_response_redacts_configured_secrets_in_safe_field_paths():
    settings = replace(replay_settings(), llm_api_key="unknown-field-configured-key")
    with TestClient(create_app(build_runtime(settings))) as client:
        result = create(client)
        action = proposal(result)
        action["unknown-field-configured-key"] = "unexpected-value"
        response = client.post("/v2/investigations/" + result["session_id"] + "/approvals",
            json={"proposal": action})
        assert response.status_code == 409
        assert "unknown-field-configured-key" not in response.text
        assert "unexpected-value" not in response.text
