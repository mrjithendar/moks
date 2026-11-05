"""Anomaly detection in shadow mode, model governance, and semantic skill recommendation.

Prometheus is a fake that answers the exact feature queries with synthetic, seasonal series (the numbers are made
up for the test and say nothing about any real system). Everything else is real: PostgreSQL, the registry,
artifact storage and validation, the job handler and the investigation engine.
"""
import json
import math
import pickle
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from moks.db import session_scope
from moks.ml import anomaly, artifacts, baseline, features, iforest, registry, skill_rank
from moks.models import ActionPlan, AuditEvent, DetectionState, Incident, MaintenanceWindow, MLArtifact, MLCapability, MLModel, MLObservation
from tests.conftest import login, make_env, make_user

STEP = 300
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def series_value(name: str, t: int, spike_after: int | None = None) -> float | None:
    day = math.sin(2 * math.pi * (t % 86400) / 86400)
    jitter = ((t // STEP) * 7919 % 13) / 130  # deterministic noise
    spiking = spike_after is not None and t >= spike_after
    return {
        "request_rate": 20 + 10 * day + jitter,
        "errors_rate": (0.2 + jitter / 10) * (40 if spiking else 1),
        "latency_p95": (0.12 + 0.03 * day + jitter / 100) * (6 if spiking else 1),
        "cpu_throttling": 0.02 + jitter / 100,
        "memory_working_set": 200e6 + 20e6 * day,
        "memory_limit": 512e6,
        "restarts": 3.0,
    }[name]


class FakePrometheus:
    def __init__(self, namespace="shop", workloads=("payments",), spike_after=None, absent=(), fail=(), reset_at=None):
        self.qs = {v: k for k, v in features.queries(namespace).items()}
        self.workloads, self.spike_after, self.absent, self.fail, self.reset_at = workloads, spike_after, set(absent), set(fail), reset_at
        self.calls = []

    async def __call__(self, promql, a, b, step):
        name = self.qs[promql]
        self.calls.append((name, a, b, step))
        if name in self.fail:
            raise RuntimeError("down")
        if name in self.absent:
            return [], True
        ts = features.grid(a, b, step)
        assert len(ts) <= features.CHUNK_POINTS  # never above the connector's downsampling threshold
        out = []
        for wl in self.workloads:
            vals = []
            for t in ts:
                v = series_value(name, t, self.spike_after)
                if name == "restarts" and self.reset_at and t >= self.reset_at:
                    v = 1.0  # an exporter restart: the counter starts again
                vals.append([t, v])
            out.append({"metric": {"workload": wl}, "values": vals})
        return out, True


async def env_with_admin():
    env = f"an-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    email = f"{env}@example.com"
    await make_user(email, ["admin"])
    return env, email


async def set_mode(name, mode):
    async with session_scope() as db:
        await db.execute(update(MLCapability).where(MLCapability.name == name).values(mode=mode))


# ---- features ------------------------------------------------------------------------------------------------


def test_counter_resets_sparse_traffic_and_absent_telemetry_are_kept_apart():
    ts = [0, 300, 600, 900, 1200]
    raw = {"request_rate": [{"metric": {"workload": "w"}, "values": [[0, 10], [300, 0.01], [600, 10], [900, None], [1200, 10]]}],
           "errors_rate": [{"metric": {"workload": "w"}, "values": [[0, 1], [300, 0.01], [600, 0], [1200, 1]]}],
           "restarts": [{"metric": {"workload": "w"}, "values": [[0, 5], [300, 6], [600, 1], [900, 2], [1200, 2]]}],
           "memory_working_set": [{"metric": {"workload": "w"}, "values": [[t, 100] for t in ts]}]}
    fr = features.assemble("e", "n", 300, ts, raw)[0]
    assert fr.values["error_ratio"] == [0.1, None, 0.0, None, 0.1]  # sparse traffic and a gap are missing, not zero
    assert fr.values["restart_rate"] == [None, 12.0, 12.0, 12.0, 0.0]  # 6->1 is a reset: an increase of 1, never negative
    assert fr.quality["counter_resets"] == 1 and fr.quality["sparse_traffic_points"] == 1
    assert fr.values["memory_utilization"] == [None] * 5  # no limit, so no ratio
    assert set(fr.quality["absent"]) == {"latency_p95", "cpu_throttling", "memory_utilization"}


async def test_collection_is_chunked_below_the_point_cap_and_a_failed_query_is_named():
    prom = FakePrometheus(fail={"latency_p95"})
    frames = await features.collect(prom, "e", "shop", NOW - timedelta(days=1), NOW, STEP)
    assert {c[0] for c in prom.calls} == set(features.queries("shop")) and all(len(features.grid(a, b, s)) <= 144 for _, a, b, s in prom.calls)
    assert "latency_p95" in frames[0].quality["incomplete_queries"] and "latency_p95" in frames[0].quality["absent"]
    with pytest.raises(ValueError):
        features.queries('shop"} or vector(1) #')


# ---- algorithms -----------------------------------------------------------------------------------------------


async def frame(days, spike_after=None, **kw):
    return (await features.collect(FakePrometheus(spike_after=spike_after, **kw), "e", "shop", NOW - timedelta(days=days), NOW, STEP))[0]


async def test_the_seasonal_baseline_flags_a_spike_and_not_the_daily_cycle():
    fr = await frame(10)
    model = baseline.fit([fr])
    calm = anomaly.window(fr, int((NOW - timedelta(hours=1)).timestamp()), int(NOW.timestamp()) + 1)
    assert not baseline.score(model, calm)["unusual"]
    spiked = await frame(10, spike_after=int((NOW - timedelta(minutes=50)).timestamp()))
    r = baseline.score(model, anomaly.window(spiked, int((NOW - timedelta(hours=1)).timestamp()), int(NOW.timestamp()) + 1))
    assert r["unusual"] and r["deviations"][0]["feature"] in ("error_ratio", "latency_p95") and r["deviations"][0]["direction"] == "higher"


async def test_the_isolation_forest_scores_outliers_higher_and_is_deterministic():
    fr = await frame(8)
    rows = [row for _, row in fr.rows()]
    a, b = iforest.fit(rows, seed=3), iforest.fit(rows, seed=3)
    assert a == b
    normal = iforest.score(a, rows[100])
    odd = iforest.score(a, [r * 10 if r else r for r in rows[100]])
    assert odd > normal and odd > a["threshold"]
    assert iforest.score(a, [None] * 6) is None  # nothing to score is not ordinary
    with pytest.raises(ValueError):
        iforest.fit(rows[:5])


# ---- artifacts ----------------------------------------------------------------------------------------------------


def test_artifacts_refuse_tampering_mismatches_and_pickles():
    state = {f: {"week": {}, "day": {}, "all": [1.0, 0.1], "samples": 10} for f in features.FEATURES}
    body, sha = artifacts.encode(algorithm="seasonal_baseline", cohort="e/n/w", feature_schema=features.FEATURE_SCHEMA, features=list(features.FEATURES), step=STEP, state=state)
    ok = dict(algorithm="seasonal_baseline", feature_schema=features.FEATURE_SCHEMA, features=list(features.FEATURES), step=STEP)
    assert artifacts.decode(body, sha, **ok)["state"] == state
    cases = {
        "checksum mismatch": (body.replace(b'"samples":10', b'"samples":11'), sha, ok),
        "different feature schema": (body, sha, {**ok, "features": list(features.FEATURES)[:5]}),
        "s step": (body, sha, {**ok, "step": 60}),
        "not a isolation_forest": (body, sha, {**ok, "algorithm": "isolation_forest"}),
        "not JSON": (pickle.dumps(state), __import__("hashlib").sha256(pickle.dumps(state)).hexdigest(), ok),
    }
    for why, (b, h, exp) in cases.items():
        with pytest.raises(artifacts.ArtifactError, match=why.split(" ", 1)[1] if why.startswith("s ") else why):
            artifacts.decode(b, h, **exp)
    future = json.loads(body)
    future["library"]["moks_ml"] = "2.0"
    fb = json.dumps(future, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(artifacts.ArtifactError, match="this build reads"):
        artifacts.decode(fb, __import__("hashlib").sha256(fb).hexdigest(), **ok)


def test_a_malformed_forest_is_refused_before_it_scores():
    good = iforest.fit([[float(i % 7), float(i % 5), 1, 1, 1, 1] for i in range(64)], trees=5)
    iforest.validate(good, 6)
    bad = json.loads(json.dumps(good))
    bad["trees"][0] = {"d": 99, "s": 0, "l": {"n": 1}, "r": {"n": 1}}
    with pytest.raises(ValueError):
        iforest.validate(bad, 6)
    with pytest.raises(ValueError):
        iforest.validate({**good, "__reduce__": "os.system"}, 6)


# ---- training, governance and shadow inference -------------------------------------------------------------------


async def test_training_needs_representative_history():
    env, _ = await env_with_admin()
    out = await anomaly.train(env, "shop", query=FakePrometheus(), now=NOW - timedelta(days=12))  # the fake has data, but only 2 days are asked for below
    assert out["status"] == "ok"
    from moks.config import get_settings

    s = get_settings()
    old = s.ml_anomaly_training_days
    s.ml_anomaly_training_days = 2
    try:
        short = await anomaly.train(env, "shop", query=FakePrometheus(), now=NOW)
    finally:
        s.ml_anomaly_training_days = old
    assert short["cohorts"][0]["status"] == "insufficient_history" and "days of usable history" in short["cohorts"][0]["reason"]


async def test_absent_telemetry_is_not_modeled_as_healthy():
    env, _ = await env_with_admin()
    out = await anomaly.train(env, "shop", query=FakePrometheus(absent={"request_rate", "errors_rate", "latency_p95", "cpu_throttling", "memory_limit"}), now=NOW)
    c = out["cohorts"][0]
    assert c["status"] == "insufficient_history" and c["history_days"] == 0  # one feature at a time is not usable history
    assert {"request_rate", "error_ratio", "latency_p95", "cpu_throttling", "memory_utilization"} <= set(c["quality"]["absent"])


async def trained(env):
    out = await anomaly.train(env, "shop", query=FakePrometheus(), now=NOW - timedelta(hours=2))
    models = {m["algorithm"]: m for m in out["cohorts"][0]["models"]}
    assert set(models) == {"seasonal_baseline", "isolation_forest"} and all(m["status"] == "candidate" for m in models.values())
    return models


async def test_training_registers_evaluated_candidates_and_promotion_is_explicit_and_gated():
    env, _ = await env_with_admin()
    models = await trained(env)
    b = models["seasonal_baseline"]
    assert {g["name"] for g in b["metrics"]["gates"]} == {"history", "validation_windows", "flags_per_day", "feature_coverage"}
    assert b["metrics"]["detection"]["incidents"] == 0 and "cannot be measured" in b["metrics"]["detection"]["note"]  # no labels, so no recall is claimed
    async with session_scope() as db:
        m = await db.get(MLModel, uuid.UUID(b["id"]))
        assert m.status == "candidate" and m.artifact_sha256 and m.provenance["commit"] and m.dataset_version.startswith("prometheus:")
        await set_mode("anomaly_detection", "disabled")
        with pytest.raises(ValueError, match="disabled"):
            await registry.promote(db, m, None, "champion")
        await set_mode("anomaly_detection", "shadow")
        m.metrics = {**m.metrics, "gates": [{"name": "flags_per_day", "passed": False}]}
        with pytest.raises(ValueError, match="gates not passed"):
            await registry.promote(db, m, None, "champion")
    await set_mode("anomaly_detection", "disabled")


async def test_one_champion_per_cohort_and_rollback_restores_the_previous_one():
    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        first = await trained(env)
        second = await trained(env)
        async with session_scope() as db:
            a = await db.get(MLModel, uuid.UUID(first["seasonal_baseline"]["id"]))
            b = await db.get(MLModel, uuid.UUID(second["isolation_forest"]["id"]))
            await registry.promote(db, a, None, "champion")
            out = await registry.promote(db, b, None, "champion")
            assert out["replaced"]["id"] == str(a.id)
            n = (await db.execute(select(func.count()).select_from(MLModel).where(MLModel.cohort == a.cohort, MLModel.status == "champion"))).scalar_one()
            assert n == 1
            back = await registry.rollback(db, "anomaly_detection", a.cohort, None)
            assert back["champion"]["id"] == str(a.id) and back["rolled_back"]["id"] == str(b.id)
        third = await trained(env)  # trained while a champion exists: compared on the same validation windows
        cmp = third["isolation_forest"]["metrics"]["champion_comparison"]
        assert cmp["champion"] == str(a.id) and cmp["champion_algorithm"] == "seasonal_baseline" and "champion_flags_per_day" in cmp
    finally:
        await set_mode("anomaly_detection", "disabled")


async def champion_for(env):
    models = await trained(env)
    async with session_scope() as db:
        m = await db.get(MLModel, uuid.UUID(models["seasonal_baseline"]["id"]))
        await registry.promote(db, m, None, "champion")
        return m.id, m.cohort, m.artifact_sha256


async def mutations():
    async with session_scope() as db:
        return [(await db.execute(select(func.count()).select_from(t))).scalar_one() for t in (Incident, DetectionState, ActionPlan)] + [
            (await db.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.action.like("incident%") | AuditEvent.action.like("action%")))).scalar_one()]


async def test_shadow_inference_records_observations_and_changes_nothing_operational():
    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        mid, cohort, _ = await champion_for(env)
        before = await mutations()
        out = await anomaly.tick(env, query=FakePrometheus(spike_after=int((NOW - timedelta(minutes=40)).timestamp())), now=NOW)
        calm = await anomaly.tick(env, query=FakePrometheus(), now=NOW - timedelta(days=1))
        after = await mutations()
        async with session_scope() as db:
            obs = (await db.execute(select(MLObservation).where(MLObservation.model_id == mid).order_by(MLObservation.window_to))).scalars().all()
    finally:
        await set_mode("anomaly_detection", "disabled")
    assert out["observations"] == 1 and calm["observations"] == 1 and before == after  # no incident, detection state, plan or audit of either
    calm_obs, spiked = obs
    assert spiked.status == "scored" and spiked.unusual and spiked.mode == "shadow" and spiked.context["role"] == "champion"
    assert not calm_obs.unusual and spiked.deviations[0]["direction"] == "higher" and "Not a fault" in spiked.context["note"]
    assert "drift_psi" in spiked.data_quality


async def test_maintenance_missing_telemetry_and_a_tampered_artifact_are_recorded_not_scored():
    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        mid, cohort, sha = await champion_for(env)
        async with session_scope() as db:
            from moks.models import Environment, User

            e = (await db.execute(select(Environment).where(Environment.name == env))).scalar_one()
            u = (await db.execute(select(User).limit(1))).scalar_one()
            db.add(MaintenanceWindow(environment_id=e.id, namespace="shop", rule_id="*", starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=1), reason="planned upgrade", created_by=u.id))
        spike = FakePrometheus(spike_after=int((NOW - timedelta(minutes=40)).timestamp()))
        await anomaly.tick(env, query=spike, now=NOW)
        await anomaly.tick(env, query=FakePrometheus(absent=set(features.queries("shop"))), now=NOW + timedelta(minutes=30))
        async with session_scope() as db:
            row = await db.get(MLArtifact, sha)
            row.body = row.body.replace(b'"format_version":1', b'"format_version":1 ')
        await anomaly.tick(env, query=FakePrometheus(), now=NOW + timedelta(hours=3))
        async with session_scope() as db:
            obs = (await db.execute(select(MLObservation).where(MLObservation.model_id == mid).order_by(MLObservation.window_to))).scalars().all()
    finally:
        await set_mode("anomaly_detection", "disabled")
    assert [o.status for o in obs] == ["maintenance", "no_telemetry", "model_invalid"]
    assert not obs[0].unusual and obs[0].context["maintenance"]["reason"] == "planned upgrade"
    assert "checksum mismatch" in obs[2].data_quality["error"]


