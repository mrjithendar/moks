"""Sync schedules: one approval, then the executor syncs one-sided changes on every run (docs/SECRETS_SYNC.md).

A schedule names mappings and an interval. Its hash covers the environment, the requester, the interval and each
mapping at the version it had, and an approval binds that hash, as a plan approval binds a plan's. The approval
follows the plan rules: never one's own schedule, except a global administrator's. Editing the schedule, or any of
its mappings, changes the hash: the schedule goes back to pending and runs nothing until it is approved again.

On a run the executor compares every mapping as a normal plan, requested by the schedule's requester, and writes
only items whose change is one-sided and allowed by the mapping's direction: changed on one side since the last
verified sync, or missing on one side. Conflicts (changed on both sides, or a first sync where the sides differ),
recreated secrets, blocked items and failures are left out and listed on the schedule for a person; nothing is
ever deleted. The approval check runs again in the executor before anything is written.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..actions.service import is_admin, principal_of
from ..models import Environment, SecretMapping, SecretSchedule, SecretScheduleApproval
from ..observability import POLICY_DENIALS
from ..rbac import P, Principal
from .service import SyncError, require

HASH_VERSION = 1
AUTO = ("changed_kubernetes", "changed_aws", "missing_kubernetes", "missing_aws")  # one-sided: what a run may write on its own
MAX_MAPPINGS = 200


def canonical(s: SecretSchedule, env_name: str) -> str:
    return json.dumps({"v": HASH_VERSION, "schedule": str(s.id), "environment": env_name, "requested_by": str(s.requested_by),
                       "interval_minutes": s.interval_minutes, "mappings": {str(k): int(v) for k, v in sorted((s.mapping_versions or {}).items())}},
                      sort_keys=True, separators=(",", ":"))


def compute_hash(s: SecretSchedule, env_name: str) -> str:
    return hashlib.sha256(canonical(s, env_name).encode()).hexdigest()


async def _mappings(db: AsyncSession, env: Environment, ids: list[uuid.UUID]) -> list[SecretMapping]:
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise SyncError(422, "a schedule needs at least one mapping")
    if len(ids) > MAX_MAPPINGS:
        raise SyncError(422, f"a schedule holds at most {MAX_MAPPINGS} mappings")
    rows = (await db.execute(select(SecretMapping).where(SecretMapping.id.in_(ids), SecretMapping.environment_id == env.id, SecretMapping.status == "active"))).scalars().all()
    found = {m.id for m in rows}
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise SyncError(404, f"no active mapping {missing[0]} in {env.name}")
    return list(rows)


def _namespaces(rows) -> str:
    ns = sorted({m.namespace for m in rows})
    return ns[0] if len(ns) == 1 else "*"


def _check_interval(minutes: int) -> int:
    if not 5 <= int(minutes) <= 10080:
        raise SyncError(422, "the interval is between 5 minutes and 7 days")
    return int(minutes)


def _propose_rights(principal: Principal, env: Environment, rows) -> None:
    # Runs execute as the requester, so they need to be able to propose and execute everything on the schedule.
    for m in rows:
        for perm in (P.SECRETS_PROPOSE, P.SECRETS_COMPARE, P.SECRETS_EXECUTE):
            require(principal, perm, env, m.namespace)


async def create(db: AsyncSession, principal: Principal, env: Environment, *, name: str, mapping_ids: list[uuid.UUID], interval_minutes: int) -> SecretSchedule:
    rows = await _mappings(db, env, mapping_ids)
    _propose_rights(principal, env, rows)
    if (await db.execute(select(SecretSchedule.id).where(SecretSchedule.environment_id == env.id, SecretSchedule.name == name, SecretSchedule.status != "retired"))).first():
        raise SyncError(409, f"a schedule named {name} already exists in {env.name}")
    s = SecretSchedule(environment_id=env.id, name=name, mapping_ids=[str(m.id) for m in rows], mapping_versions={str(m.id): m.version for m in rows},
                       interval_minutes=_check_interval(interval_minutes), status="pending", requested_by=uuid.UUID(principal.user_id))
    db.add(s)
    await db.flush()
    s.schedule_hash = compute_hash(s, env.name)
    await audit.record(db, action="secrets.schedule_create", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_schedule",
                       resource_id=str(s.id), environment=env.name, namespace=_namespaces(rows), decision="allow",
                       details={"name": name, "interval_minutes": s.interval_minutes, "mappings": [f"{m.namespace}/{m.secret_name} <-> {m.aws_secret_name}" for m in rows],
                                "hash": s.schedule_hash})
    return s


async def load(db: AsyncSession, env: Environment, schedule_id: uuid.UUID, lock: bool = False) -> SecretSchedule:
    q = select(SecretSchedule).where(SecretSchedule.id == schedule_id)
    s = (await db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if s is None or s.environment_id != env.id or s.status == "retired":
        raise SyncError(404, "schedule not found")
    return s


async def schedule_mappings(db: AsyncSession, s: SecretSchedule) -> list[SecretMapping]:
    ids = [uuid.UUID(i) for i in s.mapping_ids or []]
    return list((await db.execute(select(SecretMapping).where(SecretMapping.id.in_(ids)))).scalars().all()) if ids else []


async def update(db: AsyncSession, principal: Principal, env: Environment, schedule_id: uuid.UUID, version: int, *, name: str | None = None,
                 mapping_ids: list[uuid.UUID] | None = None, interval_minutes: int | None = None, refresh: bool = False) -> SecretSchedule:
    """Renaming keeps the approval. A new mapping set or interval, or refresh (take the mappings' current versions
    after they were edited), changes what was approved: the schedule waits for a new approval."""
    s = await load(db, env, schedule_id, lock=True)
    if version != s.version:
        raise SyncError(409, "the schedule changed since you loaded it; reload it")
    if str(s.requested_by) != principal.user_id:
        raise SyncError(403, "only the person who created this schedule can edit it")
    if name is not None and name != s.name:
        if (await db.execute(select(SecretSchedule.id).where(SecretSchedule.environment_id == env.id, SecretSchedule.name == name,
                                                             SecretSchedule.status != "retired", SecretSchedule.id != s.id))).first():
            raise SyncError(409, f"a schedule named {name} already exists in {env.name}")
        s.name = name
    if mapping_ids is not None or interval_minutes is not None or refresh:
        rows = await _mappings(db, env, mapping_ids if mapping_ids is not None else [uuid.UUID(i) for i in s.mapping_ids])
        _propose_rights(principal, env, rows)
        s.mapping_ids, s.mapping_versions = [str(m.id) for m in rows], {str(m.id): m.version for m in rows}
        if interval_minutes is not None:
            s.interval_minutes = _check_interval(interval_minutes)
        new = compute_hash(s, env.name)
        if new != s.schedule_hash:
            s.schedule_hash, s.status, s.paused_reason, s.next_run_at = new, "pending", None, None
    s.version += 1
    s.updated_at = datetime.now(timezone.utc)
    await audit.record(db, action="secrets.schedule_edit", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_schedule",
                       resource_id=str(s.id), environment=env.name, decision="allow",
                       details={"name": s.name, "interval_minutes": s.interval_minutes, "mappings": s.mapping_ids, "hash": s.schedule_hash, "status": s.status})
    return s


async def decide(db: AsyncSession, principal: Principal, env: Environment, schedule_id: uuid.UUID, approve: bool, comment: str, presented_hash: str) -> SecretSchedule:
    s = await load(db, env, schedule_id, lock=True)
    if s.status != "pending":
        raise SyncError(409, f"schedule is {s.status}, not pending")
    if str(s.requested_by) == principal.user_id and not is_admin(principal):
        POLICY_DENIALS.labels("self_approval").inc()
        raise SyncError(403, "you cannot approve or reject your own schedule")
    rows = await schedule_mappings(db, s)
    for m in rows:
        require(principal, P.SECRETS_APPROVE, env, m.namespace)
    if presented_hash != s.schedule_hash or compute_hash(s, env.name) != s.schedule_hash:
        POLICY_DENIALS.labels("plan_hash").inc()
        raise SyncError(409, "the schedule you reviewed differs from the stored one; reload and review again")
    stale = [m for m in rows if m.status != "active" or m.version != (s.mapping_versions or {}).get(str(m.id))]
    if approve and stale:
        raise SyncError(409, f"{stale[0].namespace}/{stale[0].secret_name} changed after the schedule was made; its creator refreshes it first")
    db.add(SecretScheduleApproval(schedule_id=s.id, approver_id=uuid.UUID(principal.user_id), decision="approve" if approve else "reject",
                                  schedule_hash=s.schedule_hash, comment=comment[:2000]))
    now = datetime.now(timezone.utc)
    s.status, s.next_run_at, s.updated_at = ("active", now, now) if approve else ("rejected", None, now)
    s.version += 1
    await audit.record(db, action="secrets.schedule_approve" if approve else "secrets.schedule_reject", actor_id=principal.user_id, actor_email=principal.email,
                       resource_type="secret_schedule", resource_id=str(s.id), environment=env.name, namespace=_namespaces(rows), decision="allow",
                       details={"hash": s.schedule_hash, "comment": comment[:500], "self_approved": str(s.requested_by) == principal.user_id})
    return s


async def set_paused(db: AsyncSession, principal: Principal, env: Environment, schedule_id: uuid.UUID, paused: bool) -> SecretSchedule:
    """Pausing is for the creator or an approver. Resuming checks the approval is still valid first."""
    s = await load(db, env, schedule_id, lock=True)
    rows = await schedule_mappings(db, s)
    if str(s.requested_by) != principal.user_id:
        for m in rows:
            require(principal, P.SECRETS_APPROVE, env, m.namespace)
    now = datetime.now(timezone.utc)
    if paused:
        if s.status != "active":
            raise SyncError(409, f"schedule is {s.status}; only an active schedule can be paused")
        s.status, s.paused_reason = "paused", f"paused by {principal.email}"
    else:
        if s.status != "paused":
            raise SyncError(409, f"schedule is {s.status}, not paused")
        s.status, s.paused_reason, s.next_run_at = "active", None, now
        try:
            await check(db, s, env)
        except SyncError as exc:
            s.status, s.paused_reason = "paused", exc.message
            raise SyncError(409, f"cannot resume: {exc.message}") from None
    s.version += 1
    s.updated_at = now
    await audit.record(db, action="secrets.schedule_pause" if paused else "secrets.schedule_resume", actor_id=principal.user_id, actor_email=principal.email,
                       resource_type="secret_schedule", resource_id=str(s.id), environment=env.name, decision="allow")
    return s


async def retire(db: AsyncSession, principal: Principal, env: Environment, schedule_id: uuid.UUID) -> SecretSchedule:
    s = await load(db, env, schedule_id, lock=True)
    if str(s.requested_by) != principal.user_id:
        for m in await schedule_mappings(db, s):
            require(principal, P.SECRETS_APPROVE, env, m.namespace)
    s.status, s.next_run_at, s.updated_at = "retired", None, datetime.now(timezone.utc)
    s.version += 1
    await audit.record(db, action="secrets.schedule_retire", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_schedule",
                       resource_id=str(s.id), environment=env.name, decision="allow")
    return s


async def approval(db: AsyncSession, s: SecretSchedule) -> SecretScheduleApproval | None:
    """The latest decision on the schedule's current hash, if it is an approval."""
    rows = (await db.execute(select(SecretScheduleApproval).where(SecretScheduleApproval.schedule_id == s.id, SecretScheduleApproval.schedule_hash == s.schedule_hash)
                             .order_by(SecretScheduleApproval.created_at.desc()))).scalars().all()
    return rows[0] if rows and rows[0].decision == "approve" else None


