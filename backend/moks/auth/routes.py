"""Sign-in, sign-out and current-user endpoints.

Modes (MOKS_AUTH_MODE):
- oidc:  authorization code flow with PKCE and nonce against any OIDC provider (Okta, Entra ID,
         Google, Keycloak, Dex...). Users are created on first sign-in WITHOUT roles unless
         listed in MOKS_BOOTSTRAP_ADMINS or mapped through MOKS_OIDC_GROUP_ROLES.
- local: email + password (argon2id). Users are created by an administrator (UI or CLI);
         there is no self-registration and no first-visitor setup page.
- dev:   password-less sign-in for local development only (refused unless MOKS_ENV=development).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import RoleBinding, User
from ..rbac import ROLE_DESCRIPTIONS, P, parse_grant
from .deps import get_auth
from .ratelimit import limiter
from .sessions import AuthContext, clear_cookie, client_ip, create_session, revoke, sign, unsign

log = logging.getLogger("moks.auth")
router = APIRouter(tags=["auth"])
hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash(secrets.token_hex(16))
OIDC_COOKIE = "moks_oidc"


def safe_next(target: str | None) -> str:
    """Only allow redirects back into MOKS (no open redirects)."""
    base = get_settings().base_path or ""
    if target and target.startswith(base + "/") and not target.startswith("//") and "\\" not in target and "://" not in target:
        return target
    return base + "/"


async def _ensure_bootstrap(db: AsyncSession, user: User) -> None:
    """Grant Administrator to bootstrap emails (idempotent)."""
    if user.email.lower() not in get_settings().bootstrap_admin_set:
        return
    exists = (await db.execute(select(RoleBinding).where(
        RoleBinding.user_id == user.id, RoleBinding.role == "admin", RoleBinding.environment == "*", RoleBinding.namespace == "*"
    ))).scalar_one_or_none()
    if not exists:
        db.add(RoleBinding(user_id=user.id, role="admin", environment="*", namespace="*", source="bootstrap"))
        await audit.record(db, action="rbac.bootstrap_admin", actor_id=user.id, actor_email=user.email,
                           resource_type="user", resource_id=str(user.id))


class MeOut(BaseModel):
    id: str
    email: str
    display_name: str
    auth_mode: str
    csrf_token: str
    roles: list[dict[str, str]]
    permissions: list[str]


@router.get("/auth/config")
async def auth_config() -> dict[str, Any]:
    s = get_settings()
    return {"mode": s.auth_mode, "login_url": f"{s.api_prefix}/auth/oidc/login" if s.auth_mode == "oidc" else None}


@router.get("/auth/me", response_model=MeOut)
async def me(ctx: AuthContext = Depends(get_auth)) -> MeOut:
    return MeOut(
        id=str(ctx.user.id), email=ctx.user.email, display_name=ctx.user.display_name or ctx.user.email,
        auth_mode=get_settings().auth_mode, csrf_token=ctx.session.csrf_token,
        roles=[{"role": b.role, "environment": b.environment, "namespace": b.namespace} for b in ctx.principal.bindings],
        permissions=sorted(ctx.principal.permissions()),
    )


@router.get("/auth/roles")
async def roles(ctx: AuthContext = Depends(get_auth)) -> list[dict[str, str]]:
    return [{"role": r, "description": d} for r, d in ROLE_DESCRIPTIONS.items()]


@router.post("/auth/logout")
async def logout(request: Request, response: Response, ctx: AuthContext = Depends(get_auth),
                 db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    await revoke(db, ctx.session)
    clear_cookie(response)
    await audit.record(db, action="auth.logout", actor_id=ctx.user.id, actor_email=ctx.user.email, ip=client_ip(request))
    redirect = s.base_path + "/login"
    if s.auth_mode == "oidc":
        meta = await _discovery()
        if meta.get("end_session_endpoint"):
            params = {"post_logout_redirect_uri": s.public_url.rstrip("/") + "/login", "client_id": s.oidc_client_id}
            if ctx.session.id_token_hint:
                params["id_token_hint"] = ctx.session.id_token_hint
            redirect = meta["end_session_endpoint"] + "?" + urlencode(params)
    return {"redirect": redirect}


# ---- local -----------------------------------------------------------------------------------

EMAIL = r"^[^@\s]{1,64}@[^@\s]{1,255}$"


class LoginIn(BaseModel):
    email: str = Field(pattern=EMAIL, max_length=320)
    password: str = Field(min_length=1, max_length=512)


@router.post("/auth/login")
async def local_login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    s = get_settings()
    if s.auth_mode != "local":
        raise HTTPException(404, "Password sign-in is not enabled")
    ip = client_ip(request) or "unknown"
    limiter.check(f"login-ip:{ip}", s.rate_login_per_minute)
    limiter.check(f"login-user:{body.email.lower()}", s.rate_login_per_minute)
    user = (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    ok = False
    try:
        ok = hasher.verify(user.password_hash if user and user.password_hash else _DUMMY_HASH, body.password)
    except (VerificationError, InvalidHashError):
        ok = False
    if not ok or user is None or not user.is_active or user.auth_provider != "local":
        await audit.record(None, action="auth.login", actor_email=body.email.lower(), decision="deny", outcome="failure",
                           details={"mode": "local"}, ip=ip)
        raise HTTPException(401, "Invalid email or password")
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(body.password)
    await create_session(db, response, request, user)
    await _ensure_bootstrap(db, user)
    await audit.record(db, action="auth.login", actor_id=user.id, actor_email=user.email, decision="allow", details={"mode": "local"}, ip=ip)
    return {"status": "ok"}


# ---- dev -------------------------------------------------------------------------------------

class DevLoginIn(BaseModel):
    email: str = Field(pattern=EMAIL, max_length=320)


@router.post("/auth/dev-login")
async def dev_login(body: DevLoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    s = get_settings()
    if s.auth_mode != "dev" or s.env != "development":
        raise HTTPException(404, "Not found")
    email = body.email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=email.split("@")[0], auth_provider="dev")
        db.add(user)
        await db.flush()
    await _ensure_bootstrap(db, user)
    await create_session(db, response, request, user)
    await audit.record(db, action="auth.login", actor_id=user.id, actor_email=email, decision="allow", details={"mode": "dev"},
                       ip=client_ip(request))
    return {"status": "ok"}


# ---- OIDC ------------------------------------------------------------------------------------

_discovery_cache: dict[str, Any] = {}


async def _discovery() -> dict[str, Any]:
    s = get_settings()
    cached = _discovery_cache.get(s.oidc_issuer)
    if cached and cached[0] > time.time():
        return cached[1]
    url = s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            r.raise_for_status()
            meta = r.json()
            jwks = (await c.get(meta["jwks_uri"])).json()
    except Exception as exc:
        raise HTTPException(503, f"Identity provider unavailable ({type(exc).__name__})") from None
    if meta.get("issuer", "").rstrip("/") != s.oidc_issuer.rstrip("/"):
        raise HTTPException(503, "Identity provider issuer mismatch")
    meta["_jwks"] = jwks
    _discovery_cache[s.oidc_issuer] = (time.time() + 3600, meta)
    return meta


def _redirect_uri() -> str:
    s = get_settings()
    return s.public_url.rstrip("/") + "/api/auth/oidc/callback"


@router.get("/auth/oidc/login")
async def oidc_login(request: Request, next: str | None = None) -> RedirectResponse:
    s = get_settings()
    if s.auth_mode != "oidc":
        raise HTTPException(404, "OIDC sign-in is not enabled")
    limiter.check(f"oidc-ip:{client_ip(request)}", s.rate_login_per_minute * 3)
    meta = await _discovery()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        "state": state, "nonce": nonce, "verifier": verifier, "next": safe_next(next), "exp": int(time.time()) + 600,
    }).encode()).decode()
    params = {
        "response_type": "code", "client_id": s.oidc_client_id, "redirect_uri": _redirect_uri(), "scope": s.oidc_scopes,
        "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    resp = RedirectResponse(meta["authorization_endpoint"] + "?" + urlencode(params), status_code=302)
    resp.set_cookie(OIDC_COOKIE, sign(payload), max_age=600, path=f"{s.api_prefix}/auth/oidc", secure=s.cookie_secure,
                    httponly=True, samesite="lax")
    return resp


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, db: AsyncSession = Depends(get_db), code: str | None = None,
                        state: str | None = None, error: str | None = None) -> Response:
    s = get_settings()
    if s.auth_mode != "oidc":
        raise HTTPException(404, "OIDC sign-in is not enabled")
    login_page = s.base_path + "/login"
    if error:
        return RedirectResponse(f"{login_page}?error={urlencode({'e': error[:60]})[2:]}", status_code=302)
    raw = request.cookies.get(OIDC_COOKIE)
    payload = unsign(raw, 600) if raw else None
    if not payload or not code or not state:
        return RedirectResponse(f"{login_page}?error=expired", status_code=302)
    data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    if not secrets.compare_digest(data["state"], state):
        return RedirectResponse(f"{login_page}?error=state", status_code=302)

    meta = await _discovery()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(meta["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(),
                "client_id": s.oidc_client_id, "client_secret": s.oidc_client_secret, "code_verifier": data["verifier"],
            }, headers={"Accept": "application/json"})
        r.raise_for_status()
        tokens = r.json()
    except Exception as exc:
        log.warning("oidc token exchange failed", extra={"event": "oidc_token_failed", "errorType": type(exc).__name__})
        return RedirectResponse(f"{login_page}?error=token", status_code=302)

    claims = _validate_id_token(tokens.get("id_token", ""), meta, data["nonce"])
    if claims is None:
        await audit.record(None, action="auth.login", decision="deny", outcome="failure", details={"mode": "oidc", "reason": "id_token"},
                           ip=client_ip(request))
        return RedirectResponse(f"{login_page}?error=id_token", status_code=302)
    email = str(claims.get("email") or "").lower()
    if not email or claims.get("email_verified") is False:
        return RedirectResponse(f"{login_page}?error=email", status_code=302)
    domains = {d.strip().lower() for d in s.oidc_allowed_domains.split(",") if d.strip()}
    if domains and email.rsplit("@", 1)[-1] not in domains:
        await audit.record(None, action="auth.login", actor_email=email, decision="deny", outcome="failure",
                           details={"mode": "oidc", "reason": "domain"}, ip=client_ip(request))
        return RedirectResponse(f"{login_page}?error=domain", status_code=302)

    user = (await db.execute(select(User).where(User.auth_provider == "oidc", User.subject == str(claims["sub"])))).scalar_one_or_none()
    if user is None:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is not None and user.auth_provider != "oidc":
            return RedirectResponse(f"{login_page}?error=account_conflict", status_code=302)
    if user is None:
        user = User(email=email, display_name=str(claims.get("name") or email), auth_provider="oidc", subject=str(claims["sub"]))
        db.add(user)
        await db.flush()
        await audit.record(db, action="user.create", actor_id=user.id, actor_email=email, resource_type="user",
                           resource_id=str(user.id), details={"via": "oidc"})
    if not user.is_active:
        return RedirectResponse(f"{login_page}?error=disabled", status_code=302)
    user.display_name = str(claims.get("name") or user.display_name)
    await _apply_group_grants(db, user, claims)
    await _ensure_bootstrap(db, user)
    resp = RedirectResponse(data["next"], status_code=302)
    await create_session(db, resp, request, user, id_token=tokens.get("id_token"))
    resp.delete_cookie(OIDC_COOKIE, path=f"{s.api_prefix}/auth/oidc")
    await audit.record(db, action="auth.login", actor_id=user.id, actor_email=email, decision="allow", details={"mode": "oidc"},
                       ip=client_ip(request))
    return resp


def _validate_id_token(id_token: str, meta: dict[str, Any], nonce: str) -> dict[str, Any] | None:
    from authlib.jose import JsonWebKey, JsonWebToken
    from authlib.jose.errors import JoseError

    s = get_settings()
    try:
        keys = JsonWebKey.import_key_set(meta["_jwks"])
        jwt = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"])
        claims = jwt.decode(id_token, keys, claims_options={
            "iss": {"essential": True, "value": meta["issuer"]},
            "aud": {"essential": True, "value": s.oidc_client_id},
            "exp": {"essential": True},
            "sub": {"essential": True},
            "nonce": {"essential": True, "value": nonce},
        })
        claims.validate(leeway=60)
        return dict(claims)
    except (JoseError, ValueError, KeyError) as exc:
        log.warning("id token rejected", extra={"event": "oidc_id_token_rejected", "errorType": type(exc).__name__})
        return None


async def _apply_group_grants(db: AsyncSession, user: User, claims: dict[str, Any]) -> None:
    s = get_settings()
    try:
        mapping: dict[str, list[str]] = json.loads(s.oidc_group_roles or "{}")
    except ValueError:
        log.error("MOKS_OIDC_GROUP_ROLES is not valid JSON", extra={"event": "config_error"})
        return
    if not mapping:
        return
    groups = claims.get(s.oidc_groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    grants = {parse_grant(g) for grp in groups for g in mapping.get(str(grp), [])}
    # Group-derived bindings mirror the IdP on every sign-in; manual bindings are untouched.
    await db.execute(delete(RoleBinding).where(RoleBinding.user_id == user.id, RoleBinding.source == "oidc-group"))
    existing = {(r.role, r.environment, r.namespace) for r in (await db.execute(
        select(RoleBinding).where(RoleBinding.user_id == user.id))).scalars()}
    for b in grants:
        if (b.role, b.environment, b.namespace) in existing:
            continue
        db.add(RoleBinding(user_id=user.id, role=b.role, environment=b.environment, namespace=b.namespace, source="oidc-group"))
    await audit.record(db, action="rbac.sync_groups", actor_id=user.id, actor_email=user.email, resource_type="user",
                       resource_id=str(user.id), details={"grants": sorted(f"{b.role}:{b.environment}:{b.namespace}" for b in grants)})


__all__ = ["router", "hasher", "P"]
