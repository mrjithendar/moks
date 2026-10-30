"""FastAPI dependencies for authentication, CSRF and authorization."""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..observability import POLICY_DENIALS
from ..rbac import P
from .ratelimit import limiter
from .sessions import COOKIE, AuthContext, client_ip, csrf_ok, load

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # non-browser clients; the CSRF token still applies
    public = urlsplit(get_settings().public_url)
    o = urlsplit(origin)
    return (o.scheme, o.netloc) == (public.scheme, public.netloc)


async def get_auth(request: Request, db: AsyncSession = Depends(get_db, scope="function")) -> AuthContext:
    ctx = await load(db, request.cookies.get(COOKIE))
    if ctx is None:
        raise HTTPException(401, "Not signed in")
    s = get_settings()
    limiter.check(f"api:{ctx.session.id}", s.rate_api_per_minute)
    if request.method in UNSAFE:
        if not _origin_ok(request) or not csrf_ok(ctx.session, request.headers.get("x-csrf-token")):
            POLICY_DENIALS.labels("csrf").inc()
            raise HTTPException(403, "CSRF check failed; reload the page and try again")
    request.state.auth = ctx
    return ctx


async def deny(request: Request, ctx: AuthContext, perm: P | str, environment: str | None, namespace: str | None, reason: str) -> HTTPException:
    POLICY_DENIALS.labels("rbac").inc()
    await audit.record(None, action="authz.deny", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="permission", resource_id=str(perm), environment=environment, namespace=namespace,
                       decision="deny", outcome="denied", details={"path": request.url.path, "method": request.method, "reason": reason},
                       ip=client_ip(request), request_id=request.headers.get("x-request-id"))
    return HTTPException(403, reason)


async def require(request: Request, ctx: AuthContext, perm: P, environment: str | None = None, namespace: str | None = None) -> None:
    if not ctx.principal.can(perm, environment, namespace):
        where = f" in {environment}" if environment else ""
        where += f"/{namespace}" if namespace else ""
        raise await deny(request, ctx, perm, environment, namespace, f"You need {perm}{where}")


def needs(perm: P):
    """Dependency for global permissions (admin.*, audit.read)."""
    async def dep(request: Request, ctx: AuthContext = Depends(get_auth)) -> AuthContext:
        await require(request, ctx, perm, "*" if perm.startswith("admin") or perm == P.AUDIT_READ else None,
                      "*" if perm.startswith("admin") or perm == P.AUDIT_READ else None)
        return ctx
    return dep
