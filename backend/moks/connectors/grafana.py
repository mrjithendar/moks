"""Grafana HTTP API connector: dashboards, panel queries, data sources and unified alerting.

Authenticate with a Grafana service-account token (Viewer role is enough for dashboards and
alert state). Grafana's own permissions apply: endpoints the token may not use return a clear
`forbidden` error instead of failing the investigation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .base import BoundedHttp, ConnectorError, HttpLimits, http_auth_from


class GrafanaConnector:
    type = "grafana"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        base_url = config.get("base_url")
        if not base_url:
            raise ConnectorError("config", "grafana connector needs config.base_url (include the sub-path, e.g. /grafana)")
        cfg = {"auth": "bearer" if credential_ref and credential_ref.get("type") not in (None, "none") else "none", **config}
        headers, auth = http_auth_from(cfg, credential_ref)
        self.base_url = base_url.rstrip("/")
        self.public_url = (config.get("public_url") or self.base_url).rstrip("/")
        self.http = BoundedHttp("grafana", self.base_url, headers=headers, auth=auth, verify=config.get("verify_tls", True),
                                limits=HttpLimits(timeout_seconds=float(config.get("timeout_seconds", 15)),
                                                  retries=int(config.get("retries", 2))), transport=transport)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def test(self) -> str:
        health = await self.http.get_json("health", "/api/health")
        found = await self.http.get_json("search", "/api/search", params={"type": "dash-db", "limit": 1})
        return f"Grafana {health.get('version', '?')} reachable; dashboards visible to the token: {'yes' if found else 'none'}"

    def link(self, path: str, start: datetime | None = None, end: datetime | None = None, extra: dict | None = None) -> str:
        params = dict(extra or {})
        if start and end:
            params.update({"from": int(start.timestamp() * 1000), "to": int(end.timestamp() * 1000)})
        return f"{self.public_url}{path}" + (f"?{urlencode(params)}" if params else "")

    async def search_dashboards(self, query: str | None, tag: str | None, limit: int = 30,
                                start: datetime | None = None, end: datetime | None = None) -> list[dict]:
        params: list[tuple[str, Any]] = [("type", "dash-db"), ("limit", min(limit, 100))]
        if query:
            params.append(("query", query))
        if tag:
            params.append(("tag", tag))
        rows = await self.http.get_json("search", "/api/search", params=params)
        return [{"uid": r.get("uid"), "title": r.get("title"), "folder": r.get("folderTitle"), "tags": r.get("tags", []),
                 "url": self.link(r.get("url", "").removeprefix(self._prefix()), start, end)} for r in rows[:limit]]

    def _prefix(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.public_url).path.rstrip("/")

    async def dashboard(self, uid: str, start: datetime | None = None, end: datetime | None = None) -> dict:
        body = await self.http.get_json("dashboard", f"/api/dashboards/uid/{quote(uid, safe='')}")
        dash = body.get("dashboard", {})
        panels = []
        for p in _flatten_panels(dash.get("panels") or []):
            panels.append({
                "id": p.get("id"), "title": p.get("title"), "type": p.get("type"),
                "datasource": _ds_name(p.get("datasource")),
                "queries": [t.get("expr") or t.get("query") or t.get("rawSql") for t in p.get("targets") or [] if
                            t.get("expr") or t.get("query") or t.get("rawSql")][:6],
                "url": self.link(body.get("meta", {}).get("url", "").removeprefix(self._prefix()), start, end, {"viewPanel": p.get("id")}),
            })
        return {"uid": uid, "title": dash.get("title"), "tags": dash.get("tags", []), "panels": panels[:60],
                "variables": [v.get("name") for v in (dash.get("templating") or {}).get("list", [])],
                "url": self.link(body.get("meta", {}).get("url", "").removeprefix(self._prefix()), start, end)}

    async def datasources(self) -> list[dict]:
        try:
            rows = await self.http.get_json("datasources", "/api/datasources")
        except ConnectorError as exc:
            if exc.kind == "forbidden":
                raise ConnectorError("forbidden", "the Grafana token lacks datasources:read; grant it or skip data sources") from None
            raise
        return [{"uid": r.get("uid"), "name": r.get("name"), "type": r.get("type"), "default": r.get("isDefault")} for r in rows]

    async def alert_rules(self) -> list[dict]:
        """Grafana-managed alert rules with state (Prometheus-compatible API)."""
        body = await self.http.get_json("alert_rules", "/api/prometheus/grafana/api/v1/rules")
        out = []
        for group in (body.get("data") or {}).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("type") not in (None, "alerting"):
                    continue
                out.append({
                    "group": group.get("name"), "folder": group.get("file"), "name": rule.get("name"), "state": rule.get("state"),
                    "health": rule.get("health"), "query": rule.get("query"), "labels": rule.get("labels", {}),
                    "annotations": rule.get("annotations", {}),
                    "active": [{"labels": a.get("labels", {}), "state": a.get("state"), "activeAt": a.get("activeAt"), "value": a.get("value")}
                               for a in rule.get("alerts", [])[:20]],
                })
        return out


def _flatten_panels(panels: list[dict]) -> list[dict]:
    out = []
    for p in panels:
        if p.get("type") == "row":
            out.extend(_flatten_panels(p.get("panels") or []))
        else:
            out.append(p)
    return out


def _ds_name(ds: Any) -> str | None:
    if isinstance(ds, dict):
        return ds.get("uid") or ds.get("type")
    return ds
