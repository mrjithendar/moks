"""The executor process (`moks executor`, MOKS_PROCESS_ROLE=executor).

It is the only MOKS component with write credentials. For each queued plan it re-verifies
everything against current state, proves it is talking to the approved cluster and object, records
its intent durably, mutates, records what it applied, and validates. A mutation is attempted once:
when the outcome cannot be established (crash, lost connection) the plan goes to
reconciliation_required for a person, and is never retried.

Recovery is conflict-aware. It touches only the fields MOKS changed, and only while they still hold
what MOKS applied; if anyone changed them meanwhile, it stops and asks for reconciliation.

Ownership. Every claim gets a unique token. A heartbeat, checkpoint or final result is written only
while the row still holds that token in status executing, and the lease is judged by database time
only. When a renewal is refused, or the database stays unreachable until the lease could have
lapsed, the running work is cancelled and writes nothing more. Ownership is checked again right
before each mutation and before recovery.

The limit of this fence: a database lease cannot atomically fence a request to the Kubernetes API.
A request sent just before the lease lapses can land after it. UID and resourceVersion preconditions
limit that request to the exact object version that was reviewed, the durable intent checkpoint
sends the plan to reconciliation_required, and nothing is ever replayed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple

from sqlalchemy import func, select, text

from .. import audit, velero, versions
from ..config import get_settings
from ..connectors import registry
from ..connectors.base import ConnectorError
from ..db import session_scope
from ..incidents import add_event
from ..models import ActionPlan, Connector, Environment
from ..observability import ACTIONS, tracer
from .catalog import CATALOG, check_patch, get_path, patch_leaves, paths_patch
from .service import PlanError, verify_for_execution

log = logging.getLogger("moks.executor")
BAD_WAITING = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "CreateContainerError", "InvalidImageName"}
VALIDATION_TIMEOUT = 180
POLL = 3.0
LEASE_SECONDS = 90
OBJECT_SETTLE_SECONDS = 30
PRE_RESTORE_TIMEOUT = 900  # plans run one at a time, so a long wait would hold back urgent rollbacks
UNCERTAIN = {"timeout", "unavailable"}  # the request may have reached the API server


class LeaseLost(Exception):
    """This process may no longer act on the plan. It must stop and write nothing."""


class Claim(NamedTuple):
    plan_id: uuid.UUID
    token: uuid.UUID


async def _default_writer(env: Environment, connector_id: str | None = None):
    from .writer import KubernetesWriter

    async with session_scope() as db:
        q = select(Connector).where(Connector.environment_id == env.id, Connector.type == "kubernetes", Connector.enabled.is_(True))
        if connector_id:
            q = q.where(Connector.id == uuid.UUID(connector_id))
        row = (await db.execute(q)).scalars().first()
    if row is None:
        raise ConnectorError("config", "no kubernetes connector for this environment")
    target = (row.config or {}).get("executor")
    if not target:
        raise ConnectorError("config", f"connector {row.name} has no write target (config.executor)")
    return KubernetesWriter(target, (row.config or {}).get("executor_credential_ref"))


# Tests replace this factory with an in-memory writer.
writer_factory: Callable[..., Any] = _default_writer


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-executor"


async def claim_plan(wid: str) -> Claim | None:
    async with session_scope() as db:
        row = (await db.execute(text("""
            UPDATE action_plans SET status = 'executing', worker_id = :w, executed_at = now(), heartbeat_at = now(),
                execution_token = gen_random_uuid(), lease_expires_at = now() + make_interval(secs => :lease)
            WHERE id = (SELECT id FROM action_plans WHERE status = 'queued' ORDER BY queued_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING id, execution_token"""), {"w": wid, "lease": LEASE_SECONDS})).first()
        return Claim(row[0], row[1]) if row else None


class Fence:
    """Proof of ownership for one execution. Local time only ever shortens how long it is trusted:
    the clock starts before the renewal is sent, so a stalled process never overestimates its lease."""

    OWNED = "id = :id AND status = 'executing' AND execution_token = :token AND lease_expires_at > now()"

    def __init__(self, claim: Claim):
        self.plan_id, self.token = claim
        self.renewed_at = time.monotonic()
        self.lost = False

    @property
    def lapsed(self) -> bool:
        return time.monotonic() - self.renewed_at >= LEASE_SECONDS

    async def renew(self) -> bool:
        started = time.monotonic()
        async with session_scope() as db:
            row = (await db.execute(text(f"""UPDATE action_plans SET heartbeat_at = now(),
                        lease_expires_at = GREATEST(lease_expires_at, now() + make_interval(secs => :lease))
                     WHERE {self.OWNED} RETURNING id"""), {"lease": LEASE_SECONDS, "id": self.plan_id, "token": self.token})).first()
        if row:
            self.renewed_at = started
        return row is not None

    async def verify(self) -> None:
        """Fail closed: not being able to prove ownership counts as not having it."""
        if self.lost or self.lapsed:
            self.lost = True
            raise LeaseLost("the execution lease lapsed")
        try:
            async with session_scope() as db:
                row = (await db.execute(text(f"SELECT 1 FROM action_plans WHERE {self.OWNED}"), {"id": self.plan_id, "token": self.token})).first()
        except Exception as exc:
            self.lost = True
            raise LeaseLost(f"ownership could not be verified ({type(exc).__name__})") from None
        if row is None:
            self.lost = True
            raise LeaseLost("this execution no longer owns the plan")

    async def checkpoint(self, state: str, **values: Any) -> None:
        """Committed before moving on: this row is what survives a crash. Refused without ownership."""
        try:
            async with session_scope() as db:
                plan = (await db.execute(select(ActionPlan).where(ActionPlan.id == self.plan_id, ActionPlan.status == "executing", ActionPlan.execution_token == self.token,
                                                                  ActionPlan.lease_expires_at > func.now()).with_for_update())).scalar_one_or_none()
                if plan is not None:
                    plan.mutation_state = state
                    plan.checkpoint = {**(plan.checkpoint or {}), **values, "updated_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:
            self.lost = True
            raise LeaseLost(f"the checkpoint could not be recorded ({type(exc).__name__})") from None
        if plan is None:
            self.lost = True
            raise LeaseLost("this execution no longer owns the plan")


async def _heartbeat(fence: Fence, work: asyncio.Task) -> None:
    """Renews the lease. A refusal stops the work at once. Database errors are retried only until
    the lease could have lapsed, because after that another decision may already have been made."""
    while True:
        await asyncio.sleep(LEASE_SECONDS / 3)
        try:
            refused = not await asyncio.wait_for(fence.renew(), LEASE_SECONDS / 3)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("lease renewal failed", extra={"event": "action_lease_renewal_failed"})
            refused = fence.lapsed
        if refused:
            fence.lost = True
            log.error("execution lease lost, stopping", extra={"event": "action_lease_lost"})
            work.cancel()
            return


async def validate_deployment(writer, ns: str, name: str, expect_replicas: int | None, timeout: int, baseline: dict[str, int] | None = None) -> dict:
    """baseline: pod -> restarts before the action, so old restarts are not blamed on this change."""
    deadline = time.monotonic() + timeout
    baseline = baseline or {}
    while True:
        st = await writer.state(ns, name)
        s = st["summary"]
        desired = st["replicas"] if expect_replicas is None else expect_replicas
        observed = (s.get("observedGeneration") or 0) >= (s.get("generation") or 0)
        last = {"desired": desired, "ready": s.get("ready"), "updated": s.get("updated"), "available": s.get("available"),
                "observedGeneration": s.get("observedGeneration"), "generation": s.get("generation")}
        if observed:  # until then the current ReplicaSet may still be the old one
            pods = await writer.pods_of(ns, name)
            bad = [{"pod": p["name"], "reason": pr.get("reason")} for p in pods for pr in p.get("problems", [])
                   if pr.get("state") == "waiting" and pr.get("reason") in BAD_WAITING]
            bad += [{"pod": p["name"], "reason": "restarted"} for p in pods if (p.get("restarts") or 0) > baseline.get(p["name"], 0)]
            last["bad_pods"] = bad[:10]
            if bad:
                return {"ok": False, "reason": "pods failing: " + ", ".join(f"{b['pod']} {b['reason']}" for b in bad[:5]), **last}
            complete = (s.get("updated") or 0) >= desired and (s.get("available") or 0) >= desired and (s.get("ready") or 0) >= desired and \
                (desired > 0 or not [p for p in pods if p["phase"] == "Running"])
            if complete:
                if desired > 0:
                    last["services"] = await _selecting_services(writer, ns, st)
                    empty = [x["name"] for x in last["services"] if not x["ready"]]
                    if empty:
                        return {"ok": False, "reason": f"no ready endpoints for Service {', '.join(empty)}", **last}
                return {"ok": True, **last}
        if time.monotonic() > deadline:
            return {"ok": False, "reason": f"rollout not complete within {timeout}s", **last}
        await asyncio.sleep(POLL)


async def _selecting_services(writer, ns: str, state: dict) -> list[dict]:
    labels = ((state.get("template") or {}).get("metadata") or {}).get("labels") or {}
    return [{"name": s["name"], "ready": s["endpoints"]["ready"]} for s in await writer.services(ns)
            if s["selector"] and all(labels.get(k) == v for k, v in s["selector"].items())]


async def validate_object(writer, ns: str, kind: str, name: str, expected: dict[tuple[str, ...], Any], timeout: int, before: dict | None = None) -> dict:
    """Services and HPAs: the fields read back as expected, and the object still does its job."""
    deadline = time.monotonic() + min(timeout, OBJECT_SETTLE_SECONDS)
    while True:
        obj = await writer.get_object(ns, kind, name)
        wrong = [".".join(p) for p, v in expected.items() if not _matches(get_path(obj, p), v)]
        last: dict[str, Any] = {"read_back": not wrong}
        if wrong:
            last["reason"] = f"read back different values for {', '.join(wrong)}"
        elif kind == "Service":
            svc = next((s for s in await writer.services(ns) if s["name"] == name), None)
            last["endpoints"] = svc["endpoints"] if svc else None
            if svc is not None and (not svc["selector"] or svc["endpoints"]["ready"] > 0):
                return {"ok": True, **last}
            last["reason"] = "the Service has no ready endpoints"
        else:
            status = obj.get("status") or {}
            settled = (status.get("observedGeneration") or 0) >= (obj["metadata"].get("generation") or 0)
            lost = [c for c, state in _conditions(obj).items() if state == "False" and _conditions(before or {}).get(c) != "False"]
            last["conditions"] = _conditions(obj)
            if settled and not lost:
                return {"ok": True, **last}
            last["reason"] = f"autoscaler lost {', '.join(lost)}" if lost else "the autoscaler has not observed the change"
        if time.monotonic() > deadline:
            return {"ok": False, **last}
        await asyncio.sleep(POLL)


def _matches(live: Any, wanted: Any) -> bool:
    """A merge patch merges maps, so the live map may hold keys the patch did not name."""
    return all(live.get(k) == v for k, v in wanted.items()) if isinstance(wanted, dict) and isinstance(live, dict) else live == wanted


def _conditions(obj: dict) -> dict[str, str]:
    return {c.get("type"): c.get("status") for c in (obj.get("status") or {}).get("conditions") or [] if c.get("type") in ("AbleToScale", "ScalingActive")}


def _touched(action: str, kind: str, params: dict) -> list[tuple[str, ...]]:
    if action == "scale_deployment":
        return [("spec", "replicas")]
    if action == "apply_config_patch":
        return list(patch_leaves(kind, params["patch"]))
    return [("spec", "template")]


async def _mutate(writer, action: str, ns: str, name: str, kind: str, rv: str, params: dict) -> dict:
    if action == "restart_deployment":
        return await writer.restart(ns, name, rv)
    if action == "scale_deployment":
        return await writer.scale(ns, name, rv, params["replicas"])
    if action == "rollback_deployment":
        return await writer.rollback(ns, name, rv, params["to_revision"])
    if action == "patch_resources":
        return await writer.patch_resources(ns, name, rv, params["container"], {"requests": params.get("requests") or {}, "limits": params.get("limits") or {}})
    if action == "apply_config_patch":
        return await writer.merge_patch(ns, kind, name, rv, params["patch"])
    raise ConnectorError("bad_request", f"{action} is not executable")


async def _validate(writer, kind: str, ns: str, name: str, expected: dict, timeout: int, expect_replicas: int | None, baseline: dict, before_obj: dict) -> dict:
    if kind == "Deployment":
        v = await validate_deployment(writer, ns, name, expect_replicas, timeout, baseline)
        live = await writer.get_object(ns, kind, name)
        wrong = [".".join(p) for p, x in expected.items() if not _matches(get_path(live, p), x)]
        return {**v, "ok": False, "reason": f"read back different values for {', '.join(wrong)}"} if v["ok"] and wrong else v
    return await validate_object(writer, ns, kind, name, expected, timeout, before_obj)


async def _recover(fence: Fence, writer, kind: str, ns: str, name: str, uid: str, before: dict, applied: dict, timeout: int, baseline: dict, before_obj: dict) -> tuple[str, dict]:
    """Restore only what MOKS changed, and only if it still holds what MOKS applied."""
    live = await writer.get_object(ns, kind, name)
    if live["metadata"].get("uid") != uid:
        return "reconciliation_required", {"status": "conflict", "reason": "the object was deleted or replaced"}
    paths = [tuple(k.split(".")) for k in applied]
    changed = [".".join(p) for p in paths if get_path(live, p) != applied[".".join(p)]]
    if changed:
        return "reconciliation_required", {"status": "conflict", "reason": f"changed by someone else after MOKS applied it: {', '.join(changed)}", "changed": changed}
    wanted: dict[str, Any] = {}
    for p in paths:
        node = wanted
        for part in p[:-1]:
            node = node.setdefault(part, {})
        node[p[-1]] = before[".".join(p)]
    patch = paths_patch([list(p) for p in paths], live, wanted)
    await fence.checkpoint("applied", recovery_intent=patch)  # proves ownership and survives a crash
    out = await writer.merge_patch(ns, kind, name, live["metadata"]["resourceVersion"], patch)
    expected = {p: before[".".join(p)] for p in paths if before[".".join(p)] is not None}
    replicas = before.get("spec.replicas") if kind == "Deployment" else None
    v = await _validate(writer, kind, ns, name, expected, timeout, replicas, baseline, before_obj)
    return ("recovered" if v["ok"] else "failed"), {"status": "applied", "output": out, "validation": v}


async def _delete_pod(fence: Fence, writer, ns: str, name: str, pre: dict, timeout: int, result: dict) -> str:
    obj = await writer.get_object(ns, "Pod", name)
    if obj["metadata"].get("uid") != pre.get("uid"):
        raise ConnectorError("conflict", f"pod {ns}/{name} is a different pod than the one reviewed")
    if not any(o.get("controller") for o in obj["metadata"].get("ownerReferences") or []):
        raise ConnectorError("forbidden", "this pod no longer has a controller")
    await fence.checkpoint("intent", uid=pre["uid"], kind="Pod")
    try:
        out = await writer.delete_managed_pod(ns, name, pre["uid"])
    except ConnectorError as exc:
        if exc.kind not in UNCERTAIN:
            await fence.checkpoint("not_applied", error=exc.message)
        raise
    await fence.checkpoint("applied")
    result["steps"].append({"step": "execute", "output": out})
    deadline = time.monotonic() + timeout
    while True:
        try:
            live = await writer.get_object(ns, "Pod", name)
        except ConnectorError as exc:
            if exc.kind != "not_found":
                raise
            live = None
        if live is None or live["metadata"].get("uid") != pre["uid"]:
            result["validation"] = {"ok": True, "gone": True}
            return "succeeded"
        if time.monotonic() > deadline:
            result["validation"] = {"ok": False, "reason": f"the pod was still terminating after {timeout}s"}
            return "failed"
        await asyncio.sleep(POLL)


async def _execute_object(fence: Fence, writer, snap: dict, identity: dict | None, timeout: int, result: dict) -> str:
    action, kind, ns, name, params = snap["action"], snap["target_kind"], snap["namespace"], snap["target_name"], snap["params"]
    pre = snap["pre_state"] or {}
    if identity and await writer.cluster_uid() != identity.get("cluster_uid"):
        raise ConnectorError("conflict", "the write target is not the cluster this plan was approved for")
    if action == "delete_pod":
        return await _delete_pod(fence, writer, ns, name, pre, timeout, result)
    obj = await writer.get_object(ns, kind, name)
    uid = obj["metadata"].get("uid")
    if pre.get("uid") and uid != pre["uid"]:
        raise ConnectorError("conflict", f"{kind} {ns}/{name} is a different object than the one reviewed (deleted and recreated)")
    rv = obj["metadata"]["resourceVersion"]
    if pre.get("resourceVersion") and rv != pre["resourceVersion"]:
        raise ConnectorError("conflict", f"{kind} {ns}/{name} changed after review (resourceVersion {pre['resourceVersion']} -> {rv})")
    if action == "apply_config_patch":
        check_patch(kind, params["patch"], snap["env"], obj)  # against the live object, not the reviewed one
    paths = _touched(action, kind, params)
    before = {".".join(p): get_path(obj, p) for p in paths}
    baseline = {p["name"]: p.get("restarts") or 0 for p in await writer.pods_of(ns, name)} if kind == "Deployment" else {}
    await fence.checkpoint("intent", uid=uid, kind=kind, before=before)
    try:
        out = await _mutate(writer, action, ns, name, kind, rv, params)
    except ConnectorError as exc:
        if exc.kind not in UNCERTAIN:
            await fence.checkpoint("not_applied", error=exc.message)  # refused by the API server
        raise
    result["steps"].append({"step": "execute", "output": out})
    after = await writer.get_object(ns, kind, name)
    applied = {".".join(p): get_path(after, p) for p in paths}
    await fence.checkpoint("applied", applied=applied, restart_baseline=baseline)
    expected = patch_leaves(kind, params["patch"]) if action == "apply_config_patch" else {("spec", "replicas"): params["replicas"]} if action == "scale_deployment" else {}
    expect_replicas = params["replicas"] if action == "scale_deployment" else None
    v = await _validate(writer, kind, ns, name, expected, timeout, expect_replicas, baseline, obj)
    result["validation"] = v
    if v["ok"]:
        return "succeeded"
    if not snap["auto_recover"]:
        return "failed"
    status, rec = await _recover(fence, writer, kind, ns, name, uid, before, applied, timeout, baseline, obj)
    result["recovery"] = rec
    return status


async def _velero_phase(writer, plural: str, name: str, timeout: int, wanted: set[str]) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        obj = await writer.velero_get(plural, name)
        phase = (obj.get("status") or {}).get("phase")
        if phase in wanted or time.monotonic() > deadline:
            return obj
        await asyncio.sleep(POLL)


async def _velero_started(writer, plural: str, name: str, timeout: int) -> dict:
    """Velero runs for hours. The plan only proves it was accepted; the Backups page follows the rest."""
    obj = await _velero_phase(writer, plural, name, timeout, {"InProgress", "WaitingForPluginOperations", *velero.DONE})
    phase = (obj.get("status") or {}).get("phase")
    ok = phase not in (None, "New", "FailedValidation", "Failed")
    return {"ok": ok, "phase": phase, "reason": None if ok else "; ".join((obj.get("status") or {}).get("validationErrors") or []) or f"Velero reports {phase or 'no phase'}"}


async def _execute_velero(fence: Fence, writer, snap: dict, identity: dict | None, timeout: int, result: dict) -> str:
    action, name, params, pre = snap["action"], snap["target_name"], snap["params"], snap["pre_state"] or {}
    if identity and await writer.cluster_uid() != identity.get("cluster_uid"):
        raise ConnectorError("conflict", "the write target is not the cluster this plan was approved for")
    suffix = str(fence.plan_id)[:8]
    if action == "velero_configure":
        async with session_scope() as db:
            store = await velero.get_store(db)
        if store is None or velero.fingerprint(store) != params["store_fingerprint"]:
            raise ConnectorError("conflict", "the R2 settings changed after this plan was made")
        payloads = velero.secret_payloads(store)  # fails before anything is written
        await fence.checkpoint("intent")
        for secret, data, overwrite in payloads:
            await fence.verify()
            result["steps"].append({"step": "secret", "name": secret, "outcome": await writer.write_secret(secret, data, overwrite)})
        await fence.verify()
        await writer.velero_apply("backupstoragelocations", velero.LOCATION, velero.location_body(store, snap["env"].name))
        await fence.checkpoint("applied")
        obj = await _velero_phase(writer, "backupstoragelocations", velero.LOCATION, timeout, {"Available"})
        phase = (obj.get("status") or {}).get("phase")
        result["validation"] = {"ok": phase == "Available", "reason": None if phase == "Available" else f"location is {phase or 'not validated yet'}: {(obj.get('status') or {}).get('message') or 'no message'}"}
        return "succeeded" if phase == "Available" else "failed"
    if action == "velero_backup":
        backup = f"moks-{suffix}"
        await fence.checkpoint("intent")
        await writer.velero_create("backups", velero.backup_body(backup, params))
        await fence.checkpoint("applied", object=backup)
        result["steps"].append({"step": "backup", "name": backup})
        result["validation"] = await _velero_started(writer, "backups", backup, timeout)
    elif action == "velero_schedule":
        await fence.checkpoint("intent")
        await writer.velero_apply("schedules", name, velero.schedule_body(name, params))
        await fence.checkpoint("applied", object=name)
        result["steps"].append({"step": "schedule", "name": name})
        result["validation"] = {"ok": True, "reason": None}
    else:
        live = await writer.velero_get("backups", name)
        if (live.get("metadata") or {}).get("uid") != pre.get("uid"):
            raise ConnectorError("conflict", f"backup {name} is not the one reviewed")
        if params.get("pre_restore_backup", True):
            safety = f"pre-restore-{suffix}"
            await fence.checkpoint("intent")
            await writer.velero_create("backups", velero.backup_body(safety, {**params, "include_volumes": params.get("restore_volumes", True), "ttl_days": velero.KEEP_DAYS}))
            await fence.checkpoint("applied", pre_restore_backup=safety)
            obj = await _velero_phase(writer, "backups", safety, PRE_RESTORE_TIMEOUT, velero.DONE)
            phase = (obj.get("status") or {}).get("phase")
            result["steps"].append({"step": "pre_restore_backup", "name": safety, "phase": phase})
            if phase != "Completed":
                result["validation"] = {"ok": False, "reason": f"the pre-restore backup is {phase or 'not finished'}, so the restore did not start"}
                return "failed"
        restore = f"moks-{suffix}"
        await fence.checkpoint("intent")
        await writer.velero_create("restores", velero.restore_body(restore, name, params))
        await fence.checkpoint("applied", object=restore)
        result["steps"].append({"step": "restore", "name": restore})
        result["validation"] = await _velero_started(writer, "restores", restore, timeout)
    return "succeeded" if result["validation"]["ok"] else "failed"


async def _perform(fence: Fence, snapshot: dict, identity: dict | None, env, timeout: int, result: dict) -> str:
    plan_id = fence.plan_id
    params, writer = snapshot["params"], None
    try:
        if snapshot["action"] == "diagnostic_session":
            from types import SimpleNamespace

            from ..diagnostics import service as diag

            writer = await writer_factory(env)
            await fence.checkpoint("intent")
            out = await diag.start(SimpleNamespace(params=params), writer)
            await fence.checkpoint("applied")
            result["steps"].append({"step": "start_session", "output": out})
            result["validation"] = {"ok": out["ok"], "reason": None if out["ok"] else f"session failed ({out['phase']})"}
            return "succeeded" if out["ok"] else "failed"
        if snapshot["action"] == "open_pull_request":
            async with session_scope() as db:
                conns, _ = await registry.for_environment(db, env.id)
            git = conns.get("git")
            if git is None:
                raise ConnectorError("config", "no git connector")
            git[1].check_repo(params["repository"])
            branch = f"moks/{str(plan_id)[:8]}"
            await fence.checkpoint("intent")
            pr = await git[1].provider.open_pull_request(params["repository"], params.get("base", "main"), branch, params["path"],
                                                        params["content"], f"MOKS: {params['title']}", params["title"],
                                                        params.get("body", "") + f"\n\nProposed through MOKS action plan {plan_id}.")
            await fence.checkpoint("applied")
            result["steps"].append({"step": "execute", "pull_request": pr})
            result["validation"] = {"ok": bool(pr.get("number")), "pull_request": pr.get("url")}
            return "succeeded" if pr.get("number") else "failed"
        writer = await writer_factory(env, (identity or {}).get("connector_id"))
        run = _execute_velero if snapshot["action"] in velero.ACTIONS else _execute_object
        return await run(fence, writer, snapshot, identity, timeout, result)
    finally:
        if writer is not None:
            try:
                await writer.aclose()
            except Exception:
                pass


async def execute_plan(claim: Claim) -> None:
    start = time.monotonic()
    with tracer().start_as_current_span("action.execute") as span:
        span.set_attribute("moks.plan.id", str(claim[0]))
        await _execute_plan(claim)
    async with session_scope() as db:
        plan = await db.get(ActionPlan, claim[0])
    log.info("action finished", extra={"event": "action_finished", "operation": plan.action, "action": plan.action, "outcome": "success" if plan.status == "succeeded" else "failure", "durationMs": int((time.monotonic() - start) * 1000)})


async def _execute_plan(claim: Claim) -> None:
    plan_id, token = claim
    fence = Fence(claim)
    async with session_scope() as db:
        plan = await db.get(ActionPlan, plan_id)
        env = await db.get(Environment, plan.environment_id)
        if plan.status != "executing" or plan.execution_token != token:
            log.error("claim no longer valid", extra={"event": "action_claim_stale"})
            return
        try:
            await verify_for_execution(db, plan, env)
        except PlanError as exc:
            plan.status, plan.finished_at, plan.execution_token = "failed", datetime.now(timezone.utc), None
            plan.lease_expires_at = None
            plan.result = {"error": exc.message, "stage": "verification"}
            await audit.record(db, action="action.execute", actor_id=plan.executed_by, resource_type="action_plan", resource_id=str(plan.id),
                               environment=env.name, namespace=plan.namespace, decision="deny", outcome="denied", details={"reason": exc.message})
            ACTIONS.labels(plan.action, "denied").inc()
            return
        snapshot = {c: getattr(plan, c) for c in ("action", "namespace", "target_kind", "target_name", "params", "pre_state", "auto_recover", "incident_id", "executed_by", "worker_id")}
        identity = plan.target_identity
        snapshot["env"] = env
        env_snapshot = env
    result: dict[str, Any] = {"steps": []}
    status = "failed"
    ns, name = snapshot["namespace"], snapshot["target_name"]
    work = asyncio.create_task(_perform(fence, snapshot, identity, env_snapshot, VALIDATION_TIMEOUT, result))
    heartbeat = asyncio.create_task(_heartbeat(fence, work))
    try:
        status = await work
    except asyncio.CancelledError:
        if not fence.lost:
            work.cancel()
            raise
    except LeaseLost:
        pass
    except ConnectorError as exc:
        result["error"] = f"{exc.kind}: {exc.message}"
    except Exception as exc:
        log.exception("execution error", extra={"event": "action_error"})
        result["error"] = f"internal error ({type(exc).__name__})"
    finally:
        heartbeat.cancel()
    if fence.lost:  # reconcile_stale() decides the outcome from the durable checkpoint
        ACTIONS.labels(snapshot["action"], "lease_lost").inc()
        log.error("execution stopped without a result", extra={"event": "action_lease_lost"})
        return
    async with session_scope() as db:
        plan = (await db.execute(select(ActionPlan).where(ActionPlan.id == plan_id).with_for_update())).scalar_one()
        if plan.status != "executing" or plan.execution_token != token:  # the lease was lost and this plan was already reconciled
            log.error("late execution result ignored", extra={"event": "action_late_result"})
            plan.result = {**(plan.result or {}), "late_result": {"status": status, **result}}
            return
        if status == "failed" and "error" in result and plan.mutation_state in ("intent", "applied"):
            status = "reconciliation_required"  # the mutation may have landed, or validation never finished
        ACTIONS.labels(snapshot["action"], status).inc()
        plan.status, plan.result, plan.finished_at = status, result, datetime.now(timezone.utc)
        plan.lease_expires_at, plan.execution_token = None, None
        await audit.record(db, action="action.execute", actor_id=plan.executed_by, resource_type="action_plan", resource_id=str(plan.id),
                           environment=env_snapshot.name, namespace=ns, decision="allow", outcome=status,
                           details={"action": snapshot["action"], "target": f"{snapshot['target_kind']}/{name}", "result": result})
        if plan.incident_id:
            await add_event(db, plan.incident_id, "validation", {
                "text": f"{snapshot['action']} on {snapshot['target_kind']}/{name}: {status}"
                        + (f" ({result.get('validation', {}).get('reason')})" if result.get("validation", {}).get("reason") else ""),
                "plan_id": str(plan_id), "status": status}, plan.executed_by)


async def reconcile_stale() -> list[uuid.UUID]:
    """Plans whose executor stopped renewing its lease. Nothing is retried: a plan that never reached
    its mutation fails, and any other waits for a person, with the live values to compare against."""
    async with session_scope() as db:
        now = (await db.execute(select(func.now()))).scalar_one()
        stale = (await db.execute(select(ActionPlan).where(ActionPlan.status == "executing").where(
            (ActionPlan.lease_expires_at.is_(None)) | (ActionPlan.lease_expires_at <= now)).with_for_update(skip_locked=True))).scalars().all()
        for plan in stale:
            env = await db.get(Environment, plan.environment_id)
            attempted = plan.mutation_state != "none"
            plan.status = "reconciliation_required" if attempted else "failed"
            plan.finished_at, plan.lease_expires_at, plan.execution_token = now, None, None
            plan.result = {**(plan.result or {}), "error": "the executor stopped while this plan was running; " + (
                "the change may or may not have been applied" if attempted else "no change was attempted"), "stage": "reconciliation"}
            ACTIONS.labels(plan.action, plan.status).inc()
            await audit.record(db, action="action.stale", actor_id=plan.executed_by, resource_type="action_plan", resource_id=str(plan.id),
                               environment=env.name, namespace=plan.namespace, outcome=plan.status, details={"mutation_state": plan.mutation_state})
            if plan.incident_id:
                await add_event(db, plan.incident_id, "validation", {"text": f"{plan.action}: {plan.result['error']}", "plan_id": str(plan.id), "status": plan.status}, plan.executed_by)
        ids = [(p.id, p.environment_id, p.target_kind, p.namespace, p.target_name, p.checkpoint, p.target_identity) for p in stale if p.status == "reconciliation_required"]
    for row in ids:
        await _observe(*row)
    return [r[0] for r in ids]


async def _observe(plan_id, env_id, kind, ns, name, checkpoint, identity) -> None:
    """Best effort: what the object looks like now, so the person has facts rather than a guess."""
    if kind not in ("Deployment", "Service", "HorizontalPodAutoscaler") or not checkpoint:
        return
    writer = None
    try:
        async with session_scope() as db:
            env = await db.get(Environment, env_id)
        writer = await writer_factory(env, (identity or {}).get("connector_id"))
        obj = await writer.get_object(ns, kind, name)
        before = checkpoint.get("before") or {}
        observed = {"same_object": obj["metadata"].get("uid") == checkpoint.get("uid"), "resourceVersion": obj["metadata"].get("resourceVersion"),
                    "unchanged_from_before": {k: get_path(obj, k.split(".")) == v for k, v in before.items()}}
    except Exception as exc:
        observed = {"error": getattr(exc, "message", type(exc).__name__)}
    finally:
        if writer is not None:
            try:
                await writer.aclose()
            except Exception:
                pass
    async with session_scope() as db:
        plan = await db.get(ActionPlan, plan_id)
        plan.result = {**(plan.result or {}), "observed": observed}


async def close_undecided_sessions(db) -> None:
    """A session waits on its plan. If the plan will never run, the session must not stay "requested"."""
    await db.execute(text("""UPDATE diagnostic_sessions s SET status = 'expired' FROM action_plans p
                             WHERE s.plan_id = p.id AND s.status = 'requested' AND p.status IN ('expired', 'rejected', 'cancelled')"""))


async def run_executor(stop: asyncio.Event | None = None) -> None:
    from ..diagnostics import service as diag

    s = get_settings()
    if s.process_role != "executor":
        raise RuntimeError("run the executor with MOKS_PROCESS_ROLE=executor")
    stop = stop or asyncio.Event()
    wid = worker_id()
    reported = 0.0
    log.info("executor started", extra={"event": "executor_started"})
    while not stop.is_set():
        try:
            async with session_scope() as db:
                await db.execute(text("UPDATE action_plans SET status = 'expired' WHERE status IN ('pending','approved') AND expires_at < now()"))
                await close_undecided_sessions(db)
            await reconcile_stale()
            if time.monotonic() - reported > 60:
                reported = time.monotonic()
                await versions.report("executor")
            claim = await claim_plan(wid)
            if claim:
                await execute_plan(claim)
                continue
            await diag.tick()
        except Exception:
            log.exception("executor loop error", extra={"event": "executor_loop_error"})
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
