"""Loki HTTP API connector (bounded LogQL queries and label discovery)."""
from __future__ import annotations

import re
from dataclasses import dataclass
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

    async def labels(self, start: datetime, end: datetime, query: str | None = None) -> list[str]:
        params = {"start": _ns(start), "end": _ns(end)}
        if query:
            params["query"] = query
        data = await self._api("labels", "/loki/api/v1/labels", params)
        return sorted(data or [])

    async def label_values(self, label: str, start: datetime, end: datetime, query: str | None = None) -> list[str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            raise ConnectorError("bad_request", f"invalid label name {label!r}")
        params = {"start": _ns(start), "end": _ns(end)}
        if query:
            params["query"] = query
        data = await self._api("label_values", f"/loki/api/v1/label/{label}/values", params)
        return sorted(data or [])[:500]

    async def format_query(self, logql: str) -> str | None:
        """Loki's own parser: rejects bad syntax and reads no data. None if this Loki has no such endpoint."""
        try:
            await self.http.get_json("format_query", "/loki/api/v1/format_query", params={"query": logql})
            return logql
        except ConnectorError as exc:
            if exc.kind == "not_found":
                return None
            raise

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
            return {"resultType": "matrix", "series": series, "total_series": len(data.get("result", [])), "stats": _stats(data)}
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
#
# Scope is enforced on what Loki will actually parse, so this reads LogQL the way Loki's lexer does:
# strings, comments and selectors are separate tokens. A regex over the text is not enough. In
# `{namespace="a"} # "\n{namespace="b"} # "` the second selector is real, and a regex that pairs the
# quotes hides it.

_MATCHER = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_RANGE = re.compile(r"\[\s*([0-9a-z.]+)\s*(?::\s*[0-9a-z.]*\s*)?\]")
_OFFSET = re.compile(r"\boffset\s+-?([0-9a-z.]+)")
_METRIC = re.compile(r"\b(rate|rate_counter|count_over_time|bytes_rate|bytes_over_time|sum_over_time|avg_over_time|max_over_time|min_over_time|"
                     r"first_over_time|last_over_time|stdvar_over_time|stddev_over_time|quantile_over_time|absent_over_time|vector)\b")
_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


@dataclass(frozen=True)
class LogQLShape:
    """What a query reaches, read from its tokens."""
    selectors: tuple[tuple[tuple[str, str, str], ...], ...]  # each selector's (label, op, raw value) matchers
    has_comment: bool
    code: str  # the query with strings blanked and comments removed
    lookback_seconds: float

    @property
    def metric(self) -> bool:
        return bool(_METRIC.search(self.code))

    def namespaces(self, namespace_label: str = "namespace") -> list[str | None]:
        """The namespace each selector is pinned to by plain equality, else None. A value with an
        escape is not treated as pinned: no namespace name needs one, and decoding it twice differently
        from Loki is how a check gets bypassed."""
        out: list[str | None] = []
        for matchers in self.selectors:
            found = None
            for name, op, value in matchers:
                if name == namespace_label and op == "=" and "\\" not in value:
                    found = value
            out.append(found)
        return out


def _lex(q: str) -> tuple[list[str], list[str], bool]:
    """Splits a query into its code outside selectors, the bodies of its selectors, and whether it has a comment."""
    code: list[str] = []
    selectors: list[str] = []
    body: list[str] | None = None
    comment = False
    i = 0
    while i < len(q):
        ch = q[i]
        if ch == "#":  # to the end of the line, in or out of a selector
            comment = True
            while i < len(q) and q[i] != "\n":
                i += 1
            continue
        if ch in "\"`":
            end = i + 1
            while end < len(q) and q[end] != ch:
                end += 2 if ch == '"' and q[end] == "\\" else 1
            if end >= len(q):
                raise ConnectorError("bad_request", "LogQL query has an unterminated string")
            if body is not None:
                body.append(q[i:end + 1])
            else:
                code.append(ch + ch)
            i = end + 1
            continue
        if ch == "{":
            if body is not None:
                raise ConnectorError("bad_request", "LogQL stream selector is not closed")
            body = []
        elif ch == "}":
            if body is None:
                raise ConnectorError("bad_request", "LogQL query has an unmatched }")
            selectors.append("".join(body))
            body = None
            code.append("{}")
        elif body is not None:
            body.append(ch)
        else:
            code.append(ch)
        i += 1
    if body is not None:
        raise ConnectorError("bad_request", "LogQL stream selector is not closed")
    return ["".join(code)], selectors, comment


def _seconds(text: str) -> float:
    parts = _DURATION.findall(text)
    if not parts or "".join(a + b for a, b in parts) != text:
        raise ConnectorError("bad_request", f"cannot read duration {text!r} in LogQL query")
    return sum(float(n) * _SECONDS[u] for n, u in parts)


def analyze_logql(logql: str) -> LogQLShape:
    """Raises on anything it cannot read, so an unreadable query is refused rather than assumed safe."""
    code_parts, bodies, comment = _lex(logql)
    code = code_parts[0]
    if not bodies:
        raise ConnectorError("bad_request", "LogQL query has no stream selector")
    selectors = []
    for body in bodies:
        matchers = []
        for part in _split_matchers(body):
            m = _MATCHER.fullmatch(part)
            if not m:
                raise ConnectorError("bad_request", f"cannot parse stream selector matcher {part.strip()!r}")
            matchers.append((m.group(1), m.group(2), m.group(3)))
        selectors.append(tuple(matchers))
    ranges = [_seconds(m) for m in _RANGE.findall(code)]
    offsets = [_seconds(m) for m in _OFFSET.findall(code)]
    return LogQLShape(tuple(selectors), comment, code, max(ranges, default=0) + max(offsets, default=0))


def logql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def selector_namespaces(logql: str, namespace_label: str = "namespace") -> list[str | None]:
    """For each stream selector in the query, the namespace it is pinned to by an equality matcher,
    or None if it is not pinned. Raises on selectors it cannot read."""
    return analyze_logql(logql).namespaces(namespace_label)


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
