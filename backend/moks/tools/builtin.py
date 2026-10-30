"""Read-only tools: Kubernetes, Prometheus, Loki."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from ..connectors.base import ConnectorError
from ..connectors.loki import logql_escape, selector_namespaces
from ..connectors.prometheus import auto_step
from ..diagnosis.detectors import in_window, is_unhealthy, triage
from ..rbac import P
from ..redaction import redact
from ..scope import ScopeError
from .contracts import K8S_NAME, ToolArgs, ToolContext, ToolResult, ToolSpec

Namespace = Field(pattern=K8S_NAME, max_length=63, description="Kubernetes namespace")
Name = Field(pattern=K8S_NAME, max_length=253)
LABEL_SELECTOR = r"^[A-Za-z0-9_./\-=!,() ]{0,500}$"

# ---- Kubernetes -------------------------------------------------------------------------------


class NamespaceArgs(ToolArgs):
    namespace: str = Namespace


class ListPodsArgs(ToolArgs):
    namespace: str = Namespace
    label_selector: str | None = Field(default=None, pattern=LABEL_SELECTOR, description="e.g. app=checkout")
    unhealthy_only: bool = Field(default=False, description="only pods with problems, restarts or not ready")


class PodArgs(ToolArgs):
    namespace: str = Namespace
    pod: str = Name


class PodLogArgs(ToolArgs):
    namespace: str = Namespace
    pod: str = Name
    container: str | None = Field(default=None, pattern=K8S_NAME)
    previous: bool = Field(default=False, description="logs of the previous (crashed) container instance")
    tail_lines: int = Field(default=150, ge=1, le=500)
    since_minutes: int | None = Field(default=None, ge=1, le=1440)


class EventArgs(ToolArgs):
    namespace: str = Namespace
    object_name: str | None = Field(default=None, pattern=K8S_NAME)
    warnings_only: bool = True


class DeploymentArgs(ToolArgs):
    namespace: str = Namespace
    deployment: str = Name


class NoArgs(ToolArgs):
    pass


async def k8s_triage(ctx: ToolContext, a: NamespaceArgs) -> ToolResult:
    k = ctx.connector("kubernetes")
    pods, truncated = await k.list_pods(a.namespace)
    events = await k.events(a.namespace, warnings_only=True, limit=60)
    services = await k.services(a.namespace)
    workloads = await k.workloads(a.namespace)
    try:
        ingresses = await k.ingresses(a.namespace)
    except ConnectorError:
        ingresses = []
    findings = triage(pods, events, services, workloads, ingresses, since=ctx.time_from)
    owners = {p["name"]: p.get("owner") for p in pods}
    for f in findings:  # skills target the owning workload of a pod finding
        pod_res = next((r for r in f.get("resources", []) if r.startswith("pod/")), None)
        if pod_res and owners.get(pod_res[4:]):
            f["owner"] = owners[pod_res[4:]]
    unhealthy = [p for p in pods if is_unhealthy(p, ctx.time_from)]
    recent_events = [e for e in events if in_window(e.get("lastSeen"), ctx.time_from)]
    summary = (f"{len(pods)} pods ({len(unhealthy)} need attention in the window), {len(workloads)} workloads, {len(services)} services, "
               f"{len(recent_events)} warning events in the window; {len(findings)} findings" + (" [pod list truncated]" if truncated else ""))
    return ToolResult(summary, {
        "findings": findings,
        "pods_truncated": bool(truncated),
        "unhealthyPods": unhealthy[:40],
        "workloads": workloads,
        "services": [{k_: v for k_, v in s.items() if k_ != "podContainerPorts"} for s in services],
        "warningEvents": events[:40],
        "ingresses": ingresses,
    }, {"api": f"GET pods, events, services, endpointslices, deployments, ingresses in namespace {a.namespace}"})


async def k8s_config_audit(ctx: ToolContext, a: NamespaceArgs) -> ToolResult:
    """Configuration hygiene from pod specs and events; Secret/ConfigMap contents are never read."""
    from ..diagnosis.config_audit import audit

    k = ctx.connector("kubernetes")
    pods = await k.raw_pods(a.namespace)
    events = await k.events(a.namespace, warnings_only=True, limit=100)
    r = audit(pods, events)
    high = sum(1 for w in r["workloads"] for i in w["issues"] if i["severity"] == "high")
    return ToolResult(f"{len(r['workloads'])} workloads audited in {a.namespace}: {high} high-severity issues, "
                      f"{len(r['missing_references'])} missing ConfigMap/Secret references; {r['issue_counts']}", r,
                      {"api": f"GET pods and warning events in {a.namespace} (Secret/ConfigMap contents are not read)"})


async def k8s_list_pods(ctx: ToolContext, a: ListPodsArgs) -> ToolResult:
    pods, truncated = await ctx.connector("kubernetes").list_pods(a.namespace, a.label_selector)
    if a.unhealthy_only:
        pods = [p for p in pods if is_unhealthy(p, ctx.time_from)]
    restarting = sorted(pods, key=lambda p: p["restarts"], reverse=True)
    recent = sum(1 for p in pods if any(pr.get("state") == "lastTerminated" and in_window(pr.get("finishedAt"), ctx.time_from) for pr in p["problems"]))
    return ToolResult(
        f"{len(pods)} {'pods needing attention' if a.unhealthy_only else 'pods'} in {a.namespace}" + (f" matching {a.label_selector}" if a.label_selector else "") +
        f"; {recent} restarted since {ctx.time_from.strftime('%H:%M')} UTC" + (" [truncated]" if truncated else ""),
        restarting, {"api": f"GET /api/v1/namespaces/{a.namespace}/pods" + (f"?labelSelector={a.label_selector}" if a.label_selector else "")})


async def k8s_describe_pod(ctx: ToolContext, a: PodArgs) -> ToolResult:
    d = await ctx.connector("kubernetes").describe_pod(a.namespace, a.pod)
    s = d["summary"]
    return ToolResult(f"pod {a.pod}: phase {s['phase']}, ready {s['ready']}, restarts {s['restarts']}, {len(s['problems'])} problems",
                      d, {"api": f"GET /api/v1/namespaces/{a.namespace}/pods/{a.pod} (+ events)"})


async def k8s_pod_logs(ctx: ToolContext, a: PodLogArgs) -> ToolResult:
    since = a.since_minutes * 60 if a.since_minutes else None
    text = await ctx.connector("kubernetes").pod_logs(a.namespace, a.pod, a.container, a.previous, a.tail_lines, since)
    lines = text.splitlines()
    errors = [ln for ln in lines if re.search(r"(?i)\b(error|exception|fatal|panic|traceback|refused|timeout|denied|invalid)\b", ln)]
    return ToolResult(
        f"{len(lines)} log lines from {a.pod}{' (previous container)' if a.previous else ''}; {len(errors)} look like errors",
        {"lines": [redact(ln[:2000]) for ln in lines[-a.tail_lines:]], "errorLines": [redact(ln[:2000]) for ln in errors[-30:]]},
        {"api": f"GET /api/v1/namespaces/{a.namespace}/pods/{a.pod}/log?tailLines={a.tail_lines}&previous={str(a.previous).lower()}"
                + (f"&container={a.container}" if a.container else "")})


async def k8s_events(ctx: ToolContext, a: EventArgs) -> ToolResult:
    ev = await ctx.connector("kubernetes").events(a.namespace, involved_name=a.object_name, warnings_only=a.warnings_only)
    return ToolResult(f"{len(ev)} {'warning ' if a.warnings_only else ''}events in {a.namespace}" + (f" for {a.object_name}" if a.object_name else ""),
                      ev, {"api": f"GET /api/v1/namespaces/{a.namespace}/events"})


async def k8s_workloads(ctx: ToolContext, a: NamespaceArgs) -> ToolResult:
    w = await ctx.connector("kubernetes").workloads(a.namespace)
    degraded = [x for x in w if x["desired"] and (x["ready"] or 0) < x["desired"]]
    return ToolResult(f"{len(w)} workloads in {a.namespace}; {len(degraded)} below desired replicas", w,
                      {"api": f"GET deployments, statefulsets, daemonsets in {a.namespace}"})


async def k8s_rollout_history(ctx: ToolContext, a: DeploymentArgs) -> ToolResult:
    h = await ctx.connector("kubernetes").rollout_history(a.namespace, a.deployment)
    revs = h["revisions"]
    latest = revs[0] if revs else None
    return ToolResult(
        f"deployment {a.deployment}: {len(revs)} revisions; current revision {latest['revision'] if latest else '?'} created {latest['created'] if latest else '?'}",
        h, {"api": f"GET deployment {a.namespace}/{a.deployment} and its ReplicaSets"})


async def k8s_services(ctx: ToolContext, a: NamespaceArgs) -> ToolResult:
    svcs = await ctx.connector("kubernetes").services(a.namespace)
    empty = [s["name"] for s in svcs if s["selector"] and s["endpoints"]["ready"] == 0]
    return ToolResult(f"{len(svcs)} services in {a.namespace}; without ready endpoints: {', '.join(empty) or 'none'}", svcs,
                      {"api": f"GET services, endpointslices, pods in {a.namespace}"})


async def k8s_ingresses(ctx: ToolContext, a: NamespaceArgs) -> ToolResult:
    ing = await ctx.connector("kubernetes").ingresses(a.namespace)
    return ToolResult(f"{len(ing)} ingresses in {a.namespace}", ing, {"api": f"GET /apis/networking.k8s.io/v1/namespaces/{a.namespace}/ingresses"})


async def k8s_nodes(ctx: ToolContext, a: NoArgs) -> ToolResult:
    nodes = await ctx.connector("kubernetes").nodes()
    bad = [n["name"] for n in nodes if not n["ready"] or n["pressure"] or n["unschedulable"]]
    return ToolResult(f"{len(nodes)} nodes; not ready/pressured/cordoned: {', '.join(bad) or 'none'}", nodes, {"api": "GET /api/v1/nodes"})


async def k8s_dns_health(ctx: ToolContext, a: NoArgs) -> ToolResult:
    ctx.scope.require("kube-system")  # CoreDNS lives there; an environment deny list wins
    d = await ctx.connector("kubernetes").dns_health()
    ready = sum(1 for p in d["pods"] if p["ready"].split("/")[0] == p["ready"].split("/")[1] and p["phase"] == "Running")
    return ToolResult(f"CoreDNS: {ready}/{len(d['pods'])} pods ready", d, {"api": "GET kube-system coredns deployment, kube-dns service, pods"})


# ---- Prometheus -------------------------------------------------------------------------------


class PromQueryArgs(ToolArgs):
    promql: str = Field(min_length=1, max_length=2000, description="PromQL expression")
    mode: Literal["instant", "range"] = "range"
    minutes: int | None = Field(default=None, ge=1, le=10080, description="look back this many minutes (bounded by the investigation window)")
    step: int | None = Field(default=None, ge=5, le=3600, description="range step in seconds; default chosen from the window")


class PodResourcesArgs(ToolArgs):
    namespace: str = Namespace
    pod_regex: str = Field(default=".+", max_length=200, pattern=r"^[A-Za-z0-9_.*+?|()\[\]^$-]+$", description="RE2 regex on pod name")
    minutes: int | None = Field(default=None, ge=5, le=10080)


class AlertsArgs(ToolArgs):
    firing_only: bool = True


def _range_link(prom, promql: str, minutes: int) -> dict:
    link = prom.graph_link(promql, minutes)
    return {"query": promql, "link": link} if link else {"query": promql}


async def prom_query(ctx: ToolContext, a: PromQueryArgs) -> ToolResult:
    prom = ctx.connector("prometheus")
    start, end = ctx.window(a.minutes)
    if a.mode == "instant":
        r = await prom.instant(a.promql, end)
    else:
        step = a.step or auto_step(start, end)
        r = await prom.range(a.promql, start, end, step)
        r["step"] = step
    r["start"], r["end"] = start.isoformat(), end.isoformat()
    return ToolResult(f"{a.mode} query returned {len(r['series'])} series", r,
                      _range_link(prom, a.promql, int((end - start).total_seconds() // 60)))


async def prom_pod_resources(ctx: ToolContext, a: PodResourcesArgs) -> ToolResult:
    """Templated, namespace-pinned queries over cAdvisor metrics."""
    prom = ctx.connector("prometheus")
    start, end = ctx.window(a.minutes)
    step = auto_step(start, end)
    sel = f'namespace="{a.namespace}", pod=~"{a.pod_regex}", container!="", container!="POD"'
    queries = {
        "memory_working_set_bytes": f"max by (pod, container) (container_memory_working_set_bytes{{{sel}}})",
        "memory_limit_bytes": f"max by (pod, container) (container_spec_memory_limit_bytes{{{sel}}})",
        "cpu_cores": f"sum by (pod, container) (rate(container_cpu_usage_seconds_total{{{sel}}}[5m]))",
        "cpu_throttled_ratio": f"sum by (pod, container) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m])) / "
                               f"clamp_min(sum by (pod, container) (rate(container_cpu_cfs_periods_total{{{sel}}}[5m])), 1)",
    }
    out = {}
    for key, q in queries.items():
        try:
            out[key] = await prom.range(q, start, end, step)
        except ConnectorError as exc:
            out[key] = {"error": exc.message, "series": []}
    total = sum(len(v.get("series", [])) for v in out.values())
    return ToolResult(f"resource usage for pods ~{a.pod_regex} in {a.namespace}: {total} series", {"queries": queries, "results": out, "step": step},
                      {"queries": list(queries.values())})


async def prom_alerts(ctx: ToolContext, a: AlertsArgs) -> ToolResult:
    rules = await ctx.connector("prometheus").alerts()
    visible = []
    for r in rules:
        active = [x for x in r["active"] if (ctx.scope.allows(x["labels"]["namespace"]) if x["labels"].get("namespace") else ctx.scope.cluster_wide)]
        if a.firing_only and not any(x["state"] == "firing" for x in active):
            continue
        if not ctx.scope.cluster_wide and not active:
            continue
        visible.append({**r, "active": active})
    firing = sum(1 for r in visible for x in r["active"] if x["state"] == "firing")
    return ToolResult(f"{len(visible)} alert rules shown; {firing} firing alert instances", visible, {"api": "GET /api/v1/rules?type=alert"})


async def prom_targets(ctx: ToolContext, a: NoArgs) -> ToolResult:
    t = await ctx.connector("prometheus").targets()
    down = [x for x in t if x["health"] != "up"]
    return ToolResult(f"{len(t)} scrape targets; {len(down)} not up", {"down": down, "total": len(t)}, {"api": "GET /api/v1/targets"})


# ---- Loki -------------------------------------------------------------------------------------


class LokiSearchArgs(ToolArgs):
    namespace: str = Namespace
    pod: str | None = Field(default=None, pattern=r"^[a-z0-9.*\-]{1,253}$", description="pod name or RE2 pattern")
    container: str | None = Field(default=None, pattern=K8S_NAME)
    contains: str | None = Field(default=None, max_length=200, description="case-insensitive text to find")
    trace_id: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{16,32}$")
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{4,100}$")
    errors_only: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    minutes: int | None = Field(default=None, ge=1, le=10080)


class LokiQueryArgs(ToolArgs):
    logql: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=100, ge=1, le=500)
    minutes: int | None = Field(default=None, ge=1, le=10080)

    @field_validator("logql")
    @classmethod
    def _has_selector(cls, v: str) -> str:
        if "{" not in v:
            raise ValueError("LogQL must contain a stream selector")
        return v


def build_logql(a: LokiSearchArgs, ns_label: str) -> str:
    matchers = [f'{ns_label}="{logql_escape(a.namespace)}"']
    if a.pod:
        op = "=~" if any(c in a.pod for c in "*.") else "="
        matchers.append(f'pod{op}"{logql_escape(a.pod.replace("*", ".*") if op == "=~" else a.pod)}"')
    if a.container:
        matchers.append(f'container="{logql_escape(a.container)}"')
    q = "{" + ", ".join(matchers) + "}"
    if a.trace_id:
        q += f' |= "{logql_escape(a.trace_id.lower())}"'
    if a.request_id:
        q += f' |= "{logql_escape(a.request_id)}"'
    if a.contains:
        q += f' |~ "(?i){logql_escape(re.escape(a.contains))}"'
    if a.errors_only:
        q += ' |~ "(?i)(error|exception|fatal|panic|level=(err|error)|\\"level\\":\\"error\\")"'
    return q


async def _loki_run(ctx: ToolContext, logql: str, limit: int, minutes: int | None) -> ToolResult:
    loki = ctx.connector("loki")
    start, end = ctx.window(minutes)
    r = await loki.query_range(logql, start, end, limit=limit)
    for row in r.get("lines", []):
        row["line"] = redact(row["line"])
    n = len(r.get("lines", [])) if r["resultType"] == "streams" else len(r.get("series", []))
    r["start"], r["end"] = start.isoformat(), end.isoformat()
    return ToolResult(f"LogQL returned {n} {'lines' if r['resultType'] == 'streams' else 'series'}", r, {"query": logql})


class LokiLogsArgs(ToolArgs):
    namespace: str | None = Field(default=None, pattern=K8S_NAME, max_length=63, description="omit for every namespace you may read")
    pod: str | None = Field(default=None, pattern=r"^[a-z0-9.*\-]{1,253}$")
    container: str | None = Field(default=None, pattern=K8S_NAME)
    contains: str | None = Field(default=None, max_length=200)
    trace_id: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{16,32}$")
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{4,100}$")
    level: Literal["error", "warn", "info", "debug"] | None = None
    limit: int = Field(default=200, ge=1, le=500)
    minutes: int | None = Field(default=None, ge=1, le=10080)


LEVELS = {"error": "error|err|fatal|critical|panic", "warn": "warn|warning", "info": "info", "debug": "debug|trace"}


async def loki_logs(ctx: ToolContext, a: LokiLogsArgs) -> ToolResult:
    """Logs across every permitted namespace (or one), newest first, with detailed filters."""
    loki = ctx.connector("loki")
    start, end = ctx.window(a.minutes)
    if a.namespace:
        ctx.scope.require(a.namespace)
        namespaces = [a.namespace]
    else:
        namespaces = [n for n in await loki.label_values(loki.namespace_label, start, end) if ctx.scope.allows(n)]
    if not namespaces:
        return ToolResult("no permitted namespaces have logs in this window", {"resultType": "streams", "lines": []}, {})
    ns_re = "|".join(re.escape(n) for n in namespaces[:200])
    matchers = [f'{loki.namespace_label}=~"^({logql_escape(ns_re)})$"']
    if a.pod:
        matchers.append(f'pod=~"{logql_escape(a.pod.replace("*", ".*"))}"')
    if a.container:
        matchers.append(f'container="{a.container}"')
    labels = await loki.labels(start, end)
    if a.level and "level" in labels:
        matchers.append(f'level=~"(?i){LEVELS[a.level]}"')
    q = "{" + ", ".join(matchers) + "}"
    if a.level and "level" not in labels:
        q += f' |~ "(?i)(level\\W{{0,3}}({LEVELS[a.level]})\\b)"'
    if a.trace_id:
        q += f' |= "{a.trace_id.lower()}"'
    if a.request_id:
        q += f' |= "{logql_escape(a.request_id)}"'
    if a.contains:
        q += f' |~ "(?i){logql_escape(re.escape(a.contains))}"'
    r = await loki.query_range(q, start, end, limit=a.limit)
    for row in r.get("lines", []):
        row["line"] = redact(row["line"])
    r["namespaces"] = namespaces
    r["start"], r["end"] = start.isoformat(), end.isoformat()
    return ToolResult(f"{len(r.get('lines', []))} log lines from {len(namespaces)} namespace(s)", r, {"query": q})


# ---- Metric catalog and builder ----------------------------------------------------------------

METRIC_NAME = r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,200}$"
LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,100}$")


class MetricNamesArgs(ToolArgs):
    search: str | None = Field(default=None, max_length=100, pattern=r"^[a-zA-Z0-9_:]*$")


class MetricArgs(ToolArgs):
    metric: str = Field(pattern=METRIC_NAME)
    namespace: str | None = Field(default=None, pattern=K8S_NAME, max_length=63)
    labels: dict[str, str] = Field(default_factory=dict, max_length=10, description="label -> exact value, or ~regex")
    aggregate: Literal["none", "sum", "avg", "max", "min", "count"] = "sum"
    by: list[str] = Field(default_factory=list, max_length=5)
    rate: bool | None = Field(default=None, description="apply rate() over 5m; default: on for counters")
    rate_window: str = Field(default="5m", pattern=r"^[0-9]{1,3}[smh]$")
    topk: int | None = Field(default=None, ge=1, le=100)
    bottomk: int | None = Field(default=None, ge=1, le=100)
    instant: bool = False
    step: int | None = Field(default=None, ge=5, le=3600)
    minutes: int | None = Field(default=None, ge=5, le=10080)


SCOPED_DISCOVERY_NAMESPACES = 20


class MetricMetadataArgs(ToolArgs):
    names: list[str] | None = Field(default=None, max_length=5000, description="limit to these metrics; required unless you may read all namespaces")


class MetricLabelsArgs(ToolArgs):
    metric: str = Field(pattern=METRIC_NAME)
    namespace: str | None = Field(default=None, pattern=K8S_NAME, max_length=63)
    label: str | None = Field(default=None, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,100}$", description="values of this label; omit for label names")


async def prom_metric_labels(ctx: ToolContext, a: MetricLabelsArgs) -> ToolResult:
    """Real labels of a metric. Pinned to a namespace unless the user may read all of them, because
    label values name workloads."""
    env = ctx.environment.name
    if a.namespace:
        ctx.scope.require(a.namespace)
    elif not (ctx.principal.can(P.METRICS_RAW_QUERY, env, "*") and ctx.scope.cluster_wide):
        raise ScopeError("choose a namespace: label values across all namespaces need metrics.raw_query for all namespaces")
    prom = ctx.connector("prometheus")
    start, end = ctx.window(60)
    match = a.metric + (f'{{namespace="{a.namespace}"}}' if a.namespace else "")
    if a.label:
        values = await prom.label_values(a.label, match, start, end, limit=200)
        return ToolResult(f"{len(values)} values of {a.label}", {"label": a.label, "values": values}, {"api": "GET /api/v1/label/<label>/values", "query": match})
    names = [n for n in await prom.label_names(match, start, end) if n != "__name__"]
    return ToolResult(f"{len(names)} labels on {a.metric}", {"labels": names}, {"api": "GET /api/v1/labels", "query": match})


def _sees_all_metrics(ctx: ToolContext) -> bool:
    return ctx.principal.can(P.METRICS_RAW_QUERY, ctx.environment.name, "*") and ctx.scope.cluster_wide


async def prom_metric_metadata(ctx: ToolContext, a: MetricMetadataArgs) -> ToolResult:
    if a.names is None and not _sees_all_metrics(ctx):
        raise ScopeError("metadata for every metric needs metrics.raw_query for all namespaces; ask for the metrics you can read")
    data = await ctx.connector("prometheus").metadata()
    if a.names is not None:
        wanted = set(a.names)
        data = {k: v for k, v in data.items() if k in wanted}
    return ToolResult(f"metadata for {len(data)} metrics", {"metadata": data}, {"api": "GET /api/v1/metadata"})


async def prom_metric_names(ctx: ToolContext, a: MetricNamesArgs) -> ToolResult:
    """All names for environment-wide users. Others see only names with series in a namespace they
    may read, found through series discovery: the global list says what runs on the cluster."""
    prom = ctx.connector("prometheus")
    start, end = ctx.window(60)
    extra: dict = {}
    if _sees_all_metrics(ctx):
        names = await prom.label_values("__name__", None, start, end, limit=5000)
    else:
        permitted = ctx.scope.filter(await prom.label_values("namespace", None, start, end, limit=2000))
        found: set[str] = set()
        for ns in permitted[:SCOPED_DISCOVERY_NAMESPACES]:
            found.update(await prom.label_values("__name__", f'{{namespace="{ns}"}}', start, end, limit=5000))
        names = sorted(found)
        extra = {"scoped": True, "namespaces_checked": permitted[:SCOPED_DISCOVERY_NAMESPACES], "namespaces_total": len(permitted)}
    if a.search:
        names = [n for n in names if a.search.lower() in n.lower()]
    return ToolResult(f"{len(names)} metric names", {"names": names[:5000], **extra}, {"api": "GET /api/v1/label/__name__/values"})


def build_promql(a: MetricArgs) -> str:
    matchers = []
    if a.namespace:
        matchers.append(f'namespace="{a.namespace}"')
    for k, v in a.labels.items():
        if not LABEL_NAME.match(k) or k == "namespace" and a.namespace:
            raise ConnectorError("bad_request", f"invalid label {k!r}")
        val = v[1:] if v.startswith("~") else v
        op = "=~" if v.startswith("~") else "="
        matchers.append(f'{k}{op}"{logql_escape(val)}"')
    sel = a.metric + ("{" + ", ".join(matchers) + "}" if matchers else "")
    counter = a.metric.endswith(("_total", "_count", "_sum", "_bucket"))
    expr = f"rate({sel}[{a.rate_window}])" if (a.rate if a.rate is not None else counter) else sel
    by = [b for b in a.by if LABEL_NAME.match(b)]
    if a.aggregate != "none":
        expr = f"{a.aggregate}{' by (' + ', '.join(by) + ')' if by else ''} ({expr})"
    if a.topk:
        expr = f"topk({a.topk}, {expr})"
    elif a.bottomk:
        expr = f"bottomk({a.bottomk}, {expr})"
    return expr


async def prom_metric(ctx: ToolContext, a: MetricArgs) -> ToolResult:
    """Chart a metric with label filters; without a namespace it needs environment-wide metric access."""
    env = ctx.environment.name
    if a.namespace:
        ctx.scope.require(a.namespace)
    elif not (ctx.principal.can(P.METRICS_RAW_QUERY, env, "*") and ctx.scope.cluster_wide):
        raise ScopeError("choose a namespace: querying a metric across all namespaces needs metrics.raw_query for all namespaces")
    q = build_promql(a)
    prom = ctx.connector("prometheus")
    start, end = ctx.window(a.minutes)
    step = a.step or auto_step(start, end)
    r = await prom.instant(q, end) if a.instant else await prom.range(q, start, end, step)
    r.update(step=step, start=start.isoformat(), end=end.isoformat())
    return ToolResult(f"{a.metric}: {len(r['series'])} series", r, _range_link(prom, q, int((end - start).total_seconds() // 60)))


async def loki_search(ctx: ToolContext, a: LokiSearchArgs) -> ToolResult:
    return await _loki_run(ctx, build_logql(a, ctx.connector("loki").namespace_label), a.limit, a.minutes)


async def loki_query(ctx: ToolContext, a: LokiQueryArgs) -> ToolResult:
    loki = ctx.connector("loki")
    if not ctx.scope.unrestricted:
        pinned = selector_namespaces(a.logql, loki.namespace_label)
        for ns in pinned:
            if ns is None:
                raise ScopeError(f'every stream selector must pin {loki.namespace_label}="<namespace>" for your scope')
            ctx.scope.require(ns)
    return await _loki_run(ctx, a.logql, a.limit, a.minutes)


TOOLS: list[ToolSpec] = [
    ToolSpec("k8s_triage", "Namespace health check: pods, warning events, services/endpoints, workloads, ingresses, with automatic findings. Start here.",
             "kubernetes", NamespaceArgs, k8s_triage),
    ToolSpec("k8s_list_pods", "List pods with status, readiness, restarts and problem reasons.", "kubernetes", ListPodsArgs, k8s_list_pods),
    ToolSpec("k8s_describe_pod", "Pod details: containers, images, resources, probes, state, last state, env names, events.", "kubernetes", PodArgs, k8s_describe_pod),
    ToolSpec("k8s_pod_logs", "Container logs (tail), optionally from the previous crashed instance.", "kubernetes", PodLogArgs, k8s_pod_logs),
    ToolSpec("k8s_events", "Kubernetes events in a namespace, optionally for one object.", "kubernetes", EventArgs, k8s_events),
    ToolSpec("k8s_config_audit", "Configuration hygiene per workload: requests/limits, probes, image pinning, security context, literal secret-looking env vars, Secret/ConfigMap references and missing ones (contents never read).",
             "kubernetes", NamespaceArgs, k8s_config_audit),
    ToolSpec("k8s_workloads", "Deployments, StatefulSets and DaemonSets with replica health and images.", "kubernetes", NamespaceArgs, k8s_workloads),
    ToolSpec("k8s_rollout_history", "Deployment revisions (ReplicaSets) with images and creation times.", "kubernetes", DeploymentArgs, k8s_rollout_history),
    ToolSpec("k8s_services", "Services with selectors, ports, ready endpoints and matching pods.", "kubernetes", NamespaceArgs, k8s_services),
    ToolSpec("k8s_ingresses", "Ingress rules and backends.", "kubernetes", NamespaceArgs, k8s_ingresses),
    ToolSpec("k8s_nodes", "Node readiness, pressure, taints and capacity (cluster-scoped).", "kubernetes", NoArgs, k8s_nodes,
             cluster_scoped=True, namespace_arg=None),
    ToolSpec("k8s_dns_health", "CoreDNS deployment, service endpoints and pods (cluster DNS triage).", "kubernetes", NoArgs, k8s_dns_health,
             cluster_scoped=True, namespace_arg=None),
    ToolSpec("prom_query", "Run a bounded PromQL query (instant or range). Show the exact query to the user.", "prometheus", PromQueryArgs, prom_query,
             extra_permission=P.METRICS_RAW_QUERY, namespace_arg=None),
    ToolSpec("prom_pod_resources", "CPU, memory, memory limit and CPU throttling for pods in a namespace (cAdvisor).", "prometheus",
             PodResourcesArgs, prom_pod_resources),
    ToolSpec("prom_alerts", "Prometheus alert rules and their active/firing instances within your scope.", "prometheus", AlertsArgs, prom_alerts,
             namespace_arg=None),
    ToolSpec("prom_targets", "Scrape targets that are down (cluster-scoped).", "prometheus", NoArgs, prom_targets, cluster_scoped=True, namespace_arg=None),
    ToolSpec("loki_search", "Search logs by namespace/pod/container, text, trace ID or request ID (builds LogQL).", "loki", LokiSearchArgs, loki_search),
    ToolSpec("loki_logs", "Recent logs across every permitted namespace (or one) with pod/container/level/text/trace/request filters.",
             "loki", LokiLogsArgs, loki_logs, namespace_arg=None),
    ToolSpec("prom_metric_labels", "Label names of a metric, or the values of one label, within your scope.", "prometheus", MetricLabelsArgs, prom_metric_labels, namespace_arg=None),
    ToolSpec("prom_metric_metadata", "Type, unit and help text Prometheus has for its metrics.", "prometheus", MetricMetadataArgs, prom_metric_metadata, namespace_arg=None),
    ToolSpec("prom_metric_names", "Catalog of metric names in Prometheus (optionally filtered).", "prometheus", MetricNamesArgs, prom_metric_names,
             namespace_arg=None),
    ToolSpec("prom_metric", "Chart one metric with label filters, aggregation and rate (query built safely; exact PromQL returned).",
             "prometheus", MetricArgs, prom_metric, namespace_arg=None),
    ToolSpec("loki_query", "Run a bounded LogQL query; selectors must pin a permitted namespace.", "loki", LokiQueryArgs, loki_query, namespace_arg=None),
]

def _all_tools() -> list[ToolSpec]:
    from .phase2 import TOOLS as PHASE2

    specs = TOOLS + PHASE2
    # FinOps Kubernetes tools also need live cluster and metrics connectors.
    return [ToolSpec(**{**t.__dict__, "also_requires": ("kubernetes", "prometheus")}) if t.connector_type == "cost_model" else
            ToolSpec(**{**t.__dict__, "also_requires": ("kubernetes",)}) if t.name == "git_compare_deployment" else t for t in specs]


REGISTRY: dict[str, ToolSpec] = {t.name: t for t in _all_tools()}