async def test_disabled_means_no_scoring_and_no_scheduling():
    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "disabled")
    out = await anomaly.tick(env, query=FakePrometheus(), now=NOW)
    assert out == {"skipped": "anomaly_detection is disabled"}


async def test_observations_are_scoped_and_feedback_is_a_judgment(client):
    env, admin = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        await champion_for(env)
        await anomaly.tick(env, query=FakePrometheus(), now=datetime.now(timezone.utc))
    finally:
        await set_mode("anomaly_detection", "disabled")
    other = f"o-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(other, [f"investigator:{env}:billing"])
    h = await login(client, other)
    assert (await client.get("/moks/api/ml/observations", params={"environment": env}, headers=h)).json()["observations"] == []
    h = await login(client, admin)
    obs = (await client.get("/moks/api/ml/observations", params={"environment": env}, headers=h)).json()["observations"]
    assert obs and obs[0]["cohort"].startswith(f"{env}/shop/")
    r = await client.post(f"/moks/api/ml/observations/{obs[0]['id']}/feedback", json={"verdict": "false_positive", "note": "batch job"}, headers=h)
    assert r.status_code == 200 and r.json()["feedback"]["verdict"] == "false_positive"
    viewer = f"v-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(viewer, [f"viewer:{env}:*"])
    hv = await login(client, viewer)
    assert (await client.put("/moks/api/ml/capabilities/anomaly_detection", json={"mode": "shadow", "reason": "try"}, headers=hv)).status_code == 403
    h = await login(client, admin)
    assert (await client.put("/moks/api/ml/capabilities/anomaly_detection", json={"mode": "enabled", "reason": "try"}, headers=h)).status_code == 422  # shadow is the most it allows


