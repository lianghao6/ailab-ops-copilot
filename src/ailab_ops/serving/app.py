"""Default V2 HTTP factory with an explicit V1-runtime compatibility adapter."""

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


def _workspace(app):
    assets = Path(__file__).parent / "static" / "v2"
    app.mount("/static/v2", StaticFiles(directory=assets), name="v2-static")

    @app.get("/", include_in_schema=False)
    async def workspace():
        return FileResponse(assets / "index.html", headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def workspace_headers(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/v2/"):
            response.headers["Content-Security-Policy"] = UI_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            if request.url.path.startswith("/static/v2/") and response.status_code in (200, 304):
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    return app


def create_app(runtime=None):
    if runtime is None or callable(getattr(runtime, "investigate", None)):
        from .v2 import create_v2_app
        return _workspace(create_v2_app(runtime))
    from ..legacy.runtime import Runtime
    if not isinstance(runtime, Runtime):
        raise ValueError("Expected a V2 investigation runtime or an explicit legacy Runtime")
    from .legacy_app import create_legacy_app
    return create_legacy_app(runtime)
