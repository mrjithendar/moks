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

Completeness is tracked per query and per chunk: `success`, `failed` (an error, a timeout, a Prometheus status
other than success) or `partial` (warnings, a truncated result, the series cap). A missing sample in a failed or
partial chunk is unknown. It becomes zero only where the metric's definition says absence means zero, and only
inside a chunk that succeeded completely.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class MetricDefinition:
    """What a query's silence means. absent_means_zero: a complete answer with no sample at an instant is a true
    zero, as with rate() over a 5xx selector that matches nothing when there are no errors."""
    absent_means_zero: bool = False


METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    "request_rate": MetricDefinition(),
    "errors_rate": MetricDefinition(absent_means_zero=True),  # no 5xx series means no 5xx responses
    "latency_p95": MetricDefinition(),
    "cpu_throttling": MetricDefinition(),
    "memory_working_set": MetricDefinition(),
    "memory_limit": MetricDefinition(),
    "restarts": MetricDefinition(),  # no kube-state-metrics is not zero restarts
}


class WindowError(ValueError):
    """A window with no whole step in it: reported as `status`, never scored as 0% coverage."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class Window:
    start: int  # epoch seconds, a multiple of step, included
    end: int    # epoch seconds, a multiple of step, excluded
    step: int
    grid: list[int]
    status: str  # "ok" or "insufficient_window"
    reason: str = ""

    def request(self) -> tuple[datetime, datetime]:
        """The bounds to send to /api/v1/query_range. Prometheus answers start, start + step, ... up to and
        including end, so the request starts on the first grid timestamp and ends one second before `end`:
        exactly this window's grid, and never the first timestamp of the next one."""
        return datetime.fromtimestamp(self.start, timezone.utc), datetime.fromtimestamp(self.end - 1, timezone.utc)


def align_window(start: datetime, end: datetime, step: int) -> Window:
    """The one grid every feature query and frame uses, in training, validation and scoring alike.

    Policy: start and end are floored to a multiple of `step` in UTC epoch seconds, and the window is half-open,
    [start, end). The grid is start, start + step, ... < end, so two runs over overlapping periods sample the same
    instants and adjacent windows share no timestamp. A window shorter than one step, or one that holds no
    whole grid timestamp after flooring, is `insufficient_window` with an empty grid."""
    if step < 2:
        raise ValueError("step must be at least 2 seconds")
    s, e = start.timestamp(), end.timestamp()
    a, b = math.floor(s / step) * step, math.floor(e / step) * step
    if e - s < step or b <= a:
        why = "empty or reversed window" if e <= s else f"window of {e - s:.0f}s holds no whole {step}s step"
        return Window(a, a, step, [], "insufficient_window", why)
    return Window(a, b, step, list(range(a, b, step)), "ok")


def chunks(window: Window) -> list[Window]:
    """The window split on the grid into pieces of at most CHUNK_POINTS timestamps: each grid timestamp falls in
    exactly one chunk, with no overlap at the edges and no gap between them."""
    out = []
    for i in range(0, len(window.grid), CHUNK_POINTS):
        g = window.grid[i:i + CHUNK_POINTS]
        out.append(Window(g[0], g[-1] + window.step, window.step, g, "ok"))
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


ChunkStatus = dict[str, list[tuple[Window, str]]]  # query -> each chunk and how it ended: success | failed | partial


