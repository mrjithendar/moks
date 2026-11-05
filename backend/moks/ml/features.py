"""Metric features for anomaly detection, on one fixed grid for training and inference alike.

A cohort is one workload in one namespace of one environment. Pods come and go with every rollout, so series are
aggregated to the workload in PromQL (label_replace strips the ReplicaSet and pod suffixes) and pod churn never
creates a new cohort. Every feature is sampled at the same step and aligned to multiples of it.

  request_rate        requests per second                   rate() of the request counter
  error_ratio         5xx share of requests                 missing, not 0, when traffic is too sparse to say
  latency_p95         seconds                               from the request duration histogram
  cpu_throttling      throttled share of CFS periods
  memory_utilization  working set over the memory limit     missing when no limit is set
  restart_rate        container restarts per hour            from raw counters, with resets handled here

Absent telemetry is not healthy behavior: a feature with no series at all is `absent`, a gap is `missing`, and
both are counted in data quality instead of being filled with zeros. Rates use PromQL's counter-reset handling;
the restart counter is read raw and a decrease is treated as a reset.

The connector caps a query at 25 series and 240 points, so reads are split into chunks below that, and a query
that hits the series cap is reported as incomplete rather than silently scored on part of the namespace.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

FEATURE_SCHEMA = "metrics-anomaly/1"
FEATURES = ("request_rate", "error_ratio", "latency_p95", "cpu_throttling", "memory_utilization", "restart_rate")
SPARSE_RPS = 0.05          # below this, an error ratio says more about chance than about the service
CHUNK_POINTS = 144         # under the connector's 240-point cap, so nothing is downsampled
SERIES_CAP = 25            # the connector's MAX_SERIES
NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

# One series per workload: the pod name minus its ReplicaSet hash and pod suffix (or a StatefulSet ordinal).
_WORKLOAD = 'label_replace({expr}, "workload", "$1", "pod", "^(.+?)(-[a-z0-9]{{6,10}}-[a-z0-9]{{5}}|-[0-9]+)$")'


def queries(namespace: str, metrics: dict[str, str] | None = None) -> dict[str, str]:
    """PromQL per feature, pinned to one namespace. Metric names are configurable because request metrics
    differ by application; the defaults are the Prometheus client conventions and cAdvisor."""
    if not NAME.match(namespace):
        raise ValueError(f"invalid namespace {namespace!r}")
    m = {"requests": "http_requests_total", "duration": "http_request_duration_seconds_bucket", "status_label": "code", "restarts": "kube_pod_container_status_restarts_total", **(metrics or {})}
    for k in ("requests", "duration", "restarts"):
        if not re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$", m[k]):
            raise ValueError(f"invalid metric name for {k}")
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", m["status_label"]):
        raise ValueError("invalid status label")
    sel = f'namespace="{namespace}"'
    cont = f'{sel}, container!="", container!="POD"'
    w = lambda e: f"sum by (workload) ({_WORKLOAD.format(expr=e)})"
    buckets = _WORKLOAD.format(expr=f"rate({m['duration']}{{{sel}}}[5m])")
    throttled = w(f"rate(container_cpu_cfs_throttled_periods_total{{{cont}}}[5m])")
    periods = w(f"rate(container_cpu_cfs_periods_total{{{cont}}}[5m])")
    return {
        "request_rate": w(f"rate({m['requests']}{{{sel}}}[5m])"),
        "errors_rate": w(f"rate({m['requests']}{{{sel}, {m['status_label']}=~\"5..\"}}[5m])"),
        "latency_p95": f"histogram_quantile(0.95, sum by (workload, le) ({buckets}))",
        "cpu_throttling": f"{throttled} / clamp_min({periods}, 1)",
        "memory_working_set": w(f"container_memory_working_set_bytes{{{cont}}}"),
        "memory_limit": w(f"container_spec_memory_limit_bytes{{{cont}}}"),
        "restarts": w(f"{m['restarts']}{{{sel}}}"),
    }


def grid(start: datetime, end: datetime, step: int) -> list[int]:
    """Timestamps aligned to multiples of the step, so two runs sample the same instants."""
    a = math.ceil(start.timestamp() / step) * step
    b = math.floor(end.timestamp() / step) * step
    return list(range(a, b + 1, step))


def chunks(start: datetime, end: datetime, step: int) -> list[tuple[datetime, datetime]]:
    out, cur, span = [], start, timedelta(seconds=step * (CHUNK_POINTS - 1))
    while cur < end:
        nxt = min(end, cur + span)
        out.append((cur, nxt))
        cur = nxt + timedelta(seconds=step)
    return out


def _by_workload(series: list[dict]) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    for s in series:
        name = (s.get("metric") or {}).get("workload")
        if not name:
            continue
        pts = out.setdefault(name, {})
        for ts, v in s.get("values") or []:
            if v is not None:
                pts[int(float(ts))] = float(v)
    return out


def increases(points: dict[int, float], step: int) -> dict[int, float]:
    """Per-step increase of a raw counter. A decrease is a reset (a restarted exporter, a replaced pod), so the
    increase is the new value, never negative. The first point and points after a gap have no increase."""
    out: dict[int, float] = {}
    prev_t, prev_v = None, None
    for t in sorted(points):
        v = points[t]
        if prev_t is not None and t - prev_t == step:
            out[t] = v if v < prev_v else v - prev_v
        prev_t, prev_v = t, v
    return out


@dataclass
class CohortFrame:
    """One workload's features on the grid. None means missing at that instant."""
    environment: str
    namespace: str
    workload: str
    step: int
    timestamps: list[int]
    values: dict[str, list[float | None]]
    quality: dict[str, Any] = field(default_factory=dict)

    @property
    def cohort(self) -> str:
        return f"{self.environment}/{self.namespace}/{self.workload}"

    def rows(self) -> list[tuple[int, list[float | None]]]:
        return [(t, [self.values[f][i] for f in FEATURES]) for i, t in enumerate(self.timestamps)]


