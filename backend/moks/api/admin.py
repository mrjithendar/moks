"""Administration: connectors, LLM providers, users and role bindings, audit log.

Credentials are never accepted or returned here: rows store credential *references*
(env var names under MOKS_CRED_* or files under MOKS_CREDENTIALS_DIR) that point to Kubernetes
Secrets mounted into the pods.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import needs
from ..auth.routes import EMAIL, hasher
from ..auth.sessions import AuthContext, client_ip, revoke_all_for_user
from ..connectors import registry
from ..connectors.base import describe_credential_ref
from ..db import get_db
from ..llm.providers import LLMError, complete_metered, provider_for
from ..models import AuditEvent, Connector, Environment, LLMConfig, RoleBinding, User
from ..rbac import ROLES, P

router = APIRouter(prefix="/admin", tags=["admin"])

CONNECTOR_TYPES = ["kubernetes", "prometheus", "loki", "grafana", "cloudwatch", "git", "aws_cost", "cost_model"]


class CredentialRef(BaseModel):
    type: Literal["none", "env", "file"] = "none"
    name: str | None = Field(default=None, pattern=r"^MOKS_CRED_[A-Z0-9_]{1,80}$")
    path: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._/-]{1,200}$")

    @field_validator("path")
    @classmethod
    def _no_traversal(cls, v: str | None) -> str | None:
        if v and (".." in v.split("/") or v.startswith("/")):
            raise ValueError("path must be relative to MOKS_CREDENTIALS_DIR")
        return v


def _check_no_secrets(config: dict[str, Any]) -> None:
    for k in config:
        if any(w in k.lower() for w in ("password", "secret", "token", "apikey", "api_key", "private")):
            raise HTTPException(422, f"config key {k!r} looks like a credential; use credential_ref instead")


# ---- connectors --------------------------------------------------------------------------------

class ConnectorIn(BaseModel):
    environment: str
    type: Literal["kubernetes", "prometheus", "loki", "grafana", "cloudwatch", "git", "aws_cost", "cost_model"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)


def connector_out(c: Connector, env_name: str) -> dict[str, Any]:
    return {"id": str(c.id), "environment": env_name, "type": c.type, "name": c.name, "enabled": c.enabled, "config": c.config,
            "credential": describe_credential_ref(c.credential_ref), "credential_ref": c.credential_ref,
            "implemented": c.type in registry.CONNECTOR_TYPES,
            "last_test_at": c.last_test_at.isoformat() if c.last_test_at else None, "last_test_ok": c.last_test_ok,
            "last_test_message": c.last_test_message}


@router.get("/connectors")
async def list_connectors(ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Connector, Environment.name).join(Environment).order_by(Environment.name, Connector.type))).all()
    return [connector_out(c, n) for c, n in rows]


async def _env_id(db: AsyncSession, name: str) -> uuid.UUID:
    env = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
    if env is None:
        raise HTTPException(404, f"Unknown environment {name!r}")
    return env.id


@router.post("/connectors", status_code=201)
async def create_connector(body: ConnectorIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _check_no_secrets(body.config)
    c = Connector(environment_id=await _env_id(db, body.environment), type=body.type, name=body.name, enabled=body.enabled,
                  config=body.config, credential_ref=body.credential_ref.model_dump(exclude_none=True))
    db.add(c)
    await db.flush()
    await audit.record(db, action="connector.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(c.id), environment=body.environment, details=body.model_dump())
    return connector_out(c, body.environment)


@router.put("/connectors/{cid}")
async def update_connector(cid: uuid.UUID, body: ConnectorIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)),
                           db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _check_no_secrets(body.config)
    c = await db.get(Connector, cid)
    if c is None:
        raise HTTPException(404, "Unknown connector")
    before = {"config": c.config, "credential_ref": c.credential_ref, "enabled": c.enabled}
    c.environment_id = await _env_id(db, body.environment)
    c.type, c.name, c.enabled, c.config = body.type, body.name, body.enabled, body.config
    c.credential_ref = body.credential_ref.model_dump(exclude_none=True)
    c.last_test_ok = None
    await audit.record(db, action="connector.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(c.id), environment=body.environment, details={"before": before, "after": body.model_dump()})
    return connector_out(c, body.environment)


@router.delete("/connectors/{cid}")
async def delete_connector(cid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    c = await db.get(Connector, cid)
    if c is None:
        raise HTTPException(404, "Unknown connector")
    await db.delete(c)
    await audit.record(db, action="connector.delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(cid), details={"type": c.type, "name": c.name})
    return {"status": "deleted"}


@router.post("/connectors/{cid}/test")
async def test_connector(cid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(Connector, cid)
    if c is None:
        raise HTTPException(404, "Unknown connector")
    ok, msg = await registry.test(c)
    c.last_test_at, c.last_test_ok, c.last_test_message = datetime.now(timezone.utc), ok, msg[:1000]
    await audit.record(db, action="connector.test", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(cid), outcome="success" if ok else "failure", details={"message": msg[:500]})
    return {"ok": ok, "message": msg}


# ---- LLM ---------------------------------------------------------------------------------------

class LLMIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    purpose: Literal["chat", "embedding"] = "chat"
    provider: Literal["openai", "gemini", "bedrock", "ollama"]
    model: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    region: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_output_tokens: int = Field(default=2000, ge=100, le=16000)
    daily_budget_usd: Decimal = Field(default=Decimal("5"), ge=0, le=10000)
    input_price_per_mtok: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    output_price_per_mtok: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    embedding_dimensions: int | None = Field(default=None, ge=8, le=8192)


def llm_out(c: LLMConfig, spent: Decimal | None = None) -> dict[str, Any]:
    return {"id": str(c.id), "name": c.name, "purpose": c.purpose, "provider": c.provider, "model": c.model, "endpoint": c.endpoint,
            "region": c.region, "credential": describe_credential_ref(c.credential_ref), "credential_ref": c.credential_ref,
            "timeout_seconds": c.timeout_seconds, "max_output_tokens": c.max_output_tokens, "daily_budget_usd": str(c.daily_budget_usd),
            "input_price_per_mtok": str(c.input_price_per_mtok), "output_price_per_mtok": str(c.output_price_per_mtok),
            "embedding_dimensions": c.embedding_dimensions, "is_active": c.is_active,
            "spent_last_24h_usd": str(spent) if spent is not None else None}


@router.get("/llm")
async def list_llm(ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    from datetime import timedelta

    from ..models import LLMUsage

    rows = (await db.execute(select(LLMConfig).order_by(LLMConfig.purpose, LLMConfig.name))).scalars().all()
    since = datetime.now(timezone.utc) - timedelta(days=1)
    spent = dict((await db.execute(select(LLMUsage.llm_config_id, func.sum(LLMUsage.cost_usd)).where(LLMUsage.created_at >= since)
                                   .group_by(LLMUsage.llm_config_id))).all())
    return [llm_out(r, spent.get(r.id, Decimal(0))) for r in rows]


@router.post("/llm", status_code=201)
async def create_llm(body: LLMIn, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = LLMConfig(**{**body.model_dump(exclude={"credential_ref"}), "credential_ref": body.credential_ref.model_dump(exclude_none=True)})
    db.add(c)
    await db.flush()
    await audit.record(db, action="llm.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config",
                       resource_id=str(c.id), details=body.model_dump(mode="json"))
    return llm_out(c)


@router.put("/llm/{lid}")
async def update_llm(lid: uuid.UUID, body: LLMIn, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    if c.purpose == "embedding" and c.is_active and (c.model != body.model or c.embedding_dimensions != body.embedding_dimensions):
        raise HTTPException(409, "Changing the active embedding model requires a new configuration and a reindex (Phase 2)")
    for k, v in body.model_dump(exclude={"credential_ref"}).items():
        setattr(c, k, v)
    c.credential_ref = body.credential_ref.model_dump(exclude_none=True)
    await audit.record(db, action="llm.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config",
                       resource_id=str(c.id), details=body.model_dump(mode="json"))
    return llm_out(c)


@router.post("/llm/{lid}/activate")
async def activate_llm(lid: uuid.UUID, active: bool = True, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)),
                       db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    if active:
        for other in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == c.purpose, LLMConfig.is_active.is_(True)))).scalars():
            other.is_active = False
        await db.flush()
    c.is_active = active
    await audit.record(db, action="llm.activate" if active else "llm.deactivate", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="llm_config", resource_id=str(c.id))
    return llm_out(c)


@router.post("/llm/{lid}/test")
async def test_llm(lid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    if c.purpose != "chat":
        return {"ok": False, "message": "Embedding tests arrive with Phase 2 RAG"}
    try:
        resp = await complete_metered(provider_for(c), "Reply with the JSON object {\"ok\": true} and nothing else.",
                                      [{"role": "user", "content": "connection test"}], json_mode=True, max_tokens=50)
        ok, msg = True, f"{c.provider}/{c.model} answered ({resp.tokens_in}+{resp.tokens_out} tokens): {resp.text[:80]}"
    except LLMError as exc:
        ok, msg = False, f"{exc.kind}: {exc.message}"
    await audit.record(db, action="llm.test", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config",
                       resource_id=str(c.id), outcome="success" if ok else "failure", details={"message": msg[:300]})
    return {"ok": ok, "message": msg}


# ---- users and role bindings ------------------------------------------------------------------

class UserIn(BaseModel):
    email: str = Field(pattern=EMAIL, max_length=320)
    display_name: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, min_length=12, max_length=512)


class BindingIn(BaseModel):
    role: Literal["viewer", "investigator", "operator", "approver", "admin"]
    environment: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,100}$")
    namespace: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,63}$")


async def user_out(db: AsyncSession, u: User) -> dict[str, Any]:
    bs = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == u.id).order_by(RoleBinding.created_at))).scalars().all()
    return {"id": str(u.id), "email": u.email, "display_name": u.display_name, "auth_provider": u.auth_provider, "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "bindings": [{"id": str(b.id), "role": b.role, "environment": b.environment, "namespace": b.namespace, "source": b.source} for b in bs]}


@router.get("/users")
async def list_users(ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [await user_out(db, u) for u in users]


@router.post("/users", status_code=201)
async def create_user(body: UserIn, request: Request, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)),
                      db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    from ..config import get_settings

    mode = get_settings().auth_mode
    if mode == "oidc":
        raise HTTPException(422, "With OIDC, users are created at their first sign-in; grant roles afterwards")
    if (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none():
        raise HTTPException(409, "User exists")
    if mode == "local" and not body.password:
        raise HTTPException(422, "A password of at least 12 characters is required in local mode")
    u = User(email=body.email.lower(), display_name=body.display_name, auth_provider=mode,
             password_hash=hasher.hash(body.password) if mode == "local" and body.password else None)
    db.add(u)
    await db.flush()
    await audit.record(db, action="user.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="user",
                       resource_id=str(u.id), details={"email": u.email}, ip=client_ip(request))
    return await user_out(db, u)


@router.post("/users/{uid}/active")
async def set_active(uid: uuid.UUID, active: bool, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    u = await db.get(User, uid)
    if u is None:
        raise HTTPException(404, "Unknown user")
    if u.id == ctx.user.id and not active:
        raise HTTPException(409, "You cannot disable your own account")
    u.is_active = active
    if not active:
        await revoke_all_for_user(db, u.id)
    await audit.record(db, action="user.enable" if active else "user.disable", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="user", resource_id=str(u.id))
    return await user_out(db, u)


@router.post("/users/{uid}/bindings", status_code=201)
async def add_binding(uid: uuid.UUID, body: BindingIn, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    u = await db.get(User, uid)
    if u is None:
        raise HTTPException(404, "Unknown user")
    if body.role not in ROLES:
        raise HTTPException(422, "Unknown role")
    exists = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == uid, RoleBinding.role == body.role,
                                                         RoleBinding.environment == body.environment,
                                                         RoleBinding.namespace == body.namespace))).scalar_one_or_none()
    if not exists:
        db.add(RoleBinding(user_id=uid, created_by=ctx.user.id, **body.model_dump()))
        await audit.record(db, action="rbac.grant", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="user",
                           resource_id=str(uid), environment=body.environment, namespace=body.namespace, details=body.model_dump())
    await db.flush()
    return await user_out(db, u)


@router.delete("/users/{uid}/bindings/{bid}")
async def remove_binding(uid: uuid.UUID, bid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    b = await db.get(RoleBinding, bid)
    if b is None or b.user_id != uid:
        raise HTTPException(404, "Unknown binding")
    if uid == ctx.user.id and b.role == "admin" and b.environment == "*" and b.namespace == "*":
        others = (await db.execute(select(func.count()).select_from(RoleBinding).where(
            RoleBinding.role == "admin", RoleBinding.environment == "*", RoleBinding.namespace == "*", RoleBinding.user_id != uid))).scalar_one()
        if not others:
            raise HTTPException(409, "You are the last administrator")
    await db.delete(b)
    await audit.record(db, action="rbac.revoke", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="user",
                       resource_id=str(uid), environment=b.environment, namespace=b.namespace, details={"role": b.role})
    await db.flush()
    u = await db.get(User, uid)
    return await user_out(db, u)


# ---- audit -------------------------------------------------------------------------------------

@router.get("/audit")
async def list_audit(action: str | None = None, actor: str | None = None, decision: str | None = None, environment: str | None = None,
                     namespace: str | None = None, outcome: str | None = None, resource: str | None = None, q_text: str | None = None,
                     since: str | None = None, until: str | None = None,
                     before_id: int | None = None, limit: int = 100, ctx: AuthContext = Depends(needs(P.AUDIT_READ)),
                     db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    from datetime import datetime as _dt

    from sqlalchemy import String, cast, or_

    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(max(limit, 1), 500))
    if namespace:
        q = q.where(AuditEvent.namespace == namespace)
    if outcome:
        q = q.where(AuditEvent.outcome == outcome)
    if resource:
        q = q.where(or_(AuditEvent.resource_type.ilike(f"%{resource}%"), AuditEvent.resource_id.ilike(f"%{resource}%")))
    if q_text:
        q = q.where(cast(AuditEvent.details, String).ilike(f"%{q_text[:200]}%"))
    for value, op in ((since, "ge"), (until, "le")):
        if value:
            try:
                t = _dt.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(422, f"invalid time {value!r}") from None
            q = q.where(AuditEvent.ts >= t if op == "ge" else AuditEvent.ts <= t)
    if action:
        q = q.where(AuditEvent.action.like(action.replace("*", "%")))
    if actor:
        q = q.where(AuditEvent.actor_email.ilike(f"%{actor}%"))
    if decision:
        q = q.where(AuditEvent.decision == decision)
    if environment and environment != "all":
        q = q.where(AuditEvent.environment == environment)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    rows = (await db.execute(q)).scalars().all()
    return {"events": [{
        "id": r.id, "ts": r.ts.isoformat(), "actor_email": r.actor_email, "action": r.action, "resource_type": r.resource_type,
        "resource_id": r.resource_id, "environment": r.environment, "namespace": r.namespace, "decision": r.decision,
        "outcome": r.outcome, "details": r.details, "ip": r.ip, "chain_seq": r.chain_seq, "hash": r.hash,
    } for r in rows]}


@router.get("/audit/facets")
async def audit_facets(ctx: AuthContext = Depends(needs(P.AUDIT_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Distinct values for the audit filter dropdowns (most recent 20k records)."""
    from sqlalchemy import text as _text

    rows = {}
    for col in ("action", "environment", "namespace", "outcome", "resource_type", "actor_email"):
        rows[col] = [r[0] for r in (await db.execute(_text(
            f"SELECT DISTINCT {col} FROM (SELECT {col} FROM audit_events ORDER BY id DESC LIMIT 20000) t WHERE {col} IS NOT NULL ORDER BY 1 LIMIT 200"))).all()]
    return rows


@router.get("/audit/verify")
async def verify_audit(ctx: AuthContext = Depends(needs(P.AUDIT_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await audit.verify_chain(db)