def assemble(environment: str, namespace: str, step: int, ts: list[int], raw: dict[str, list[dict]], status: ChunkStatus | None = None,
             definitions: dict[str, MetricDefinition] | None = None) -> list[CohortFrame]:
    """Raw query results to one frame per workload. Pure, so the arithmetic is tested without Prometheus.
    `status` says how the chunk holding each timestamp ended, per query; None means every query succeeded."""
    defs = definitions or METRIC_DEFINITIONS
    ok_at = {q: {t for ch, outcome in log if outcome == "success" for t in ch.grid} for q, log in (status or {}).items()}
    complete = lambda q, t: status is None or t in ok_at.get(q, ())
    incomplete = sorted(q for q, log in (status or {}).items() if any(outcome != "success" for _, outcome in log))
    chunk_counts = {q: Counter(outcome for _, outcome in log) for q, log in (status or {}).items()}
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
        sparse = unknown_errors = inferred_zero = 0
        for t in ts:
            r = req.get(t)
            values["request_rate"].append(r)
            if r is None or r < SPARSE_RPS:
                values["error_ratio"].append(None)
                sparse += r is not None
            elif not complete("errors_rate", t):
                values["error_ratio"].append(None)  # the error query failed or came back partial here: unknown, not 0
                unknown_errors += 1
            elif err.get(t) is not None:
                values["error_ratio"].append(min(1.0, max(0.0, err[t] / r)))
            elif defs["errors_rate"].absent_means_zero:
                values["error_ratio"].append(0.0)  # a complete answer with traffic and no 5xx series: no errors
                inferred_zero += 1
            else:
                values["error_ratio"].append(None)
            values["latency_p95"].append(lat.get(t) if lat.get(t) is not None and math.isfinite(lat[t]) else None)
            values["cpu_throttling"].append(thr.get(t))
            limit = lim.get(t)
            values["memory_utilization"].append(mem[t] / limit if t in mem and limit and limit > 0 else None)
            values["restart_rate"].append(inc[t] * 3600 / step if t in inc else None)
        sources = {"request_rate": req, "error_ratio": req, "latency_p95": lat, "cpu_throttling": thr, "memory_utilization": lim, "restart_rate": restarts}
        quality = {"points": len(ts), "coverage": {f: round(sum(v is not None for v in values[f]) / len(ts), 3) if ts else 0.0 for f in FEATURES},
                   "absent": [f for f in FEATURES if not sources[f]], "sparse_traffic_points": sparse, "counter_resets": resets,
                   "unknown_error_points": unknown_errors, "inferred_zero_error_points": inferred_zero,
                   "incomplete_queries": incomplete, "chunks": {q: {k: c.get(k, 0) for k in ("success", "failed", "partial")} for q, c in chunk_counts.items()}}
        frames.append(CohortFrame(environment, namespace, wl, step, ts, values, quality))
    return frames


Query = Callable[[str, datetime, datetime, int], Awaitable[tuple[list[dict], bool]]]


def _on_grid(series: list[dict], grid: set[int]) -> list[dict]:
    """Keeps only samples on this chunk's own grid, so one chunk can never fill or overwrite another's instants."""
    out = []
    for s in series:
        vals = [p for p in s.get("values") or [] if float(p[0]) == int(float(p[0])) and int(float(p[0])) in grid]
        out.append({**s, "values": vals})
    return out


async def collect(query: Query, environment: str, namespace: str, start: datetime, end: datetime, step: int, metrics: dict[str, str] | None = None,
                  workloads: set[str] | None = None) -> list[CohortFrame]:
    """Runs every feature query in chunks of the aligned window and assembles the frames. `query` returns
    (series, complete) and is the caller's authorized path to Prometheus. Each chunk of each query is recorded
    as success, failed (it raised) or partial (complete=False, or the series cap was reached). Raises
    WindowError when the window holds no whole step."""
    win = align_window(start, end, step)
    if win.status != "ok":
        raise WindowError(win.status, win.reason)
    qs = queries(namespace, metrics)
    raw: dict[str, list[dict]] = {k: [] for k in qs}
    status: ChunkStatus = {k: [] for k in qs}
    for ch in chunks(win):
        a, b = ch.request()
        for name, promql in qs.items():
            try:
                series, complete = await query(promql, a, b, step)
            except Exception:
                status[name].append((ch, "failed"))
                continue
            status[name].append((ch, "success" if complete and len(series) < SERIES_CAP else "partial"))
            raw[name] += _on_grid(series, set(ch.grid))
    frames = assemble(environment, namespace, step, win.grid, raw, status)
    return [f for f in frames if workloads is None or f.workload in workloads]


def hour_of_week(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.weekday() * 24 + d.hour


def history_days(frame: CohortFrame) -> float:
    """How much representative history there is: points where at least two features were present."""
    usable = sum(1 for _, row in frame.rows() if sum(v is not None for v in row) >= 2)
    return usable * frame.step / 86400
