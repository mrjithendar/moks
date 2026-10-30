"""Diagnostic sessions: disposable, TTL-bound pods in the diagnostics namespace.

Request -> action plan `diagnostic_session` (approval required in production and for elevated
sessions) -> executor starts the pod -> catalog packages install inside it -> users run catalog
commands -> TTL expiry or termination deletes the pod. Every step is audited.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .. import audit
from ..config import get_settings
from ..db import session_scope
from ..models import ActionPlan, DiagnosticCommand, DiagnosticSession, Environment
from ..rbac import P, Principal
from ..redaction import redact
from . import catalog

log = logging.getLogger("moks.diagnostics")


def pod_manifest(sess: DiagnosticSession, ns: str) -> dict:
    ttl = sess.ttl_minutes * 60
    pip_only = all(catalog.PACKAGES[p]["kind"] == "pip" for p in sess.packages)
    sec = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": not sess.elevated, "capabilities": {"drop": ["ALL"]}}
    pod_sec: dict[str, Any] = {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534, "seccompProfile": {"type": "RuntimeDefault"}}
    if sess.elevated:
        pod_sec = {"runAsNonRoot": False, "runAsUser": 0, "seccompProfile": {"type": "RuntimeDefault"}}
        sec["capabilities"] = {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"]}
    big = sess.image_key == "python"
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": f"moks-diag-{str(sess.id)[:8]}", "namespace": ns,
                     "labels": {"app": "moks-diagnostic", "moks.io/session": str(sess.id)},
                     "annotations": {"moks.io/requested-by": str(sess.requested_by), "moks.io/expires-at": sess.expires_at.isoformat()}},
        "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
            "activeDeadlineSeconds": ttl + 60, "terminationGracePeriodSeconds": 1, "securityContext": pod_sec,
            "containers": [{
                "name": "diag", "image": sess.image, "command": ["sleep", str(ttl)], "securityContext": sec,
                "env": [{"name": "HOME", "value": "/tmp"}, {"name": "PIP_USER", "value": "1"}] if pip_only or big else [{"name": "HOME", "value": "/tmp"}],
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                              "limits": {"cpu": "250m", "memory": "256Mi" if big else "128Mi", "ephemeral-storage": "256Mi"}},
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}],
        },
    }


async def request(db, principal: Principal, env: Environment, image_key: str, packages: list[str], ttl_minutes: int, reason: str) -> tuple[DiagnosticSession, ActionPlan]:
    from ..actions.service import PlanError, compute_hash

    if image_key not in catalog.IMAGES:
        raise PlanError(400, f"unknown image {image_key!r}; choose from {', '.join(catalog.IMAGES)}")
    try:
        elevated = catalog.check_packages(packages, image_key)
    except catalog.ArgError as exc:
        raise PlanError(422, str(exc)) from None
    if not 5 <= ttl_minutes <= 240:
        raise PlanError(422, "TTL must be 5-240 minutes")
    ns = get_settings().diagnostics_namespace
    if not principal.can(P.ACTION_REQUEST, env.name, None):
        raise PlanError(403, f"you lack action.request in {env.name}")
    sess = DiagnosticSession(environment_id=env.id, image_key=image_key, image=catalog.IMAGES[image_key]["image"], packages=packages,
                             elevated=elevated, ttl_minutes=ttl_minutes, requested_by=uuid.UUID(principal.user_id), reason=reason[:2000],
                             pod_namespace=ns, status="requested")
    db.add(sess)
    await db.flush()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = ActionPlan(environment_id=env.id, namespace=ns, action="diagnostic_session", target_kind="DiagnosticSession",
                      target_name=f"moks-diag-{str(sess.id)[:8]}", params={"session_id": str(sess.id), "image": sess.image,
                                                                          "packages": packages, "elevated": elevated, "ttl_minutes": ttl_minutes},
                      reason=reason or "diagnostic session", evidence=[],
                      expected_impact=f"A disposable pod ({sess.image}) runs in {ns} for at most {ttl_minutes} minutes.",
                      blast_radius="None for workloads: no service account token, resource limits, network policy egress only.",
                      preconditions=[f"namespace {ns} exists with its NetworkPolicy and quota"],
                      validation=["pod reaches Running", "packages install successfully" if packages else "no packages requested"],
                      rollback="Terminate the session; the pod is deleted (also automatically at TTL).",
                      requires_approval=env.is_production or elevated, requested_by=uuid.UUID(principal.user_id),
                      expires_at=now + timedelta(minutes=env.plan_ttl_minutes), status="pending", plan_hash="", auto_recover=False)
    db.add(plan)
    await db.flush()
    plan.plan_hash = compute_hash(plan, env.name)
    sess.plan_id = plan.id
    await audit.record(db, action="diagnostic.request", actor_id=principal.user_id, actor_email=principal.email, resource_type="diagnostic_session",
                       resource_id=str(sess.id), environment=env.name, namespace=ns, decision="allow",
                       details={"image": sess.image, "packages": packages, "elevated": elevated, "ttl_minutes": ttl_minutes})
    return sess, plan


async def start(plan: ActionPlan, writer) -> dict:
    """Executor: create the pod, wait for Running, install packages."""
    async with session_scope() as db:
        sess = await db.get(DiagnosticSession, uuid.UUID(plan.params["session_id"]))
        sess.status, sess.started_at = "starting", datetime.now(timezone.utc)
        sess.expires_at = sess.started_at + timedelta(minutes=sess.ttl_minutes)
        manifest = pod_manifest(sess, sess.pod_namespace)
        sess_id, ns, packages, image_key = sess.id, sess.pod_namespace, list(sess.packages), sess.image_key
    name = await writer.create_pod(ns, manifest)
    phase = "Pending"
    for _ in range(60):
        phase = await writer.pod_phase(ns, name)
        if phase in ("Running", "Failed", "Succeeded"):
            break
        await asyncio.sleep(2)
    installs = []
    if phase == "Running":
        for argv in catalog.install_argv(packages, image_key):
            code, out = await writer.exec(ns, name, argv, timeout=120)
            installs.append({"argv": argv, "exit_code": code, "output": redact(out[-4000:])})
    ok = phase == "Running" and all(i["exit_code"] == 0 for i in installs)
    async with session_scope() as db:
        sess = await db.get(DiagnosticSession, sess_id)
        sess.pod_name, sess.status = name, "ready" if ok else "failed"
        if not ok:
            sess.error = f"pod phase {phase}" if phase != "Running" else "package installation failed"
        for i in installs:
            db.add(DiagnosticCommand(session_id=sess_id, actor_id=sess.requested_by, tool="install", args={"packages": packages},
                                     argv=i["argv"], exit_code=i["exit_code"], output=i["output"], status="done",
                                     finished_at=datetime.now(timezone.utc)))
    if not ok:
        await writer.delete_pod(ns, name)
    return {"ok": ok, "pod": name, "phase": phase, "installs": [{"argv": i["argv"], "exit_code": i["exit_code"]} for i in installs]}


async def run_command(cmd_id: int, writer) -> None:
    async with session_scope() as db:
        cmd = await db.get(DiagnosticCommand, cmd_id)
        sess = await db.get(DiagnosticSession, cmd.session_id)
        cmd.status = "running"
        argv, ns, pod, actor = list(cmd.argv), sess.pod_namespace, sess.pod_name, cmd.actor_id
        ready = sess.status == "ready" and sess.expires_at and sess.expires_at > datetime.now(timezone.utc)
        env = await db.get(Environment, sess.environment_id)
    if not ready:
        code, out, status = None, "session is not ready or has expired", "failed"
    else:
        try:
            code, out = await writer.exec(ns, pod, argv, timeout=30)
            status = "done"
        except Exception as exc:
            code, out, status = None, f"exec failed ({type(exc).__name__}: {str(exc)[:200]})", "failed"
    async with session_scope() as db:
        cmd = await db.get(DiagnosticCommand, cmd_id)
        cmd.exit_code, cmd.output, cmd.status, cmd.finished_at = code, redact(out), status, datetime.now(timezone.utc)
        await audit.record(db, action="diagnostic.command", actor_id=actor, resource_type="diagnostic_session", resource_id=str(cmd.session_id),
                           environment=env.name, namespace=ns, outcome=status, details={"tool": cmd.tool, "argv": argv, "exit_code": code})


async def tick(writer_override=None) -> None:
    """Executor housekeeping: run queued commands, delete expired or terminated session pods."""
    from ..actions.executor import writer_factory

    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        cmds = (await db.execute(select(DiagnosticCommand.id, DiagnosticSession.environment_id).join(
            DiagnosticSession, DiagnosticSession.id == DiagnosticCommand.session_id).where(DiagnosticCommand.status == "queued")
            .order_by(DiagnosticCommand.created_at).limit(5))).all()
        expired = (await db.execute(select(DiagnosticSession).where(
            DiagnosticSession.status.in_(["ready", "starting"]), DiagnosticSession.expires_at < now))).scalars().all()
        to_delete = [(s.id, s.environment_id, s.pod_namespace, s.pod_name) for s in expired]
        terminating = (await db.execute(select(DiagnosticSession).where(DiagnosticSession.status == "terminated",
                                                                        DiagnosticSession.ended_at.is_(None)))).scalars().all()
        to_delete += [(s.id, s.environment_id, s.pod_namespace, s.pod_name) for s in terminating]
    writers: dict = {}

    async def w(env_id):
        if writer_override is not None:
            return writer_override
        if env_id not in writers:
            async with session_scope() as db:
                env = await db.get(Environment, env_id)
            writers[env_id] = await writer_factory(env)
        return writers[env_id]

    for cmd_id, env_id in cmds:
        await run_command(cmd_id, await w(env_id))
    for sid, env_id, ns, pod in to_delete:
        if pod:
            await (await w(env_id)).delete_pod(ns, pod)
        async with session_scope() as db:
            s = await db.get(DiagnosticSession, sid)
            s.status = "expired" if s.status != "terminated" else "terminated"
            s.ended_at = now
            await audit.record(db, action="diagnostic.cleanup", resource_type="diagnostic_session", resource_id=str(sid), namespace=ns,
                               details={"pod": pod, "reason": s.status})
    if writer_override is None:
        for wr in writers.values():
            try:
                await wr.aclose()
            except Exception:
                pass
