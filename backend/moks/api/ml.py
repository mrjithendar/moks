"""ML insights and governance: capability modes, observations and feedback, similar incidents, models, learning
records and datasets (docs/ML.md).

Scoped reads (observations, similar incidents, learning records) are filtered row by row with the reader's
bindings, so nothing outside them is counted or returned. Global actions (modes, training, promotion, rollback,
datasets) need ml.admin or learning.export and are audited. None of it grants access to infrastructure.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, jobs
from ..auth.deps import get_auth, needs, require
from ..auth.sessions import AuthContext
from ..db import get_db
from ..ml import CAPABILITIES, anomaly, learning, modes, registry, similar
from ..models import Environment, Investigation, LearningDataset, LearningJudgment, LearningRecord, MLCapability, MLModel, MLObservation
from ..rbac import P

router = APIRouter(prefix="/ml", tags=["ml"])


def _ns(ns: str | None) -> str | None:
    return None if ns in (None, "*") else ns


# ---- status and switches ---------------------------------------------------------------------------------


@router.get("/status")
async def status(request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    if not ctx.principal.can_anywhere(P.ML_READ):
        await require(request, ctx, P.ML_READ)
    out: dict[str, Any] = {"capabilities": await modes(db), "note": "ML in MOKS is advisory: it never grants access, opens incidents, changes severity or runs actions."}
    if ctx.principal.can(P.ML_ADMIN):
        rows = (await db.execute(select(MLModel.capability, MLModel.status, func.count()).group_by(MLModel.capability, MLModel.status))).all()
        out["models"] = [{"capability": c, "status": s, "count": n} for c, s, n in rows]
    return out


class ModeIn(BaseModel):
    mode: Literal["disabled", "shadow", "enabled"]
    reason: str = Field(min_length=3, max_length=500)


@router.put("/capabilities/{name}")
async def set_mode(name: str, body: ModeIn, request: Request, ctx: AuthContext = Depends(needs(P.ML_ADMIN)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    spec = CAPABILITIES.get(name)
    if spec is None:
        raise HTTPException(404, "Unknown capability")
    if body.mode not in spec["modes"]:
        raise HTTPException(422, f"{name} supports {', '.join(spec['modes'])}")
    row = await db.get(MLCapability, name)
    before = row.mode if row else spec["default"]
    if row is None:
        row = MLCapability(name=name, mode=body.mode)
        db.add(row)
    row.mode, row.reason, row.updated_by, row.updated_at = body.mode, body.reason, ctx.user.id, datetime.now(timezone.utc)
    await audit.record(db, action="ml.capability", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="ml_capability", resource_id=name, decision="allow",
                       details={"from": before, "to": body.mode, "reason": body.reason})
    return (await modes(db))[name]


# ---- observations ------------------------------------------------------------------------------------------


@router.get("/observations")
async def observations(environment: str | None = None, namespace: str | None = None, unusual: bool = False, hours: int = 24, limit: int = 200,
                       ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=min(max(hours, 1), 24 * 30))
    q = select(MLObservation).where(MLObservation.observed_at >= since).order_by(MLObservation.observed_at.desc()).limit(min(limit, 500) * 4)
    if environment:
        q = q.where(MLObservation.environment == environment)
    if namespace:
        q = q.where(MLObservation.namespace == namespace)
    if unusual:
        q = q.where(MLObservation.unusual.is_(True))
    rows = [o for o in (await db.execute(q)).scalars().all() if ctx.principal.can(P.ML_READ, o.environment, o.namespace)][: min(limit, 500)]
    return {"observations": [anomaly.observation_out(o) for o in rows], "mode": (await modes(db))["anomaly_detection"], "note": anomaly.NOTE}


class FeedbackIn(BaseModel):
    verdict: Literal["true_positive", "false_positive", "expected", "unsure"]
    note: str = Field(default="", max_length=2000)


@router.post("/observations/{oid}/feedback")
async def observation_feedback(oid: uuid.UUID, body: FeedbackIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    o = await db.get(MLObservation, oid)
    if o is None or not ctx.principal.can(P.ML_READ, o.environment, o.namespace):
        raise HTTPException(404, "Observation not found")
    await require(request, ctx, P.ML_FEEDBACK, o.environment, o.namespace)
    j = await learning.judge(db, ctx.user.id, target_kind="anomaly_observation", target_id=str(o.id), verdict=body.verdict, note=body.note, environment=o.environment, namespace=o.namespace)
    o.feedback = {"verdict": body.verdict, "judgment": str(j.id), "at": datetime.now(timezone.utc).isoformat()}
    return anomaly.observation_out(o)


# ---- similar incidents ---------------------------------------------------------------------------------------


@router.get("/similar-incidents")
async def similar_for(investigation_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inv = await db.get(Investigation, investigation_id)
    env = await db.get(Environment, inv.environment_id) if inv else None
    if inv is None or env is None or not ctx.principal.can(P.INVESTIGATION_READ, env.name, _ns(inv.namespace)):
        raise HTTPException(404, "Investigation not found")
    await require(request, ctx, P.ML_READ, env.name, _ns(inv.namespace))
    feat = learning.features(inv.report or {}, inv.question)
    return await similar.find(db, ctx.principal, feat, environment=env.name, namespace=_ns(inv.namespace), exclude_incident=inv.incident_id)


# ---- models -------------------------------------------------------------------------------------------------------


@router.get("/models")
async def list_models(capability: str | None = None, status_: str | None = None, ctx: AuthContext = Depends(needs(P.ML_ADMIN)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(MLModel).order_by(MLModel.capability, MLModel.cohort, MLModel.created_at.desc()).limit(500)
    if capability:
        q = q.where(MLModel.capability == capability)
    if status_:
        q = q.where(MLModel.status == status_)
    return [registry.model_out(m) for m in (await db.execute(q)).scalars().all()]


class PromoteIn(BaseModel):
    to: Literal["shadow", "champion"]


@router.post("/models/{mid}/promote")
async def promote(mid: uuid.UUID, body: PromoteIn, ctx: AuthContext = Depends(needs(P.ML_ADMIN)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    m = await db.get(MLModel, mid)
    if m is None:
        raise HTTPException(404, "Model not found")
    try:
        out = await registry.promote(db, m, ctx.user.id, body.to)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await audit.record(db, action="ml.promote", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="ml_model", resource_id=str(m.id), decision="allow",
                       details={"to": body.to, "cohort": m.cohort, "algorithm": m.algorithm, "version": m.version, "replaced": (out["replaced"] or {}).get("id")})
    return out


class RejectIn(BaseModel):
    note: str = Field(min_length=3, max_length=500)


@router.post("/models/{mid}/reject")
async def reject(mid: uuid.UUID, body: RejectIn, ctx: AuthContext = Depends(needs(P.ML_ADMIN)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    m = await db.get(MLModel, mid)
    if m is None:
        raise HTTPException(404, "Model not found")
    await registry.reject(db, m, body.note)
    await audit.record(db, action="ml.reject", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="ml_model", resource_id=str(m.id), decision="allow", details={"note": body.note})
    return registry.model_out(m)


class RollbackIn(BaseModel):
    capability: str = Field(max_length=40)
    cohort: str = Field(max_length=300)


@router.post("/rollback")
async def rollback(body: RollbackIn, ctx: AuthContext = Depends(needs(P.ML_ADMIN)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        out = await registry.rollback(db, body.capability, body.cohort, ctx.user.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    await audit.record(db, action="ml.rollback", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="ml_model", resource_id=body.cohort, decision="allow",
                       details={"rolled_back": out["rolled_back"]["id"], "champion": (out["champion"] or {}).get("id")})
    return out


class TrainIn(BaseModel):
    environment: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


@router.post("/train", status_code=202)
async def train(body: TrainIn, ctx: AuthContext = Depends(needs(P.ML_ADMIN)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    if (await db.execute(select(Environment).where(Environment.name == body.environment))).scalar_one_or_none() is None:
        raise HTTPException(404, "Environment not found")
    try:
        job = await jobs.enqueue("ml_anomaly_train", {"environment": body.environment, "namespace": body.namespace, "created_by": str(ctx.user.id)},
                                 dedupe_key=f"ml_anomaly_train:{body.environment}:{body.namespace}", created_by=ctx.user.id)
    except jobs.QueueFull as exc:
        raise HTTPException(429, str(exc)) from None
    await audit.record(db, action="ml.train", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="ml_capability", resource_id="anomaly_detection", decision="allow",
                       environment=body.environment, namespace=body.namespace)
    return {"job_id": str(job) if job else None, "queued": job is not None}


# ---- learning records, judgments, datasets ------------------------------------------------------------------


@router.get("/learning/records")
async def records(environment: str | None = None, label_state: str | None = None, kind: str | None = None, limit: int = 100,
                  ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    q = select(LearningRecord).order_by(LearningRecord.created_at.desc()).limit(min(limit, 500) * 4)
    if environment:
        q = q.where(LearningRecord.environment == environment)
    if label_state:
        q = q.where(LearningRecord.label_state == label_state)
    if kind:
        q = q.where(LearningRecord.kind == kind)
    rows = [r for r in (await db.execute(q)).scalars().all() if ctx.principal.can(P.LEARNING_REVIEW, r.environment, _ns(r.namespace))]
    return [learning.record_out(r) for r in rows[: min(limit, 500)]]


class ReviewIn(BaseModel):
    approve: bool
    confirmed_cause: str = Field(default="", max_length=2000)
    resolution: str = Field(default="", max_length=2000)
    verification: Literal["verified", "unverified", "failed"] = "unverified"
    note: str = Field(default="", max_length=1000)


@router.post("/learning/records/{rid}/review")
async def review(rid: uuid.UUID, body: ReviewIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    r = await db.get(LearningRecord, rid)
    if r is None or not ctx.principal.can(P.LEARNING_REVIEW, r.environment, _ns(r.namespace)):
        raise HTTPException(404, "Record not found")
    try:
        await learning.review(db, r, ctx.user.id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await audit.record(db, action="learning.review", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="learning_record", resource_id=str(r.id), decision="allow",
                       environment=r.environment, namespace=r.namespace, details={"approve": body.approve, "verification": body.verification})
    return learning.record_out(r)


class JudgmentIn(BaseModel):
    target_kind: Literal["passage", "similar_incident", "skill_recommendation"]
    target_id: str = Field(min_length=1, max_length=200)
    verdict: str = Field(max_length=20)
    note: str = Field(default="", max_length=2000)
    investigation_id: uuid.UUID


@router.post("/learning/judgments", status_code=201)
async def add_judgment(body: JudgmentIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    inv = await db.get(Investigation, body.investigation_id)
    env = await db.get(Environment, inv.environment_id) if inv else None
    if inv is None or env is None or not ctx.principal.can(P.INVESTIGATION_READ, env.name, _ns(inv.namespace)):
        raise HTTPException(404, "Investigation not found")
    await require(request, ctx, P.ML_FEEDBACK, env.name, _ns(inv.namespace))
    rec = (await db.execute(select(LearningRecord).where(LearningRecord.investigation_id == inv.id))).scalar_one_or_none()
    try:
        j = await learning.judge(db, ctx.user.id, target_kind=body.target_kind, target_id=body.target_id, verdict=body.verdict, note=body.note, context_id=str(inv.id),
                                 environment=env.name, namespace=inv.namespace, record_id=rec.id if rec else None)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"id": str(j.id), "verdict": j.verdict, "state": j.state, "note": "Recorded for review. Nothing trains on it until a reviewer marks it reviewed."}


@router.get("/learning/judgments")
async def judgments(state: str = "open", limit: int = 200, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    rows = (await db.execute(select(LearningJudgment).where(LearningJudgment.state == state).order_by(LearningJudgment.created_at.desc()).limit(min(limit, 500) * 4))).scalars().all()
    mine = [j for j in rows if j.environment and ctx.principal.can(P.LEARNING_REVIEW, j.environment, _ns(j.namespace))]
    return [{"id": str(j.id), "target_kind": j.target_kind, "target_id": j.target_id, "verdict": j.verdict, "note": j.note, "environment": j.environment, "namespace": j.namespace,
             "state": j.state, "context_id": j.context_id, "created_at": j.created_at.isoformat() if j.created_at else None} for j in mine[: min(limit, 500)]]


class JudgmentReviewIn(BaseModel):
    state: Literal["reviewed", "dismissed"]


@router.post("/learning/judgments/{jid}/review")
async def review_judgment(jid: uuid.UUID, body: JudgmentReviewIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    j = await db.get(LearningJudgment, jid)
    if j is None or not j.environment or not ctx.principal.can(P.LEARNING_REVIEW, j.environment, _ns(j.namespace)):
        raise HTTPException(404, "Judgment not found")
    j.state, j.reviewed_by, j.reviewed_at = body.state, ctx.user.id, datetime.now(timezone.utc)
    await audit.record(db, action="learning.judgment_review", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="learning_judgment", resource_id=str(j.id), decision="allow",
                       environment=j.environment, namespace=j.namespace, details={"state": body.state})
    return {"id": str(j.id), "state": j.state}


class DatasetIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,98}$")
    purpose: Literal["incident_similarity", "skill_ranking", "retrieval", "anomaly"]
    realm: Literal["operational", "evaluation"] = "operational"
    environment: str | None = Field(default=None, max_length=100)
    method: Literal["grouped", "chronological"] = "grouped"
    eval_fraction: float = Field(default=0.2, gt=0, lt=1)
    seed: int = 7


def _dataset_out(d: LearningDataset) -> dict:
    return {"id": str(d.id), "name": d.name, "version": d.version, "purpose": d.purpose, "realm": d.realm, "feature_schema": d.feature_schema, "split": d.split, "counts": d.counts,
            "checksum": d.checksum, "checksum_version": d.checksum_version, "state": d.state, **learning.provenance(d),
            "created_at": d.created_at.isoformat() if d.created_at else None, "expires_at": d.expires_at.isoformat() if d.expires_at else None}


@router.post("/learning/datasets", status_code=201)
async def build(body: DatasetIn, ctx: AuthContext = Depends(needs(P.LEARNING_EXPORT)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    try:
        ds = await learning.build_dataset(db, created_by=ctx.user.id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await audit.record(db, action="learning.dataset_build", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="learning_dataset", resource_id=str(ds.id), decision="allow",
                       details={"name": ds.name, "version": ds.version, "counts": ds.counts, "checksum": ds.checksum})
    return _dataset_out(ds)


@router.get("/learning/datasets")
async def datasets(ctx: AuthContext = Depends(needs(P.LEARNING_EXPORT)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict]:
    return [_dataset_out(d) for d in (await db.execute(select(LearningDataset).order_by(LearningDataset.created_at.desc()).limit(200))).scalars().all()]


@router.get("/learning/datasets/{did}/export", response_class=PlainTextResponse)
async def export(did: uuid.UUID, ctx: AuthContext = Depends(needs(P.LEARNING_EXPORT)), db: AsyncSession = Depends(get_db, scope="function")) -> PlainTextResponse:
    ds = await db.get(LearningDataset, did)
    if ds is None:
        raise HTTPException(404, "Dataset not found")
    try:
        rows = await learning.export_rows(db, ds)
    except learning.DatasetUnavailable as exc:
        raise HTTPException(exc.status, exc.message) from None
    prov = learning.provenance(ds)
    await audit.record(db, action="learning.dataset_export", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="learning_dataset", resource_id=str(ds.id), decision="allow",
                       details={"rows": len(rows), "checksum": ds.checksum, "provenance": prov["provenance"]})
    return PlainTextResponse("\n".join(json.dumps(r, default=str) for r in rows) + "\n", media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{ds.name}-v{ds.version}.jsonl"', "X-MOKS-Dataset-Checksum": ds.checksum,
                                      "X-MOKS-Dataset-Provenance": prov["provenance"]})
