"""Secrets: discovery inventory, mappings, reviewed sync, rollback and deletion plans, sync schedules, and viewing
values (docs/SECRETS_SYNC.md).

This API never reads a secret value and never calls AWS or reads a Kubernetes Secret: discovery, comparison,
execution and reveals are executor jobs. What it returns is names, owners, versions, key names (to people with
secrets.compare in the namespace) and masked fingerprints. A reveal (secrets.reveal) comes back sealed to the
requesting browser's key (secrets/seal.py): this process passes the ciphertext on once and cannot open it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, jobs
from ..actions import service as actions
from ..auth.deps import deny, get_auth
from ..auth.sessions import AuthContext
from ..config import get_settings
from ..connectors import registry
from ..connectors.base import ConnectorError
from ..db import get_db
from ..models import (Connector, Environment, Job, SecretInventory, SecretInventoryScope, SecretMapping, SecretPlan, SecretPlanApproval, SecretPlanItem,
                      SecretSchedule, SecretScheduleApproval, User)
from ..rbac import P
from ..scope import ScopeError, effective_scope
from ..secrets import compare, schedules, service
from ..secrets.seal import SealError, public_key
from ..secrets.content import FORMATS, SUPPORTED_TYPES
from .common import env_or_404

router = APIRouter(prefix="/secrets", tags=["secrets"])


def _enabled() -> None:
    if not get_settings().secrets_sync_enabled:
        raise HTTPException(404, "Secret sync is not enabled (MOKS_SECRETS_SYNC_ENABLED)")


def _http(exc: service.SyncError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


async def _env(request: Request, db: AsyncSession, ctx: AuthContext, name: str, perm: P = P.SECRETS_DISCOVER) -> Environment:
    _enabled()
    env = await env_or_404(db, name)
    try:
        effective_scope(ctx.principal, env, perm)
    except ScopeError as exc:
        raise await deny(request, ctx, perm, env.name, None, str(exc))
    return env


def _see(ctx: AuthContext, perm: P, env: Environment, ns: str | None) -> bool:
    return service.can(ctx.principal, perm, env, ns)


def _policy() -> dict[str, Any]:
    manual = schedules.manual_only()
    return {"mode": "manual" if manual else "scheduled", "schedules_run": not manual,
            "explanation": schedules.MANUAL if manual else "an approved schedule writes one-sided changes on its own; conflicts are left for review"}


# ---- scope --------------------------------------------------------------------------------------------------


@router.get("/config")
async def config(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Whether the feature is on, and the environments where this person may see secrets."""
    if not get_settings().secrets_sync_enabled:
        return {"enabled": False, "environments": []}
    envs = (await db.execute(select(Environment).order_by(Environment.name))).scalars().all()
    out = []
    for env in envs:
        if not ctx.principal.can_anywhere(P.SECRETS_DISCOVER) or not ctx.principal.environments_for(P.SECRETS_DISCOVER, [env.name]):
            continue
        n = (await db.execute(select(func.count()).select_from(Connector).where(Connector.environment_id == env.id, Connector.type == "aws_secrets", Connector.enabled.is_(True)))).scalar_one()
        out.append({"name": env.name, "display_name": env.display_name or env.name, "is_production": env.is_production, "aws_connectors": n})
    return {"enabled": True, "environments": out, "formats": list(FORMATS), "types": list(SUPPORTED_TYPES), "classifications": compare.CLASSIFICATIONS}


@router.get("/{environment}/scope")
async def scope(environment: str, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    kubes, kube_errors = await registry.of_type(db, env.id, "kubernetes")
    aws_rows = (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.type == "aws_secrets", Connector.enabled.is_(True)).order_by(Connector.name))).scalars().all()
    aws_out, scopes = [], []
    for row in aws_rows:
        try:
            aws = service.aws_scope(row)
            scopes.append(aws)
            s = aws.describe_scope()
            aws_out.append({"id": str(row.id), "name": row.name, **{k: s[k] for k in ("account_id", "region", "allowed_prefixes", "required_tags", "kubernetes_namespaces",
                                                                                        "allow_delete", "recovery_window_days", "name_template")}})
        except ConnectorError as exc:
            aws_out.append({"id": str(row.id), "name": row.name, "error": exc.message})
    sc = effective_scope(ctx.principal, env, P.SECRETS_DISCOVER)
    kube_out = []
    for row, inst in kubes:
        namespaces, error = [], None
        try:
            namespaces = await inst.list_namespaces()
        except ConnectorError as exc:
            error = exc.message
        visible = [n for n in namespaces if sc.allows(n) and any(a.namespace_allowed(n) for a in scopes)]
        uid = (await db.execute(select(SecretInventoryScope.cluster_uid).where(SecretInventoryScope.connector_id == row.id, SecretInventoryScope.cluster_uid.is_not(None))
                                .order_by(SecretInventoryScope.discovered_at.desc()).limit(1))).scalar_one_or_none()
        kube_out.append({"id": str(row.id), "name": row.name, "writable": bool((row.config or {}).get("executor")), "cluster_uid": uid, "namespaces": visible, "error": error})
    last = (await db.execute(select(func.max(SecretInventoryScope.discovered_at)).where(SecretInventoryScope.environment_id == env.id))).scalar_one_or_none()
    return {"environment": env.name, "kubernetes": kube_out, "aws": aws_out, "cluster_wide": sc.cluster_wide,
            "can": {p.value.split(".", 1)[1]: ctx.principal.can_anywhere(p) for p in (P.SECRETS_DISCOVER, P.SECRETS_COMPARE, P.SECRETS_MAP, P.SECRETS_PROPOSE,
                                                                                    P.SECRETS_APPROVE, P.SECRETS_EXECUTE, P.SECRETS_REVEAL)},
            "discovery": {"interval_minutes": get_settings().secrets_discovery_interval_minutes, "last_at": last.isoformat() if last else None},
            "policy": _policy(),
            "errors": kube_errors}


# ---- discovery ----------------------------------------------------------------------------------------------


class DiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kube_connector_id: uuid.UUID | None = None
    namespaces: list[str] = Field(default_factory=list, max_length=200)  # empty: every secretsSync namespace this person may see
    aws_connector_id: uuid.UUID | None = None


