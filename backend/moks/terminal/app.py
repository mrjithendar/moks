"""The moks-terminal process: sign-in checks and terminal routes, and nothing else from the portal."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from .. import jobs
from ..config import get_settings
from ..db import dispose, session_scope
from ..observability import setup_logging, setup_tracing, start_metrics_server
from . import api, service
from .profiles import profiles

log = logging.getLogger("moks.terminal")
REAP_SECONDS = 15


async def _reaper(stop: asyncio.Event) -> None:
    pods = service.pods_factory()
    try:
        while not stop.is_set():
            try:
                await service.reap(pods)
            except Exception as exc:
                log.warning("terminal cleanup failed", extra={"event": "terminal_reap_failed", "errorType": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), REAP_SECONDS)
            except TimeoutError:
                pass
    finally:
        await pods.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    start_metrics_server(s.metrics_port)
    log.info("terminal gateway started", extra={"event": "terminal_started", "profiles": len(profiles())})  # also proves the profiles parse
    stop = asyncio.Event()
    task = asyncio.create_task(_reaper(stop))
    yield
    stop.set()
    await asyncio.wait([task], timeout=10)
    await dispose()


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging(s.log_level)
    app = FastAPI(title="MOKS terminal gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    setup_tracing(f"{s.otel_service_name}-terminal", app)

    @app.middleware("http")
    async def headers(request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.exception_handler(jobs.QueueFull)
    async def queue_full(_request, exc: jobs.QueueFull):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    router = APIRouter(prefix=s.api_prefix)
    router.include_router(api.router)
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

    return app
