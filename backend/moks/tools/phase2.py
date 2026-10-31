"""Read-only tools: Grafana, CloudWatch, Git, knowledge search, cost."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import Field

from ..connectors.base import ConnectorError
from ..connectors.finops import allocate, parse_cpu, parse_mem, rightsize
from ..db import session_scope
from ..rbac import P
from .contracts import K8S_NAME, ToolArgs, ToolContext, ToolResult, ToolSpec

Namespace = Field(pattern=K8S_NAME, max_length=63)
REPO = r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$"
PATH = r"^[A-Za-z0-9_./@+-]{1,500}$"

# ---- Grafana ----------------------------------------------------------------------------------


class DashSearchArgs(ToolArgs):
    query: str | None = Field(default=None, max_length=200)
    tag: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class DashArgs(ToolArgs):
    uid: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class FiringArgs(ToolArgs):
    firing_only: bool = True


async def grafana_dashboards(ctx: ToolContext, a: DashSearchArgs) -> ToolResult:
    g = ctx.connector("grafana")
    rows = await g.search_dashboards(a.query, a.tag, a.limit, ctx.time_from, ctx.time_to)
    return ToolResult(f"{len(rows)} dashboards" + (f" matching {a.query!r}" if a.query else ""), rows, {"api": "GET /api/search?type=dash-db"})


async def grafana_dashboard(ctx: ToolContext, a: DashArgs) -> ToolResult:
    d = await ctx.connector("grafana").dashboard(a.uid, ctx.time_from, ctx.time_to)
    return ToolResult(f"dashboard {d['title']}: {len(d['panels'])} panels", d, {"api": f"GET /api/dashboards/uid/{a.uid}", "link": d["url"]})


async def grafana_alerts(ctx: ToolContext, a: FiringArgs) -> ToolResult:
    rules = await ctx.connector("grafana").alert_rules()
    out = []
    for r in rules:
        active = [x for x in r["active"] if (ctx.scope.allows(x["labels"]["namespace"]) if x["labels"].get("namespace") else ctx.scope.cluster_wide)]
        if a.firing_only and not any(x["state"].lower().startswith("alerting") or x["state"] == "firing" for x in active):
            continue
        if not ctx.scope.cluster_wide and not active:
            continue
        out.append({**r, "active": active})
    return ToolResult(f"{len(out)} Grafana alert rules shown", out, {"api": "GET /api/prometheus/grafana/api/v1/rules"})


# ---- CloudWatch -------------------------------------------------------------------------------


class LogGroupArgs(ToolArgs):
    prefix: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./#-]{1,200}$")


class InsightsArgs(ToolArgs):
    query: str = Field(min_length=3, max_length=4000, description="CloudWatch Logs Insights query")
    log_groups: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=100, ge=1, le=500)
    minutes: int | None = Field(default=None, ge=1, le=10080)


class CwMetricArgs(ToolArgs):
    namespace: str = Field(pattern=r"^[A-Za-z0-9/_.:-]{1,255}$", description="CloudWatch namespace, e.g. AWS/ApplicationELB")
    metric: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,255}$")
    dimensions: dict[str, str] = Field(default_factory=dict, max_length=10)
    stat: Literal["Average", "Sum", "Maximum", "Minimum", "SampleCount", "p50", "p90", "p95", "p99"] = "Average"
    minutes: int | None = Field(default=None, ge=5, le=10080)


class AlarmArgs(ToolArgs):
    state: Literal["ALARM", "OK", "INSUFFICIENT_DATA"] | None = "ALARM"


async def cw_log_groups(ctx: ToolContext, a: LogGroupArgs) -> ToolResult:
    rows = await ctx.connector("cloudwatch").log_groups(a.prefix)
    return ToolResult(f"{len(rows)} allowed log groups", rows, {"api": "logs:DescribeLogGroups (filtered by allowed_log_groups)"})


async def cw_logs_query(ctx: ToolContext, a: InsightsArgs) -> ToolResult:
    start, end = ctx.window(a.minutes)
    r = await ctx.connector("cloudwatch").insights(a.query, a.log_groups, start, end, a.limit)
    lines = [{"ts": row.get("@timestamp"), "line": row.get("@message") or str(row)} for row in r["rows"]]
    return ToolResult(f"Logs Insights returned {len(r['rows'])} rows from {len(a.log_groups)} log group(s)", {**r, "lines": lines},
                      {"query": r["query"], "scope": {"log_groups": a.log_groups, "start": r["start"], "end": r["end"]}})


async def cw_metric(ctx: ToolContext, a: CwMetricArgs) -> ToolResult:
    start, end = ctx.window(a.minutes)
    r = await ctx.connector("cloudwatch").metric(a.namespace, a.metric, a.dimensions, a.stat, start, end)
    return ToolResult(f"{a.namespace}/{a.metric} {a.stat}: {len(r['series'][0]['values'])} points", r,
                      {"query": f"{a.namespace} {a.metric} {a.dimensions} {a.stat} period={r['period']}s"})


class CwListArgs(ToolArgs):
    namespace: str | None = Field(default=None, pattern=r"^[A-Za-z0-9/_.:-]{1,255}$")
    metric: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,255}$")


async def cw_metrics_list(ctx: ToolContext, a: CwListArgs) -> ToolResult:
    rows = await ctx.connector("cloudwatch").list_metrics(a.namespace, a.metric)
    return ToolResult(f"{len(rows)} CloudWatch metrics", rows, {"api": "cloudwatch:ListMetrics"})


async def cw_alarms(ctx: ToolContext, a: AlarmArgs) -> ToolResult:
    rows = await ctx.connector("cloudwatch").alarms(a.state)
    return ToolResult(f"{len(rows)} CloudWatch alarms in state {a.state or 'any'}", rows, {"api": "cloudwatch:DescribeAlarms"})


# ---- Git --------------------------------------------------------------------------------------


class CommitsArgs(ToolArgs):
    repository: str = Field(pattern=REPO)
    path: str | None = Field(default=None, pattern=PATH)
    hours: int = Field(default=72, ge=1, le=720)
    limit: int = Field(default=20, ge=1, le=100)


class ReadFileArgs(ToolArgs):
    repository: str = Field(pattern=REPO)
    path: str = Field(pattern=PATH)
    ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./-]{1,200}$")


class CompareDeployArgs(ToolArgs):
    namespace: str = Namespace
    deployment: str = Field(pattern=K8S_NAME, max_length=253)
    repository: str = Field(pattern=REPO)


async def git_recent_commits(ctx: ToolContext, a: CommitsArgs) -> ToolResult:
    since = datetime.now(timezone.utc) - timedelta(hours=a.hours)
    rows = await ctx.connector("git").recent_commits(a.repository, a.path, since, a.limit)
    return ToolResult(f"{len(rows)} commits in {a.repository}" + (f" touching {a.path}" if a.path else "") + f" in the last {a.hours} h", rows,
                      {"api": f"GET /repos/{a.repository}/commits"})


async def git_read_file(ctx: ToolContext, a: ReadFileArgs) -> ToolResult:
    r = await ctx.connector("git").read_file(a.repository, a.path, a.ref)
    return ToolResult(f"{a.repository}:{a.path}@{r['ref']} ({len(r['text'])} chars)", r, {"link": r["url"]})


async def git_compare_deployment(ctx: ToolContext, a: CompareDeployArgs) -> ToolResult:
    meta = await ctx.connector("kubernetes").deployment_meta(a.namespace, a.deployment)
    r = await ctx.connector("git").compare_deployment(a.repository, meta["images"], meta["annotations"])
    r["deployment"] = {"name": a.deployment, "namespace": a.namespace, "images": meta["images"], "revision": meta["summary"].get("revision")}
    summary = (f"{a.deployment} runs {r['deployed_commit']['sha'][:8]} ({r['source']}); default branch is {r['behind_default_branch_by']} commit(s) ahead"
               if r.get("resolved") else f"{a.deployment}: deployed revision cannot be tied to Git ({r['reason']})")
    return ToolResult(summary, r, {"link": r.get("compare_url")})


# ---- Knowledge (RAG) -------------------------------------------------------------------------


class KbArgs(ToolArgs):
    query: str = Field(min_length=2, max_length=500, description="What to look for in runbooks, docs, manifests and postmortems")
    k: int = Field(default=6, ge=1, le=12)


async def kb_search(ctx: ToolContext, a: KbArgs) -> ToolResult:
    from ..rag import service

    async with session_scope() as db:
        r = await service.search(db, ctx.principal, a.query, a.k, environment=ctx.environment.name, namespace=ctx.focus_namespace, neighbors=True)
    hits = r["results"]
    summary = f"{len(hits)} knowledge passages ({r.get('mode')}) for {a.query!r}. Documented knowledge only: current state needs live evidence."
    if r["evidence"]["state"] != "supported":
        summary += f" {r['evidence']['reason']}"
    return ToolResult(summary, hits, {"index": r.get("index"), "citations": [h["citation"] for h in hits], "evidence": r["evidence"], "fallback_reason": r.get("fallback_reason")})


# ---- FinOps -----------------------------------------------------------------------------------


class CostArgs(ToolArgs):
    group_by: Literal["service", "account", "region", "usage_type", "tag"] = "service"
    tag_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:/=+@-]{1,128}$")
    days: int = Field(default=14, ge=1, le=90)


class DaysArgs(ToolArgs):
    days: int = Field(default=30, ge=1, le=90)


class NoArgs(ToolArgs):
    pass


class AllocArgs(ToolArgs):
    namespace: str | None = Field(default=None, pattern=K8S_NAME, max_length=63, description="omit for every permitted namespace")
    days: int = Field(default=7, ge=1, le=30)


async def _namespaces(ctx: ToolContext, namespace: str | None) -> list[str]:
    if namespace:
        ctx.scope.require(namespace)
        return [namespace]
    return ctx.scope.filter(await ctx.connector("kubernetes").list_namespaces())[:40]


async def finops_aws_costs(ctx: ToolContext, a: CostArgs) -> ToolResult:
    r = await ctx.connector("aws_cost").costs(a.group_by, a.days, tag_key=a.tag_key)
    top = ", ".join(f"{g['name']} {g['amount']}" for g in r["groups"][:3])
    return ToolResult(f"AWS {r['metric']} {r['total']} {r['unit']} over {a.days} days; top: {top}", r,
                      {"api": "ce:GetCostAndUsage", "scope": {"start": r["start"], "end": r["end"], "group_by": r["group_by"]}})


async def finops_aws_forecast(ctx: ToolContext, a: DaysArgs) -> ToolResult:
    r = await ctx.connector("aws_cost").forecast(a.days)
    return ToolResult(f"forecast for the next {a.days} days: {r['total']} {r['unit']}", r, {"api": "ce:GetCostForecast"})


async def finops_aws_anomalies(ctx: ToolContext, a: DaysArgs) -> ToolResult:
    rows = await ctx.connector("aws_cost").anomalies(a.days)
    return ToolResult(f"{len(rows)} cost anomalies in {a.days} days; total impact {round(sum(r['impact'] for r in rows), 2)}", rows,
                      {"api": "ce:GetAnomalies"})


async def finops_budgets(ctx: ToolContext, a: NoArgs) -> ToolResult:
    rows = await ctx.connector("aws_cost").budgets()
    over = [b["name"] for b in rows if (b["forecast_pct"] or b["actual_pct"] or 0) >= 100]
    return ToolResult(f"{len(rows)} budgets; at or over 100%: {', '.join(over) or 'none'}", rows, {"api": "budgets:DescribeBudgets"})


def _workload_of(pod: dict) -> str:
    meta = pod["metadata"]
    owner = (meta.get("ownerReferences") or [{}])[0]
    name = owner.get("name") or meta["name"]
    h = (meta.get("labels") or {}).get("pod-template-hash")
    if owner.get("kind") == "ReplicaSet" and h and name.endswith("-" + h):
        name = name[: -len(h) - 1]
    return f"{owner.get('kind', 'Pod') if owner.get('kind') != 'ReplicaSet' else 'Deployment'}/{name}"


async def _usage(ctx: ToolContext, namespaces: list[str], days: int) -> tuple[dict, dict, list[str]]:
    prom = ctx.connector("prometheus")
    rng = f"{days * 24}h"
    step = "5m" if days <= 2 else "15m"
    ns = namespaces[0] if len(namespaces) == 1 else "^(" + "|".join(re.escape(n) for n in namespaces) + ")$"
    sel = (f'namespace="{ns}"' if len(namespaces) == 1 else f'namespace=~"{ns}"') + ', container!="", container!="POD"'
    queries = {
        "cpu_avg": f"avg_over_time(sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))[{rng}:{step}])",
        "cpu_p95": f"quantile_over_time(0.95, sum by (namespace, pod, container) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))[{rng}:{step}])",
        "mem_avg": f"avg_over_time(sum by (namespace, pod, container) (container_memory_working_set_bytes{{{sel}}})[{rng}:{step}])",
        "mem_max": f"max_over_time(sum by (namespace, pod, container) (container_memory_working_set_bytes{{{sel}}})[{rng}:{step}])",
    }
    now = datetime.now(timezone.utc)
    values: dict[str, dict[tuple[str, str, str], float]] = {}
    for key, q in queries.items():
        r = await prom.instant(q, now)
        # The series cap (25) is too small for whole namespaces: re-query unshaped when needed.
        values[key] = {(s["metric"].get("namespace", ""), s["metric"].get("pod", ""), s["metric"].get("container", "")): (s.get("value") or [0, 0])[1] or 0.0
                       for s in r.get("all_series", r["series"])}
    return values, queries, list(queries.values())


async def _prices(ctx: ToolContext) -> tuple[float, float, str]:
    model = ctx.connector("cost_model")
    nodes = await ctx.connector("kubernetes").nodes()
    total_cpu = sum(parse_cpu(n["allocatable"].get("cpu")) for n in nodes)
    total_gib = sum(parse_mem(n["allocatable"].get("memory")) for n in nodes) / 1024**3
    cpu_p, gib_p = model.unit_prices(total_cpu, total_gib)
    return cpu_p, gib_p, model.describe()


async def _pods_by_namespace(ctx: ToolContext, namespaces: list[str], named: str | None) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """One unreachable namespace must not fail an all-namespaces report; a namespace asked for by name still does."""
    k = ctx.connector("kubernetes")
    pods: dict[str, list[dict]] = {}
    skipped: dict[str, str] = {}
    for ns in namespaces:
        try:
            pods[ns] = await k.raw_pods(ns)
        except ConnectorError as exc:
            if named:
                raise
            skipped[ns] = exc.message
    return pods, skipped


async def finops_k8s_allocation(ctx: ToolContext, a: AllocArgs) -> ToolResult:
    k = ctx.connector("kubernetes")
    namespaces = await _namespaces(ctx, a.namespace)
    pods_by_ns, skipped = await _pods_by_namespace(ctx, namespaces, a.namespace)
    usage, queries, _ = await _usage(ctx, namespaces, a.days)
    cpu_p, gib_p, model = await _prices(ctx)
    storage_price = ctx.connector("cost_model").storage_gib_month
    agg: dict[tuple[str, str], dict] = {}
    for ns, pods in pods_by_ns.items():
        pvc_size = {}
        if storage_price:
            for pvc in await k.pvcs(ns):
                pvc_size[pvc["name"]] = parse_mem(pvc["capacity"] or pvc["request"])
        for p in pods:
            if p.get("status", {}).get("phase") not in ("Running", "Pending"):
                continue
            wl = _workload_of(p)
            w = agg.setdefault((ns, wl), {"namespace": ns, "workload": wl, "kind": wl.split("/")[0], "pods": 0, "cpu_request": 0.0, "mem_request": 0.0,
                                          "cpu_avg": 0.0, "mem_avg": 0.0, "storage_bytes": 0.0})
            w["pods"] += 1
            for v in p["spec"].get("volumes") or []:
                claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
                if claim:
                    w["storage_bytes"] += pvc_size.get(claim, 0.0)
            for c in p["spec"].get("containers", []):
                req = (c.get("resources") or {}).get("requests") or {}
                w["cpu_request"] += parse_cpu(req.get("cpu"))
                w["mem_request"] += parse_mem(req.get("memory"))
                key = (ns, p["metadata"]["name"], c["name"])
                w["cpu_avg"] += float(usage["cpu_avg"].get(key, 0.0))
                w["mem_avg"] += float(usage["mem_avg"].get(key, 0.0))
    rows = allocate(list(agg.values()), cpu_p, gib_p, a.days * 24, storage_price)
    by_resource = {r: round(sum(x["cost_by_resource"][r] for x in rows), 4) for r in ("cpu", "memory", "storage")}
    by_ns: dict[str, float] = {}
    by_kind: dict[str, float] = {}
    for r in rows:
        r["cpu_request"], r["cpu_avg"] = round(r["cpu_request"], 3), round(r["cpu_avg"], 3)
        r["mem_request_mib"], r["mem_avg_mib"] = round(r.pop("mem_request") / 1024**2), round(r.pop("mem_avg") / 1024**2)
        r["storage_gib"] = round(r.pop("storage_bytes") / 1024**3, 2)
        by_ns[r["namespace"]] = by_ns.get(r["namespace"], 0) + r["cost"]
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["cost"]
    total, idle = round(sum(r["cost"] for r in rows), 2), round(sum(r["idle_cost"] for r in rows), 2)
    scope_txt = a.namespace or f"{len(namespaces)} namespaces"
    return ToolResult(f"{scope_txt}: est. cost {total} over {a.days} d ({len(rows)} workloads; cpu {by_resource['cpu']}, memory {by_resource['memory']}, "
                      f"storage {by_resource['storage']}), idle {idle}",
                      {"workloads": rows, "total": total, "idle": idle, "days": a.days, "namespaces": namespaces, "skipped_namespaces": skipped,
                       "by_resource": by_resource, "by_namespace": {k: round(v, 4) for k, v in sorted(by_ns.items(), key=lambda kv: -kv[1])},
                       "by_kind": {k: round(v, 4) for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])},
                       "unit_prices": {"cpu_hour": round(cpu_p, 6), "gib_hour": round(gib_p, 6), "storage_gib_month": storage_price}, "cost_model": model,
                       "note": "Current pods only; usage from cAdvisor via Prometheus; cost charged on max(request, usage)."},
                      {"queries": list(queries.values())})


async def finops_rightsizing(ctx: ToolContext, a: AllocArgs) -> ToolResult:
    namespaces = await _namespaces(ctx, a.namespace)
    pods_by_ns, skipped = await _pods_by_namespace(ctx, namespaces, a.namespace)
    pods = [p for rows in pods_by_ns.values() for p in rows]
    usage, queries, _ = await _usage(ctx, namespaces, a.days)
    cpu_p, gib_p, model = await _prices(ctx)
    per: dict[tuple[str, str, str], dict] = {}
    for p in pods:
        if p.get("status", {}).get("phase") != "Running":
            continue
        wl = _workload_of(p)
        for c in p["spec"].get("containers", []):
            res = c.get("resources") or {}
            pns = p["metadata"]["namespace"]
            key = (pns, wl, c["name"])
            e = per.setdefault(key, {"namespace": pns, "workload": wl, "container": c["name"], "replicas": 0,
                                     "cpu_request": parse_cpu((res.get("requests") or {}).get("cpu")),
                                     "cpu_limit": parse_cpu((res.get("limits") or {}).get("cpu")),
                                     "mem_request": parse_mem((res.get("requests") or {}).get("memory")),
                                     "mem_limit": parse_mem((res.get("limits") or {}).get("memory")), "cpu_p95": 0.0, "mem_max": 0.0})
            e["replicas"] += 1
            k = (pns, p["metadata"]["name"], c["name"])
            e["cpu_p95"] = max(e["cpu_p95"], float(usage["cpu_p95"].get(k, 0.0)))
            e["mem_max"] = max(e["mem_max"], float(usage["mem_max"].get(k, 0.0)))
    recs = rightsize(list(per.values()), cpu_p, gib_p)
    saving = round(sum(r["monthly_saving"] for r in recs if r["action"] == "reduce"), 2)
    risks = sum(1 for r in recs if r["action"] == "increase")
    return ToolResult(f"{a.namespace or f'{len(namespaces)} namespaces'}: {len(recs)} rightsizing recommendations; potential saving {saving}/month; {risks} under-provisioned",
                      {"recommendations": recs, "monthly_saving": saving, "days": a.days, "cost_model": model, "skipped_namespaces": skipped,
                       "note": "Recommendations need a reviewed config patch (action plan) to apply."},
                      {"queries": list(queries.values())})


async def eks_cluster(ctx: ToolContext, a: NoArgs) -> ToolResult:
    """EKS control plane (read-only): needs config.cluster_name and config.region on the kubernetes connector."""
    import asyncio

    from ..connectors.kubernetes import eks_control_plane

    entry = ctx.connectors.get("kubernetes")
    cfg = (entry[0].config or {}) if entry and entry[0] is not None else {}
    if not cfg.get("cluster_name"):
        raise ConnectorError("config", "set cluster_name and region on this environment's kubernetes connector to read the EKS control plane")
    r = await asyncio.to_thread(eks_control_plane, cfg["cluster_name"], cfg.get("region") or "us-east-1", cfg.get("role_arn"))
    issues = len(r["cluster"]["health_issues"]) + sum(len(n["health_issues"]) for n in r["nodegroups"]) + sum(len(x["health_issues"]) for x in r["addons"])
    return ToolResult(f"EKS {r['cluster']['name']} {r['cluster']['version']} {r['cluster']['status']}: {len(r['nodegroups'])} node groups, "
                      f"{len(r['addons'])} add-ons, {issues} health issues", r, {"api": "eks:DescribeCluster, ListNodegroups/DescribeNodegroup, ListAddons/DescribeAddon"})


TOOLS: list[ToolSpec] = [
    ToolSpec("eks_cluster", "EKS control plane: version, status, endpoint access, logging, node groups (scaling, health), add-ons and Fargate profiles.",
             "kubernetes", NoArgs, eks_cluster, cluster_scoped=True, namespace_arg=None),
    ToolSpec("grafana_dashboards", "Search Grafana dashboards (links open at the investigation window).", "grafana", DashSearchArgs, grafana_dashboards, namespace_arg=None),
    ToolSpec("grafana_dashboard", "Panels and their queries for one Grafana dashboard.", "grafana", DashArgs, grafana_dashboard, namespace_arg=None),
    ToolSpec("grafana_alerts", "Grafana-managed alert rules and their firing instances within your scope.", "grafana", FiringArgs, grafana_alerts, namespace_arg=None),
    ToolSpec("cw_log_groups", "CloudWatch log groups this environment may query.", "cloudwatch", LogGroupArgs, cw_log_groups, cluster_scoped=True, namespace_arg=None),
    ToolSpec("cw_logs_query", "Run a bounded CloudWatch Logs Insights query on allowed log groups.", "cloudwatch", InsightsArgs, cw_logs_query, cluster_scoped=True, namespace_arg=None),
    ToolSpec("cw_metric", "One CloudWatch metric over the window.", "cloudwatch", CwMetricArgs, cw_metric, cluster_scoped=True, namespace_arg=None),
    ToolSpec("cw_metrics_list", "Discover CloudWatch metrics and their dimensions.", "cloudwatch", CwListArgs, cw_metrics_list, cluster_scoped=True, namespace_arg=None),
    ToolSpec("cw_alarms", "CloudWatch alarms by state.", "cloudwatch", AlarmArgs, cw_alarms, cluster_scoped=True, namespace_arg=None),
    ToolSpec("git_recent_commits", "Recent commits in an allowed repository, optionally for one path.", "git", CommitsArgs, git_recent_commits, cluster_scoped=True, namespace_arg=None),
    ToolSpec("git_read_file", "Read one file (manifest, config, runbook) from an allowed repository; secrets are never read.", "git", ReadFileArgs, git_read_file, cluster_scoped=True, namespace_arg=None),
    ToolSpec("git_compare_deployment", "Tie a running Deployment to its Git commit when image tags/annotations carry a SHA; never guesses.", "git", CompareDeployArgs, git_compare_deployment),
    ToolSpec("kb_search", "Search runbooks, docs, manifests and approved postmortems you may read (cited passages).", "knowledge", KbArgs, kb_search, namespace_arg=None),
    ToolSpec("finops_aws_costs", "AWS cost by service/account/region/usage type/tag (Cost Explorer, cached).", "aws_cost", CostArgs, finops_aws_costs,
             cluster_scoped=True, namespace_arg=None, extra_permission=P.FINOPS_READ),
    ToolSpec("finops_aws_forecast", "AWS cost forecast.", "aws_cost", DaysArgs, finops_aws_forecast, cluster_scoped=True, namespace_arg=None, extra_permission=P.FINOPS_READ),
    ToolSpec("finops_aws_anomalies", "AWS Cost Anomaly Detection findings with root causes.", "aws_cost", DaysArgs, finops_aws_anomalies,
             cluster_scoped=True, namespace_arg=None, extra_permission=P.FINOPS_READ),
    ToolSpec("finops_budgets", "AWS Budgets: limit, actual and forecast spend.", "aws_cost", NoArgs, finops_budgets, cluster_scoped=True, namespace_arg=None, extra_permission=P.FINOPS_READ),
    ToolSpec("finops_k8s_allocation", "Estimated cost per workload (cpu/memory/storage, idle) in one namespace or all permitted ones.", "cost_model",
             AllocArgs, finops_k8s_allocation, extra_permission=P.FINOPS_READ, namespace_arg=None),
    ToolSpec("finops_rightsizing", "CPU/memory request recommendations from p95/max usage, with savings and OOM risk (one or all namespaces).", "cost_model",
             AllocArgs, finops_rightsizing, extra_permission=P.FINOPS_READ, namespace_arg=None),
]