async def check(db: AsyncSession, s: SecretSchedule, env: Environment) -> None:
    """Everything a run relies on, against current state and permissions. The executor calls this before a run
    and again before writing; any failure pauses the schedule with the reason."""
    if s.status != "active":
        raise SyncError(409, f"schedule is {s.status}")
    if compute_hash(s, env.name) != s.schedule_hash:
        raise SyncError(409, "schedule hash mismatch")
    rows = await schedule_mappings(db, s)
    by_id = {str(m.id): m for m in rows}
    for mid, version in (s.mapping_versions or {}).items():
        m = by_id.get(mid)
        if m is None or m.status != "active":
            raise SyncError(409, "a mapping on this schedule was retired; edit the schedule")
        if m.version != version:
            raise SyncError(409, f"{m.namespace}/{m.secret_name} was edited after the schedule was approved; refresh the schedule and approve it again")
    a = await approval(db, s)
    if a is None:
        raise SyncError(403, "the schedule has no approval for its current version")
    try:
        requester = await principal_of(db, s.requested_by)
        _propose_rights(requester, env, rows)
    except Exception as exc:
        raise SyncError(403, f"the schedule's creator may no longer run it: {getattr(exc, 'message', exc)}") from None
    try:
        approver = await principal_of(db, a.approver_id)
        if a.approver_id == s.requested_by and not is_admin(approver):
            raise SyncError(403, "the approver is the creator and no longer an administrator")
        for m in rows:
            require(approver, P.SECRETS_APPROVE, env, m.namespace)
    except Exception as exc:
        raise SyncError(403, f"the approval is no longer valid: {getattr(exc, 'message', exc)}") from None


def next_after(s: SecretSchedule, now: datetime) -> datetime:
    return now + timedelta(minutes=s.interval_minutes)
