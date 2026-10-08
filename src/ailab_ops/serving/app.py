"""HTTP API.

Endpoints, and why each exists:

    POST /v1/diagnose          the main synchronous endpoint
    POST /v1/diagnose/stream   the same, as server-sent events
    GET  /v1/health            liveness/readiness, including saturation signals
    GET  /v1/stats             counters, gate, cache, sessions, tool usage
    GET  /v1/jobs              browse the world (for the UI)
    GET  /v1/jobs/{job_id}     one job, ground truth withheld
    GET  /                     a small single-file web UI

几处值得推敲的 API 设计取舍：

* **`/v1/health` distinguishes liveness from saturation.** Returning 200 while
  every upstream slot is busy is correct for liveness and useless for
  operations, so the response carries `status: degraded` and the gate numbers.
  A load balancer reading only the status code would keep sending traffic to a
  saturated replica; the counters are there so it can be told not to.
* **Errors are typed and carry a retry hint.** A 429 with `retry_after_s` lets
  a client back off correctly. A bare 500 does not, and produces the retry
  storm that the gate exists to prevent.
* **Identity comes from the request body in this build.** In production it
  comes from a verified header the gateway sets; the code marks the seam
  (`resolve_identity`) rather than quietly trusting the body. The deployment is
  deliberately simulated, and quietly shipping an auth bypass would be wrong.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import get_settings
from ..runtime import Runtime, build_runtime
from .limits import LimitExceeded
from .service import CopilotService, DiagnosisRequest, ServiceError

# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class DiagnoseBody(BaseModel):
    question: str = Field(..., min_length=3, max_length=4000)
    session_id: str | None = None
    tenant_id: str = "tenant-01"
    user_id: str = "analyst"
    priority: int = 1
    batch_id: str | None = None
    max_steps: int | None = Field(None, ge=1, le=24)
    no_cache: bool = False


class PrepareBatchBody(BaseModel):
    tenant_id: str
    batch_id: str


class LimitsBody(BaseModel):
    """Declared at module scope, not inside the factory.

    A Pydantic model defined inside `create_app` cannot be resolved from the
    stringified annotations that `from __future__ import annotations` produces,
    so FastAPI falls back to treating the parameter as a *query* parameter. The
    symptom is a confusing "body: field required" 422 on a POST that is clearly
    sending a body. Worth knowing: it is a non-obvious interaction between two
    things that are each individually fine.
    """

    user_qps: float | None = None
    tenant_concurrency: int | None = None
    tenant_token_per_min: int | None = None
    tenant_cost_per_day_usd: float | None = None


class GateBody(BaseModel):
    max_concurrency: int | None = None
    queue_maxsize: int | None = None
    queue_timeout_s: float | None = None


def resolve_identity(request: Request, body_tenant: str, body_user: str) -> tuple[str, str]:
    """Where identity comes from.

    In this build it is the request body, which is fine because the whole
    build has no real tenants. In production this is
    the function you replace with header verification against your gateway's
    signed identity, and the reason it is a named function rather than inline
    is so that the substitution is a one-line change with an obvious home.
    """
    return body_tenant, body_user


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------


def create_app(runtime: Runtime | None = None) -> FastAPI:
    # Legacy APIs are available only with an explicitly supplied V1 runtime.
    if runtime is None or not isinstance(runtime, Runtime):
        from .v2 import create_v2_app
        return create_v2_app(runtime)
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Building the runtime is the slow part (~0.5s: generate/load the
        # world, build the index, construct the tools). It happens once, at
        # startup, not per request -- see runtime.py for why.
        rt = runtime or build_runtime(settings)
        app.state.rt = rt
        app.state.svc = CopilotService(rt, settings)
        reaper = asyncio.create_task(_queue_reaper(app.state.svc))
        app.state.reaper = reaper
        yield
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass

    app = FastAPI(
        title="AILab Ops Copilot",
        version="0.1.0",
        description=(
            "面向训练与评测平台的企业级 AIOps 助手，诊断失败 job 的根因。"
        ),
        lifespan=lifespan,
    )

    def svc(request: Request) -> CopilotService:
        s = getattr(request.app.state, "svc", None)
        if s is None:
            raise HTTPException(status_code=503, detail="service is still starting")
        return s

    # ---- main endpoints -------------------------------------------------

    @app.post("/v1/diagnose")
    async def diagnose(body: DiagnoseBody, request: Request) -> JSONResponse:
        s = svc(request)
        tenant, user = resolve_identity(request, body.tenant_id, body.user_id)
        req = DiagnosisRequest(
            question=body.question,
            tenant_id=tenant,
            user_id=user,
            session_id=body.session_id,
            priority=_priority(body.priority),
            batch_id=body.batch_id,
            max_steps=body.max_steps,
            no_cache=body.no_cache,
        )
        try:
            resp = await s.diagnose(req)
        except ServiceError as exc:
            headers = {"Retry-After": str(int(exc.retry_after_s))} if exc.retry_after_s else None
            return JSONResponse(
                status_code=exc.status,
                content={"error": exc.kind, "message": str(exc), "retry_after_s": exc.retry_after_s},
                headers=headers,
            )
        except LimitExceeded as exc:
            return JSONResponse(
                status_code=429,
                content={"error": exc.kind, "message": exc.detail, "retry_after_s": exc.retry_after_s},
            )
        return JSONResponse(content=resp.to_dict())

    @app.post("/v1/diagnose/stream")
    async def diagnose_stream(body: DiagnoseBody, request: Request) -> StreamingResponse:
        s = svc(request)
        tenant, user = resolve_identity(request, body.tenant_id, body.user_id)
        req = DiagnosisRequest(
            question=body.question,
            tenant_id=tenant,
            user_id=user,
            session_id=body.session_id,
            priority=_priority(body.priority),
            batch_id=body.batch_id,
            max_steps=body.max_steps,
            stream=True,
            no_cache=body.no_cache,
        )

        async def gen():
            # A heartbeat keeps intermediaries from closing an idle stream and,
            # more importantly, distinguishes "the model is thinking" from
            # "the connection is dead" for the client.
            try:
                async for ev in s.diagnose_stream(req):
                    yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False, default=str)}\n\n"
            except asyncio.CancelledError:
                # The client hung up. Releasing happens inside the service's
                # finally blocks; the only thing to do here is stop quietly
                # rather than logging a spurious error.
                raise
            except Exception as exc:
                payload = {"kind": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
                yield f"event: error\ndata: {json.dumps(payload)}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Nginx-style proxies buffer responses by default, which
                # destroys streaming. This header is the single most common
                # missing piece in a "streaming does not work in prod" bug.
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/health")
    async def health(request: Request) -> JSONResponse:
        s = getattr(request.app.state, "svc", None)
        if s is None:
            return JSONResponse(status_code=503, content={"status": "starting"})
        h = s.health()
        return JSONResponse(content=h, status_code=200 if h["status"] == "ok" else 200)

    @app.get("/v1/stats")
    async def stats(request: Request) -> JSONResponse:
        return JSONResponse(content=svc(request).stats())

    @app.post("/v1/admin/force-degrade")
    async def force_degrade(request: Request, on: bool = True) -> JSONResponse:
        """故障注入：把模型服务置为不可用。

        Exposed as an endpoint rather than a config flag so a class can flip it
        while watching the UI, which is how the degradation ladder becomes
        concrete instead of theoretical.
        """
        s = svc(request)
        s.force_degrade = on
        return JSONResponse(content={"force_degrade": s.force_degrade})

    @app.post("/v1/admin/prepare-batch")
    async def prepare_batch(body: PrepareBatchBody, request: Request) -> JSONResponse:
        s = svc(request)
        s.limiter.register_batch(body.tenant_id, body.batch_id)
        return JSONResponse(content={"tenant_id": body.tenant_id, "batch_id": body.batch_id,
                                     "registered": True})

    # ---- benchmark controls --------------------------------------------

    @app.post("/v1/admin/limits")
    async def set_limits(body: LimitsBody, request: Request) -> JSONResponse:
        """Adjust the ceilings, so a benchmark can isolate one control at a time."""
        s = svc(request)
        snap = s.limiter.set_limits(
            user_qps=body.user_qps,
            tenant_concurrency=body.tenant_concurrency,
            tenant_token_per_min=body.tenant_token_per_min,
            tenant_cost_per_day_usd=body.tenant_cost_per_day_usd,
        )
        return JSONResponse(content=snap)

    @app.post("/v1/admin/reset-counters")
    async def reset_counters(request: Request) -> JSONResponse:
        """Zero the per-tenant usage windows between benchmark scenarios."""
        s = svc(request)
        s.limiter.reset_counters()
        return JSONResponse(content={"reset": True})

    @app.post("/v1/admin/reset-gate")
    async def reset_gate(request: Request) -> JSONResponse:
        """Zero the gate's statistics (not its capacity) between scenarios."""
        s = svc(request)
        s.gate.reset_stats()
        return JSONResponse(content=s.gate.stats().to_dict())

    @app.post("/v1/admin/gate")
    async def set_gate(body: GateBody, request: Request) -> JSONResponse:
        """Resize the upstream gate.

        Refuses to act while the gate is busy rather than silently doing
        something surprising; the caller is expected to retry after it drains.
        Also clears the breakers, because a smaller gate means more timeouts,
        and a breaker left open from the previous scenario would mask it.
        """
        s = svc(request)
        try:
            cfg = s.gate.reconfigure(
                max_concurrency=body.max_concurrency,
                queue_maxsize=body.queue_maxsize,
                queue_timeout_s=body.queue_timeout_s,
            )
        except RuntimeError as exc:
            return JSONResponse(status_code=409, content={"error": "gate_busy", "message": str(exc)})
        return JSONResponse(content=cfg)

    @app.post("/v1/admin/cache-clear")
    async def cache_clear(request: Request) -> JSONResponse:
        svc(request).cache.clear()
        return JSONResponse(content={"cleared": True})

    # ---- read-only browsing --------------------------------------------

    @app.get("/v1/jobs")
    async def list_jobs(
        request: Request,
        status: str | None = None,
        team_id: str | None = None,
        cluster: str | None = None,
        queue: str | None = None,
        difficulty: str | None = None,
        limit: int = 30,
    ) -> JSONResponse:
        rt: Runtime = request.app.state.rt
        rows = rt.world.jobs
        if status:
            rows = [j for j in rows if j.status.upper() == status.upper()]
        if team_id:
            rows = [j for j in rows if j.team_id == team_id]
        if cluster:
            rows = [j for j in rows if j.cluster == cluster]
        if queue:
            rows = [j for j in rows if j.queue == queue]
        if difficulty:
            rows = [j for j in rows if (j.difficulty or "") == difficulty]
        rows = sorted(rows, key=lambda j: j.submitted_at, reverse=True)[: max(1, min(limit, 200))]
        return JSONResponse(
            content={
                "n": len(rows),
                "jobs": [
                    {
                        "job_id": j.job_id,
                        "name": j.name,
                        "team_id": j.team_id,
                        "status": j.status,
                        "exit_code": j.exit_code,
                        "cluster": j.cluster,
                        "queue": j.queue,
                        "duration_s": j.duration_s,
                        "difficulty": j.difficulty,
                        "failed": j.status != "SUCCEEDED",
                    }
                    for j in rows
                ],
            }
        )

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        rt: Runtime = request.app.state.rt
        res = rt.registry.call("get_job", {"job_id": job_id})
        if not res.ok:
            raise HTTPException(status_code=404, detail=res.error)
        return JSONResponse(content=res.data)

    @app.get("/v1/incidents")
    async def incidents(request: Request, limit: int = 20) -> JSONResponse:
        rt: Runtime = request.app.state.rt
        rows = sorted(rt.world.incidents, key=lambda i: i.opened_at, reverse=True)[:limit]
        return JSONResponse(
            content={
                "n": len(rows),
                "incidents": [
                    {
                        "incident_id": i.incident_id,
                        "job_id": i.job_id,
                        "opened_at": i.opened_at,
                        "severity": i.severity,
                        "affected": i.root_cause_name,
                        "category": i.category,
                        "resolution": i.resolution,
                    }
                    for i in rows
                ],
            }
        )

    @app.get("/v1/meta")
    async def meta(request: Request) -> JSONResponse:
        rt: Runtime = request.app.state.rt
        return JSONResponse(
            content={
                "company": rt.world.company,
                "seed": rt.world.seed,
                "boot_ms": round(rt.boot_ms, 1),
                "source": rt.source,
                "summary": rt.world.summary().__dict__,
                "provenance": (
                    "全部实体、job、日志和指标均由 faults.yaml 本地生成，可由 seed 完全复现。"
                ),
            }
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(_UI_HTML)

    return app


def _priority(v: int):
    from .gate import Priority

    try:
        return Priority(int(v))
    except ValueError:
        return Priority.NORMAL


async def _queue_reaper(s: CopilotService) -> None:
    """Promote long-waiting low-priority waiters, periodically.

    Kept as a background task rather than done lazily on acquire, because a
    waiter that is never acquired-from again would otherwise never be promoted
    -- which is exactly the starvation case the promotion rule exists to fix.
    """
    while True:
        try:
            await asyncio.sleep(1.0)
            s.gate.try_promote_starved()
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


# --------------------------------------------------------------------------
# A single-file web UI. No build step, no dependencies.
# --------------------------------------------------------------------------


def _load_ui() -> str:
    p = Path(__file__).with_name("static") / "index.html"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "<h1>AILab Ops Copilot</h1><p>UI asset missing; the API is at /docs.</p>"


_UI_HTML = _load_ui()
