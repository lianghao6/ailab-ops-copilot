"""Investigation and simulated approval HTTP API."""

import asyncio
import math
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ailab_ops.approvals import ApprovalError
from ailab_ops.runtime import build_runtime
from ailab_ops.v2_runtime import TIMELINE_DEFAULT_LIMIT, TIMELINE_MAX_LIMIT
from .limits import LimitExceeded


class Identity(BaseModel):
    # Simulation identities, not authentication. Verify at a trusted gateway
    # before adapting this example for real tenants or real actions.
    tenant_id: str = Field("tenant-01", min_length=1, max_length=100)
    user_id: str = Field("analyst", min_length=1, max_length=100)


class InvestigationBody(Identity):
    case_id: str = Field(pattern=r"^case-[a-z0-9]+(?:-[a-z0-9]+)*$")
    question: str | None = Field(None, min_length=1, max_length=4000)
    max_steps: int | None = Field(None, ge=0, le=64)
    max_tokens: int = Field(32768, ge=0, le=1_000_000)
    deadline_s: float = Field(120, ge=0, le=3600, allow_inf_nan=False)


class ProposalBody(Identity):
    proposal: dict


class DecisionBody(Identity):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = ""


def create_v2_app(runtime=None):
    @asynccontextmanager
    async def lifespan(app):
        rt = runtime if runtime is not None else build_runtime()
        app.state.rt = rt
        app.state.svc = rt

        async def maintain():
            while True:
                await asyncio.sleep(1)
                rt.gate.try_promote_starved()
                for record in tuple(rt.records.values()):
                    record.orchestrator.approval_service.expire()

        maintenance = asyncio.create_task(maintain())
        try:
            yield
        finally:
            maintenance.cancel()
            try:
                await maintenance
            except asyncio.CancelledError:
                pass
            rt.close()

    app = FastAPI(title="AILab Ops Copilot V2", version="2.0", lifespan=lifespan)

    @app.exception_handler(KeyError)
    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"error": {"kind": "not_found", "retryable": False}})

    @app.exception_handler(ApprovalError)
    async def conflict(request, exc):
        error = {"error": {"kind": "approval_conflict", "message": str(exc), "retryable": False}}
        return JSONResponse(status_code=409, content=request.app.state.rt.present(error))

    @app.exception_handler(LimitExceeded)
    async def limited(request, exc):
        return JSONResponse(status_code=429, content={"error": {"kind": exc.kind,
            "retryable": exc.retry_after_s is not None, "retry_after_s": exc.retry_after_s}},
            headers=_retry_headers(exc.retry_after_s))

    @app.post("/v2/investigations")
    async def investigate(body: InvestigationBody, request: Request):
        result = await request.app.state.rt.investigate(**body.model_dump())
        error = result["error"]
        status = 200
        if error:
            status = 422 if error["kind"] == "replay_miss" else (
                429 if error["kind"].startswith("tenant_") else 503 if error["retryable"] else 502)
        headers = _retry_headers((error["retry_after_s"] or 1) if error and error["retryable"] else None)
        return JSONResponse(result, status_code=status, headers=headers)

    @app.get("/v2/investigations/{session_id}")
    async def investigation(session_id: str, request: Request, tenant_id: str = "tenant-01"):
        return request.app.state.rt.get_investigation(session_id, tenant_id)

    @app.get("/v2/investigations/{session_id}/evidence")
    async def evidence(session_id: str, request: Request, tenant_id: str = "tenant-01"):
        return request.app.state.rt.get_evidence(session_id, tenant_id)

    @app.get("/v2/investigations/{session_id}/timeline")
    async def timeline(session_id: str, request: Request, tenant_id: str = "tenant-01",
                       limit: int = Query(TIMELINE_DEFAULT_LIMIT, ge=1, le=TIMELINE_MAX_LIMIT)):
        return request.app.state.rt.get_timeline(session_id, tenant_id, limit=limit)

    @app.get("/v2/investigations/{session_id}/approvals")
    async def approvals(session_id: str, request: Request, tenant_id: str = "tenant-01"):
        return request.app.state.rt.get_approvals(session_id, tenant_id)

    @app.post("/v2/investigations/{session_id}/approvals")
    async def propose(session_id: str, body: ProposalBody, request: Request):
        return request.app.state.rt.request_action(session_id, body.proposal, body.tenant_id)

    @app.get("/v2/approvals/{approval_id}")
    async def approval(approval_id: str, request: Request, tenant_id: str = "tenant-01"):
        return request.app.state.rt.get_approval(approval_id, tenant_id)

    @app.post("/v2/approvals/{approval_id}/approve")
    async def approve(approval_id: str, body: DecisionBody, request: Request):
        service = request.app.state.rt.approval_service(approval_id, body.tenant_id)
        return request.app.state.rt.present(service.approve(approval_id, actor=body.actor).to_dict())

    @app.post("/v2/approvals/{approval_id}/reject")
    async def reject(approval_id: str, body: DecisionBody, request: Request):
        service = request.app.state.rt.approval_service(approval_id, body.tenant_id)
        return request.app.state.rt.present(service.reject(approval_id, actor=body.actor, reason=body.reason).to_dict())

    @app.post("/v2/approvals/{approval_id}/execute")
    async def execute(approval_id: str, body: DecisionBody, request: Request):
        service = request.app.state.rt.approval_service(approval_id, body.tenant_id)
        return request.app.state.rt.present(service.execute(approval_id, actor=body.actor))

    @app.get("/v2/health")
    @app.get("/v1/health")
    @app.get("/health")
    async def health(request: Request):
        return request.app.state.rt.health()

    return app


def _retry_headers(seconds):
    return {"Retry-After": str(max(1, math.ceil(seconds)))} if seconds is not None and math.isfinite(seconds) else {}
