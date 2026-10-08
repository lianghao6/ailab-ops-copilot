import httpx
import json
import pytest
from fastapi.testclient import TestClient

from ailab_ops.models import OpenAIModelGateway
from ailab_ops.runtime import build_runtime
from ailab_ops.serving.app import create_app
from test_v2_runtime import replay_settings


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("API tests must use an explicit offline transport")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.fixture
def client():
    with TestClient(create_app(build_runtime(replay_settings()))) as client:
        yield client


def create(client):
    response = client.post("/v2/investigations", json={"case_id": "case-gpu-assert"})
    assert response.status_code == 200, response.text
    return response.json()


def test_health_report_retrieval_and_tenant_isolation(client):
    assert client.get("/v2/health").json()["model_mode"] == "replay"
    result = create(client)
    path = "/v2/investigations/" + result["session_id"]
    assert client.get(path).json()["report"] == result["report"]
    assert client.get(path, params={"tenant_id": "other"}).status_code == 404


def test_replay_miss_is_typed_422_and_budget_is_a_stopped_result(client):
    result = client.post("/v2/investigations", json={"case_id": "case-gpu-assert", "question": "unrecorded"})
    assert result.status_code == 422
    assert result.json()["error"]["kind"] == "replay_miss"
    assert not result.json()["error"]["retryable"]
    result = client.post("/v2/investigations", json={"case_id": "case-gpu-assert", "max_steps": 0})
    assert result.status_code == 200
    assert result.json()["stop_reason"] == "budget_exhausted:steps"


def proposal(result):
    return {"tool": "annotate_incident", "arguments": {"case_id": "case-gpu-assert", "note": "Review diagnosis"},
            "reason": "Preserve investigation", "risk": "Incorrect annotation", "rollback": "Remove note",
            "evidence_ids": result["evidence_ids"]}


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_pending_approval_decision_and_simulation(client, decision):
    result = create(client)
    path = "/v2/investigations/" + result["session_id"]
    requested = client.post(path + "/approvals", json={"proposal": proposal(result)})
    assert requested.status_code == 200, requested.text
    approval_id = requested.json()["request_id"]
    assert client.get(path).json()["phase"] == "awaiting_approval"
    approval_path = "/v2/approvals/" + approval_id
    assert client.get(approval_path).json()["status"] == "pending"
    assert client.post(approval_path + "/execute", json={"actor": "operator"}).status_code == 409
    assert client.post(approval_path + "/approve", json={"actor": "reviewer", "tenant_id": "other"}).status_code == 404
    decided = client.post(approval_path + "/" + decision, json={"actor": "reviewer", "reason": "Reviewed"})
    assert decided.status_code == 200, decided.text
    if decision == "approve":
        executed = client.post(approval_path + "/execute", json={"actor": "operator"})
        assert executed.json()["simulated"] is True
        assert client.post(approval_path + "/execute", json={"actor": "operator"}).json() == executed.json()
    else:
        assert client.post(approval_path + "/execute", json={"actor": "operator"}).status_code == 409
    assert client.get(path).json()["phase"] == "completed"


