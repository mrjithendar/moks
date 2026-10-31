"""Session lifecycle: request, provision, attach rules, ending, and cleanup.

Credentials exist in this process only inside provision(), and in the pod's memory-backed volume.
The session ends when they expire: there is no renewal, so a session cannot outlive what was issued.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select

from .. import audit
from ..config import get_settings
from ..db import session_scope
from ..models import RoleBinding, Session, TerminalSession, User
from ..rbac import Binding, P, Principal
from . import pods as podlib
from .aws import Aws, TerminalError, check_identity, source_identity
from .profiles import IN_CLUSTER, Profile, find, kube_namespaces

log = logging.getLogger("moks.terminal")

MIN_MINUTES = 15  # the shortest AWS role session
STARTUP_SECONDS = 240
OPEN = ("starting", "ready")

END_TEXT = {
    "stopped": "You stopped this session.",
    "revoked": "An administrator ended this session.",
    "access_removed": "Your terminal access was removed, so the session ended.",
    "logged_out": "You signed out of MOKS, so the session ended.",
    "idle": "The session ended after a period with no input.",
    "max_lifetime": "The session reached its maximum lifetime.",
    "credentials_expired": "The session's credentials expired, so it ended.",
    "shell_exited": "The shell exited.",
    "pod_lost": "The session's pod no longer exists.",
}

aws_factory: Callable[[], Any] = Aws
pods_factory: Callable[[], Any] = podlib.Pods


def now() -> datetime:
    return datetime.now(timezone.utc)


def session_out(s: TerminalSession, owner: str | None = None) -> dict:
    return {"id": str(s.id), "owner": owner, "environment": s.environment, "profile": s.profile, "account": s.aws_account, "role_arn": s.role_arn, "region": s.region,
            "cluster": s.cluster, "namespaces": s.namespaces, "purpose": s.purpose, "status": s.status, "end_reason": s.end_reason, "end_text": END_TEXT.get(s.end_reason or ""),
            "error": s.error, "image": s.image, "pod": s.pod_name, "aws_identity": s.aws_identity, "kube_identity": s.kube_identity, "role_session_name": s.role_session_name,
            "idle_minutes": s.idle_minutes, "lifetime_minutes": s.lifetime_minutes, "created_at": s.created_at.isoformat() if s.created_at else None,
            "ready_at": s.ready_at.isoformat() if s.ready_at else None, "expires_at": s.expires_at.isoformat(), "last_input_at": s.last_input_at.isoformat() if s.last_input_at else None,
            "credential_expires_at": s.credential_expires_at.isoformat() if s.credential_expires_at else None, "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "attach_count": s.attach_count}


def validate(profile: Profile | None, environment: str, account: str, region: str, cluster: str | None) -> Profile:
    """Every part of the selection must be one a profile offers. Nothing is defaulted or substituted."""
    if profile is None or profile.environment != environment:
        raise TerminalError(f"no terminal profile for environment {environment}")
    if profile.account != account:
        raise TerminalError(f"profile {profile.name} is for account {profile.account}, not {account}")
    if region not in profile.regions:
        raise TerminalError(f"profile {profile.name} does not allow region {region}")
    if cluster and cluster not in profile.clusters():
        raise TerminalError(f"profile {profile.name} does not allow cluster {cluster}")
    return profile


async def create(db, user: User, login_session: str, principal: Principal, *, profile_name: str, environment: str, account: str, region: str,
                 cluster: str | None, purpose: str, minutes: int) -> TerminalSession:
    cfg = get_settings()
    profile = validate(find(profile_name), environment, account, region, cluster)
    if not cfg.terminal_image:
        raise TerminalError("no terminal image is configured")
    mine = (await db.execute(select(func.count()).select_from(TerminalSession).where(TerminalSession.owner_id == user.id, TerminalSession.status.in_(OPEN)))).scalar_one()
    if mine >= cfg.terminal_user_sessions:
        raise TerminalError(f"you already have {mine} open terminal sessions; stop one first")
    lifetime = max(MIN_MINUTES, min(minutes, cfg.terminal_max_minutes))
    sid = uuid.uuid4()
    s = TerminalSession(id=sid, owner_id=user.id, login_session=login_session, environment=environment, profile=profile.name, aws_account=account,
                        role_arn=profile.role_arn, region=region, cluster=cluster, namespaces=kube_namespaces() if cluster == IN_CLUSTER else [], purpose=purpose,
                        image=cfg.terminal_image, pod_name=f"moks-term-{str(sid)[:8]}", pod_namespace=cfg.terminal_namespace, role_session_name=f"moks-term-{str(sid)[:8]}",
                        idle_minutes=cfg.terminal_idle_minutes, lifetime_minutes=lifetime, expires_at=now() + timedelta(minutes=lifetime), status="starting")
    db.add(s)
    await db.flush()
    await audit.record(db, action="terminal.create", actor_id=user.id, actor_email=user.email, resource_type="terminal_session", resource_id=str(sid), environment=environment,
                       decision="allow", details=scope_of(s))
    return s


def scope_of(s: TerminalSession) -> dict:
    return {"purpose": s.purpose, "account": s.aws_account, "role": s.role_arn, "region": s.region, "cluster": s.cluster, "namespaces": s.namespaces,
            "profile": s.profile, "image": s.image, "pod": s.pod_name, "role_session": s.role_session_name}


async def provision(sid: uuid.UUID, email: str, aws=None, pods=None) -> None:
    """Runs after the request returns. On any failure the session is failed and no shell opens."""
    own = pods is None
    aws, pods = aws or aws_factory(), pods or pods_factory()
    try:
        async with session_scope() as db:
            s = await db.get(TerminalSession, sid)
            profile_name, name, region, cluster, session_name, seconds = s.profile, s.pod_name, s.region, s.cluster, s.role_session_name, s.lifetime_minutes * 60
            ns = (s.namespaces or ["default"])[0]
        profile = find(profile_name)
        if profile is None:
            raise TerminalError(f"profile {profile_name} no longer exists")
        creds = await aws.assume(profile, region, session_name, seconds, source_identity(email))
        found = await aws.identity(creds, region)
        check_identity(found, profile, session_name)  # before any pod exists
        async with session_scope() as db:
            s = await db.get(TerminalSession, sid)
            if s.status != "starting":
                return
            s.credential_expires_at, s.expires_at = creds.expires_at, min(s.expires_at, creds.expires_at)
            manifest = podlib.pod_manifest(s)
        await pods.create(manifest)
        await pods.wait_running(name, STARTUP_SECONDS - 30)
        await pods.write_file(name, podlib.AWS_FILE, creds.shared_file())
        await pods.write_file(name, f"{podlib.SECRETS_DIR}/aws-config", f"[default]\nregion = {region}\noutput = json\n".encode())
        inside = await _shell_identity(pods, name)
        check_identity(inside, profile, session_name)  # what the shell itself sees, through the pod's own network
        kube = None
        if cluster == IN_CLUSTER:
            server, ca = await pods.server()
            token = await pods.service_account_token(name, seconds)
            await pods.write_file(name, podlib.KUBECONFIG, podlib.kubeconfig(server, ca, token, ns))
        elif cluster:
            code, out = await pods.run(name, ["aws", "eks", "update-kubeconfig", "--name", cluster, "--region", region, "--kubeconfig", podlib.KUBECONFIG], timeout=45)
            if code != 0:
                raise TerminalError(f"could not configure EKS cluster {cluster}: {out[-200:]}")
        if cluster:
            kube = await _kube_identity(pods, name, cluster)
        async with session_scope() as db:
            s = await db.get(TerminalSession, sid)
            if s.status == "starting":
                s.status, s.ready_at, s.last_input_at, s.aws_identity, s.kube_identity = "ready", now(), now(), inside["arn"], kube
                await audit.record(db, action="terminal.ready", actor_id=s.owner_id, resource_type="terminal_session", resource_id=str(sid), environment=s.environment,
                                   details={**scope_of(s), "aws_identity": inside["arn"], "kube_identity": kube, "credentials_expire": creds.expires_at.isoformat()})
    except TerminalError as exc:
        await _fail(sid, str(exc))
    except Exception as exc:
        log.exception("terminal provisioning failed", extra={"event": "terminal_failed", "errorType": type(exc).__name__})
        await _fail(sid, f"the session could not start ({type(exc).__name__})")
    finally:
        if own:
            await pods.aclose()


async def _shell_identity(pods, name: str) -> dict[str, str]:
    code, out = await pods.run(name, ["aws", "sts", "get-caller-identity", "--output", "json"], timeout=45)
    if code != 0:
        raise TerminalError(f"the shell could not verify its AWS identity (exit {code}): check the terminal network policy. {out[-200:]}")
    try:
        row = json.loads(out)
        return {"account": row["Account"], "arn": row["Arn"]}
    except (ValueError, KeyError):
        raise TerminalError("the shell returned an unreadable AWS identity") from None


async def _kube_identity(pods, name: str, cluster: str) -> str:
    code, out = await pods.run(name, ["kubectl", "auth", "whoami", "-o", "json", "--request-timeout=15s"], timeout=30)
    if code != 0:
        raise TerminalError(f"the shell could not reach cluster {cluster}: {out[-200:]}")
    try:
        who = json.loads(out)["status"]["userInfo"]["username"]
    except (ValueError, KeyError):
        raise TerminalError("the cluster returned an unreadable identity") from None
    # A safety net for a mistaken binding, not the control: RBAC is what enforces read-only.
    for check in (["create", "pods"], ["get", "secrets"], ["*", "*"]):
        code, _ = await pods.run(name, ["kubectl", "auth", "can-i", *check, "--all-namespaces", "--request-timeout=15s"], timeout=30)
        if code == 0:
            raise TerminalError(f"refused: {who} may {' '.join(check)} across the cluster, so this is not a read-only identity")
    return who


async def _fail(sid: uuid.UUID, reason: str) -> None:
    async with session_scope() as db:
        s = await db.get(TerminalSession, sid)
        if s.status == "starting":
            s.status, s.error, s.ended_at = "failed", reason[:500], now()
            await audit.record(db, action="terminal.failed", actor_id=s.owner_id, resource_type="terminal_session", resource_id=str(sid), environment=s.environment,
                               outcome="failure", details={"reason": reason[:500]})


async def end(db, s: TerminalSession, reason: str, actor: User | None = None) -> None:
    if s.status not in OPEN:
        return
    s.status, s.end_reason, s.ended_at = "ended", reason, now()
    await audit.record(db, action="terminal.end", actor_id=actor.id if actor else s.owner_id, actor_email=actor.email if actor else None, resource_type="terminal_session",
                       resource_id=str(s.id), environment=s.environment, details={"reason": reason, "pod": s.pod_name, "attachments": s.attach_count})


async def _allowed(db, s: TerminalSession) -> str | None:
    """Why this session may no longer run, or None."""
    user = await db.get(User, s.owner_id)
    rows = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == s.owner_id))).scalars().all()
    principal = Principal(str(s.owner_id), user.email if user else "", [Binding(b.role, b.environment, b.namespace) for b in rows])
    if user is None or not user.is_active or not principal.can(P.TERMINAL_USE, s.environment, None):
        return "access_removed"
    login = await db.get(Session, s.login_session)
    if login is None or login.revoked_at is not None or login.expires_at <= now():
        return "logged_out"
    return None


def stop_reason(s: TerminalSession, moment: datetime) -> str | None:
    if s.status == "starting":
        return None
    if s.credential_expires_at and s.credential_expires_at <= moment:
        return "credentials_expired"
    if s.expires_at <= moment:
        return "max_lifetime"
    if s.last_input_at and s.last_input_at + timedelta(minutes=s.idle_minutes) <= moment:
        return "idle"
    return None


async def reap(pods) -> None:
    """Runs on a timer in every gateway. Safe to overlap, because ending and deleting are idempotent."""
    moment = now()
    async with session_scope() as db:
        rows = (await db.execute(select(TerminalSession).where(TerminalSession.status.in_(OPEN)).with_for_update(skip_locked=True))).scalars().all()
        for s in rows:
            if s.status == "starting" and s.created_at + timedelta(seconds=STARTUP_SECONDS) <= moment:
                s.status, s.error, s.ended_at = "failed", "the session did not start in time", moment
                await audit.record(db, action="terminal.failed", actor_id=s.owner_id, resource_type="terminal_session", resource_id=str(s.id), environment=s.environment,
                                   outcome="failure", details={"reason": "startup timeout"})
                continue
            reason = stop_reason(s, moment) or await _allowed(db, s)
            if reason:
                await end(db, s, reason)
    async with session_scope() as db:
        done = (await db.execute(select(TerminalSession).where(TerminalSession.status.notin_(OPEN), TerminalSession.pod_deleted_at.is_(None)))).scalars().all()
        for s in done:
            await pods.delete(s.pod_name)
            s.pod_deleted_at = now()
            await audit.record(db, action="terminal.cleanup", actor_id=s.owner_id, resource_type="terminal_session", resource_id=str(s.id), environment=s.environment,
                               details={"pod": s.pod_name, "reason": s.end_reason or s.status})
    existing = await pods.sessions()
    async with session_scope() as db:
        known = {str(i) for i in (await db.execute(select(TerminalSession.id).where(TerminalSession.status.in_(OPEN)))).scalars().all()}
        lost = (await db.execute(select(TerminalSession).where(TerminalSession.status == "ready"))).scalars().all()
        for s in lost:
            if s.pod_name not in existing:
                await end(db, s, "pod_lost")
    for name, sid in existing.items():  # a pod whose session is over, or was never recorded
        if sid not in known:
            await pods.delete(name)


async def mark_input(sid: uuid.UUID) -> None:
    async with session_scope() as db:
        s = await db.get(TerminalSession, sid)
        if s and s.status == "ready":
            s.last_input_at = now()