def assemble(environment: str, namespace: str, step: int, ts: list[int], raw: dict[str, list[dict]], incomplete: set[str] | None = None) -> list[CohortFrame]:
    """Raw query results to one frame per workload. Pure, so the arithmetic is tested without Prometheus."""
    incomplete = incomplete or set()
    per = {k: _by_workload(v) for k, v in raw.items()}
    workloads = sorted({w for d in per.values() for w in d})
    frames = []
    for wl in workloads:
        req, err, lat = per.get("request_rate", {}).get(wl, {}), per.get("errors_rate", {}).get(wl, {}), per.get("latency_p95", {}).get(wl, {})
        thr, mem, lim = per.get("cpu_throttling", {}).get(wl, {}), per.get("memory_working_set", {}).get(wl, {}), per.get("memory_limit", {}).get(wl, {})
        restarts = per.get("restarts", {}).get(wl, {})
        inc = increases(restarts, step)
        resets = sum(1 for t in sorted(restarts) if t - step in restarts and restarts[t] < restarts[t - step])
        values: dict[str, list[float | None]] = {f: [] for f in FEATURES}
        sparse = 0
        for t in ts:
            r = req.get(t)
            values["request_rate"].append(r)
            if r is None or r < SPARSE_RPS:
                values["error_ratio"].append(None)
                sparse += r is not None
            else:
                values["error_ratio"].append(min(1.0, max(0.0, (err.get(t) or 0.0) / r)))
            values["latency_p95"].append(lat.get(t) if lat.get(t) is not None and math.isfinite(lat[t]) else None)
            values["cpu_throttling"].append(thr.get(t))
            limit = lim.get(t)
            values["memory_utilization"].append(mem[t] / limit if t in mem and limit and limit > 0 else None)
            values["restart_rate"].append(inc[t] * 3600 / step if t in inc else None)
        sources = {"request_rate": req, "error_ratio": req, "latency_p95": lat, "cpu_throttling": thr, "memory_utilization": lim, "restart_rate": restarts}
        quality = {"points": len(ts), "coverage": {f: round(sum(v is not None for v in values[f]) / len(ts), 3) if ts else 0.0 for f in FEATURES},
                   "absent": [f for f in FEATURES if not sources[f]], "sparse_traffic_points": sparse, "counter_resets": resets,
                   "incomplete_queries": sorted(incomplete)}
        frames.append(CohortFrame(environment, namespace, wl, step, ts, values, quality))
    return frames


Query = Callable[[str, datetime, datetime, int], Awaitable[tuple[list[dict], bool]]]


async def collect(query: Query, environment: str, namespace: str, start: datetime, end: datetime, step: int, metrics: dict[str, str] | None = None,
                  workloads: set[str] | None = None) -> list[CohortFrame]:
    """Runs every feature query in chunks and assembles the frames. `query` returns (series, complete) and is
    the caller's authorized path to Prometheus; a failed query leaves its features absent and says so."""
    qs = queries(namespace, metrics)
    raw: dict[str, list[dict]] = {k: [] for k in qs}
    incomplete: set[str] = set()
    for a, b in chunks(start, end, step):
        for name, promql in qs.items():
            try:
                series, complete = await query(promql, a, b, step)
            except Exception:
                incomplete.add(name)
                continue
            if not complete or len(series) >= SERIES_CAP:
                incomplete.add(name)
            raw[name] += series
    frames = assemble(environment, namespace, step, grid(start, end, step), raw, incomplete)
    return [f for f in frames if workloads is None or f.workload in workloads]


def hour_of_week(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.weekday() * 24 + d.hour


def history_days(frame: CohortFrame) -> float:
    """How much representative history there is: points where at least two features were present."""
    usable = sum(1 for _, row in frame.rows() if sum(v is not None for v in row) >= 2)
    return usable * frame.step / 86400