@router.post("/{environment}/discover", status_code=202)
async def discover(environment: str, body: DiscoverIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Queues a read-only listing. It never writes to Kubernetes or AWS."""
    env = await _env(request, db, ctx, environment)
    payload: dict[str, Any] = {"environment_id": str(env.id), "requested_by": ctx.principal.user_id, "namespaces": []}
    if body.kube_connector_id:
        try:
            row = await service.connector(db, env, body.kube_connector_id, "kubernetes")
        except service.SyncError as exc:
            raise _http(exc) from None
        aws_rows = (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.type == "aws_secrets", Connector.enabled.is_(True)))).scalars().all()
        allowed = []
        if not body.namespaces:
            body.namespaces = [ns for ns in get_settings().secrets_namespace_list if _see(ctx, P.SECRETS_DISCOVER, env, ns)
                               and any(service.aws_scope(r).namespace_allowed(ns) for r in aws_rows)]
        for ns in body.namespaces:
            if not _see(ctx, P.SECRETS_DISCOVER, env, ns):
                raise await deny(request, ctx, P.SECRETS_DISCOVER, env.name, ns, f"You need secrets.discover in {env.name}/{ns}")
            if not any(service.aws_scope(r).namespace_allowed(ns) for r in aws_rows):
                raise HTTPException(403, f"no AWS secrets connector in {env.name} syncs namespace {ns}")
            allowed.append(ns)
        if not allowed:
            raise HTTPException(422, "no namespace in secretsSync.namespaces is both in your scope and synced by an AWS connector")
        payload.update(kube_connector_id=str(row.id), namespaces=allowed)
    if body.aws_connector_id:
        if not _see(ctx, P.SECRETS_DISCOVER, env, None):
            raise await deny(request, ctx, P.SECRETS_DISCOVER, env.name, None, "Listing AWS secrets needs secrets.discover for every namespace in this environment")
        try:
            row = await service.connector(db, env, body.aws_connector_id, "aws_secrets")
        except service.SyncError as exc:
            raise _http(exc) from None
        payload["aws_connector_id"] = str(row.id)
    if not payload.get("kube_connector_id") and not payload.get("aws_connector_id"):
        raise HTTPException(422, "choose a Kubernetes connector and namespaces, an AWS connector, or both")
    job_id = await jobs.enqueue("secret_discover", payload, created_by=ctx.user.id)
    await audit.record(db, action="secrets.discover_request", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email, resource_type="secret_inventory",
                       resource_id=str(job_id), environment=env.name, namespace=",".join(payload["namespaces"])[:253] or "-", decision="allow",
                       details={"namespaces": payload["namespaces"], "aws": bool(payload.get("aws_connector_id"))})
    return {"job_id": str(job_id)}


@router.get("/{environment}/discover/{job_id}")
async def discovery_status(environment: str, job_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    job = await db.get(Job, job_id)
    if job is None or job.type != "secret_discover" or job.payload.get("environment_id") != str(env.id):
        raise HTTPException(404, "discovery not found")
    return {"status": job.status, "error": job.error, "result": job.result, "created_at": job.created_at.isoformat() if job.created_at else None}


def _inventory_out(r: SecretInventory, mapping: SecretMapping | None, show_keys: bool) -> dict[str, Any]:
    return {"id": str(r.id), "side": r.side, "connector_id": str(r.connector_id), "namespace": r.namespace, "name": r.name, "arn": r.arn, "type": r.type,
            "keys": (r.key_names or []) if show_keys and r.key_names is not None else None, "key_count": len(r.key_names) if r.key_names is not None else None,
            "owner": r.owner, "syncable": r.syncable, "excluded": r.excluded, "last_modified": r.last_modified, "version": r.version, "tags": r.tags if r.side == "aws" else {},
            "extra": {k: v for k, v in (r.extra or {}).items() if k in ("immutable", "last_applied", "rotation_enabled", "deleted_date", "kms_key_id")},
            "discovered_at": r.discovered_at.isoformat() if r.discovered_at else None,
            "mapping": None if mapping is None else {"id": str(mapping.id), "status": mapping.last_status, "direction": mapping.direction,
                                                     "other": mapping.aws_secret_name if r.side == "kubernetes" else f"{mapping.namespace}/{mapping.secret_name}"}}


@router.get("/{environment}/inventory")
async def inventory(environment: str, request: Request, side: Literal["kubernetes", "aws"] = "kubernetes", q: str = Query(default="", max_length=200),
                    namespace: list[str] = Query(default=[]), type: list[str] = Query(default=[], max_length=20), owner: str = Query(default="", max_length=60),
                    status: str = Query(default="", max_length=30), prefix: str = Query(default="", max_length=200), limit: int = Query(default=500, ge=1, le=2000),
                    ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    query = select(SecretInventory).where(SecretInventory.environment_id == env.id, SecretInventory.side == side)
    if q:
        needle = q.replace("\\", "").replace("%", "").replace("_", "\\_")
        query = query.where(SecretInventory.name.ilike(f"%{needle}%"))
    if namespace and side == "kubernetes":
        query = query.where(SecretInventory.namespace.in_(namespace))
    if type:
        query = query.where(SecretInventory.type.in_(type))
    if owner:
        query = query.where(SecretInventory.owner["manager"].astext == owner) if owner != "none" else query.where(or_(SecretInventory.owner["manager"].astext.is_(None), SecretInventory.owner["manager"].astext == "moks"))
    if prefix and side == "aws":
        query = query.where(SecretInventory.name.startswith(prefix))
    rows = (await db.execute(query.order_by(SecretInventory.namespace, SecretInventory.name).limit(limit + 1))).scalars().all()
    maps = (await db.execute(select(SecretMapping).where(SecretMapping.environment_id == env.id, SecretMapping.status == "active"))).scalars().all()
    by_kube = {(m.kube_connector_id, m.namespace, m.secret_name): m for m in maps}
    by_aws = {(m.aws_connector_id, m.aws_secret_name): m for m in maps}
    cluster_wide = _see(ctx, P.SECRETS_DISCOVER, env, None)
    out, hidden = [], 0
    for r in rows[:limit]:
        m = by_kube.get((r.connector_id, r.namespace, r.name)) if side == "kubernetes" else by_aws.get((r.connector_id, r.name))
        ns = r.namespace if side == "kubernetes" else (m.namespace if m else None)
        if not (cluster_wide or (ns is not None and _see(ctx, P.SECRETS_DISCOVER, env, ns))):
            hidden += 1
            continue
        state = "excluded" if r.excluded else (m.last_status or "not_compared") if m else "unmapped"
        if status and state != status:
            continue
        out.append({**_inventory_out(r, m, ns is not None and _see(ctx, P.SECRETS_COMPARE, env, ns)), "sync_status": state})
    scopes = (await db.execute(select(SecretInventoryScope).where(SecretInventoryScope.environment_id == env.id, SecretInventoryScope.side == side))).scalars().all()
    synced = _synced(await _aws_scopes(db, env))
    types = (await db.execute(select(SecretInventory.type).where(SecretInventory.environment_id == env.id, SecretInventory.side == side, SecretInventory.type.is_not(None))
                              .distinct().order_by(SecretInventory.type))).scalars().all()
    return {"items": out, "truncated": len(rows) > limit, "hidden": hidden, "types": list(types),
            # a namespace that is no longer synced (an older, wider connector setting) keeps its last listing, but its errors are not shown
            "scopes": [{"scope": s.scope_key, "complete": s.complete, "count": s.count, "error": s.error, "discovered_at": s.discovered_at.isoformat() if s.discovered_at else None}
                       for s in scopes if side == "aws" and cluster_wide or side == "kubernetes" and _see(ctx, P.SECRETS_DISCOVER, env, s.scope_key) and synced(s.scope_key)]}


async def _aws_scopes(db: AsyncSession, env: Environment) -> list:
    out = []
    for row in (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.type == "aws_secrets", Connector.enabled.is_(True)))).scalars().all():
        try:
            out.append(service.aws_scope(row))
        except ConnectorError:
            continue
    return out


def _synced(aws_scopes: list):
    return lambda ns: any(a.namespace_allowed(ns) for a in aws_scopes)


# ---- mappings -----------------------------------------------------------------------------------------------


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kube_connector_id: uuid.UUID
    namespace: str = Field(max_length=253)
    secret_name: str = Field(max_length=253)
    aws_connector_id: uuid.UUID
    aws_secret_name: str | None = Field(default=None, max_length=512)
    payload_format: Literal["moks-k8s-v1", "json", "text", "binary"] = "moks-k8s-v1"
    key_mapping: dict[str, Any] = Field(default_factory=dict)
    k8s_type: str = Field(default="Opaque", max_length=100)
    direction: Literal["export", "import", "both"] = "export"
    expected_owner: dict[str, str] = Field(default_factory=dict)
    management_policy: Literal["refuse_managed", "override"] = "refuse_managed"


class MappingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    aws_secret_name: str | None = Field(default=None, max_length=512)
    payload_format: Literal["moks-k8s-v1", "json", "text", "binary"] | None = None
    key_mapping: dict[str, Any] | None = None
    k8s_type: str | None = Field(default=None, max_length=100)
    direction: Literal["export", "import", "both"] | None = None
    expected_owner: dict[str, str] | None = None
    management_policy: Literal["refuse_managed", "override"] | None = None


def mapping_out(m: SecretMapping, ctx: AuthContext, env: Environment) -> dict[str, Any]:
    b = m.baseline or {}
    keys = _see(ctx, P.SECRETS_COMPARE, env, m.namespace)
    return {"id": str(m.id), "version": m.version, "kube_connector_id": str(m.kube_connector_id), "cluster_name": m.cluster_name, "cluster_uid": m.cluster_uid,
            "namespace": m.namespace, "secret_name": m.secret_name, "aws_connector_id": str(m.aws_connector_id), "aws_account": m.aws_account, "aws_region": m.aws_region,
            "aws_secret_name": m.aws_secret_name, "aws_secret_arn": m.aws_secret_arn, "payload_format": m.payload_format, "key_mapping": m.key_mapping, "k8s_type": m.k8s_type,
            "direction": m.direction, "expected_owner": m.expected_owner, "management_policy": m.management_policy, "status": m.status, "last_status": m.last_status,
            "last_compared_at": m.last_compared_at.isoformat() if m.last_compared_at else None, "busy": bool(m.lease_owner and m.lease_expires_at and m.lease_expires_at > datetime.now(timezone.utc)),
            "baseline": None if not b else {"at": b.get("at"), "source": b.get("source"), "kube_rv": b.get("kube_rv"), "aws_version": b.get("aws_version"),
                                            "keys": sorted(b.get("keys") or {}) if keys else None}, "updated_at": m.updated_at.isoformat() if m.updated_at else None}


@router.get("/{environment}/mappings")
async def list_mappings(environment: str, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    rows = (await db.execute(select(SecretMapping).where(SecretMapping.environment_id == env.id, SecretMapping.status == "active")
                             .order_by(SecretMapping.namespace, SecretMapping.secret_name))).scalars().all()
    return {"items": [mapping_out(m, ctx, env) for m in rows if _see(ctx, P.SECRETS_DISCOVER, env, m.namespace)]}


@router.post("/{environment}/mappings/propose")
async def propose_mappings(environment: str, body: dict[str, Any], request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Draft mappings for discovered secrets, with collision-safe names to review and edit. Nothing is saved."""
    env = await _env(request, db, ctx, environment, P.SECRETS_MAP)
    ids = [uuid.UUID(str(i)) for i in (body.get("inventory_ids") or [])][:200]
    aws_id = body.get("aws_connector_id")
    try:
        aws_row = await service.connector(db, env, aws_id, "aws_secrets")
    except (service.SyncError, ValueError, TypeError) as exc:
        raise HTTPException(getattr(exc, "status", 422), getattr(exc, "message", "choose an AWS secrets connector")) from None
    aws = service.aws_scope(aws_row)
    rows = (await db.execute(select(SecretInventory).where(SecretInventory.id.in_(ids), SecretInventory.environment_id == env.id))).scalars().all() if ids else []
    taken = set((await db.execute(select(SecretMapping.aws_secret_name).where(SecretMapping.status == "active", SecretMapping.aws_account == aws.account_id,
                                                                             SecretMapping.aws_region == aws.region))).scalars().all())
    existing_aws = set((await db.execute(select(SecretInventory.name).where(SecretInventory.side == "aws", SecretInventory.connector_id == aws_row.id))).scalars().all())
    names = {}
    for r in rows:
        names[r.id] = (await db.get(Connector, r.connector_id)).name
    out = []
    for r in rows:
        if r.side == "kubernetes":
            if not _see(ctx, P.SECRETS_MAP, env, r.namespace):
                continue
            name = service.default_aws_name(aws.name_template, env, names[r.id], r.namespace, r.name)
            out.append({"inventory_id": str(r.id), "kube_connector_id": str(r.connector_id), "namespace": r.namespace, "secret_name": r.name, "aws_connector_id": str(aws_row.id),
                        "aws_secret_name": name, "payload_format": "moks-k8s-v1", "k8s_type": r.type if r.type in SUPPORTED_TYPES else "Opaque", "direction": "export",
                        "expected_owner": {"kubernetes": (r.owner or {}).get("manager") or "none", "aws": "none"}, "management_policy": "refuse_managed",
                        "excluded": r.excluded, "owner": r.owner, "collision": name in taken, "aws_exists": name in existing_aws,
                        "allowed": aws.namespace_allowed(r.namespace) and aws.name_allowed(name)})
        elif _see(ctx, P.SECRETS_MAP, env, None):
            out.append({"inventory_id": str(r.id), "aws_connector_id": str(aws_row.id), "aws_secret_name": r.name, "namespace": "", "secret_name": "", "payload_format": "json",
                        "k8s_type": "Opaque", "direction": "import", "expected_owner": {"kubernetes": "none", "aws": (r.owner or {}).get("manager") or "none"},
                        "management_policy": "refuse_managed", "owner": r.owner, "collision": r.name in taken, "aws_exists": True, "excluded": r.excluded, "allowed": True})
    return {"items": out}


@router.post("/{environment}/mappings", status_code=201)
async def create_mapping(environment: str, body: MappingIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_MAP)
    try:
        m = await service.create_mapping(db, ctx.principal, env, body.model_dump())
    except service.SyncError as exc:
        if exc.status == 403:
            raise await deny(request, ctx, P.SECRETS_MAP, env.name, body.namespace, exc.message)
        raise _http(exc) from None
    return mapping_out(m, ctx, env)


@router.patch("/{environment}/mappings/{mapping_id}")
async def update_mapping(environment: str, mapping_id: uuid.UUID, body: MappingPatch, request: Request, ctx: AuthContext = Depends(get_auth),
                         db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_MAP)
    try:
        m = await service.update_mapping(db, ctx.principal, env, mapping_id, body.model_dump(exclude_none=True))
    except service.SyncError as exc:
        raise _http(exc) from None
    return mapping_out(m, ctx, env)


@router.delete("/{environment}/mappings/{mapping_id}")
async def retire_mapping(environment: str, mapping_id: uuid.UUID, version: int, request: Request, ctx: AuthContext = Depends(get_auth),
                         db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Stops managing the pair. Neither secret is touched."""
    env = await _env(request, db, ctx, environment, P.SECRETS_MAP)
    try:
        m = await service.retire_mapping(db, ctx.principal, env, mapping_id, version)
    except service.SyncError as exc:
        raise _http(exc) from None
    return mapping_out(m, ctx, env)


class BulkMapIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    aws_connector_id: uuid.UUID
    kube_connector_id: uuid.UUID | None = None   # for AWS secrets: the cluster to import into (default: the first writable one)
    namespace: str | None = Field(default=None, max_length=253)  # for AWS secrets: the namespace to import into
    direction: Literal["export", "import", "both"] | None = None  # default: export for Kubernetes Secrets, import for AWS secrets
    compare: bool = True                           # start one sync plan for everything mapped


def k8s_name(aws_name: str) -> str:
    """A Kubernetes Secret name for an imported AWS secret: its last path part, lowercased, other characters as -."""
    import re

    last = aws_name.rstrip("/").rsplit("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9.-]+", "-", last).strip("-.")[:253] or "imported"


@router.post("/{environment}/mappings/bulk", status_code=201)
async def bulk_map(environment: str, body: BulkMapIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Map many discovered secrets at once with the defaults Map would propose, skip (with the reason) what cannot be
    mapped, and optionally start one comparison for all of them. Nothing is written to either side here."""
    env = await _env(request, db, ctx, environment, P.SECRETS_MAP)
    try:
        aws_row = await service.connector(db, env, body.aws_connector_id, "aws_secrets")
    except service.SyncError as exc:
        raise _http(exc) from None
    aws = service.aws_scope(aws_row)
    rows = (await db.execute(select(SecretInventory).where(SecretInventory.id.in_(body.inventory_ids), SecretInventory.environment_id == env.id))).scalars().all()
    kubes = {c.id: c for c in (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.type == "kubernetes", Connector.enabled.is_(True)))).scalars().all()}
    target = kubes.get(body.kube_connector_id) if body.kube_connector_id else next((c for c in kubes.values() if (c.config or {}).get("executor")), None)
    created, skipped = [], []
    for r in sorted(rows, key=lambda x: (x.namespace or "", x.name)):
        label = f"{r.namespace}/{r.name}" if r.side == "kubernetes" else r.name
        if r.excluded:
            skipped.append({"inventory_id": str(r.id), "secret": label, "why": f"cannot be synced: {r.excluded}"})
            continue
        if r.side == "kubernetes":
            kube = kubes.get(r.connector_id)
            spec = {"kube_connector_id": r.connector_id, "namespace": r.namespace, "secret_name": r.name, "aws_connector_id": aws_row.id,
                    "aws_secret_name": service.default_aws_name(aws.name_template, env, kube.name if kube else "cluster", r.namespace, r.name), "payload_format": "moks-k8s-v1",
                    "k8s_type": r.type if r.type in SUPPORTED_TYPES else "Opaque", "direction": body.direction or "export",
                    "expected_owner": {"kubernetes": (r.owner or {}).get("manager") or "none", "aws": "none"}, "management_policy": "refuse_managed"}
        else:
            if not body.namespace or target is None:
                skipped.append({"inventory_id": str(r.id), "secret": label, "why": "choose the cluster and namespace to import AWS secrets into"})
                continue
            moks_made = (r.owner or {}).get("manager") == "moks"
            spec = {"kube_connector_id": target.id, "namespace": body.namespace, "secret_name": k8s_name(r.name), "aws_connector_id": aws_row.id, "aws_secret_name": r.name,
                    "payload_format": "moks-k8s-v1" if moks_made else "json", "k8s_type": "Opaque", "direction": body.direction or "import",
                    "expected_owner": {"kubernetes": "none", "aws": "none" if moks_made else (r.owner or {}).get("manager") or "none"}, "management_policy": "refuse_managed"}
        try:
            async with db.begin_nested():
                m = await service.create_mapping(db, ctx.principal, env, spec)
            created.append(m)
        except service.SyncError as exc:
            skipped.append({"inventory_id": str(r.id), "secret": label, "why": exc.message})
    plan_out = None
    if created and body.compare and all(_see(ctx, P.SECRETS_PROPOSE, env, m.namespace) for m in created):
        try:
            plan = await service.create_plan(db, ctx.principal, env, kind="sync", mapping_ids=[m.id for m in created][:get_settings().secrets_max_plan_items],
                                             reason=f"Compare {len(created)} newly mapped secret{'s' if len(created) != 1 else ''}")
            plan_out = await _start_compare(db, plan, ctx, env)
        except service.SyncError as exc:
            skipped.append({"inventory_id": None, "secret": "comparison", "why": exc.message})
    return {"created": [mapping_out(m, ctx, env) for m in created], "skipped": skipped, "plan_id": plan_out["id"] if plan_out else None}


