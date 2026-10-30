"""Loki HTTP API connector (bounded LogQL queries and label discovery)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import BoundedHttp, ConnectorError, HttpLimits, http_auth_from

MAX_LINES = 500
MAX_LINE_CHARS = 2000


class LokiConnector:
    type = "loki"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        base_url = config.get("base_url")
        if not base_url:
            raise ConnectorError("config", "loki connector needs config.base_url")
        headers, auth = http_auth_from(config, credential_ref)
        if config.get("tenant_id"):
            headers["X-Scope-OrgID"] = str(config["tenant_id"])
        self.base_url = base_url.rstrip("/")
        # Name of the label holding the Kubernetes namespace in this Loki (used for scoping).
        self.namespace_label = config.get("namespace_label", "namespace")
        self.http = BoundedHttp(
            "loki", self.base_url, headers=headers, auth=auth, verify=config.get("verify_tls", True),
            limits=HttpLimits(timeout_seconds=float(config.get("timeout_seconds", 20)), retries=int(config.get("retries", 2))),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _api(self, operation: str, path: str, params: Any) -> Any:
        body = await self.http.get_json(operation, path, params=params)
        if not isinstance(body, dict) or body.get("status") not in ("success", None):
            raise ConnectorError("bad_request", f"loki: {body.get('error') if isinstance(body, dict) else 'unexpected response'}")
        return body.get("data", body)

    async def test(self) -> str:
        now = datetime.now(timezone.utc)
        labels = await self.labels(now.replace(microsecond=0) - _minutes(60), now)
        return f"Loki reachable; {len(labels)} labels in the last hour"

    async def labels(self, start: datetime, end: datetime) -> list[str]:
        data = await self._api("labels", "/loki/api/v1/labels", {"start": _ns(start), "end": _ns(end)})
        return sorted(data or [])

    async def label_values(self, label: str, start: datetime, end: datetime, query: str | None = None) -> list[str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            raise ConnectorError("bad_request", f"invalid label name {label!r}")
        params = {"start": _ns(start), "end": _ns(end)}
        if query:
            params["query"] = query
        data = await self._api("label_values", f"/loki/api/v1/label/{label}/values", params)
        return sorted(data or [])[:500]

    async def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 200,
                          direction: str = "backward") -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_LINES))
        data = await self._api("query_range", "/loki/api/v1/query_range", {
            "query": logql, "start": _ns(start), "end": _ns(end), "limit": limit, "direction": direction,
        })
        rtype = data.get("resultType")
        if rtype == "streams":
            lines = []
            for stream in data.get("result", []):
                labels = stream.get("stream", {})
                for ts, line in stream.get("values", []):
                    lines.append({
                        "ts": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                        "labels": labels,
                        "line": line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…",
                    })
            lines.sort(key=lambda r: r["ts"], reverse=(direction == "backward"))
            return {"resultType": "streams", "lines": lines[:limit], "stats": _stats(data)}
        if rtype == "matrix":
            series = [{"metric": r.get("metric", {}), "values": [[float(t), float(v)] for t, v in r.get("values", [])][-240:]}
                      for r in data.get("result", [])[:25]]
            return {"resultType": "matrix", "series": series, "stats": _stats(data)}
        return {"resultType": rtype, "raw": data.get("result")}


def _stats(data: dict[str, Any]) -> dict[str, Any]:
    summary = (data.get("stats") or {}).get("summary") or {}
    return {k: summary.get(k) for k in ("totalBytesProcessed", "totalLinesProcessed", "execTime") if k in summary}


def _ns(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1e9))


def _minutes(n: int):
    from datetime import timedelta

    return timedelta(minutes=n)


# ---- LogQL helpers -----------------------------------------------------------------------

_SELECTOR = re.compile(r"\{([^{}]*)\}")
_MATCHER = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')


def logql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def selector_namespaces(logql: str, namespace_label: str = "namespace") -> list[str | None]:
    """For each stream selector in the query, return the namespace it is pinned to by an
    equality matcher, or None if it is not pinned. Raises on unparsable selectors."""
    selectors = _SELECTOR.findall(_strip_strings_outside_selectors(logql))
    if not selectors:
        raise ConnectorError("bad_request", "LogQL query has no stream selector")
    pinned: list[str | None] = []
    for body in selectors:
        found: str | None = None
        for part in _split_matchers(body):
            m = _MATCHER.fullmatch(part)
            if not m:
                raise ConnectorError("bad_request", f"cannot parse stream selector matcher {part.strip()!r}")
            name, op, value = m.groups()
            if name == namespace_label and op == "=":
                found = value.encode().decode("unicode_escape")
        pinned.append(found)
    return pinned


def _split_matchers(body: str) -> list[str]:
    parts, buf, in_str, esc = [], [], False, False
    for ch in body:
        if esc:
            buf.append(ch)
            esc = False
            continue
        if ch == "\\":
            buf.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
        if ch == "," and not in_str:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _strip_strings_outside_selectors(q: str) -> str:
    """Blank out quoted strings and backtick strings that are not inside {...} so braces in
    line filters (e.g. |= "{") do not look like selectors."""
    out, depth, i = [], 0, 0
    while i < len(q):
        ch = q[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if depth == 0 and ch in "\"`":
            end = i + 1
            while end < len(q) and q[end] != ch:
                end += 2 if (ch == '"' and q[end] == "\\") else 1
            out.append(ch + " " * max(0, end - i - 1) + (ch if end < len(q) else ""))
            i = end + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)
