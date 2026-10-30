"""Prometheus HTTP API connector (instant/range queries, labels, targets, alerts)."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import httpx

from .base import BoundedHttp, ConnectorError, HttpLimits, http_auth_from

MAX_SERIES = 25
MAX_POINTS_PER_SERIES = 240


class PrometheusConnector:
    type = "prometheus"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        base_url = config.get("base_url")
        if not base_url:
            raise ConnectorError("config", "prometheus connector needs config.base_url (include any path prefix, e.g. /prometheus)")
        headers, auth = http_auth_from(config, credential_ref)
        self.base_url = base_url.rstrip("/")
        self.public_url = (config.get("public_url") or "").rstrip("/") or None
        self.http = BoundedHttp(
            "prometheus", self.base_url, headers=headers, auth=auth, verify=config.get("verify_tls", True),
            limits=HttpLimits(timeout_seconds=float(config.get("timeout_seconds", 15)), retries=int(config.get("retries", 2))),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _api(self, operation: str, path: str, params: Any = None, method: str = "GET") -> Any:
        if method == "POST":
            body = await self.http.request_json(operation, "POST", path, data=params)
        else:
            body = await self.http.get_json(operation, path, params=params)
        if not isinstance(body, dict) or body.get("status") != "success":
            msg = body.get("error") if isinstance(body, dict) else "unexpected response"
            raise ConnectorError("bad_request", f"prometheus: {msg}")
        return body.get("data"), body.get("warnings") or []

    async def test(self) -> str:
        data, _ = await self._api("buildinfo", "/api/v1/status/buildinfo")
        return f"Prometheus {data.get('version', '?')} reachable"

    async def instant(self, promql: str, at: datetime) -> dict[str, Any]:
        data, warnings = await self._api("query", "/api/v1/query", {"query": promql, "time": at.timestamp()}, method="POST")
        out = {"resultType": data.get("resultType"), "series": _shape(data), "warnings": warnings}
        if data.get("resultType") == "vector" and len(data.get("result") or []) > MAX_SERIES:
            # Instant vectors are small per series; aggregation callers (FinOps) need all of them.
            out["all_series"] = [{"metric": i.get("metric", {}), "value": [i["value"][0], _num(i["value"][1])]} for i in data["result"][:5000]]
        return out

    async def range(self, promql: str, start: datetime, end: datetime, step_seconds: int) -> dict[str, Any]:
        if end <= start:
            raise ConnectorError("bad_request", "range end must be after start")
        points = (end - start).total_seconds() / step_seconds
        if points > 11_000:
            raise ConnectorError("bad_request", "too many points; increase the step or narrow the window")
        data, warnings = await self._api("query_range", "/api/v1/query_range", {
            "query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step_seconds,
        }, method="POST")
        return {"resultType": data.get("resultType"), "series": _shape(data), "warnings": warnings}

    async def label_values(self, label: str, match: str | None, start: datetime, end: datetime) -> list[str]:
        params: list[tuple[str, Any]] = [("start", start.timestamp()), ("end", end.timestamp())]
        if match:
            params.append(("match[]", match))
        data, _ = await self._api("label_values", f"/api/v1/label/{label}/values", params)
        return sorted(data)[:500]

    async def targets(self) -> list[dict[str, Any]]:
        data, _ = await self._api("targets", "/api/v1/targets", {"state": "active"})
        out = []
        for t in data.get("activeTargets", [])[:500]:
            out.append({
                "job": t.get("labels", {}).get("job") or t.get("scrapePool"),
                "instance": t.get("labels", {}).get("instance"),
                "health": t.get("health"),
                "lastError": t.get("lastError") or None,
                "lastScrape": t.get("lastScrape"),
            })
        return out

    async def alerts(self) -> list[dict[str, Any]]:
        data, _ = await self._api("rules", "/api/v1/rules", {"type": "alert"})
        out = []
        for group in data.get("groups", []):
            for rule in group.get("rules", []):
                out.append({
                    "group": group.get("name"), "name": rule.get("name"), "state": rule.get("state"),
                    "query": rule.get("query"), "for": rule.get("duration"),
                    "labels": rule.get("labels", {}), "annotations": rule.get("annotations", {}),
                    "active": [
                        {"labels": a.get("labels", {}), "state": a.get("state"), "activeAt": a.get("activeAt"), "value": a.get("value")}
                        for a in rule.get("alerts", [])[:20]
                    ],
                })
        return out

    async def active_alerts(self) -> list[dict]:
        """Firing and pending alert instances (/api/v1/alerts); used by the alert poller where no
        Alertmanager exists."""
        data, _ = await self._api("alerts", "/api/v1/alerts")
        return [{"labels": a.get("labels", {}), "annotations": a.get("annotations", {}), "state": a.get("state"),
                 "activeAt": a.get("activeAt"), "value": a.get("value")} for a in data.get("alerts", [])[:500]]

    def graph_link(self, promql: str, minutes: int) -> str | None:
        if not self.public_url:
            return None
        from urllib.parse import quote

        return f"{self.public_url}/graph?g0.expr={quote(promql)}&g0.tab=0&g0.range_input={max(1, minutes)}m"


def _num(v: str) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _shape(data: dict[str, Any]) -> list[dict[str, Any]]:
    rtype = data.get("resultType")
    result = data.get("result")
    if rtype == "scalar" or rtype == "string":
        return [{"metric": {}, "value": [result[0], _num(result[1])]}]
    series = []
    for item in (result or [])[:MAX_SERIES]:
        if "values" in item:
            values = [[ts, _num(v)] for ts, v in item["values"]]
            if len(values) > MAX_POINTS_PER_SERIES:
                stride = math.ceil(len(values) / MAX_POINTS_PER_SERIES)
                values = values[::stride]
            series.append({"metric": item.get("metric", {}), "values": values})
        else:
            ts, v = item.get("value", [None, None])
            series.append({"metric": item.get("metric", {}), "value": [ts, _num(v)]})
    return series


def auto_step(start: datetime, end: datetime, target_points: int = 120) -> int:
    seconds = max(1, int((end - start).total_seconds()))
    step = max(15, math.ceil(seconds / target_points))
    for nice in (15, 30, 60, 120, 300, 600, 900, 1800, 3600):
        if step <= nice:
            return nice
    return step