# ---- plans --------------------------------------------------------------------------------------------------


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["sync", "rollback", "delete"] = "sync"
    mapping_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=5, max_length=4000)
    targets: list[Literal["aws", "kubernetes"]] | None = None   # deletion
    rollback: dict[str, dict[str, Any]] | None = None           # {mapping_id: {version, targets}}


class ItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    included: bool | None = None
    resolution: Literal["kubernetes", "aws", "edited"] | None = None
    clear_resolution: bool = False
    key_choices: dict[str, Literal["kubernetes", "aws", "omit"]] | None = None


class Versioned(BaseModel):
    version: int


class Decision(BaseModel):
    approve: bool
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comment: str = Field(default="", max_length=2000)
    confirm: str | None = Field(default=None, max_length=100)


class Hash(BaseModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RetryIn(BaseModel):
    item_ids: list[uuid.UUID] | None = None


class RestartIn(BaseModel):
    kind: Literal["Deployment"] = "Deployment"
    name: str = Field(max_length=253)
    reason: str = Field(min_length=5, max_length=4000)


def _mask_side(st: dict[str, Any], keys: bool) -> dict[str, Any]:
    """A side state as a reviewer sees it: versions and owners, key names with masked fingerprints, never digests in full."""
    out = {k: st.get(k) for k in ("exists", "uid", "resource_version", "arn", "name", "version_id", "type", "owner", "immutable", "last_applied", "last_modified",
                                  "rotation_enabled", "deleted_date", "kms_key_id", "error", "unsupported") if k in st}
    if keys:
        out["keys"] = {k: {"fp": v["d"][:8], "empty": v["empty"], "binary": v["binary"]} for k, v in (st.get("keys") or {}).items()}
        if st.get("unsupported_keys"):
            out["unsupported_keys"] = st["unsupported_keys"]
    else:
        out["key_count"] = len(st.get("keys") or {})
    return out


def item_out(i: SecretPlanItem, m: SecretMapping, ctx: AuthContext, env: Environment) -> dict[str, Any]:
    keys = _see(ctx, P.SECRETS_COMPARE, env, m.namespace)
    diff = i.diff if keys else {t: {"hidden": True, "type_change": d.get("type_change")} for t, d in (i.diff or {}).items()}
    return {"id": str(i.id), "mapping": mapping_out(m, ctx, env), "position": i.position, "included": i.included, "classification": i.classification,
            "classification_label": compare.CLASSIFICATIONS.get(i.classification or "", "Not compared"), "first_sync": i.first_sync, "proposal": i.proposal,
            "resolution": i.resolution, "key_choices": i.key_choices if keys else {}, "rollback_version": i.rollback_version, "targets": i.targets, "actions": i.actions,
            "kubernetes": _mask_side(i.kube_state or {}, keys), "aws": _mask_side(i.aws_state or {}, keys), "source": _mask_side(i.source_state or {}, keys) if i.source_state else None,
            "baseline": None if not i.baseline else {"at": i.baseline.get("at"), "kube_rv": i.baseline.get("kube_rv"), "aws_version": i.baseline.get("aws_version")},
            "expected": {"type": (i.expected or {}).get("type"), "absent": (i.expected or {}).get("absent"), "keys": sorted((i.expected or {}).get("keys") or {}) if keys else None},
            "diff": diff, "warnings": [w.removeprefix("compare: ") for w in i.warnings or []], "impact": i.impact, "blocked": i.blocked, "status": i.status,
            "write_state": i.write_state, "attempts": i.attempts, "result": i.result, "error": i.error, "updated_at": i.updated_at.isoformat() if i.updated_at else None}


async def _plan_out(db: AsyncSession, plan: SecretPlan, ctx: AuthContext, env: Environment, full: bool = True) -> dict[str, Any]:
    items = await service.items_of(db, plan) if full else []
    mappings = await service.plan_mappings(db, plan)
    users = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_([x for x in (plan.requested_by, plan.executed_by) if x])))).scalars().all()}
    approvals = (await db.execute(select(SecretPlanApproval).where(SecretPlanApproval.plan_id == plan.id).order_by(SecretPlanApproval.created_at))).scalars().all() if full else []
    approvers = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_([a.approver_id for a in approvals])))).scalars().all()} if approvals else {}
    namespaces = sorted({m.namespace for m in mappings.values()})
    me = ctx.principal
    own = str(plan.requested_by) == me.user_id
    allowed = {
        "edit": own and plan.status == "draft",
        "submit": own and plan.status == "draft",
        "approve": plan.status == "pending" and (not own or (actions.is_admin(me) and plan.kind != "delete")) and all(_see(ctx, P.SECRETS_APPROVE, env, n) for n in namespaces),
        "execute": plan.status == "approved" and all(_see(ctx, P.SECRETS_EXECUTE, env, n) for n in namespaces),
        "retry": plan.status in ("partial", "failed") and all(_see(ctx, P.SECRETS_EXECUTE, env, n) for n in namespaces),
        "cancel": plan.status in ("comparing", "draft", "pending", "approved") and (own or all(_see(ctx, P.SECRETS_APPROVE, env, n) for n in namespaces)),
        "refresh": plan.status not in ("comparing",) and all(_see(ctx, P.SECRETS_PROPOSE, env, n) for n in namespaces),
    }
    expired = plan.expires_at is not None and plan.expires_at <= datetime.now(timezone.utc) and plan.status in ("pending", "approved", "partial", "failed")
    return {"id": str(plan.id), "environment": env.name, "kind": plan.kind, "status": plan.status, "reason": plan.reason, "version": plan.version, "plan_hash": plan.plan_hash,
            "requires_approval": plan.requires_approval, "expires_at": plan.expires_at.isoformat() if plan.expires_at else None, "expired": expired,
            "requested_by": users.get(plan.requested_by), "executed_by": users.get(plan.executed_by), "summary": plan.summary, "error": plan.error, "namespaces": namespaces,
            "created_at": plan.created_at.isoformat() if plan.created_at else None, "submitted_at": plan.submitted_at.isoformat() if plan.submitted_at else None,
            "finished_at": plan.finished_at.isoformat() if plan.finished_at else None, "source_plan_id": str(plan.source_plan_id) if plan.source_plan_id else None,
            "mapping_count": len(mappings), "allowed": allowed,
            "approvals": [{"by": approvers.get(a.approver_id), "decision": a.decision, "comment": a.comment, "at": a.created_at.isoformat() if a.created_at else None,
                           "current": a.plan_hash == plan.plan_hash} for a in approvals],
            "items": [item_out(i, mappings[i.mapping_id], ctx, env) for i in items]}