# ---- skill recommendations --------------------------------------------------------------------------------------


class Meta:
    def __init__(self, name, description, findings=(), keywords=()):
        from moks.skills import Triggers

        self.name, self.title, self.description, self.version = name, None, description, "1"
        self.triggers = Triggers(findings=list(findings), keywords=list(keywords))
        self.resource_types = []


class Sk:
    def __init__(self, name, description, **kw):
        self.name, self.digest, self.meta = name, f"d-{name}-{uuid.uuid4().hex[:4]}", Meta(name, description, **kw)


SKILLS = [Sk("crashloop-triage", "Diagnose pods in CrashLoopBackOff from previous logs and exit codes", findings=["crashloop"]),
          Sk("oom-review", "Memory limits, OOMKilled containers and working set growth", findings=["oomkilled"]),
          Sk("dns-check", "Cluster DNS resolution failures and CoreDNS health")]


async def test_ranking_sees_only_eligible_skills_and_shadow_changes_nothing():
    eligible = SKILLS[:2]  # dns-check was refused by policy before ranking
    ranked, method = await skill_rank.rank("why does payments keep restarting with OOMKilled", {"oomkilled"}, eligible)
    assert method == "tfidf" and {r["skill"] for r in ranked} == {"crashloop-triage", "oom-review"} and ranked[0]["skill"] == "oom-review"
    assert ranked[0]["matched"] and "shares" in ranked[0]["why"]
    chosen = [(SKILLS[0], 100, ["chosen by the operator"])]
    final, added = skill_rank.combine("shadow", chosen, ranked, {s.name: s for s in eligible}, 3)
    assert final == chosen and added == []


