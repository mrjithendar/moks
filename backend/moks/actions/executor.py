"""The executor process (`moks executor`, MOKS_PROCESS_ROLE=executor).

It is the only MOKS component with write credentials. For each queued plan it re-verifies
approval binding, expiry, permissions and hash; checks the live object still has the reviewed
resourceVersion; executes; validates the outcome; and, when validation fails and the plan allows,
restores the captured pre-state and validates that too. It also runs diagnostic sessions.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select, text

from .. import audit
from ..config import get_settings
from ..connectors import registry
from ..connectors.base import ConnectorError
from ..db import session_scope
from ..incidents import add_event
from ..models import ActionPlan, Connector, Environment
from ..observability import ACTIONS
from .catalog import CATALOG, inverse_merge_patch
from .service import PlanError, verify_for_execution

log = logging.getLogger("moks.executor")
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "CreateContainerError", "InvalidImageName"}
VALIDATION_TIMEOUT = 180
POLL = 3.0


async def _default_writer(env: Environment):
    from .writer import KubernetesWriter

    async with session_scope() as db:
        row = (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.type == "kubernetes",
                                                        Connector.enabled.is_(True)))).scalars().first()
    if row is None:
        raise ConnectorError("config", "no kubernetes connector for this environment")
    cfg = (row.config or {}).get("executor") or {"mode": "in_cluster"}  # the executor pod's own ServiceAccount
    return KubernetesWriter(cfg, (row.config or {}).get("executor_credential_ref"))


# Tests replace this factory with an in-memory writer.
writer_factory: Callable[[Environment], Any] = _default_writer


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-executor"


async def claim_plan(wid: str) -> uuid.UUID | None:
    async with session_scope() as db:
        row = (await db.execute(text("""
            UPDATE action_plans SET status = 'executing', worker_id = :w, executed_at = now()
            WHERE id = (SELECT id FROM action_plans WHERE status = 'queued' ORDER BY queued_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id"""), {"w": wid})).first()
        return row[0] if row else None


async def validate_deployment(writer, ns: str, name: str, expect_replicas: int | None, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while True:
        st = await writer.state(ns, name)
        s = st["summary"]
        desired = st["replicas"] if expect_replicas is None else expect_replicas
        pods = await writer.pods_of(ns, name)
        bad = [{"pod": p["name"], "reason": pr.get("reason")} for p in pods for pr in p.get("problems", [])
               if pr.get("state") == "waiting" and pr.get("reason") in BAD_WAITING]
        complete = (s.get("observedGeneration") or 0) >= (s.get("generation") or 0) and (s.get("updated") or 0) >= desired \
            and (s.get("available") or 0) >= desired and (s.get("ready") or 0) >= desired and \
            (desired > 0 or len([p for p in pods if p["phase"] == "Running"]) == 0)
        last = {"desired": desired, "ready": s.get("ready"), "updated": s.get("updated"), "available": s.get("available"),
                "observedGeneration": s.get("observedGeneration"), "generation": s.get("generation"), "bad_pods": bad[:10]}
        if bad:
            return {"ok": False, "reason": "pods failing: " + ", ".join(f"{b['pod']} {b['reason']}" for b in bad[:5]), **last}
        if complete:
            return {"ok": True, **last}
        if time.monotonic() > deadline:
            return {"ok": False, "reason": f"rollout not complete within {timeout}s", **last}
        await asyncio.sleep(POLL)


async def execute_plan(plan_id: uuid.UUID) -> None:
    async with session_scope() as db:
        plan = await db.get(ActionPlan, plan_id)
        env = await db.get(Environment, plan.environment_id)
        try:
            await verify_for_execution(db, plan, env)
        except PlanError as exc:
            plan.status, plan.finished_at = "failed", datetime.now(timezone.utc)
            plan.result = {"error": exc.message, "stage": "verification"}
            await audit.record(db, action="action.execute", actor_id=plan.executed_by, resource_type="action_plan", resource_id=str(plan.id),
                               environment=env.name, namespace=plan.namespace, decision="deny", outcome="denied", details={"reason": exc.message})
            ACTIONS.labels(plan.action, "denied").inc()
            return
        snapshot = {c: getattr(plan, c) for c in ("action", "namespace", "target_kind", "target_name", "params", "pre_state", "auto_recover", "incident_id", "executed_by")}
        env_snapshot = env
    result: dict[str, Any] = {"steps": []}
    status = "failed"
    ns, name, params = snapshot["namespace"], snapshot["target_name"], snapshot["params"]
    timeout = int(params.get("validation_timeout_seconds", VALIDATION_TIMEOUT)) if isinstance(params, dict) else VALIDATION_TIMEOUT
    writer = None
    try:
        if snapshot["action"] == "diagnostic_session":
            from types import SimpleNamespace

            from ..diagnostics import service as diag

            writer = await writer_factory(env_snapshot)
            out = await diag.start(SimpleNamespace(params=params), writer)
            result["steps"].append({"step": "start_session", "output": out})
            result["validation"] = {"ok": out["ok"], "reason": None if out["ok"] else f"session failed ({out['phase']})"}
            status = "succeeded" if out["ok"] else "failed"
        elif snapshot["action"] == "open_pull_request":
            async with session_scope() as db:
                conns, _ = await registry.for_environment(db, env_snapshot.id)
            git = conns.get("git")
            if git is None:
                raise ConnectorError("config", "no git connector")
            git[1].check_repo(params["repository"])
            branch = f"moks/{str(plan_id)[:8]}"
            pr = await git[1].provider.open_pull_request(params["repository"], params.get("base", "main"), branch, params["path"],
                                                        params["content"], f"MOKS: {params['title']}", params["title"],
                                                        params.get("body", "") + f"\n\nProposed through MOKS action plan {plan_id}.")
            result["steps"].append({"step": "execute", "pull_request": pr})
            result["validation"] = {"ok": bool(pr.get("number")), "pull_request": pr.get("url")}
            status = "succeeded" if pr.get("number") else "failed"
        else:
            writer = await writer_factory(env_snapshot)
            pre_rv = (snapshot["pre_state"] or {}).get("resourceVersion")
            if snapshot["target_kind"] == "Deployment":
                live = await writer.state(ns, name)
                if pre_rv and live["resourceVersion"] != pre_rv:
                    raise ConnectorError("conflict", f"{ns}/{name} changed after review (resourceVersion {pre_rv} -> {live['resourceVersion']})")
                restore_state = live  # full template + replicas, captured immediately before acting
                action = snapshot["action"]
                if action == "restart_deployment":
                    out = await writer.restart(ns, name, live["resourceVersion"])
                    expect = None
                elif action == "scale_deployment":
                    out = await writer.scale(ns, name, live["resourceVersion"], params["replicas"])
                    expect = params["replicas"]
                elif action == "rollback_deployment":
                    out = await writer.rollback(ns, name, live["resourceVersion"], params["to_revision"])
                    expect = None
                elif action == "patch_resources":
                    res = {"requests": params.get("requests") or {}, "limits": params.get("limits") or {}}
                    out = await writer.patch_resources(ns, name, live["resourceVersion"], params["container"], res)
                    expect = None
                elif action == "apply_config_patch":
                    out = await writer.merge_patch(ns, "Deployment", name, live["resourceVersion"], params["patch"])
                    expect = None
                else:
                    raise ConnectorError("bad_request", f"{action} is not executable")
                result["steps"].append({"step": "execute", "output": out})
                v = await validate_deployment(writer, ns, name, expect, timeout)
                result["validation"] = v
                if v["ok"]:
                    status = "succeeded"
                elif snapshot["auto_recover"]:
                    rec = await writer.restore(ns, name, restore_state)
                    rv = await validate_deployment(writer, ns, name, restore_state["replicas"], timeout)
                    result["recovery"] = {"output": rec, "validation": rv}
                    status = "recovered" if rv["ok"] else "failed"
            else:  # Service / HPA merge patch
                obj = await writer.get_object(ns, snapshot["target_kind"], name)
                if pre_rv and obj["metadata"]["resourceVersion"] != pre_rv:
                    raise ConnectorError("conflict", f"{snapshot['target_kind']} {ns}/{name} changed after review")
                inverse = inverse_merge_patch(obj, params["patch"])
                out = await writer.merge_patch(ns, snapshot["target_kind"], name, pre_rv, params["patch"])
                result["steps"].append({"step": "execute", "output": out})
                ok = True
                detail: dict[str, Any] = {}
                if snapshot["target_kind"] == "Service":
                    await asyncio.sleep(min(10, timeout))
                    svc = next((s for s in await writer.read.services(ns) if s["name"] == name), None)
                    ok = bool(svc and (not svc["selector"] or svc["endpoints"]["ready"] > 0))
                    detail = {"endpoints": svc["endpoints"] if svc else None}
                result["validation"] = {"ok": ok, **detail}
                if ok:
                    status = "succeeded"
                elif snapshot["auto_recover"]:
                    fresh = await writer.get_object(ns, snapshot["target_kind"], name)
                    await writer.merge_patch(ns, snapshot["target_kind"], name, fresh["metadata"]["resourceVersion"], inverse)
                    result["recovery"] = {"inverse_patch": inverse}
                    status = "recovered"
    except ConnectorError as exc:
        result["error"] = f"{exc.kind}: {exc.message}"
        status = "failed"
    except Exception as exc:
        log.exception("execution error", extra={"event": "action_error"})
        result["error"] = f"internal error ({type(exc).__name__})"
        status = "failed"
    finally:
        if writer is not None:
            try:
                await writer.aclose()
            except Exception:
                pass
    ACTIONS.labels(snapshot["action"], status).inc()
    async with session_scope() as db:
        plan = await db.get(ActionPlan, plan_id)
        plan.status, plan.result, plan.finished_at = status, result, datetime.now(timezone.utc)
        await audit.record(db, action="action.execute", actor_id=plan.executed_by, resource_type="action_plan", resource_id=str(plan.id),
                           environment=env_snapshot.name, namespace=ns, decision="allow", outcome=status,
                           details={"action": snapshot["action"], "target": f"{snapshot['target_kind']}/{name}", "result": result})
        if plan.incident_id:
            await add_event(db, plan.incident_id, "validation", {
                "text": f"{snapshot['action']} on {snapshot['target_kind']}/{name}: {status}"
                        + (f" ({result.get('validation', {}).get('reason')})" if result.get("validation", {}).get("reason") else ""),
                "plan_id": str(plan_id), "status": status}, plan.executed_by)


async def run_executor(stop: asyncio.Event | None = None) -> None:
    from ..diagnostics import service as diag

    s = get_settings()
    if s.process_role != "executor":
        raise RuntimeError("run the executor with MOKS_PROCESS_ROLE=executor")
    stop = stop or asyncio.Event()
    wid = worker_id()
    log.info("executor started", extra={"event": "executor_started"})
    while not stop.is_set():
        try:
            async with session_scope() as db:
                await db.execute(text("UPDATE action_plans SET status = 'expired' WHERE status IN ('pending','approved') AND expires_at < now()"))
            pid = await claim_plan(wid)
            if pid:
                await execute_plan(pid)
                continue
            await diag.tick()
        except Exception:
            log.exception("executor loop error", extra={"event": "executor_loop_error"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