async def _visible_plan(request: Request, db: AsyncSession, ctx: AuthContext, env: Environment, plan_id: uuid.UUID) -> SecretPlan:
    try:
        plan = await service.load_plan(db, plan_id, env)
    except service.SyncError as exc:
        raise _http(exc) from None
    for m in (await service.plan_mappings(db, plan)).values():
        if not _see(ctx, P.SECRETS_DISCOVER, env, m.namespace):
            raise await deny(request, ctx, P.SECRETS_DISCOVER, env.name, m.namespace, f"This plan includes {m.namespace}, which is outside your secrets scope")
    return plan


@router.post("/{environment}/plans", status_code=201)
async def create_plan(environment: str, body: PlanIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Starts with a read-only comparison by the executor; the plan is a draft to review when it finishes."""
    env = await _env(request, db, ctx, environment, P.SECRETS_PROPOSE)
    try:
        plan = await service.create_plan(db, ctx.principal, env, kind=body.kind, mapping_ids=body.mapping_ids, reason=body.reason, rollback=body.rollback, targets=body.targets)
    except service.SyncError as exc:
        if exc.status == 403:
            raise await deny(request, ctx, P.SECRETS_PROPOSE, env.name, None, exc.message)
        raise _http(exc) from None
    return await _start_compare(db, plan, ctx, env)


async def _start_compare(db: AsyncSession, plan: SecretPlan, ctx: AuthContext, env: Environment) -> dict[str, Any]:
    await db.commit()  # the executor must see the plan before it can claim the job
    plan.compare_job_id = await jobs.enqueue("secret_compare", {"plan_id": str(plan.id), "environment_id": str(env.id)}, dedupe_key=f"secret_compare:{plan.id}", created_by=ctx.user.id)
    return await _plan_out(db, plan, ctx, env)


@router.get("/{environment}/plans")
async def list_plans(environment: str, request: Request, status: str = Query(default="", max_length=20), limit: int = Query(default=50, ge=1, le=200),
                     ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    q = select(SecretPlan).where(SecretPlan.environment_id == env.id)
    if status:
        q = q.where(SecretPlan.status == status)
    plans = (await db.execute(q.order_by(SecretPlan.created_at.desc()).limit(limit))).scalars().all()
    out = []
    for p in plans:
        mappings = await service.plan_mappings(db, p)
        if all(_see(ctx, P.SECRETS_DISCOVER, env, m.namespace) for m in mappings.values()):
            out.append(await _plan_out(db, p, ctx, env, full=False))
    return {"items": out}


@router.get("/{environment}/plans/{plan_id}")
async def get_plan(environment: str, plan_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    return await _plan_out(db, await _visible_plan(request, db, ctx, env, plan_id), ctx, env)


@router.patch("/{environment}/plans/{plan_id}/items/{item_id}")
async def edit_item(environment: str, plan_id: uuid.UUID, item_id: uuid.UUID, body: ItemPatch, request: Request, ctx: AuthContext = Depends(get_auth),
                    db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_PROPOSE)
    patch = body.model_dump(exclude_none=True, exclude={"clear_resolution"})
    if body.clear_resolution:
        patch["resolution"] = None
    try:
        plan = await service.update_item(db, ctx.principal, env, plan_id, item_id, patch)
    except service.SyncError as exc:
        raise _http(exc) from None
    await db.flush()
    return await _plan_out(db, plan, ctx, env)


async def _act(request: Request, db: AsyncSession, ctx: AuthContext, env: Environment, fn, *args) -> SecretPlan:
    try:
        return await fn(db, ctx.principal, env, *args)
    except service.SyncError as exc:
        raise _http(exc) from None


@router.post("/{environment}/plans/{plan_id}/submit")
async def submit(environment: str, plan_id: uuid.UUID, body: Versioned, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_PROPOSE)
    return await _plan_out(db, await _act(request, db, ctx, env, service.submit, plan_id, body.version), ctx, env)


@router.post("/{environment}/plans/{plan_id}/decision")
async def decision(environment: str, plan_id: uuid.UUID, body: Decision, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_APPROVE)
    plan = await _act(request, db, ctx, env, service.decide, plan_id, body.approve, body.comment, body.plan_hash, body.confirm)
    return await _plan_out(db, plan, ctx, env)


@router.post("/{environment}/plans/{plan_id}/execute", status_code=202)
async def execute(environment: str, plan_id: uuid.UUID, body: Hash, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_EXECUTE)
    plan = await _act(request, db, ctx, env, service.queue, plan_id, body.plan_hash)
    await db.commit()
    plan.execute_job_id = await jobs.enqueue("secret_execute", {"plan_id": str(plan.id), "environment_id": str(env.id)}, created_by=ctx.user.id)
    return await _plan_out(db, plan, ctx, env)


@router.post("/{environment}/plans/{plan_id}/retry", status_code=202)
async def retry(environment: str, plan_id: uuid.UUID, body: RetryIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_EXECUTE)
    try:
        plan, _ = await service.retry(db, ctx.principal, env, plan_id, body.item_ids)
    except service.SyncError as exc:
        raise _http(exc) from None
    await db.commit()
    plan.execute_job_id = await jobs.enqueue("secret_execute", {"plan_id": str(plan.id), "environment_id": str(env.id)}, created_by=ctx.user.id)
    return await _plan_out(db, plan, ctx, env)


@router.post("/{environment}/plans/{plan_id}/cancel")
async def cancel(environment: str, plan_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    return await _plan_out(db, await _act(request, db, ctx, env, service.cancel, plan_id), ctx, env)


@router.post("/{environment}/plans/{plan_id}/refresh", status_code=201)
async def refresh(environment: str, plan_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """A stale or expired plan cannot be revived: this compares the same mappings again as a new plan."""
    env = await _env(request, db, ctx, environment, P.SECRETS_PROPOSE)
    plan = await _act(request, db, ctx, env, service.refresh, plan_id)
    return await _start_compare(db, plan, ctx, env)


@router.post("/{environment}/plans/{plan_id}/items/{item_id}/restart", status_code=201)
async def propose_restart(environment: str, plan_id: uuid.UUID, item_id: uuid.UUID, body: RestartIn, request: Request, ctx: AuthContext = Depends(get_auth),
                          db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """A restart is a separate action plan with its own review and approval; nothing restarts as part of a sync."""
    env = await _env(request, db, ctx, environment)
    plan = await _visible_plan(request, db, ctx, env, plan_id)
    item = await db.get(SecretPlanItem, item_id)
    if item is None or item.plan_id != plan.id:
        raise HTTPException(404, "item not found")
    m = await db.get(SecretMapping, item.mapping_id)
    if not any(w.get("kind") == body.kind and w.get("name") == body.name for w in item.impact or []):
        raise HTTPException(422, f"{body.kind}/{body.name} is not among the workloads this Secret's review found")
    try:
        ap = await actions.create_plan(db, ctx.principal, env, namespace=m.namespace, action="restart_deployment", target_name=body.name, target_kind=body.kind, params={},
                                       reason=body.reason, evidence=[{"type": "secret_sync", "plan_id": str(plan.id), "secret": f"{m.namespace}/{m.secret_name}"}])
    except actions.PlanError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return {"action_plan_id": str(ap.id), "requires_approval": ap.requires_approval}


# ---- history ------------------------------------------------------------------------------------------------


@router.get("/{environment}/history")
async def history(environment: str, request: Request, mapping_id: uuid.UUID | None = None, limit: int = Query(default=100, ge=1, le=500),
                  ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Executed items, newest first: what was written where, by whom, and the versions before and after."""
    env = await _env(request, db, ctx, environment)
    q = (select(SecretPlanItem, SecretPlan, SecretMapping).join(SecretPlan, SecretPlan.id == SecretPlanItem.plan_id).join(SecretMapping, SecretMapping.id == SecretPlanItem.mapping_id)
         .where(SecretPlan.environment_id == env.id, SecretPlanItem.status.in_(("succeeded", "failed", "stale")), SecretPlanItem.attempts > 0))
    if mapping_id:
        q = q.where(SecretPlanItem.mapping_id == mapping_id)
    rows = (await db.execute(q.order_by(SecretPlanItem.updated_at.desc()).limit(limit))).all()
    users = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_({p.executed_by for _, p, _ in rows if p.executed_by})))).scalars().all()} if rows else {}
    out = []
    for i, p, m in rows:
        if not _see(ctx, P.SECRETS_DISCOVER, env, m.namespace):
            continue
        keys = _see(ctx, P.SECRETS_COMPARE, env, m.namespace)
        changed = sorted({x["key"] for d in (i.diff or {}).values() for part in ("added", "removed", "changed") for x in d.get(part, [])}) if keys else None
        r = i.result or {}
        out.append({"item_id": str(i.id), "plan_id": str(p.id), "kind": p.kind, "mapping_id": str(m.id), "secret": f"{m.namespace}/{m.secret_name}", "aws_secret_name": m.aws_secret_name,
                    "targets": i.targets, "actions": i.actions, "status": i.status, "error": i.error, "keys": changed, "by": users.get(p.executed_by),
                    "at": i.updated_at.isoformat() if i.updated_at else None, "aws_version_before": r.get("aws_version_before"), "aws_version_after": r.get("aws_version_after"),
                    "kube_version_before": r.get("kube_version_before"), "kube_version_after": r.get("kube_version_after"),
                    "rollback_available": i.status == "succeeded" and "aws" in (i.targets or []) and bool(r.get("aws_version_before")) and p.kind == "sync"})
    return {"items": out}