async def test_enabled_adds_but_never_removes_or_reorders():
    eligible = SKILLS[:2]
    ranked, _ = await skill_rank.rank("OOMKilled memory limit", {"oomkilled"}, eligible)
    chosen = [(SKILLS[0], 100, ["chosen by the operator"])]
    final, added = skill_rank.combine("enabled", chosen, ranked, {s.name: s for s in eligible}, 3)
    assert final[0] == chosen[0] and added == ["oom-review"] and "semantic" in final[1][2][0]
    full = [(s, 5, ["findings"]) for s in SKILLS[:3]]
    assert skill_rank.combine("enabled", full, ranked, {s.name: s for s in eligible}, 3) == (full, [])  # no place left


async def test_embeddings_are_used_when_present_and_a_failure_falls_back_to_tfidf():
    async def embed(texts):
        return [[1.0, 0.0] if "OOM" in t or "Memory" in t else [0.0, 1.0] for t in texts]
    ranked, method = await skill_rank.rank("OOMKilled", set(), SKILLS[:2], embed)
    assert method == "embedding" and ranked[0]["skill"] == "oom-review"

    async def broken(texts):
        raise RuntimeError("provider down")
    _, method = await skill_rank.rank("OOMKilled", set(), SKILLS[:2], broken)
    assert method == "tfidf"


async def test_an_investigation_records_recommendations_in_shadow_without_changing_skills(client):
    from tests.test_investigation_graph import FakeKubernetes  # noqa: F401
    from tests.test_knowledge_retrieval import ask, environment, no_model
    from moks.connectors import registry as creg

    creg.CONNECTOR_TYPES["kubernetes"] = FakeKubernetes
    creg._cache.clear()
    await no_model()
    env, email = await environment()
    base = await ask(client, env, email)
    await set_mode("skill_recommendations", "shadow")
    try:
        shadow = await ask(client, env, email)
    finally:
        await set_mode("skill_recommendations", "disabled")
        creg._cache.clear()
    assert [s["name"] for s in shadow.report["skills"]] == [s["name"] for s in base.report["skills"]]
    rec = shadow.report["skill_recommendations"]
    assert rec["mode"] == "shadow" and rec["added"] == [] and rec["deterministic"] == [s["name"] for s in base.report["skills"]]
    assert "skill_recommendations" not in base.report
