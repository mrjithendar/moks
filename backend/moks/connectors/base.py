"""Shared connector plumbing: credential references, errors, bounded HTTP with retries."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..observability import CONNECTOR_LATENCY, CONNECTOR_REQUESTS

log = logging.getLogger("moks.connectors")
CREDENTIALS_DIR = Path(os.environ.get("MOKS_CREDENTIALS_DIR", "/var/run/moks-credentials"))


class ConnectorError(Exception):
    """A connector failure with a user-presentable message.

    kind: config | auth | unavailable | timeout | bad_request | not_found | too_large | forbidden | rate_limited
    retry_after: seconds the upstream asked us to wait (rate_limited only).
    """

    def __init__(self, kind: str, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


def resolve_credential(ref: dict[str, Any] | None) -> str | None:
    """Resolve a credential reference to its secret value. Values never leave the process:
    they are not stored in the database, logged, audited, returned by the API or put in prompts.

    {"type": "env", "name": "MOKS_CRED_X"}       -> environment variable (from a Kubernetes Secret)
    {"type": "file", "path": "prom/token"}       -> file under MOKS_CREDENTIALS_DIR (mounted Secret)
    """
    if not ref or ref.get("type") in (None, "none"):
        return None
    kind = ref.get("type")
    if kind == "env":
        name = str(ref.get("name", ""))
        if not name.startswith("MOKS_CRED_"):
            raise ConnectorError("config", "environment credential names must start with MOKS_CRED_")
        value = os.environ.get(name)
        if value is None:
            raise ConnectorError("config", f"credential environment variable {name} is not set")
        return value.strip()  # a value made with `echo` or from a file ends in a newline, which is an illegal header
    if kind == "file":
        path = resolve_credential_path(ref)
        try:
            return path.read_text().strip()
        except OSError as exc:
            raise ConnectorError("config", f"credential file {path} is not readable ({type(exc).__name__})") from None
    raise ConnectorError("config", f"unsupported credential reference type {kind!r}")


def resolve_credential_path(ref: dict[str, Any]) -> Path:
    base = CREDENTIALS_DIR.resolve()
    path = (base / str(ref.get("path", ""))).resolve()
    if base != path and base not in path.parents:
        raise ConnectorError("config", "credential files must be inside MOKS_CREDENTIALS_DIR")
    return path


def describe_credential_ref(ref: dict[str, Any] | None) -> str:
    """Safe, value-free description for the admin UI."""
    if not ref or ref.get("type") in (None, "none"):
        return "none"
    if ref.get("type") == "env":
        return f"env:{ref.get('name')} ({'set' if os.environ.get(str(ref.get('name'))) else 'missing'})"
    if ref.get("type") == "file":
        try:
            ok = resolve_credential_path(ref).exists()
        except ConnectorError:
            ok = False
        return f"file:{ref.get('path')} ({'present' if ok else 'missing'})"
    return str(ref.get("type"))


@dataclass
class HttpLimits:
    timeout_seconds: float = 15.0
    retries: int = 2
    max_response_bytes: int = 8_000_000


def _is_rate_limit(status: int, headers: httpx.Headers, text: str) -> bool:
    """GitHub also answers 403 for primary and secondary rate limits."""
    if status == 429:
        return True
    lowered = text.lower()
    return status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers or "rate limit" in lowered or "abuse" in lowered)


def _retry_after(headers: httpx.Headers) -> float:
    value = headers.get("retry-after", "")
    if value.isdigit():
        wait = float(value)
    elif (headers.get("x-ratelimit-reset") or "").isdigit():
        wait = float(headers["x-ratelimit-reset"]) - time.time() + 1
    else:
        wait = 60.0  # GitHub: wait at least a minute when no header says otherwise
    return min(max(wait, 1.0), 3600.0)


class BoundedHttp:
    """httpx wrapper: per-call timeout, bounded retries for idempotent requests, response size cap,
    and connector metrics. One instance per connector configuration."""

    def __init__(self, connector_type: str, base_url: str, *, headers: dict[str, str] | None = None,
                 auth: httpx.Auth | tuple[str, str] | None = None, verify: bool | str = True,
                 limits: HttpLimits | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.connector_type = connector_type
        self.limits = limits or HttpLimits()
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers or {}, auth=auth, verify=verify,
            timeout=httpx.Timeout(self.limits.timeout_seconds, connect=min(5.0, self.limits.timeout_seconds)),
            transport=transport, follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get_json(self, operation: str, path: str, params: Any = None) -> Any:
        return await self.request_json(operation, "GET", path, params=params)

    async def request_json(self, operation: str, method: str, path: str, *, params: Any = None,
                           data: Any = None, idempotent: bool = True) -> Any:
        attempts = 1 + (self.limits.retries if idempotent else 0)
        start = time.monotonic()
        last: ConnectorError | None = None
        for attempt in range(attempts):
            try:
                result = await self._once(method, path, params, data)
                CONNECTOR_REQUESTS.labels(self.connector_type, operation, "ok").inc()
                CONNECTOR_LATENCY.labels(self.connector_type, operation).observe(time.monotonic() - start)
                return result
            except ConnectorError as exc:
                last = exc
                if not exc.retryable or attempt == attempts - 1:
                    break
                await asyncio.sleep(min(2.0, 0.3 * (3 ** attempt)))
        assert last is not None
        CONNECTOR_REQUESTS.labels(self.connector_type, operation, last.kind).inc()
        CONNECTOR_LATENCY.labels(self.connector_type, operation).observe(time.monotonic() - start)
        log.warning("connector request failed", extra={"event": "connector_request_failed", "dependency": self.connector_type, "operation": operation, "outcome": "failure",
                                                       "errorCode": last.kind, "durationMs": int((time.monotonic() - start) * 1000)})
        raise last

    async def _once(self, method: str, path: str, params: Any, data: Any) -> Any:
        try:
            async with self.client.stream(method, path, params=params, data=data) as resp:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.limits.max_response_bytes:
                        raise ConnectorError("too_large", f"response exceeded {self.limits.max_response_bytes} bytes; narrow the query")
                status, headers = resp.status_code, resp.headers
        except httpx.TimeoutException:
            raise ConnectorError("timeout", f"{self.connector_type} did not answer within {self.limits.timeout_seconds:.0f}s", retryable=True) from None
        except httpx.TransportError as exc:
            raise ConnectorError("unavailable", f"cannot reach {self.connector_type} at {self.client.base_url} ({type(exc).__name__})", retryable=True) from None
        text = body.decode("utf-8", "replace")
        if _is_rate_limit(status, headers, text):
            # Never retried here: repeating a rate-limited request is what extends the block.
            wait = _retry_after(headers)
            raise ConnectorError("rate_limited", f"{self.connector_type} rate limit reached (HTTP {status}); retry in about {wait:.0f}s", retry_after=wait)
        if status in (401, 403):
            raise ConnectorError("auth" if status == 401 else "forbidden", f"{self.connector_type} rejected the credentials (HTTP {status})")
        if 300 <= status < 400:
            raise ConnectorError("bad_request", f"{self.connector_type} redirected to {headers.get('location', 'another address')}; put the final address in base_url")
        if status == 404:
            raise ConnectorError("not_found", f"{self.connector_type} endpoint not found (HTTP 404); check the base URL/path prefix")
        if status >= 500:
            raise ConnectorError("unavailable", f"{self.connector_type} returned HTTP {status}: {_short(text)}", retryable=True)
        if status >= 400:
            raise ConnectorError("bad_request", f"{self.connector_type} rejected the request (HTTP {status}): {_short(text)}")
        try:
            return httpx.Response(status, content=bytes(body)).json()
        except ValueError:
            raise ConnectorError("bad_request", f"{self.connector_type} returned non-JSON content") from None


def _short(text: str, n: int = 300) -> str:
    try:
        import json

        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("error"):
            text = str(obj["error"])
    except ValueError:
        pass
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


def http_auth_from(config: dict[str, Any], credential_ref: dict[str, Any] | None) -> tuple[dict[str, str], Any]:
    """Build headers/auth from config.auth (none | bearer | basic) and the credential reference."""
    mode = (config.get("auth") or "none").lower()
    if mode == "none":
        return {}, None
    secret = resolve_credential(credential_ref)
    if not secret:
        raise ConnectorError("config", f"auth mode {mode} requires a credential reference")
    if mode == "bearer":
        return {"Authorization": f"Bearer {secret}"}, None
    if mode == "basic":
        user = config.get("username") or ""
        return {}, (user, secret)
    raise ConnectorError("config", f"unsupported auth mode {mode!r}")
