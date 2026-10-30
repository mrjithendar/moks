"""Server-side sessions.

The browser holds only an opaque random token in an HttpOnly, Secure, SameSite=Lax cookie
scoped to the base path. The database stores its SHA-256, so a database leak does not
yield usable cookies. Mutating requests also need the per-session CSRF token in the
X-CSRF-Token header (double submit bound to the session, plus an Origin check).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import RoleBinding, Session, User
from ..rbac import Binding, Principal

COOKIE = "moks_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class AuthContext:
    user: User
    session: Session
    principal: Principal


def client_ip(request: Request) -> str | None:
    # Traefik sets X-Forwarded-For; uvicorn's proxy-headers handling (forwarded_allow_ips)
    # decides whether request.client reflects it.
    return request.client.host if request.client else None


async def create_session(db: AsyncSession, response: Response, request: Request, user: User,
                         id_token: str | None = None) -> Session:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    sess = Session(
        id=_hash(token), user_id=user.id, csrf_token=secrets.token_urlsafe(24), id_token_hint=id_token,
        expires_at=now + timedelta(hours=s.session_absolute_hours), last_seen_at=now,
        ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    db.add(sess)
    user.last_login_at = now
    set_cookie(response, token)
    return sess


def set_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        COOKIE, token, max_age=s.session_absolute_hours * 3600, path=s.base_path or "/",
        secure=s.cookie_secure, httponly=True, samesite="lax",
    )


def clear_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(COOKIE, path=s.base_path or "/", secure=s.cookie_secure, httponly=True, samesite="lax")


async def load(db: AsyncSession, token: str | None) -> AuthContext | None:
    if not token or len(token) > 200:
        return None
    s = get_settings()
    sess = await db.get(Session, _hash(token))
    now = datetime.now(timezone.utc)
    if sess is None or sess.revoked_at is not None or sess.expires_at <= now:
        return None
    if now - sess.last_seen_at > timedelta(minutes=s.session_idle_minutes):
        sess.revoked_at = now
        return None
    user = await db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    if now - sess.last_seen_at > timedelta(minutes=1):
        sess.last_seen_at = now
    rows = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == user.id))).scalars().all()
    principal = Principal(str(user.id), user.email, [Binding(r.role, r.environment, r.namespace) for r in rows])
    return AuthContext(user, sess, principal)


async def revoke(db: AsyncSession, sess: Session) -> None:
    sess.revoked_at = datetime.now(timezone.utc)


async def revoke_all_for_user(db: AsyncSession, user_id) -> None:
    await db.execute(update(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
                     .values(revoked_at=datetime.now(timezone.utc)))


def csrf_ok(sess: Session, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(sess.csrf_token, header)


# ---- Signed short-lived cookie for the OIDC round trip --------------------------------------

def sign(payload: str) -> str:
    key = get_settings().session_secret.encode()
    mac = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def unsign(value: str, max_age: int) -> str | None:
    payload, _, mac = value.rpartition(".")
    if not payload:
        return None
    key = get_settings().session_secret.encode()
    if not hmac.compare_digest(mac, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        import base64
        import json

        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None
    if int(data.get("exp", 0)) < time.time() or int(data.get("exp", 0)) > time.time() + max_age:
        return None
    return payload
