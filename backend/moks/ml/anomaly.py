"""Metric anomaly detection: offline training, bounded shadow inference, chronological evaluation.

Training (`moks ml train`, a Kubernetes CronJob, or an admin request) reads up to ml_anomaly_training_days of
features for each workload in a namespace, refuses cohorts with less than ml_anomaly_min_history_days of usable
history, fits on the earlier 80% and evaluates on the later 20%. Two algorithms are trained side by side, the
seasonal baseline and the Isolation Forest, each registered as a candidate with its evaluation, gates and a
comparison with the current champion. Nothing is promoted automatically.

Inference (the `ml_anomaly_tick` job) scores the latest window for every champion and shadow challenger in an
environment, with bounded cohorts, concurrency and query windows. It writes ml_observations and nothing else:
it never opens an incident, changes a severity, or plans or runs an action, and deterministic detection runs
exactly as before. Missing telemetry, maintenance windows, an invalid artifact and too little history are
recorded as such instead of being scored.

Scores mean the window is unusual for this cohort, not that something is broken, and never why.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import statistics
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import session_scope
from ..models import Environment, LearningRecord, MaintenanceWindow, MLObservation
from ..observability import ML_DRIFT, ML_FALLBACKS, ML_INFERENCE_SECONDS, ML_MISSING_FEATURES, ML_OBSERVATIONS
from . import artifacts, baseline, evaluation, features, iforest, registry
from .features import FEATURE_SCHEMA, FEATURES, CohortFrame

log = logging.getLogger("moks.ml.anomaly")

CAPABILITY = "anomaly_detection"
ALGORITHMS = ("seasonal_baseline", "isolation_forest")
TRAIN_SHARE = 0.8
MIN_VALIDATION_WINDOWS = 12
NOTE = "Unusual for this workload compared with its own history. Not a fault, not a cause, and not an incident."


# ---- Prometheus, through the executor ------------------------------------------------------------------


async def prometheus(db: AsyncSession, env: Environment) -> features.Query | None:
    """The automation principal's authorized path to this environment's Prometheus: the same executor, scope
    and connector detection uses. None when there is no Prometheus or no access."""
    from ..alerts import automation_principal
    from ..connectors import registry as connectors
    from ..rbac import P
    from ..scope import ScopeError, effective_scope
    from ..tools.contracts import ToolContext
    from ..tools.executor import ToolExecutor

    principal = await automation_principal(db)
    try:
        scope = effective_scope(principal, env, P.INVESTIGATION_RUN)
    except ScopeError:
        return None
    found, _ = await connectors.for_environment(db, env.id)
    if "prometheus" not in found:
        return None

    async def query(promql: str, a: datetime, b: datetime, step: int) -> tuple[list[dict], bool]:
        ctx = ToolContext(None, env, scope, principal, a, b, found, b - a + timedelta(minutes=1))
        ex = ToolExecutor(None, ctx, timeout_seconds=30, read_permissions=(P.INVESTIGATION_RUN,), audit_success=False)
        out = await ex.run("prom_query", {"promql": promql, "mode": "range", "step": step})
        if not out.ok:
            raise RuntimeError(out.error_kind or "query failed")
        return (out.data or {}).get("series") or [], not out.truncated
    return query


# ---- frames ------------------------------------------------------------------------------------------------


def window(frame: CohortFrame, a: int, b: int) -> CohortFrame:
    idx = [i for i, t in enumerate(frame.timestamps) if a <= t < b]
    return replace(frame, timestamps=[frame.timestamps[i] for i in idx], values={f: [frame.values[f][i] for i in idx] for f in FEATURES})


def drift_bins(frame: CohortFrame) -> dict[str, dict]:
    out = {}
    for f in FEATURES:
        edges, props = evaluation.bins([v for v in frame.values[f] if v is not None])
        if edges:
            out[f] = {"edges": edges, "props": props}
    return out


def _usable(frame: CohortFrame) -> list[str]:
    return [f for f in FEATURES if frame.quality.get("coverage", {}).get(f, 0) >= 0.5]


# ---- scoring ------------------------------------------------------------------------------------------------


def score_window(algorithm: str, state: dict, frame: CohortFrame) -> dict[str, Any]:
    if algorithm == "seasonal_baseline":
        return baseline.score(state, frame)
    pts = [s for _, row in frame.rows() if (s := iforest.score(state, row)) is not None]
    if not pts:
        return {"score": None, "threshold": state["threshold"], "unusual": False, "deviations": []}
    med = statistics.median(pts)
    pre = state["pre"]
    devs = []
    for i, f in enumerate(FEATURES):
        xs = [v for v in frame.values[f] if v is not None]
        if xs:
            z = (statistics.median(xs) - pre["median"][i]) / pre["scale"][i]
            devs.append({"feature": f, "z": round(z, 2), "observed": round(statistics.median(xs), 6), "typical": round(pre["median"][i], 6), "direction": "higher" if z > 0 else "lower", "points": len(xs)})
    devs.sort(key=lambda d: -abs(d["z"]))
    return {"score": round(med, 4), "threshold": round(state["threshold"], 4), "unusual": med >= state["threshold"], "deviations": devs[:6],
            "explanation": "features furthest from the training median, scaled by the training interquartile range"}


def fit(algorithm: str, frame: CohortFrame, seed: int = 0) -> dict:
    if algorithm == "seasonal_baseline":
        return baseline.fit([frame])
    return iforest.fit([row for _, row in frame.rows()], seed=seed)


def validate_on(algorithm: str, state: dict, frame: CohortFrame, minutes: int) -> list[dict]:
    out = []
    if not frame.timestamps:
        return out
    start = datetime.fromtimestamp(frame.timestamps[0], timezone.utc)
    end = datetime.fromtimestamp(frame.timestamps[-1], timezone.utc) + timedelta(seconds=frame.step)
    for a, b in evaluation.windows(start, end, minutes):
        w = window(frame, int(a.timestamp()), int(b.timestamp()))
        if not w.timestamps:
            continue
        r = score_window(algorithm, state, w)
        out.append({"cohort": frame.cohort, "window_to": b, "unusual": bool(r["unusual"]), "score": r["score"]})
    return out


async def _incidents(db: AsyncSession, frame: CohortFrame, a: datetime, b: datetime) -> list[dict]:
    """Approved incidents of this workload whose start falls in the period: the only labels anomaly evaluation uses."""
    rows = (await db.execute(select(LearningRecord).where(LearningRecord.kind == "incident", LearningRecord.label_state == "approved", LearningRecord.environment == frame.environment,
                                                          LearningRecord.namespace == frame.namespace, LearningRecord.event_from >= a, LearningRecord.event_from < b))).scalars().all()
    return [{"cohort": frame.cohort, "start": r.event_from} for r in rows if frame.workload in ((r.features or {}).get("services") or []) or r.service == frame.workload]


# ---- training -----------------------------------------------------------------------------------------------


async def train(env_name: str, namespace: str, *, query: features.Query | None = None, now: datetime | None = None, created_by: uuid.UUID | None = None,
                job_id: str | None = None, metrics: dict[str, str] | None = None) -> dict[str, Any]:
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=s.ml_anomaly_training_days)
    async with session_scope() as db:
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one_or_none()
        if env is None:
            raise ValueError(f"no environment {env_name}")
        query = query or await prometheus(db, env)
    if query is None:
        return {"environment": env_name, "namespace": namespace, "status": "no_prometheus", "cohorts": []}
    frames = (await features.collect(query, env_name, namespace, start, now, s.ml_anomaly_step_seconds, metrics))[: s.ml_anomaly_max_cohorts]
    results = []
    for fr in frames:
        results.append(await _train_cohort(fr, s, created_by, job_id))
    return {"environment": env_name, "namespace": namespace, "status": "ok", "window": [start.isoformat(), now.isoformat()], "cohorts": results}


async def _train_cohort(fr: CohortFrame, s, created_by, job_id) -> dict[str, Any]:
    hist = features.history_days(fr)
    usable = _usable(fr)
    base = {"cohort": fr.cohort, "history_days": round(hist, 2), "quality": fr.quality}
    if hist < s.ml_anomaly_min_history_days:
        return {**base, "status": "insufficient_history", "reason": f"{hist:.1f} days of usable history; {s.ml_anomaly_min_history_days} needed"}
    if len(usable) < 2:
        return {**base, "status": "insufficient_features", "reason": "fewer than two features have half coverage; absent telemetry is not modeled as healthy"}
    cut = fr.timestamps[int(len(fr.timestamps) * TRAIN_SHARE)]
    train_fr, valid_fr = window(fr, fr.timestamps[0], cut), window(fr, cut, fr.timestamps[-1] + fr.step)
    a, b = datetime.fromtimestamp(cut, timezone.utc), datetime.fromtimestamp(fr.timestamps[-1] + fr.step, timezone.utc)
    valid_days = max((b - a).total_seconds() / 86400, 1e-9)
    async with session_scope() as db:
        incidents = await _incidents(db, fr, a, b)
        champ = await registry.champion(db, CAPABILITY, fr.cohort)  # one champion per workload, whichever algorithm it is
    drift = drift_bins(train_fr)
    out = {**base, "status": "trained", "cutoff": a.isoformat(), "models": []}
    for alg in ALGORITHMS:
        try:
            state = fit(alg, train_fr)
        except ValueError as exc:
            out["models"].append({"algorithm": alg, "status": "not_trained", "reason": str(exc)})
            continue
        obs = validate_on(alg, state, valid_fr, s.ml_anomaly_window_minutes)
        flags = sum(o["unusual"] for o in obs) / valid_days
        metrics = {"validation": {"windows": len(obs), "days": round(valid_days, 3), "flags_per_day": round(flags, 3),
                                  "note": "flags on unreviewed history: an upper bound on false positives only if that period was healthy"},
                   "detection": evaluation.detection(obs, incidents)}
        if champ is not None:  # the same validation windows, so the comparison is like for like
            try:
                doc = await _load(champ)
                cobs = validate_on(champ.algorithm, doc["state"], valid_fr, s.ml_anomaly_window_minutes)
                metrics["champion_comparison"] = {"champion": str(champ.id), "champion_algorithm": champ.algorithm, "champion_version": champ.version, "champion_flags_per_day": round(sum(o["unusual"] for o in cobs) / valid_days, 3),
                                                  "champion_detection": evaluation.detection(cobs, incidents)}
            except artifacts.ArtifactError as exc:
                metrics["champion_comparison"] = {"champion": str(champ.id), "error": str(exc)}
        metrics["gates"] = [
            {"name": "history", "passed": hist >= s.ml_anomaly_min_history_days, "value": round(hist, 2)},
            {"name": "validation_windows", "passed": len(obs) >= MIN_VALIDATION_WINDOWS, "value": len(obs)},
            {"name": "flags_per_day", "passed": flags <= s.ml_anomaly_max_flags_per_day, "value": round(flags, 3), "limit": s.ml_anomaly_max_flags_per_day},
            {"name": "feature_coverage", "passed": len(usable) >= 2, "value": usable},
        ]
        body, sha = artifacts.encode(algorithm=alg, cohort=fr.cohort, feature_schema=FEATURE_SCHEMA, features=list(FEATURES), step=fr.step, state=state, params={"drift": drift})
        async with session_scope() as db:
            m = await registry.register(db, capability=CAPABILITY, cohort=fr.cohort, algorithm=alg, feature_schema=FEATURE_SCHEMA, body=body, sha=sha,
                                        params={"window_minutes": s.ml_anomaly_window_minutes, "step_seconds": fr.step}, metrics=metrics,
                                        training={"from": datetime.fromtimestamp(fr.timestamps[0], timezone.utc).isoformat(), "cutoff": a.isoformat(), "to": b.isoformat(),
                                                  "history_days": round(hist, 2), "train_points": len(train_fr.timestamps), "validation_points": len(valid_fr.timestamps), "quality": fr.quality},
                                        created_by=created_by, job_id=job_id, dataset_version=f"prometheus:{fr.cohort}@{datetime.fromtimestamp(fr.timestamps[0], timezone.utc).date()}..{a.date()}")
            out["models"].append({"algorithm": alg, "status": "candidate", "id": str(m.id), "version": m.version, "gates_passed": registry.gates_passed(m)[0], "metrics": metrics})
    return out


async def _load(m) -> dict:
    async with session_scope() as db:
        return await artifacts.load(db, m.artifact_sha256, algorithm=m.algorithm, feature_schema=FEATURE_SCHEMA, features=list(FEATURES), step=get_settings().ml_anomaly_step_seconds)


# ---- inference ----------------------------------------------------------------------------------------------


def _split_cohort(cohort: str) -> tuple[str, str, str]:
    env, ns, wl = cohort.split("/", 2)
    return env, ns, wl


def _fingerprint(model_id: uuid.UUID, window_to: datetime) -> str:
    return hashlib.sha256(f"{model_id}:{int(window_to.timestamp())}".encode()).hexdigest()


async def _maintenance(db: AsyncSession, env_id: uuid.UUID, namespace: str, now: datetime) -> dict | None:
    rows = (await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.environment_id == env_id, MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now))).scalars().all()
    hit = next((w for w in rows if w.namespace in ("*", namespace) and w.rule_id in ("*", "ml", CAPABILITY)), None)
    return {"reason": hit.reason[:200], "ends_at": hit.ends_at.isoformat()} if hit else None


async def tick(env_name: str, *, query: features.Query | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Scores every active model of one environment once. Shadow output only."""
    from . import mode

    s = get_settings()
    now = now or datetime.now(timezone.utc)
    async with session_scope() as db:
        state = await mode(db, CAPABILITY)
        if state == "disabled":
            return {"skipped": "anomaly_detection is disabled"}
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one_or_none()
        if env is None:
            return {"skipped": f"no environment {env_name}"}
        models = (await registry.active(db, CAPABILITY, prefix=f"{env_name}/"))[: s.ml_anomaly_max_cohorts * len(ALGORITHMS)]
        query = query or await prometheus(db, env)
        env_id = env.id
    if not models:
        return {"skipped": "no champion or challenger for this environment", "observations": 0}
    if query is None:
        ML_FALLBACKS.labels(CAPABILITY, "no_prometheus").inc()
        return {"skipped": "no Prometheus access for this environment"}
    by_ns: dict[str, list] = {}
    for m in models:
        by_ns.setdefault(_split_cohort(m.cohort)[1], []).append(m)
    gate = asyncio.Semaphore(s.ml_anomaly_concurrency)
    start = now - timedelta(minutes=s.ml_anomaly_window_minutes)

    async def one_namespace(ns: str, ms: list) -> list[dict]:
        async with gate:
            wanted = {_split_cohort(m.cohort)[2] for m in ms}
            try:
                frames = {f.workload: f for f in await features.collect(query, env_name, ns, start, now, s.ml_anomaly_step_seconds, workloads=wanted)}
            except Exception as exc:
                log.warning("anomaly features unavailable", extra={"event": "ml_features_failed", "namespace": ns, "error_type": type(exc).__name__})
                frames = {}
            async with session_scope() as db:
                maint = await _maintenance(db, env_id, ns, now)
            return [await _observe(m, frames.get(_split_cohort(m.cohort)[2]), maint, state, start, now) for m in ms]

    rows = [r for batch in await asyncio.gather(*(one_namespace(ns, ms) for ns, ms in by_ns.items())) for r in batch]
    async with session_scope() as db:
        for r in rows:
            await db.execute(insert(MLObservation).values(**r).on_conflict_do_nothing(index_elements=["fingerprint"]))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        ML_OBSERVATIONS.labels(CAPABILITY, r["status"]).inc()
    return {"observations": len(rows), "by_status": counts, "mode": state, "note": NOTE}


