"""Amazon CloudWatch connector: Logs Insights (start/poll/stop), metrics and alarms.

Scope is enforced here, independent of IAM: the connector only touches log groups matching
config.allowed_log_groups, in config.region, inside config.max_window_minutes, with at most
config.max_concurrent_queries running. Credentials come from the AWS default chain (IRSA / EKS
Pod Identity on EKS, the moks-mk4-runtime keys on Minikube), optionally assuming config.role_arn
for another account (allowed accounts: config.allowed_account_ids).
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any

from .base import ConnectorError

MAX_ROWS = 500
_LIMIT = re.compile(r"\|\s*limit\s+\d+", re.I)


class CloudWatchConnector:
    type = "cloudwatch"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None, session=None):
        self.region = config.get("region") or "us-east-1"
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", self.region):
            raise ConnectorError("config", f"invalid AWS region {self.region!r}")
        self.allowed = [str(p) for p in config.get("allowed_log_groups") or []]
        if not self.allowed:
            raise ConnectorError("config", "cloudwatch connector needs config.allowed_log_groups (patterns such as /aws/eks/prod/*)")
        self.role_arn = config.get("role_arn")
        self.allowed_accounts = {str(a) for a in config.get("allowed_account_ids") or []}
        if self.role_arn:
            account = self.role_arn.split(":")[4] if self.role_arn.count(":") >= 5 else ""
            if self.allowed_accounts and account not in self.allowed_accounts:
                raise ConnectorError("config", f"role account {account} is not in allowed_account_ids")
        self.max_window = timedelta(minutes=int(config.get("max_window_minutes", 360)))
        self.poll_timeout = float(config.get("poll_timeout_seconds", 60))
        self.semaphore = asyncio.Semaphore(int(config.get("max_concurrent_queries", 2)))
        self._session = session
        self._clients: dict[str, Any] = {}

    async def aclose(self) -> None:
        self._clients.clear()

    def _boto_session(self):
        if self._session is not None:
            return self._session
        import boto3

        base = boto3.session.Session(region_name=self.region)
        if not self.role_arn:
            self._session = base
            return base
        creds = base.client("sts").assume_role(RoleArn=self.role_arn, RoleSessionName="moks", DurationSeconds=3600)["Credentials"]
        self._session = boto3.session.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                                              aws_session_token=creds["SessionToken"], region_name=self.region)
        return self._session

    def _client(self, name: str):
        if name not in self._clients:
            from botocore.config import Config

            self._clients[name] = self._boto_session().client(name, region_name=self.region,
                                                              config=Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 3, "mode": "standard"}))
        return self._clients[name]

    async def _call(self, service: str, op: str, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        def run():
            return getattr(self._client(service), op)(**kwargs)

        try:
            return await asyncio.to_thread(run)
        except NoCredentialsError:
            raise ConnectorError("auth", "no AWS credentials available to MOKS for CloudWatch") from None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            msg = exc.response.get("Error", {}).get("Message", "")[:300]
            kind = {"AccessDeniedException": "forbidden", "UnrecognizedClientException": "auth", "ResourceNotFoundException": "not_found",
                    "LimitExceededException": "unavailable", "ThrottlingException": "unavailable", "InvalidParameterException": "bad_request",
                    "MalformedQueryException": "bad_request"}.get(code, "unavailable")
            raise ConnectorError(kind, f"CloudWatch {code}: {msg}", retryable=kind == "unavailable") from None
        except BotoCoreError as exc:
            raise ConnectorError("unavailable", f"CloudWatch: {type(exc).__name__}", retryable=True) from None

    def allowed_group(self, name: str) -> bool:
        return any(fnmatchcase(name, p) for p in self.allowed)

    async def test(self) -> str:
        groups = await self.log_groups(None, limit=5)
        return f"CloudWatch reachable in {self.region}; {len(groups)} allowed log group(s) visible (first: {groups[0]['name'] if groups else 'none'})"

    async def log_groups(self, prefix: str | None, limit: int = 100) -> list[dict]:
        out, token = [], None
        for _ in range(20):
            kw: dict[str, Any] = {"limit": 50}
            if prefix:
                kw["logGroupNamePrefix"] = prefix
            if token:
                kw["nextToken"] = token
            resp = await self._call("logs", "describe_log_groups", **kw)
            for g in resp.get("logGroups", []):
                if self.allowed_group(g["logGroupName"]):
                    out.append({"name": g["logGroupName"], "retentionDays": g.get("retentionInDays"), "storedBytes": g.get("storedBytes")})
            token = resp.get("nextToken")
            if not token or len(out) >= limit:
                break
        return out[:limit]

    async def insights(self, query: str, log_groups: list[str], start: datetime, end: datetime, limit: int = 200) -> dict:
        if not log_groups:
            raise ConnectorError("bad_request", "name at least one log group")
        denied = [g for g in log_groups if not self.allowed_group(g)]
        if denied:
            raise ConnectorError("forbidden", f"log group(s) not allowed for this connector: {', '.join(denied[:5])}")
        if end - start > self.max_window:
            raise ConnectorError("bad_request", f"window exceeds the connector maximum of {int(self.max_window.total_seconds() // 60)} minutes")
        if re.search(r"\bunmask\s*\(", query, re.I):
            raise ConnectorError("forbidden", "unmask() is not allowed: data protection masking stays on")
        limit = max(1, min(limit, MAX_ROWS))
        final_query = query if _LIMIT.search(query) else f"{query.rstrip()} | limit {limit}"
        async with self.semaphore:
            started = await self._call("logs", "start_query", logGroupNames=log_groups, startTime=int(start.timestamp()),
                                       endTime=int(end.timestamp()), queryString=final_query, limit=limit)
            qid = started["queryId"]
            deadline = asyncio.get_running_loop().time() + self.poll_timeout
            delay = 0.5
            try:
                while True:
                    resp = await self._call("logs", "get_query_results", queryId=qid)
                    status = resp.get("status")
                    if status in ("Complete", "Failed", "Cancelled", "Timeout"):
                        break
                    if asyncio.get_running_loop().time() > deadline:
                        await self._stop(qid)
                        raise ConnectorError("timeout", f"Logs Insights query did not finish in {self.poll_timeout:.0f}s and was stopped")
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 3)
            except asyncio.CancelledError:
                await self._stop(qid)  # investigation cancelled: do not leave the query running
                raise
        if status != "Complete":
            raise ConnectorError("unavailable", f"Logs Insights query ended with status {status}")
        rows = [{f["field"]: f["value"] for f in row if not f["field"].startswith("@ptr")} for row in resp.get("results", [])[:limit]]
        return {"queryId": qid, "query": final_query, "logGroups": log_groups, "rows": rows,
                "statistics": resp.get("statistics", {}), "start": start.isoformat(), "end": end.isoformat()}

    async def _stop(self, qid: str) -> None:
        try:
            await self._call("logs", "stop_query", queryId=qid)
        except ConnectorError:
            pass

    async def list_metrics(self, namespace: str | None, name: str | None) -> list[dict]:
        kwargs: dict[str, Any] = {}
        if namespace:
            kwargs["Namespace"] = namespace
        if name:
            kwargs["MetricName"] = name
        resp = await self._call("cloudwatch", "list_metrics", **kwargs)
        return [{"namespace": m.get("Namespace"), "metric": m.get("MetricName"), "dimensions": {d["Name"]: d["Value"] for d in m.get("Dimensions") or []}}
                for m in (resp.get("Metrics") or [])[:500]]

    async def metric(self, namespace: str, name: str, dimensions: dict[str, str], stat: str, start: datetime, end: datetime,
                     period: int | None = None) -> dict:
        if end - start > self.max_window:
            raise ConnectorError("bad_request", "window exceeds the connector maximum")
        seconds = max(60, int((end - start).total_seconds()))
        period = period or max(60, (seconds // 120 // 60) * 60 or 60)
        resp = await self._call("cloudwatch", "get_metric_data", StartTime=start, EndTime=end, ScanBy="TimestampAscending", MetricDataQueries=[{
            "Id": "m1", "ReturnData": True,
            "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": name,
                                      "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()]},
                           "Period": period, "Stat": stat},
        }])
        r = (resp.get("MetricDataResults") or [{}])[0]
        values = [[int(t.timestamp()), v] for t, v in zip(r.get("Timestamps", []), r.get("Values", []))][-240:]
        return {"series": [{"metric": {"namespace": namespace, "metric": name, **dimensions, "stat": stat}, "values": values}],
                "period": period, "status": r.get("StatusCode")}

    async def alarms(self, state: str | None) -> list[dict]:
        kw: dict[str, Any] = {"MaxRecords": 100}
        if state:
            kw["StateValue"] = state
        resp = await self._call("cloudwatch", "describe_alarms", **kw)
        return [{"name": a["AlarmName"], "state": a["StateValue"], "reason": a.get("StateReason", "")[:300],
                 "metric": a.get("MetricName"), "namespace": a.get("Namespace"),
                 "updated": a.get("StateUpdatedTimestamp").isoformat() if a.get("StateUpdatedTimestamp") else None}
                for a in resp.get("MetricAlarms", [])]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