def test_online_upstream_failure_is_retryable_and_breaker_opens():
    gateway = OpenAIModelGateway(base_url="https://model.invalid/v1", model="test", api_key="test-only",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": "unavailable"})), max_retries=0)
    rt = build_runtime(replay_settings(), gateway=gateway)
    with TestClient(create_app(rt)) as client:
        assert client.get("/v2/health").json()["model_mode"] == "online"
        for _ in range(6):
            response = client.post("/v2/investigations", json={"case_id": "case-gpu-assert"})
            assert response.status_code == 503
            assert response.json()["error"]["retryable"] is True
            assert "Retry-After" in response.headers
        assert response.json()["error"]["kind"] == "circuit_open"
        assert rt.gate.in_flight == 0
        assert client.get("/v2/health").json()["status"] == "degraded"


def test_online_adapter_executes_full_investigation_over_mock_transport():
    from ailab_ops.config import PROJECT_ROOT
    from ailab_ops.models.replay import canonical_request_hash, _message
    records = [json.loads(line) for line in (PROJECT_ROOT / "data/v2/replays/investigations.jsonl").read_text().splitlines()]
    responses = {canonical_request_hash([_message(m) for m in row["messages"]], row["available_tool_names"]): row["response"]
                 for row in records}

    def provider(request):
        body = json.loads(request.content)
        key = canonical_request_hash([_message(m) for m in body["messages"]], [t["function"]["name"] for t in body["tools"]])
        response = responses[key]
        message = {"role": "assistant", "content": response["content"]}
        if response["tool_calls"]:
            message["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call["arguments"])}} for call in response["tool_calls"]]
        return httpx.Response(200, json={"model": "test-online", "choices": [{"message": message,
            "finish_reason": response["finish_reason"]}], "usage": {"prompt_tokens": 120, "completion_tokens": 80}})

    gateway = OpenAIModelGateway(base_url="https://model.invalid/v1", model="test-online", api_key="test-only",
                                transport=httpx.MockTransport(provider), max_retries=0)
    rt = build_runtime(replay_settings(), gateway=gateway)
    with TestClient(create_app(rt)) as client:
        result = create(client)
        assert result["mode"] == "online" and result["phase"] == "completed"
        assert result["report"]["root_cause"] == "gpu_device_assert"
        assert result["budget"]["tokens_used"] == 1000
        assert rt.limiter.snapshot()["tenants"]["tenant-01"]["usd_today"] > 0


def test_api_rejects_missing_case_and_invalid_budgets(client):
    assert client.post("/v2/investigations", json={"case_id": "case-missing"}).status_code == 404
    assert client.post("/v2/investigations", json={"case_id": "../secret"}).status_code == 422
    assert client.post("/v2/investigations", json={"case_id": "case-gpu-assert", "max_steps": -1}).status_code == 422


@pytest.mark.parametrize("length,allowed", [(0, False), (1, True), (4000, True), (4001, False)])
def test_annotation_note_length_contract_at_api_boundary(client, length, allowed):
    result = create(client)
    action = proposal(result)
    action["arguments"]["note"] = "字" * length
    requested = client.post("/v2/investigations/" + result["session_id"] + "/approvals", json={"proposal": action})
    if not allowed:
        assert requested.status_code == 409
        assert "arguments.note" in requested.json()["error"]["message"]
        return
    assert requested.status_code == 200
    path = "/v2/approvals/" + requested.json()["request_id"]
    assert client.post(path + "/approve", json={"actor": "reviewer"}).status_code == 200
    executed = client.post(path + "/execute", json={"actor": "operator"})
    assert executed.status_code == 200
    assert executed.json()["simulated"] is True


@pytest.mark.parametrize("length", [0, 4001])
def test_approved_annotation_rechecks_current_string_limits_before_execution(client, length):
    result = create(client)
    orchestrator = client.app.state.rt.record(result["session_id"]).orchestrator
    note_schema = orchestrator.registry.get("annotate_incident").parameters["properties"]["note"]
    # A request approved under an older permissive policy must obey the new
    # published constraints when simulation execution is authorized again.
    note_schema.pop("minLength")
    note_schema.pop("maxLength")
    action = proposal(result)
    action["arguments"]["note"] = "x" * length
    requested = client.post("/v2/investigations/" + result["session_id"] + "/approvals", json={"proposal": action})
    assert requested.status_code == 200
    path = "/v2/approvals/" + requested.json()["request_id"]
    assert client.post(path + "/approve", json={"actor": "reviewer"}).status_code == 200
    note_schema.update(minLength=1, maxLength=4000)
    assert client.post(path + "/execute", json={"actor": "operator"}).status_code == 409
    assert client.get(path).json()["status"] == "approved"
    assert "execution_started" not in [event.event for event in orchestrator.approval_service.store.audit_events]
