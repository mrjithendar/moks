"""FastAPI application.

Path layout (all under MOKS_BASE_PATH, default /moks; the ingress forwards the full path):

  /moks/                 React SPA (index.html for every client-side route, so deep links refresh)
  /moks/assets/*         hashed static assets (immutable caching)
  /moks/api/*            JSON API, SSE streams, auth callbacks
  /moks/api/docs         OpenAPI UI when MOKS_API_DOCS=true
  /healthz, /readyz      probes (not routed by the ingress, which only matches /moks)
  :9464/metrics          Prometheus metrics on a separate port
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text

from . import __version__
from .api import admin, environments, explorer, investigations
from .auth import routes as auth_routes
from .config import get_settings
from .connectors import registry
from .db import dispose, session_scope
from .observability import HTTP_LATENCY, HTTP_REQUESTS, setup_logging, setup_tracing, start_metrics_server

log = logging.getLogger("moks.api")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    stop = asyncio.Event()
    worker_task = None
    start_metrics_server(s.metrics_port)
    if s.embedded_worker:
        from .orchestrator.worker import run_worker

        worker_task = asyncio.create_task(run_worker(stop))
    log.info("api started", extra={"event": "api_started"})
    yield
    stop.set()
    if worker_task:
        await asyncio.wait([worker_task], timeout=10)
    await registry.close_all()
    await dispose()


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging(s.log_level)
    api = s.api_prefix
    app = FastAPI(
        title="MOKS", version=__version__, lifespan=lifespan,
        docs_url=f"{api}/docs" if s.api_docs else None, redoc_url=None,
        openapi_url=f"{api}/openapi.json" if s.api_docs else None,
        swagger_ui_oauth2_redirect_url=None,
    )
    setup_tracing(s.otel_service_name, app)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.monotonic()
        try:
            response: Response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"event": "http_unhandled", "request_id": rid})
            response = JSONResponse({"detail": "Internal error"}, status_code=500)
        route = request.scope.get("route")
        template = getattr(route, "path", None) or ("spa" if request.url.path.startswith(s.base_path) else "other")
        dur = time.monotonic() - start
        if template not in ("/healthz", "/readyz"):
            HTTP_REQUESTS.labels(request.method, template, str(response.status_code)).inc()
            HTTP_LATENCY.labels(request.method, template).observe(dur)
            if template.startswith(api):
                log.info("request", extra={"event": "http_request", "route": template, "method": request.method,
                                           "status": response.status_code, "durationMs": int(dur * 1000), "request_id": rid})
        response.headers["X-Request-ID"] = rid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith(f"{api}/docs"):
            response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith(api):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    router = APIRouter(prefix=api)
    router.include_router(auth_routes.router)
    router.include_router(environments.router)
    router.include_router(investigations.router)
    router.include_router(explorer.router)
    router.include_router(admin.router)

    @router.get("/health", tags=["health"])
    async def public_health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def api_not_found(rest: str) -> None:
        raise HTTPException(404, "Not found")

    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        try:
            async with session_scope() as db:
                await asyncio.wait_for(db.execute(text("SELECT 1")), 2)
        except Exception as exc:
            return JSONResponse({"status": "unavailable", "database": type(exc).__name__}, status_code=503)
        return JSONResponse({"status": "ok"})

    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    s = get_settings()
    base = s.base_path
    static = Path(s.static_dir) if s.static_dir else None
    index = static / "index.html" if static else None

    if base:
        @app.get(base, include_in_schema=False)
        async def base_redirect() -> RedirectResponse:
            return RedirectResponse(base + "/", status_code=308)

    @app.get(base + "/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if index is None or not index.exists():
            return JSONResponse({"detail": "Web UI is not built into this image (MOKS_STATIC_DIR)."}, status_code=404)
        if path:
            candidate = (static / path).resolve()
            if static.resolve() in candidate.parents and candidate.is_file():
                immutable = path.startswith("assets/")
                return FileResponse(candidate, headers={
                    "Cache-Control": "public, max-age=31536000, immutable" if immutable else "public, max-age=300"})
            if path.startswith("assets/"):
                raise HTTPException(404, "Not found")  # never answer a missing asset with HTML
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = None  # created by the CLI (moks serve) or uvicorn factory: "moks.main:create_app"