# ---- schedules ----------------------------------------------------------------------------------------------


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    mapping_ids: list[uuid.UUID] = Field(min_length=1, max_length=schedules.MAX_MAPPINGS)
    interval_minutes: int = Field(ge=5, le=10080)


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    mapping_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=schedules.MAX_MAPPINGS)
    interval_minutes: int | None = Field(default=None, ge=5, le=10080)
    refresh: bool = False  # take the mappings' current versions after they were edited (needs a new approval)


class ScheduleDecision(BaseModel):
    approve: bool
    schedule_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comment: str = Field(default="", max_length=2000)


async def schedule_out(db: AsyncSession, s: SecretSchedule, ctx: AuthContext, env: Environment) -> dict[str, Any]:
    rows = await schedules.schedule_mappings(db, s)
    namespaces = sorted({m.namespace for m in rows})
    approvals = (await db.execute(select(SecretScheduleApproval).where(SecretScheduleApproval.schedule_id == s.id).order_by(SecretScheduleApproval.created_at))).scalars().all()
    users = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_({s.requested_by} | {a.approver_id for a in approvals})))).scalars().all()}
    own = str(s.requested_by) == ctx.principal.user_id
    approver = all(_see(ctx, P.SECRETS_APPROVE, env, n) for n in namespaces)
    stale = [f"{m.namespace}/{m.secret_name}" for m in rows if m.status != "active" or m.version != (s.mapping_versions or {}).get(str(m.id))]
    manual = schedules.manual_only()
    # shown as held before the executor's next tick has held it, so the page never suggests it will run
    held = s.status == "held" or (manual and s.status in schedules.HOLDABLE)
    return {"id": str(s.id), "name": s.name, "status": "held" if held else s.status, "paused_reason": schedules.MANUAL if held and manual else s.paused_reason,
            "held": held, "needs_fresh_approval": s.status == "held", "policy": _policy()["mode"], "interval_minutes": s.interval_minutes, "version": s.version,
            "schedule_hash": s.schedule_hash, "requested_by": users.get(s.requested_by), "namespaces": namespaces, "stale_mappings": stale,
            "mappings": [{"id": str(m.id), "secret": f"{m.namespace}/{m.secret_name}", "aws_secret_name": m.aws_secret_name, "direction": m.direction,
                          "last_status": m.last_status} for m in rows],
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None, "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "last_plan_id": str(s.last_plan_id) if s.last_plan_id else None, "last_outcome": s.last_outcome or {},
            "approvals": [{"by": users.get(a.approver_id), "decision": a.decision, "comment": a.comment, "at": a.created_at.isoformat() if a.created_at else None,
                           "current": a.schedule_hash == s.schedule_hash} for a in approvals],
            "allowed": {"edit": own and s.status != "retired" and not manual,
                        "approve": not manual and s.status in ("pending", "held") and approver and (not own or actions.is_admin(ctx.principal)),
                        "reject": s.status in ("pending", "held") and approver and (not own or actions.is_admin(ctx.principal)),
                        "pause": not held and s.status == "active" and (own or approver), "resume": not manual and s.status == "paused" and (own or approver),
                        "run": not held and s.status == "active" and (own or approver), "retire": own or approver}}


