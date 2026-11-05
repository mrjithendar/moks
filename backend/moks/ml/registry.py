"""Model versions and who promoted what.

  candidate  trained and evaluated; scores nothing
  shadow     scored beside the champion as a challenger; observations are labeled challenger
  champion   the one model per capability and cohort whose observations are shown (still shadow-mode output)
  retired    replaced or rolled back; kept so a rollback can bring it back
  rejected   a person decided it must not run

Promotion and rollback are explicit, audited by the caller, and checked here: a champion needs recorded
evaluation that passed its gates, a matching artifact, and the capability not disabled. Without a champion a
capability falls back to what MOKS does without ML.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import get_settings
from ..models import MLModel
from . import artifacts


async def register(db: AsyncSession, *, capability: str, cohort: str, algorithm: str, feature_schema: str, body: bytes, sha: str, params: dict, metrics: dict,
                   training: dict, created_by: uuid.UUID | None = None, job_id: str | None = None, dataset_version: str | None = None) -> MLModel:
    await artifacts.save(db, body, sha)
    version = int((await db.execute(select(func.coalesce(func.max(MLModel.version), 0)).where(MLModel.capability == capability, MLModel.cohort == cohort, MLModel.algorithm == algorithm))).scalar_one()) + 1
    m = MLModel(capability=capability, cohort=cohort[:300], algorithm=algorithm, version=version, status="candidate", feature_schema=feature_schema, dataset_version=dataset_version,
                artifact_sha256=sha, library_versions={**artifacts.LIBRARY, "moks": __version__}, params=params, metrics=metrics, training=training,
                provenance={"commit": get_settings().build_commit, "job": job_id, "trained_at": datetime.now(timezone.utc).isoformat(), "artifact_store": get_settings().ml_artifact_store},
                created_by=created_by)
    db.add(m)
    await db.flush()
    return m


async def champion(db: AsyncSession, capability: str, cohort: str) -> MLModel | None:
    return (await db.execute(select(MLModel).where(MLModel.capability == capability, MLModel.cohort == cohort, MLModel.status == "champion"))).scalar_one_or_none()


async def active(db: AsyncSession, capability: str, prefix: str = "", limit: int = 500) -> list[MLModel]:
    """Champions and shadow challengers whose cohort starts with prefix (an environment, say)."""
    q = select(MLModel).where(MLModel.capability == capability, MLModel.status.in_(("champion", "shadow")))
    if prefix:
        q = q.where(MLModel.cohort.startswith(prefix))
    return list((await db.execute(q.order_by(MLModel.cohort, MLModel.status, MLModel.version.desc()).limit(limit))).scalars().all())


def gates_passed(m: MLModel) -> tuple[bool, list[str]]:
    gates = (m.metrics or {}).get("gates") or []
    if not gates:
        return False, ["no recorded evaluation"]
    failed = [g["name"] for g in gates if not g.get("passed")]
    return not failed, failed


async def promote(db: AsyncSession, m: MLModel, user_id: uuid.UUID | None, to: str) -> dict[str, Any]:
    from . import mode

    if to not in ("shadow", "champion"):
        raise ValueError("promote to shadow or champion")
    if m.status in ("rejected",):
        raise ValueError("a rejected model cannot be promoted")
    if await mode(db, m.capability) == "disabled" and to == "champion":
        raise ValueError(f"{m.capability} is disabled; enable shadow mode before choosing a champion")
    if to == "champion":
        ok, failed = gates_passed(m)
        if not ok:
            raise ValueError("evaluation gates not passed: " + ", ".join(failed))
    previous = await champion(db, m.capability, m.cohort)
    now = datetime.now(timezone.utc)
    if to == "champion" and previous is not None and previous.id != m.id:
        previous.status, previous.retired_at = "retired", now
        await db.flush()  # the one-champion index holds, so the old one steps down first
    m.status, m.promoted_by, m.promoted_at = to, user_id, now
    m.retired_at = None
    await db.flush()
    return {"model": model_out(m), "replaced": model_out(previous) if previous is not None and previous.id != m.id and to == "champion" else None}


async def rollback(db: AsyncSession, capability: str, cohort: str, user_id: uuid.UUID | None) -> dict[str, Any]:
    """The champion steps down and the most recently promoted retired model returns. With none, there is no
    champion and the capability uses its deterministic fallback for this cohort."""
    cur = await champion(db, capability, cohort)
    if cur is None:
        raise ValueError("no champion to roll back")
    cur.status, cur.retired_at, cur.note = "retired", datetime.now(timezone.utc), (cur.note + " rolled back").strip()
    await db.flush()
    prev = (await db.execute(select(MLModel).where(MLModel.capability == capability, MLModel.cohort == cohort, MLModel.status == "retired", MLModel.id != cur.id,
                                                   MLModel.promoted_at.is_not(None)).order_by(MLModel.promoted_at.desc()).limit(1))).scalar_one_or_none()
    if prev is not None:
        prev.status, prev.promoted_by, prev.promoted_at, prev.retired_at = "champion", user_id, datetime.now(timezone.utc), None
        await db.flush()
    return {"rolled_back": model_out(cur), "champion": model_out(prev) if prev else None}


async def reject(db: AsyncSession, m: MLModel, note: str) -> None:
    m.status, m.retired_at, m.note = "rejected", datetime.now(timezone.utc), note[:500]
    await db.flush()


async def retire_all(db: AsyncSession, capability: str) -> int:
    """Every champion and challenger of a capability stops; nothing else changes. Used by an operator who wants a
    clean slate after a feature-schema change."""
    r = await db.execute(update(MLModel).where(MLModel.capability == capability, MLModel.status.in_(("champion", "shadow"))).values(status="retired", retired_at=func.now()))
    return r.rowcount or 0


def model_out(m: MLModel | None) -> dict[str, Any] | None:
    if m is None:
        return None
    ok, failed = gates_passed(m)
    return {"id": str(m.id), "capability": m.capability, "cohort": m.cohort, "algorithm": m.algorithm, "version": m.version, "status": m.status, "feature_schema": m.feature_schema,
            "dataset_version": m.dataset_version, "artifact_sha256": m.artifact_sha256, "library_versions": m.library_versions, "params": m.params, "metrics": m.metrics,
            "training": m.training, "provenance": m.provenance, "note": m.note, "gates_passed": ok, "gates_failed": failed,
            "created_at": m.created_at.isoformat() if m.created_at else None, "promoted_at": m.promoted_at.isoformat() if m.promoted_at else None,
            "retired_at": m.retired_at.isoformat() if m.retired_at else None}
