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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import __version__, errors, jobs, versions
from .api import actions, admin, alerts, assist, backups, proposals, conversations, detections, diagnostics, environments, evals, explorer, finops, incidents, investigations, knowledge, knowledge_explorer, logs, metrics, multi, skills
from .auth import routes as auth_routes
from .auth.deps import get_auth
from .auth.sessions import AuthContext
from .config import get_settings
from .connectors import registry
from .db import dispose, get_db, session_scope
from .observability import HTTP_LATENCY, HTTP_REQUESTS, bound, current_trace_ids, mark_failed, setup_logging, setup_tracing, start_metrics_server

log = logging.getLogger("moks.api")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


async def _follow_skills(stop: asyncio.Event) -> None:
    """Registers the image's skills, then follows the active generation and reports what this process runs."""
    from . import versions
    from .skills import registry as skill_registry

    try:
        if skill_registry.scan_on_start():
            await skill_registry.register_all()
    except Exception as exc:
        log.warning("skill scan failed", extra={"event": "skill_scan_failed", "errorType": type(exc).__name__})
    while not stop.is_set():
        try:
            await versions.report("api")
        except Exception as exc:
            log.warning("component report failed", extra={"event": "component_report_failed", "errorType": type(exc).__name__})
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    stop = asyncio.Event()
    worker_task = None
    start_metrics_server(s.metrics_port)
    if s.embedded_worker:
        from .orchestrator.worker import run_worker

        worker_task = asyncio.create_task(run_worker(stop))
    heartbeat = asyncio.create_task(_follow_skills(stop))
    log.info("api started", extra={"event": "api_started"})
    yield
    stop.set()
    heartbeat.cancel()
    if worker_task:
        await asyncio.wait([worker_task], timeout=10)
    await registry.close_all()
    await dispose()


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging(s.log_level, s.otel_service_name)
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
        rid = errors.request_id(request)
        request.state.request_id = rid
        start = time.monotonic()
        with bound(request_id=rid):
            try:
                response: Response = await call_next(request)
            except Exception as exc:
                response = errors.internal_error(request, exc)
            route = request.scope.get("route")
            template = getattr(route, "path", None) or ("spa" if request.url.path.startswith(s.base_path) else "other")
            if template.startswith("/") and request.url.path.startswith(api + "/") and not template.startswith(api):
                template = api + template  # FastAPI 0.16x leaves the including router's prefix out of route.path
            dur = time.monotonic() - start
            auth = getattr(request.state, "auth", None)
            if template not in ("/healthz", "/readyz"):
                HTTP_REQUESTS.labels(request.method, template, str(response.status_code)).inc()
                HTTP_LATENCY.labels(request.method, template).observe(dur)
                if template.startswith(api):
                    log_request(request, response, template, dur, auth)
        trace_id, _ = current_trace_ids()
        response.headers["X-Request-ID"] = rid
        if trace_id:
            response.headers["X-Trace-ID"] = trace_id
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
    for extra in (incidents, knowledge, knowledge_explorer, finops, actions, diagnostics, assist, alerts, evals, skills, multi, metrics, logs, detections, conversations, backups, proposals):
        router.include_router(extra.router)

    errors.install(app)

    @app.exception_handler(jobs.QueueFull)
    async def queue_full(_request, exc: jobs.QueueFull):
        return errors.response(429, str(exc), "queue_full", headers={"Retry-After": "30"})

    @router.get("/health", tags=["health"])
    async def public_health() -> dict[str, str]:
        return {"status": "ok", **versions.build()}

    @router.get("/system/versions", tags=["health"])
    async def system_versions(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
        return await versions.running(db)

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


def log_request(request: Request, response: Response, template: str, dur: float, auth) -> None:
    """One line per API request. 5xx is an error, 4xx a warning except the routine ones (session
    probes, missing pages), so `level=warn` lists real client trouble."""
    status = response.status_code
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 and status not in (401, 404) else logging.INFO
    extra = {"event": "http_request", "route": template, "method": request.method, "status": status, "durationMs": int(dur * 1000),
             "outcome": "failure" if status >= 500 else "success"}
    if auth is not None:
        extra["user_id"] = auth.principal.user_id
    if status >= 400:
        extra["errorCode"] = errors.code_for(status)
    log.log(level, "request", extra=extra)
    if status >= 500:
        from opentelemetry import trace

        mark_failed(trace.get_current_span(), f"http_{status}", errors.code_for(status))


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
