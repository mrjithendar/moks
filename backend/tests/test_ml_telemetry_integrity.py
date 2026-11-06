"""Anomaly features against Prometheus as it really answers: grid alignment, chunk edges and failed or partial queries.

The fake below answers like /api/v1/query_range: samples at start, start + step, ... up to and including end,
counted from the requested start and not from any grid. The collector under test is the real one.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from moks.db import session_scope
from moks.ml import anomaly, features
from moks.models import AUTOMATION_USER_ID, Environment, RoleBinding
from tests.conftest import make_env

STEP = 300
DAY0 = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
OFF = timedelta(seconds=17)  # the repro: a window starting at 00:00:17


def no_errors(t):
    return None  # no 5xx series at all, which is what rate() over a code=~"5.." selector returns with no errors


class RangePrometheus:
    """values: query name -> f(t) -> value, or None for no sample at t. fail/partial: query name -> set of chunk
    indexes (None for every chunk) that raise, or come back with complete=False and no series."""

    def __init__(self, values=None, fail=None, partial=None, workloads=("payments",)):
        self.qs = {v: k for k, v in features.queries("shop").items()}
        self.values = {"request_rate": lambda t: 10.0, "errors_rate": no_errors, "latency_p95": lambda t: 0.1, "cpu_throttling": lambda t: 0.01,
                       "memory_working_set": lambda t: 1e8, "memory_limit": lambda t: 5e8, "restarts": lambda t: 2.0, **(values or {})}
        self.fail, self.partial, self.workloads = fail or {}, partial or {}, workloads
        self.calls: list[tuple[str, float, float, int]] = []
        self.samples: dict[str, list[int]] = {}

    def _hit(self, rule, name, i):
        return name in rule and (rule[name] is None or i in rule[name])

    async def __call__(self, promql, a, b, step):
        name = self.qs[promql]
        i = sum(1 for c in self.calls if c[0] == name)
        self.calls.append((name, a.timestamp(), b.timestamp(), step))
        if self._hit(self.fail, name, i):
            raise RuntimeError("timeout")
        if self._hit(self.partial, name, i):
            return [], False
        ts, t = [], a.timestamp()
        while t <= b.timestamp():
            ts.append(t)
            t += step
        self.samples.setdefault(name, []).extend(int(x) for x in ts)
        out = []
        for wl in self.workloads:
            vals = [[t, v] for t in ts if (v := self.values[name](t)) is not None]
            if vals:
                out.append({"metric": {"workload": wl}, "values": vals})
        return out, True


async def collect(prom, start, end):
    return (await features.collect(prom, "e", "shop", start, end, STEP))[0]


# ---- finding 1: one grid for queries and frames ---------------------------------------------------------------


def test_align_window_floors_both_ends_and_is_half_open():
    w = features.align_window(DAY0 + OFF, DAY0 + timedelta(hours=1) + OFF, STEP)
    assert (w.start, w.end, w.status) == (int(DAY0.timestamp()), int(DAY0.timestamp()) + 3600, "ok")
    assert w.grid == list(range(w.start, w.end, STEP)) and len(w.grid) == 12  # the end itself is not in the window
    exact = features.align_window(DAY0, DAY0 + timedelta(hours=1), STEP)
    assert exact.grid == w.grid


async def test_an_unaligned_window_is_fully_covered():
    prom = RangePrometheus()
    fr = await collect(prom, DAY0 + OFF, DAY0 + timedelta(days=1) + OFF)
    assert len(fr.timestamps) == 288 and all(t % STEP == 0 for t in fr.timestamps)
    assert fr.quality["coverage"]["request_rate"] == 1.0 and fr.quality["coverage"]["latency_p95"] == 1.0
    assert all(a % STEP == 0 for _, a, _, _ in prom.calls)  # every request starts on the grid


async def test_three_chunks_request_every_grid_timestamp_exactly_once():
    points = 3 * features.CHUNK_POINTS + 10
    start = DAY0 + OFF
    prom = RangePrometheus()
    fr = await collect(prom, start, start + timedelta(seconds=points * STEP))
    expected = list(range(int(DAY0.timestamp()), int(DAY0.timestamp()) + points * STEP, STEP))
    assert fr.timestamps == expected
    assert len({a for n, a, _, _ in prom.calls if n == "request_rate"}) == 4  # chunked
    for name, got in prom.samples.items():
        assert set(got) == set(expected) and len(got) == len(expected), name  # no overlap at chunk edges and no gap
        assert max(Counter(got).values()) == 1
    assert fr.quality["coverage"]["request_rate"] == 1.0


@pytest.mark.parametrize("start,end", [(DAY0 + OFF, DAY0 + OFF + timedelta(seconds=STEP - 1)),  # shorter than one step
                                       (DAY0 + timedelta(seconds=290), DAY0 + timedelta(seconds=310)),  # straddles a boundary, still short
                                       (DAY0 + OFF, DAY0 + OFF),  # empty
                                       (DAY0 + OFF, DAY0)])  # reversed
async def test_short_and_empty_windows_have_an_explicit_status(start, end):
    w = features.align_window(start, end, STEP)
    assert w.status == "insufficient_window" and w.grid == [] and w.reason
    prom = RangePrometheus()
    with pytest.raises(features.WindowError) as exc:
        await features.collect(prom, "e", "shop", start, end, STEP)
    assert exc.value.status == "insufficient_window" and prom.calls == []  # nothing queried, nothing scored at 0%


async def test_training_reports_an_insufficient_window_rather_than_zero_coverage(monkeypatch):
    env = "tw-" + DAY0.strftime("%H%M%S") + "x"
    await make_env(env)
    monkeypatch.setattr(features, "align_window", lambda *a: features.Window(0, 0, STEP, [], "insufficient_window", "shorter than one step"))
    out = await anomaly.train(env, "shop", query=RangePrometheus(), now=DAY0)
    assert out["status"] == "insufficient_window" and out["cohorts"] == []


# ---- finding 2: a failed error query is unknown, not zero ------------------------------------------------------


def ratios(fr):
    return fr.values["error_ratio"]


async def test_successful_zero_error_traffic_is_zero():
    fr = await collect(RangePrometheus(), DAY0, DAY0 + timedelta(hours=2))
    assert ratios(fr) == [0.0] * 24 and fr.quality["coverage"]["error_ratio"] == 1.0


async def test_nonzero_errors_are_a_ratio():
    fr = await collect(RangePrometheus(values={"errors_rate": lambda t: 1.0}), DAY0, DAY0 + timedelta(hours=2))
    assert ratios(fr) == [0.1] * 24


async def test_a_fully_failed_error_query_is_unknown():
    fr = await collect(RangePrometheus(fail={"errors_rate": None}), DAY0, DAY0 + timedelta(hours=2))
    assert ratios(fr) == [None] * 24 and fr.quality["coverage"]["error_ratio"] == 0.0
    assert fr.quality["chunks"]["errors_rate"] == {"success": 0, "failed": 1, "partial": 0} and "errors_rate" in fr.quality["incomplete_queries"]
    assert fr.quality["coverage"]["request_rate"] == 1.0  # request volume was there all along
    assert all(row[1] is None for _, row in fr.rows())  # nothing to train or score on, and no zeros


async def test_a_partial_error_query_is_unknown():
    fr = await collect(RangePrometheus(partial={"errors_rate": None}), DAY0, DAY0 + timedelta(hours=2))
    assert ratios(fr) == [None] * 24 and fr.quality["coverage"]["error_ratio"] == 0.0
    assert fr.quality["chunks"]["errors_rate"]["partial"] == 1


async def test_mixed_chunks_keep_the_successful_ones_and_drop_only_the_failed():
    n = features.CHUNK_POINTS
    prom = RangePrometheus(fail={"errors_rate": {1}}, partial={"errors_rate": {2}})
    fr = await collect(prom, DAY0, DAY0 + timedelta(seconds=3 * n * STEP))
    r = ratios(fr)
    assert r[:n] == [0.0] * n and r[n:] == [None] * (2 * n)
    assert fr.quality["coverage"]["error_ratio"] == round(1 / 3, 3)  # failed and partial chunks never raise coverage
    assert fr.quality["chunks"]["errors_rate"] == {"success": 1, "failed": 1, "partial": 1}
    assert fr.quality["unknown_error_points"] == 2 * n


async def test_errors_present_in_a_partial_chunk_are_not_trusted_either():
    class Partial(RangePrometheus):
        async def __call__(self, promql, a, b, step):
            series, _ = await super().__call__(promql, a, b, step)
            return series, self.qs[promql] != "errors_rate"
    fr = await collect(Partial(values={"errors_rate": lambda t: 1.0}), DAY0, DAY0 + timedelta(hours=1))
    assert ratios(fr) == [None] * 12


async def test_low_traffic_is_still_missing_not_zero():
    fr = await collect(RangePrometheus(values={"request_rate": lambda t: 0.01}), DAY0, DAY0 + timedelta(hours=1))
    assert ratios(fr) == [None] * 12 and fr.quality["sparse_traffic_points"] == 12


def test_zero_is_inferred_only_for_metrics_declared_absent_means_zero():
    ts = [0, 300, 600]
    raw = {"request_rate": [{"metric": {"workload": "w"}, "values": [[t, 10] for t in ts]}], "errors_rate": []}
    assert features.METRIC_DEFINITIONS["errors_rate"].absent_means_zero is True
    assert features.METRIC_DEFINITIONS["restarts"].absent_means_zero is False  # no kube-state-metrics is not zero restarts
    assert ratios(features.assemble("e", "n", 300, ts, raw)[0]) == [0.0, 0.0, 0.0]
    strict = {**features.METRIC_DEFINITIONS, "errors_rate": features.MetricDefinition(absent_means_zero=False)}
    assert ratios(features.assemble("e", "n", 300, ts, raw, definitions=strict)[0]) == [None, None, None]


async def test_the_executor_path_reports_prometheus_warnings_as_partial(monkeypatch):
    """The real automation path: executor, prom_query and a connector whose answer carries warnings."""
    from moks.connectors import registry

    env_name = "tw-warn"
    await make_env(env_name)

    class Conn:
        warn = True

        async def range(self, promql, start, end, step):
            ts, t = [], start.timestamp()
            while t <= end.timestamp():
                ts.append([t, 1.0])
                t += step
            return {"resultType": "matrix", "series": [{"metric": {"workload": "payments"}, "values": ts}], "warnings": ["partial response"] if Conn.warn else []}

        def graph_link(self, promql, minutes):
            return None

    async def fake_for_env(db, eid):
        return {"prometheus": (None, Conn())}, {}

    monkeypatch.setattr(registry, "for_environment", fake_for_env)
    async with session_scope() as db:  # what creating an environment through the API grants automation
        db.add(RoleBinding(user_id=AUTOMATION_USER_ID, role="investigator", environment=env_name, namespace="*", source="automation"))
    async with session_scope() as db:
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one()
        query = await anomaly.prometheus(db, env)
    series, complete = await query("up", DAY0, DAY0 + timedelta(minutes=55), STEP)
    assert len(series[0]["values"]) == 12 and complete is False
    Conn.warn = False
    assert (await query("up", DAY0, DAY0 + timedelta(minutes=55), STEP))[1] is True
