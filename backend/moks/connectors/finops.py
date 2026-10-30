"""FinOps connectors.

aws_cost   - AWS Cost Explorer (cost and usage, forecast), Cost Anomaly Detection and Budgets.
             Cost Explorer bills $0.01 per API request, so identical queries are cached
             (config.cache_minutes, default 60) and the number of requests per hour is capped.
cost_model - Prices for Kubernetes capacity, used to turn requests/usage into money. For a
             fixed-price cluster (mk4 on Hetzner) set monthly_cost_usd; for cloud nodes set
             cpu_hour_usd / gib_hour_usd. No external calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .base import ConnectorError

GROUP_BY = {"service": ("DIMENSION", "SERVICE"), "account": ("DIMENSION", "LINKED_ACCOUNT"), "region": ("DIMENSION", "REGION"),
            "usage_type": ("DIMENSION", "USAGE_TYPE")}


class AwsCostConnector:
    type = "aws_cost"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None, session=None):
        self.metric = config.get("metric", "UnblendedCost")
        self.tag_keys = [str(t) for t in config.get("allocation_tag_keys") or []]
        self.cache_seconds = int(config.get("cache_minutes", 60)) * 60
        self.max_requests_per_hour = int(config.get("max_requests_per_hour", 60))
        self.allowed_accounts = {str(a) for a in config.get("allowed_account_ids") or []}
        self._session = session
        self._cache: dict[str, tuple[float, Any]] = {}
        self._requests: list[float] = []
        self._account: str | None = config.get("account_id")

    async def aclose(self) -> None:
        self._cache.clear()

    def _client(self, name: str):
        import boto3
        from botocore.config import Config

        session = self._session or boto3.session.Session()
        # Cost Explorer and Budgets are global APIs served from us-east-1.
        return session.client(name, region_name="us-east-1", config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3}))

    async def _call(self, service: str, op: str, **kwargs) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        key = hashlib.sha256(json.dumps([service, op, kwargs], sort_keys=True, default=str).encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        now = time.monotonic()
        self._requests = [t for t in self._requests if t > now - 3600]
        if service == "ce" and len(self._requests) >= self.max_requests_per_hour:
            raise ConnectorError("unavailable", f"Cost Explorer request cap of {self.max_requests_per_hour}/hour reached (each request costs $0.01)")
        if service == "ce":
            self._requests.append(now)
        try:
            result = await asyncio.to_thread(lambda: getattr(self._client(service), op)(**kwargs))
        except NoCredentialsError:
            raise ConnectorError("auth", "no AWS credentials available to MOKS for Cost Explorer") from None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", "")[:300]
            kind = "forbidden" if code in ("AccessDeniedException", "AccessDenied") else "bad_request" if "Validation" in code else "unavailable"
            if code == "DataUnavailableException":
                kind, msg = "not_found", "Cost Explorer has no data for this range yet (enable Cost Explorer; data lags up to 24 h)"
            raise ConnectorError(kind, f"{service} {code}: {msg}") from None
        except BotoCoreError as exc:
            raise ConnectorError("unavailable", f"{service}: {type(exc).__name__}", retryable=True) from None
        self._cache[key] = (time.monotonic() + self.cache_seconds, result)
        return result

    async def account_id(self) -> str:
        if not self._account:
            import boto3

            ident = await asyncio.to_thread(lambda: (self._session or boto3.session.Session()).client("sts").get_caller_identity())
            self._account = ident["Account"]
        if self.allowed_accounts and self._account not in self.allowed_accounts:
            raise ConnectorError("forbidden", f"account {self._account} is not in allowed_account_ids")
        return self._account

    async def test(self) -> str:
        end = date.today()
        r = await self.costs("service", 2)
        return f"Cost Explorer reachable for account {await self.account_id()}; {len(r['groups'])} services with cost through {end.isoformat()}"

    async def costs(self, group_by: str, days: int, granularity: str = "DAILY", tag_key: str | None = None) -> dict:
        days = max(1, min(int(days), 90))
        end = date.today()
        start = end - timedelta(days=days)
        if group_by == "tag":
            if not tag_key or (self.tag_keys and tag_key not in self.tag_keys):
                raise ConnectorError("bad_request", f"tag grouping needs one of the configured allocation tags: {self.tag_keys or 'none'}")
            group = {"Type": "TAG", "Key": tag_key}
        elif group_by in GROUP_BY:
            t, k = GROUP_BY[group_by]
            group = {"Type": t, "Key": k}
        else:
            raise ConnectorError("bad_request", f"group_by must be one of {sorted(GROUP_BY) + ['tag']}")
        resp = await self._call("ce", "get_cost_and_usage", TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                                Granularity=granularity, Metrics=[self.metric], GroupBy=[group])
        totals: dict[str, float] = {}
        series: dict[str, list] = {}
        unit = "USD"
        for period in resp.get("ResultsByTime", []):
            ts = int(datetime.fromisoformat(period["TimePeriod"]["Start"]).replace(tzinfo=timezone.utc).timestamp())
            for g in period.get("Groups", []):
                name = g["Keys"][0] or "(untagged)"
                amount = float(g["Metrics"][self.metric]["Amount"])
                unit = g["Metrics"][self.metric].get("Unit", unit)
                totals[name] = totals.get(name, 0.0) + amount
                series.setdefault(name, []).append([ts, round(amount, 4)])
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        top = [name for name, _ in ranked[:8]]
        return {
            "group_by": group_by if group_by != "tag" else f"tag:{tag_key}", "start": start.isoformat(), "end": end.isoformat(),
            "unit": unit, "metric": self.metric, "total": round(sum(totals.values()), 2),
            "groups": [{"name": n, "amount": round(a, 2)} for n, a in ranked[:50]],
            "series": [{"metric": {"group": n}, "values": series[n]} for n in top],
        }

    async def forecast(self, days: int) -> dict:
        days = max(1, min(int(days), 90))
        start = date.today() + timedelta(days=1)
        resp = await self._call("ce", "get_cost_forecast", TimePeriod={"Start": start.isoformat(), "End": (start + timedelta(days=days)).isoformat()},
                                Metric=self.metric.upper().replace("COST", "_COST"), Granularity="MONTHLY")
        return {"start": start.isoformat(), "days": days, "total": round(float(resp["Total"]["Amount"]), 2), "unit": resp["Total"].get("Unit"),
                "periods": [{"start": p["TimePeriod"]["Start"], "mean": round(float(p["MeanValue"]), 2)} for p in resp.get("ForecastResultsByTime", [])]}

    async def anomalies(self, days: int) -> list[dict]:
        days = max(1, min(int(days), 90))
        resp = await self._call("ce", "get_anomalies", DateInterval={"StartDate": (date.today() - timedelta(days=days)).isoformat()}, MaxResults=50)
        out = []
        for a in resp.get("Anomalies", []):
            causes = [{k: c.get(k) for k in ("Service", "Region", "UsageType", "LinkedAccount")} for c in a.get("RootCauses", [])[:5]]
            out.append({"id": a.get("AnomalyId"), "start": a.get("AnomalyStartDate"), "end": a.get("AnomalyEndDate"),
                        "impact": round(float((a.get("Impact") or {}).get("TotalImpact", 0) or 0), 2),
                        "max_impact": round(float((a.get("Impact") or {}).get("MaxImpact", 0) or 0), 2),
                        "score": (a.get("AnomalyScore") or {}).get("CurrentScore"), "dimension": a.get("DimensionValue"),
                        "root_causes": causes, "feedback": a.get("Feedback")})
        return out

    async def budgets(self) -> list[dict]:
        resp = await self._call("budgets", "describe_budgets", AccountId=await self.account_id(), MaxResults=50)
        out = []
        for b in resp.get("Budgets", []):
            limit = float(b["BudgetLimit"]["Amount"])
            actual = float(((b.get("CalculatedSpend") or {}).get("ActualSpend") or {}).get("Amount", 0))
            forecast = float(((b.get("CalculatedSpend") or {}).get("ForecastedSpend") or {}).get("Amount", 0) or 0)
            out.append({"name": b["BudgetName"], "type": b.get("BudgetType"), "period": b.get("TimeUnit"), "limit": round(limit, 2),
                        "actual": round(actual, 2), "forecast": round(forecast, 2), "unit": b["BudgetLimit"].get("Unit"),
                        "actual_pct": round(100 * actual / limit, 1) if limit else None,
                        "forecast_pct": round(100 * forecast / limit, 1) if limit and forecast else None})
        return out


class CostModelConnector:
    """Unit prices for Kubernetes capacity (no external calls)."""

    type = "cost_model"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None):
        self.currency = config.get("currency", "USD")
        self.monthly_cost = config.get("monthly_cost_usd")
        self.cpu_hour = config.get("cpu_hour_usd")
        self.gib_hour = config.get("gib_hour_usd")
        self.cpu_share = float(config.get("cpu_share", 0.5))
        self.storage_gib_month = float(config.get("storage_gib_month_usd", 0) or 0)
        if self.monthly_cost is None and (self.cpu_hour is None or self.gib_hour is None):
            raise ConnectorError("config", "cost_model needs monthly_cost_usd, or both cpu_hour_usd and gib_hour_usd")
        if not 0 < self.cpu_share < 1:
            raise ConnectorError("config", "cpu_share must be between 0 and 1")

    async def aclose(self) -> None:
        pass

    async def test(self) -> str:
        return f"Cost model: {self.describe()}"

    def describe(self) -> str:
        if self.monthly_cost is not None:
            return f"fixed {self.monthly_cost} {self.currency}/month split {int(self.cpu_share * 100)}% CPU / {int((1 - self.cpu_share) * 100)}% memory by capacity"
        return f"{self.cpu_hour} {self.currency} per vCPU-hour, {self.gib_hour} {self.currency} per GiB-hour"

    def unit_prices(self, total_cpu: float, total_gib: float) -> tuple[float, float]:
        """(per vCPU-hour, per GiB-hour). A fixed monthly price is spread over total capacity."""
        if self.monthly_cost is None:
            return float(self.cpu_hour), float(self.gib_hour)
        hourly = float(self.monthly_cost) / 730.0
        cpu_price = hourly * self.cpu_share / max(total_cpu, 0.001)
        gib_price = hourly * (1 - self.cpu_share) / max(total_gib, 0.001)
        return cpu_price, gib_price


# ---- Kubernetes allocation and rightsizing (pure functions; tools feed them live data) ---------

def parse_cpu(v: str | None) -> float:
    if not v:
        return 0.0
    v = str(v)
    if v.endswith("m"):
        return float(v[:-1]) / 1000
    if v.endswith("n"):
        return float(v[:-1]) / 1e9
    return float(v)


_MEM = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "k": 1e3}


def parse_mem(v: str | None) -> float:
    if not v:
        return 0.0
    v = str(v)
    for suffix in sorted(_MEM, key=len, reverse=True):
        if v.endswith(suffix):
            return float(v[: -len(suffix)]) * _MEM[suffix]
    if v.endswith("m"):  # millibytes, rare
        return float(v[:-1]) / 1000
    return float(v)


def fmt_mem(b: float) -> str:
    """Whole Mi rounded up to 16 Mi (valid in patch_resources, which accepts integer quantities)."""
    import math

    mi = b / 1024**2
    return f"{max(16, int(math.ceil(mi / 16.0) * 16))}Mi"


def fmt_cpu(c: float) -> str:
    return f"{max(10, int(round(c * 1000 / 10.0) * 10))}m"


def allocate(workloads: list[dict], cpu_price: float, gib_price: float, hours: float, storage_gib_month: float = 0.0) -> list[dict]:
    """workloads: {namespace, workload, cpu_request, mem_request (bytes), cpu_avg, mem_avg (bytes), storage_bytes?}.
    Cost is charged on max(request, usage): requested capacity is reserved whether used or not.
    Each row carries cost per resource type (cpu, memory, storage)."""
    out = []
    for w in workloads:
        cpu_alloc = max(w["cpu_request"], w["cpu_avg"])
        mem_alloc = max(w["mem_request"], w["mem_avg"]) / 1024**3
        cpu_cost = cpu_alloc * cpu_price * hours
        mem_cost = mem_alloc * gib_price * hours
        storage_cost = w.get("storage_bytes", 0) / 1024**3 * storage_gib_month * hours / 730.0
        cost = cpu_cost + mem_cost + storage_cost
        idle = (max(0.0, w["cpu_request"] - w["cpu_avg"]) * cpu_price + max(0.0, (w["mem_request"] - w["mem_avg"]) / 1024**3) * gib_price) * hours
        out.append({**w, "cost": round(cost, 4), "idle_cost": round(idle, 4),
                    "cost_by_resource": {"cpu": round(cpu_cost, 4), "memory": round(mem_cost, 4), "storage": round(storage_cost, 4)},
                    "cpu_efficiency": round(w["cpu_avg"] / w["cpu_request"], 3) if w["cpu_request"] else None,
                    "mem_efficiency": round(w["mem_avg"] / w["mem_request"], 3) if w["mem_request"] else None})
    out.sort(key=lambda r: r["cost"], reverse=True)
    return out


def rightsize(containers: list[dict], cpu_price: float, gib_price: float, headroom: float = 1.25,
              min_saving_month: float = 0.5) -> list[dict]:
    """containers: {namespace, workload, container, replicas, cpu_request, cpu_limit, mem_request, mem_limit,
    cpu_p95, mem_max}. Recommends requests at p95 CPU / max memory plus headroom; flags OOM risk."""
    recs = []
    for c in containers:
        rec_cpu = max(0.01, c["cpu_p95"] * headroom)
        rec_mem = max(32 * 1024**2, c["mem_max"] * headroom)
        findings = []
        if c["mem_limit"] and c["mem_max"] >= 0.9 * c["mem_limit"]:
            findings.append("memory usage within 10% of the limit: OOM risk")
        if c["cpu_request"] and c["cpu_p95"] > c["cpu_request"] * 1.1:
            findings.append("CPU p95 above the request: under-provisioned (throttling/noisy-neighbour risk)")
        if not c["cpu_request"] or not c["mem_request"]:
            findings.append("missing requests: the scheduler cannot reserve capacity (BestEffort/Burstable)")
        d_cpu = (c["cpu_request"] - rec_cpu) * c["replicas"]
        d_mem = (c["mem_request"] - rec_mem) / 1024**3 * c["replicas"]
        monthly = (d_cpu * cpu_price + d_mem * gib_price) * 730
        action = "keep"
        if monthly >= min_saving_month and c["cpu_request"] and c["mem_request"]:
            action = "reduce"
        elif findings:
            action = "increase" if any("risk" in f or "under" in f for f in findings) else "set_requests"
        if action == "keep":
            continue
        recs.append({
            **{k: c[k] for k in ("namespace", "workload", "container", "replicas")},
            "current": {"cpu_request": fmt_cpu(c["cpu_request"]) if c["cpu_request"] else None,
                        "mem_request": fmt_mem(c["mem_request"]) if c["mem_request"] else None,
                        "mem_limit": fmt_mem(c["mem_limit"]) if c["mem_limit"] else None},
            "observed": {"cpu_p95": round(c["cpu_p95"], 4), "mem_max": fmt_mem(c["mem_max"])},
            "recommended": {"cpu_request": fmt_cpu(rec_cpu), "mem_request": fmt_mem(rec_mem),
                            "mem_limit": fmt_mem(max(rec_mem * 1.5, c["mem_limit"] or 0)) if action != "reduce" else (fmt_mem(c["mem_limit"]) if c["mem_limit"] else None)},
            "action": action, "monthly_saving": round(monthly, 2), "findings": findings,
        })
    recs.sort(key=lambda r: (r["action"] != "increase", -r["monthly_saving"]))
    return recs