async def _observe(m, frame: CohortFrame | None, maint: dict | None, state: str, start: datetime, now: datetime) -> dict[str, Any]:
    env, ns, wl = _split_cohort(m.cohort)
    row = {"id": uuid.uuid4(), "capability": CAPABILITY, "model_id": m.id, "algorithm": m.algorithm, "environment": env, "namespace": ns, "service": wl, "cohort": m.cohort,
           "window_from": start, "window_to": now, "mode": state, "fingerprint": _fingerprint(m.id, now),
           "context": {"role": "champion" if m.status == "champion" else "challenger", "model_version": m.version, "maintenance": maint, "note": NOTE},
           "score": None, "threshold": None, "unusual": False, "deviations": [], "data_quality": {}}
    if frame is None or not any(frame.quality["coverage"].get(f, 0) >= 0.2 for f in FEATURES):
        ML_FALLBACKS.labels(CAPABILITY, "no_telemetry").inc()
        return {**row, "status": "no_telemetry", "data_quality": frame.quality if frame else {"absent": list(FEATURES)}}
    for f in FEATURES:
        if f in frame.quality["absent"] or frame.quality["coverage"].get(f, 0) < 0.5:
            ML_MISSING_FEATURES.labels(f).inc()
    if (m.training or {}).get("history_days", 0) < get_settings().ml_anomaly_min_history_days:
        return {**row, "status": "insufficient_history", "data_quality": frame.quality}
    began = time.monotonic()
    try:
        doc = await _load(m)
    except artifacts.ArtifactError as exc:
        ML_FALLBACKS.labels(CAPABILITY, "model_invalid").inc()
        return {**row, "status": "model_invalid", "data_quality": {**frame.quality, "error": str(exc)[:300]}}
    r = score_window(m.algorithm, doc["state"], frame)
    ML_INFERENCE_SECONDS.labels(CAPABILITY, m.algorithm).observe(time.monotonic() - began)
    drift = {}
    for f, b in ((doc.get("params") or {}).get("drift") or {}).items():
        p = evaluation.psi(b["props"], b["edges"], [v for v in frame.values.get(f, []) if v is not None])
        if p is not None:
            drift[f] = p
            ML_DRIFT.labels(row["cohort"], f).set(p)
    quality = {**frame.quality, "drift_psi": drift, "latency_ms": round((time.monotonic() - began) * 1000, 2)}
    if maint:  # scored for the record, never flagged: maintenance is expected to look unusual
        return {**row, "status": "maintenance", "score": r["score"], "threshold": r["threshold"], "unusual": False, "deviations": r["deviations"], "data_quality": quality}
    return {**row, "status": "scored", "score": r["score"], "threshold": r["threshold"], "unusual": bool(r["unusual"]), "deviations": r["deviations"], "data_quality": quality}


def observation_out(o: MLObservation) -> dict[str, Any]:
    return {"id": str(o.id), "model_id": str(o.model_id) if o.model_id else None, "algorithm": o.algorithm, "environment": o.environment, "namespace": o.namespace, "service": o.service,
            "cohort": o.cohort, "window_from": o.window_from.isoformat(), "window_to": o.window_to.isoformat(), "observed_at": o.observed_at.isoformat() if o.observed_at else None,
            "status": o.status, "mode": o.mode, "score": o.score, "threshold": o.threshold, "unusual": o.unusual, "deviations": o.deviations, "data_quality": o.data_quality,
            "context": o.context, "feedback": o.feedback}
