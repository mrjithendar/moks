"""Cluster backups: R2 settings (admins), status, and plans for backup, schedule and restore.

Nothing here writes to a cluster: each POST creates an action plan, and the executor does the rest
after the usual approval. R2 credentials are references; this API never accepts or returns a value.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, velero
from ..actions import service
from ..auth.deps import deny, get_auth, needs
from ..auth.sessions import AuthContext
from ..connectors import registry
from ..connectors.base import ConnectorError, describe_credential_ref, resolve_credential
from ..db import get_db
from ..models import BackupStore, Environment
from ..rbac import P
from .common import env_or_404

router = APIRouter(prefix="/backups", tags=["backups"])
NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class CredentialRef(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a pasted value has nowhere to go
    type: Literal["env", "file"]
    name: str | None = Field(default=None, pattern=r"^MOKS_CRED_[A-Z0-9_]{1,80}$")
    path: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./-]{1,200}$")


class StoreIn(BaseModel):
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    prefix: str = Field(default="clusters", pattern=r"^[A-Za-z0-9_./-]{0,200}$")
    jurisdiction: Literal["default", "eu"] = "default"
    credentials: dict[str, CredentialRef]

    @field_validator("credentials")
    @classmethod
    def _complete(cls, v: dict) -> dict:
        if set(v) != set(velero.CREDENTIAL_KEYS):
            raise ValueError(f"credentials must have exactly {', '.join(velero.CREDENTIAL_KEYS)}")
        for ref in v.values():
            if (ref.type == "env") != bool(ref.name) or (ref.type == "file") != bool(ref.path):
                raise ValueError("an env reference needs name, a file reference needs path")
        return v


class Reason(BaseModel):
    reason: str = Field(min_length=5, max_length=4000)


class BackupIn(Reason):
    included_namespaces: list[str] = Field(default=["*"], min_length=1, max_length=100)
    include_volumes: bool = True
    ttl_days: int = Field(default=velero.KEEP_DAYS, ge=1, le=velero.KEEP_DAYS)


class ScheduleIn(BackupIn):
    name: str = Field(default="daily", max_length=63)
    cron: str = Field(default="0 2 * * *", max_length=60)
    paused: bool = False


class RestoreIn(Reason):
    backup: str = Field(max_length=253)
    included_namespaces: list[str] = Field(default=["*"], min_length=1, max_length=100)
    existing_resource_policy: Literal["none", "update"] = "none"
    restore_volumes: bool = True
    pre_restore_backup: bool = True


def _has(ref: dict | None) -> bool:
    try:
        return bool(resolve_credential(ref))
    except ConnectorError:
        return False


def store_out(s: BackupStore | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {"account_id": s.account_id, "bucket": s.bucket, "prefix": s.prefix, "jurisdiction": s.jurisdiction, "endpoint": velero.endpoint(s),
            "credentials": s.credentials, "credential_status": {k: {"reference": describe_credential_ref((s.credentials or {}).get(k)), "available": _has((s.credentials or {}).get(k))} for k in velero.CREDENTIAL_KEYS},
            "fingerprint": velero.fingerprint(s), "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "last_test_at": s.last_test_at.isoformat() if s.last_test_at else None, "last_test_ok": s.last_test_ok, "last_test_error": s.last_test_error}


@router.get("/store")
async def get_store(ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return {"store": store_out(await velero.get_store(db))}


@router.put("/store")
async def put_store(body: StoreIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    s = await velero.get_store(db)
    if s is None:
        s = BackupStore(name="r2")
        db.add(s)
    s.account_id, s.bucket, s.prefix, s.jurisdiction = body.account_id, body.bucket, body.prefix.strip("/"), body.jurisdiction
    s.credentials = {k: v.model_dump(exclude_none=True) for k, v in body.credentials.items()}
    s.updated_by, s.updated_at = ctx.user.id, datetime.now(timezone.utc)
    s.last_test_at, s.last_test_ok, s.last_test_error = None, None, None
    await db.flush()
    await audit.record(db, action="backup.store_update", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email, resource_type="backup_store", resource_id="r2",
                       details={"bucket": s.bucket, "prefix": s.prefix, "jurisdiction": s.jurisdiction, "credentials": {k: describe_credential_ref(v) for k, v in s.credentials.items()}})
    return {"store": store_out(s)}


@router.post("/store/test")
async def test_store(ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    s = await velero.get_store(db)
    if s is None:
        raise HTTPException(409, "Save the R2 settings first")
    error = None
    try:
        await asyncio.to_thread(velero.probe, s)
    except ConnectorError as exc:
        error = exc.message
    except Exception as exc:
        code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        error = f"R2 refused the test ({code or type(exc).__name__})"
    s.last_test_at, s.last_test_ok, s.last_test_error = datetime.now(timezone.utc), error is None, error
    await audit.record(db, action="backup.store_test", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email, resource_type="backup_store", resource_id="r2",
                       outcome="success" if error is None else "failed", details={"error": error})
    return {"store": store_out(s)}


async def _env(request: Request, db: AsyncSession, ctx: AuthContext, name: str) -> Environment:
    env = await env_or_404(db, name)
    if not ctx.principal.can(P.BACKUP_READ, env.name, "-"):
        raise await deny(request, ctx, P.BACKUP_READ, env.name, None, "cluster backups need access to all namespaces")
    return env


@router.get("/{environment}")
async def status(environment: str, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    connectors, _ = await registry.for_environment(db, env.id)
    k8s = connectors.get("kubernetes")
    if k8s is None:
        raise HTTPException(409, "No kubernetes connector in this environment")
    try:
        state = await velero.status(k8s[1], velero.supports_volumes(env))
    except ConnectorError as exc:
        raise HTTPException(502, exc.message) from None
    store = await velero.get_store(db)
    return {**state, "environment": env.name, "is_production": env.is_production, "store": None if store is None else {
        "configured": True, "bucket": store.bucket, "prefix": velero.env_prefix(store, env.name), "fingerprint": velero.fingerprint(store), "endpoint": velero.endpoint(store)}}


async def _plan(db: AsyncSession, ctx: AuthContext, env: Environment, action: str, kind: str, name: str, params: dict, reason: str) -> dict[str, Any]:
    try:
        plan = await service.create_plan(db, ctx.principal, env, namespace="-", action=action, target_name=name, target_kind=kind, params=params,
                                         reason=reason, evidence=[], auto_recover=False)
    except service.PlanError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return {"plan_id": str(plan.id), "requires_approval": plan.requires_approval}


def _scoped(env: Environment, requested: list[str], never: set[str]) -> dict[str, list[str]]:
    try:
        included, excluded = velero.namespaces_for(env, requested, never)
    except ConnectorError as exc:
        raise HTTPException(403, exc.message) from None
    return {"included_namespaces": included, "excluded_namespaces": excluded}


@router.post("/{environment}/configure", status_code=201)
async def configure(environment: str, body: Reason, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await env_or_404(db, environment)
    store = await velero.get_store(db)
    if store is None:
        raise HTTPException(409, "An admin must save the R2 settings first")
    return await _plan(db, ctx, env, "velero_configure", "BackupStorageLocation", velero.LOCATION, {
        "store_fingerprint": velero.fingerprint(store), "endpoint": velero.endpoint(store), "bucket": store.bucket, "prefix": velero.env_prefix(store, env.name)}, body.reason)


@router.post("/{environment}/backup", status_code=201)
async def backup(environment: str, body: BackupIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await env_or_404(db, environment)
    return await _plan(db, ctx, env, "velero_backup", "Backup", "new", {
        **_scoped(env, body.included_namespaces, velero.always_excluded()), "include_volumes": body.include_volumes and velero.supports_volumes(env), "ttl_days": body.ttl_days}, body.reason)


@router.post("/{environment}/schedule", status_code=201)
async def schedule(environment: str, body: ScheduleIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await env_or_404(db, environment)
    if not NAME.match(body.name):
        raise HTTPException(422, "invalid schedule name")
    return await _plan(db, ctx, env, "velero_schedule", "Schedule", body.name, {
        **_scoped(env, body.included_namespaces, velero.always_excluded()), "include_volumes": body.include_volumes and velero.supports_volumes(env), "ttl_days": body.ttl_days,
        "cron": body.cron, "paused": body.paused}, body.reason)


@router.post("/{environment}/restore", status_code=201)
async def restore(environment: str, body: RestoreIn, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await env_or_404(db, environment)
    return await _plan(db, ctx, env, "velero_restore", "Restore", body.backup, {
        **_scoped(env, body.included_namespaces, velero.restore_excluded()), "existing_resource_policy": body.existing_resource_policy,
        "restore_volumes": body.restore_volumes and velero.supports_volumes(env), "pre_restore_backup": body.pre_restore_backup}, body.reason)