async def _visible_schedule(request: Request, db: AsyncSession, ctx: AuthContext, env: Environment, schedule_id: uuid.UUID) -> SecretSchedule:
    try:
        s = await schedules.load(db, env, schedule_id)
    except service.SyncError as exc:
        raise _http(exc) from None
    for m in await schedules.schedule_mappings(db, s):
        if not _see(ctx, P.SECRETS_DISCOVER, env, m.namespace):
            raise await deny(request, ctx, P.SECRETS_DISCOVER, env.name, m.namespace, f"This schedule includes {m.namespace}, which is outside your secrets scope")
    return s


@router.get("/{environment}/schedules")
async def list_schedules(environment: str, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    rows = (await db.execute(select(SecretSchedule).where(SecretSchedule.environment_id == env.id, SecretSchedule.status != "retired").order_by(SecretSchedule.name))).scalars().all()
    out = []
    for s in rows:
        maps = await schedules.schedule_mappings(db, s)
        if all(_see(ctx, P.SECRETS_DISCOVER, env, m.namespace) for m in maps):
            out.append(await schedule_out(db, s, ctx, env))
    return {"items": out}


@router.post("/{environment}/schedules", status_code=201)
async def create_schedule(environment: str, body: ScheduleIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """A new schedule runs nothing until someone else (or, for an administrator, they themselves) approves it."""
    env = await _env(request, db, ctx, environment, P.SECRETS_PROPOSE)
    try:
        s = await schedules.create(db, ctx.principal, env, name=body.name.strip(), mapping_ids=body.mapping_ids, interval_minutes=body.interval_minutes)
    except service.SyncError as exc:
        if exc.status == 403:
            raise await deny(request, ctx, P.SECRETS_PROPOSE, env.name, None, exc.message)
        raise _http(exc) from None
    return await schedule_out(db, s, ctx, env)


@router.patch("/{environment}/schedules/{schedule_id}")
async def edit_schedule(environment: str, schedule_id: uuid.UUID, body: SchedulePatch, request: Request, ctx: AuthContext = Depends(get_auth),
                        db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_PROPOSE)
    try:
        s = await schedules.update(db, ctx.principal, env, schedule_id, body.version, name=body.name.strip() if body.name else None,
                                   mapping_ids=body.mapping_ids, interval_minutes=body.interval_minutes, refresh=body.refresh)
    except service.SyncError as exc:
        raise _http(exc) from None
    return await schedule_out(db, s, ctx, env)


@router.post("/{environment}/schedules/{schedule_id}/decision")
async def decide_schedule(environment: str, schedule_id: uuid.UUID, body: ScheduleDecision, request: Request, ctx: AuthContext = Depends(get_auth),
                          db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment, P.SECRETS_APPROVE)
    try:
        s = await schedules.decide(db, ctx.principal, env, schedule_id, body.approve, body.comment, body.schedule_hash)
    except service.SyncError as exc:
        raise _http(exc) from None
    return await schedule_out(db, s, ctx, env)


@router.post("/{environment}/schedules/{schedule_id}/{action}")
async def pause_schedule(environment: str, schedule_id: uuid.UUID, action: Literal["pause", "resume", "run"], request: Request, ctx: AuthContext = Depends(get_auth),
                         db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """pause and resume; run moves an active schedule's next run to now (the executor picks it up within seconds)."""
    env = await _env(request, db, ctx, environment)
    try:
        if action == "run":
            schedules.require_scheduling()
            s = await _visible_schedule(request, db, ctx, env, schedule_id)
            if s.status != "active":
                raise service.SyncError(409, f"schedule is {s.status}; only an active schedule runs")
            maps = await schedules.schedule_mappings(db, s)
            if str(s.requested_by) != ctx.principal.user_id:
                for m in maps:
                    service.require(ctx.principal, P.SECRETS_APPROVE, env, m.namespace)
            s.next_run_at = datetime.now(timezone.utc)
            s.version += 1
            await audit.record(db, action="secrets.schedule_run_now", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email, resource_type="secret_schedule",
                               resource_id=str(s.id), environment=env.name, decision="allow")
        else:
            s = await schedules.set_paused(db, ctx.principal, env, schedule_id, action == "pause")
    except service.SyncError as exc:
        raise _http(exc) from None
    return await schedule_out(db, s, ctx, env)


@router.delete("/{environment}/schedules/{schedule_id}")
async def retire_schedule(environment: str, schedule_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    env = await _env(request, db, ctx, environment)
    try:
        s = await schedules.retire(db, ctx.principal, env, schedule_id)
    except service.SyncError as exc:
        raise _http(exc) from None
    return {"id": str(s.id), "status": s.status}


# ---- viewing values -----------------------------------------------------------------------------------------


class RevealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inventory_id: uuid.UUID
    public_key: dict[str, Any]  # the browser's ECDH P-256 public JWK; its private half never leaves the tab


@router.post("/{environment}/reveal", status_code=202)
async def request_reveal(environment: str, body: RevealIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Queue a read of one secret by the executor, sealed to the caller's browser key. Audited when asked and when read."""
    env = await _env(request, db, ctx, environment, P.SECRETS_REVEAL)
    try:
        public_key(body.public_key)
    except SealError as exc:
        raise HTTPException(422, str(exc)) from None
    r = await db.get(SecretInventory, body.inventory_id)
    if r is None or r.environment_id != env.id:
        raise HTTPException(404, "secret not found; discover again")
    row = await db.get(Connector, r.connector_id)
    if row is None or not row.enabled:
        raise HTTPException(409, "the connector this secret was found with is gone or disabled")
    if r.side == "kubernetes":
        if not (row.config or {}).get("executor"):
            raise HTTPException(409, f"the kubernetes connector {row.name} has no executor target")
    elif not service.aws_scope(row).name_allowed(r.name):
        raise HTTPException(403, f"{r.name} is outside the connector's allowed prefixes")
    if row.environment_id != env.id:
        raise HTTPException(409, "the connector this secret was found with belongs to another environment")
    # the exact secret, its connector as configured now and the namespace that authorizes it; the executor and the
    # hand-over both check these again against current state (service.check_reveal)
    binding = await service.bind_reveal(db, env, r, row)
    ns = binding["auth_namespace"]
    if not _see(ctx, P.SECRETS_REVEAL, env, ns):
        raise await deny(request, ctx, P.SECRETS_REVEAL, env.name, ns, "You need secrets.reveal " + (f"in {env.name}/{ns}" if ns else f"for every namespace in {env.name} to view an unmapped AWS secret"))
    payload = {"environment_id": str(env.id), "requested_by": ctx.principal.user_id, "side": r.side, "connector_id": str(row.id), "namespace": r.namespace,
               "name": r.name, "binding": binding, "public_key": {k: body.public_key[k] for k in ("kty", "crv", "x", "y")}}
    await db.commit()
    job_id = await jobs.enqueue("secret_reveal", payload, created_by=ctx.user.id)
    label = f"{r.namespace}/{r.name}" if r.side == "kubernetes" else r.name
    await audit.record(db, action="secrets.reveal_request", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email, resource_type="secret",
                       resource_id=f"{r.side}:{label}", environment=env.name, namespace=ns or "-", decision="allow", details={"side": r.side, "job": str(job_id)})
    return {"job_id": str(job_id)}


@router.get("/{environment}/reveal/{job_id}")
async def read_reveal(environment: str, job_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """The sealed values, once, to the person who asked, if they may still see that exact secret now. They are then
    removed from the job. A result the person may no longer see is removed without being shown."""
    _enabled()
    env = await env_or_404(db, environment)
    job = await db.get(Job, job_id, with_for_update=True)
    if job is None or job.type != "secret_reveal" or job.payload.get("environment_id") != str(env.id) or job.payload.get("requested_by") != ctx.principal.user_id:
        raise HTTPException(404, "reveal not found")
    if job.status in ("queued", "running"):
        return {"status": job.status}
    if job.status != "succeeded":
        return {"status": job.status, "error": job.error}
    result = job.result or {}
    if "sealed" not in result:
        raise HTTPException(410, "already shown, withheld or expired; ask again")
    binding = job.payload.get("binding") or {}
    try:
        await service.check_reveal(db, env, binding, ctx.principal)
    except service.SyncError as exc:
        job.result = {"withheld": True, "key_count": result.get("key_count")}
        await audit.record(db, action="secrets.reveal_withheld", actor_id=ctx.principal.user_id, actor_email=ctx.principal.email, resource_type="secret",
                           resource_id=f"{binding.get('side')}:{binding.get('namespace') or ''}/{binding.get('name')}", environment=env.name,
                           namespace=binding.get("auth_namespace") or "-", decision="deny", outcome="withheld", details={"job": str(job.id), "reason": exc.message})
        await db.commit()  # the sealed result is gone even though this request fails
        raise await deny(request, ctx, P.SECRETS_REVEAL, env.name, binding.get("auth_namespace"), f"Not shown: {exc.message}")
    job.result = {"delivered": True, "key_count": result.get("key_count")}
    return {"status": "succeeded", "sealed": result["sealed"], "key_count": result.get("key_count")}
