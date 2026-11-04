"""One error shape for every API failure, and the handlers that produce it.

Every error response is

    {"detail": "<message>",
     "error": {"code": "not_found", "message": "<message>", "status": 404,
               "request_id": "<id>", "trace_id": "<32 hex, when tracing is on>",
               "fields": [{"field": "body.name", "message": "Field required"}]}}   # 422 only

`detail` stays a plain string so existing clients keep working. `request_id` is the same id
the response carries in X-Request-ID and every log line of the request carries; when tracing
is on it is the trace id, so the one reference a user reports opens the trace in Jaeger and
finds the lines in Loki (trace_id is on the Alloy allowlist).

Unexpected exceptions never reach the client: they become a 500 with a generic message and
the reference, and the stack goes to the log.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .connectors.base import ConnectorError
from .observability import current_trace_ids, log_context

log = logging.getLogger("moks.api")

CODES = {
    400: "bad_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    409: "conflict", 410: "gone", 413: "too_large", 415: "unsupported_media_type", 422: "invalid_request",
    423: "locked", 428: "precondition_required", 429: "rate_limited", 500: "internal", 501: "not_implemented",
    502: "upstream_error", 503: "unavailable", 504: "upstream_timeout",
}

# A connector failure that escapes a route: the message is written for users (see ConnectorError).
CONNECTOR_STATUS = {
    "config": 503, "auth": 502, "unavailable": 503, "timeout": 504, "bad_request": 400, "not_found": 404,
    "too_large": 413, "forbidden": 403, "rate_limited": 429,
}

INTERNAL_MESSAGE = "Something went wrong inside MOKS. The error has been logged; quote the reference if you report it."


def code_for(status: int) -> str:
    return CODES.get(status) or ("client_error" if status < 500 else "server_error")


def body(status: int, message: str, code: str | None = None, *, detail: Any = None, fields: list[dict] | None = None) -> dict:
    trace_id, _ = current_trace_ids()
    err: dict[str, Any] = {"code": code or code_for(status), "message": message, "status": status,
                           "request_id": log_context().get("request_id")}
    if trace_id:
        err["trace_id"] = trace_id
    if fields:
        err["fields"] = fields
    return {"detail": message if detail is None else detail, "error": err}


def response(status: int, message: str, code: str | None = None, *, headers: dict | None = None, **kw: Any) -> JSONResponse:
    return JSONResponse(body(status, message, code, **kw), status_code=status, headers=headers)


def _location(loc: tuple | list) -> str:
    return ".".join(str(p) for p in loc) or "request"


def _validation_fields(exc: RequestValidationError) -> list[dict]:
    out = []
    for e in exc.errors()[:20]:
        out.append({"field": _location(e.get("loc", ())), "message": str(e.get("msg", "invalid value"))})
    return out


def _validation_message(fields: list[dict]) -> str:
    """'name: Field required; limit: Input should be less than 500' with body./query. dropped."""
    parts = []
    for f in fields[:5]:
        where = f["field"].split(".", 1)[1] if f["field"].split(".", 1)[0] in ("body", "query", "path", "header", "cookie") and "." in f["field"] else f["field"]
        parts.append(f"{where}: {f['message']}")
    more = f" (and {len(fields) - 5} more)" if len(fields) > 5 else ""
    return "Invalid request — " + "; ".join(parts) + more if parts else "Invalid request"


def install(app: FastAPI) -> None:
    """Registers the handlers. Unhandled exceptions are turned into a 500 by the HTTP middleware
    (`internal_error`), which sees them before Starlette's ServerErrorMiddleware."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else code_for(exc.status_code).replace("_", " ").capitalize()
        detail = None if isinstance(exc.detail, str) else exc.detail
        return response(exc.status_code, message, getattr(exc, "code", None), headers=getattr(exc, "headers", None), detail=detail)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = _validation_fields(exc)
        return response(422, _validation_message(fields), "invalid_request", fields=fields)

    @app.exception_handler(ConnectorError)
    async def connector_error(request: Request, exc: ConnectorError) -> JSONResponse:
        status = CONNECTOR_STATUS.get(exc.kind, 502)
        log.warning("connector error reached the API", extra={"event": "http_connector_error", "route": _route(request),
                                                              "errorType": type(exc).__name__, "errorCode": exc.kind, "error": exc.message[:500]})
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        return response(status, exc.message, f"connector_{exc.kind}", headers=headers)


def internal_error(request: Request, exc: BaseException) -> JSONResponse:
    log.error("unhandled error", exc_info=exc, extra={"event": "http_unhandled", "route": _route(request), "method": request.method})
    return response(500, INTERNAL_MESSAGE, "internal")


def _route(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def request_id(request: Request) -> str:
    """The caller's X-Request-ID when it is a plain token, else the trace id, else a new one.
    Anything else is replaced: the id goes into logs and headers verbatim."""
    given = request.headers.get("x-request-id")
    if given and _REQUEST_ID.match(given):
        return given
    trace_id, _ = current_trace_ids()
    return trace_id or uuid.uuid4().hex
