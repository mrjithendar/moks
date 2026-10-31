"""Action plan lifecycle: create -> (approve | reject) -> queue -> execute -> validate -> recover.

Approval binds to the plan hash: SHA-256 over the canonical JSON of environment, namespace, action,
target, parameters, requester, expiry, approval requirement, automatic recovery, the target's
resourceVersion and UID, and the connector, write target and cluster it was planned against. Any
difference (edited parameters, different target, a resource that changed, a replayed approval for
another plan) fails the hash or a precondition. Plans expire. A plan executes at
most once (status transitions under SELECT ... FOR UPDATE). A requester cannot approve their own plan
unless they are a global administrator, whose plans need no second approval: that is recorded as
action.approval_waived, and administrator status is checked again at execution. All of this is
re-verified by the executor immediately before it acts.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, velero
from ..connectors import registry
from ..connectors.base import ConnectorError
from ..incidents import add_event
from ..models import ActionPlan, Approval, Connector, Environment, RoleBinding, User
from ..observability import APPROVALS, POLICY_DENIALS
from ..rbac import Binding, P, Principal
from ..scope import ScopeError, effective_scope
from .catalog import CATALOG, check_policy, describe, needs_approval

HASH_VERSION = 2
FORCED = "Force approved: "  # marks the approval, which stays valid only while its author is an admin


class PlanError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def canonical(plan: ActionPlan, env_name: str) -> str:
    return json.dumps({
        "environment": env_name, "namespace": plan.namespace, "action": plan.action, "target_kind": plan.target_kind,
        "target_name": plan.target_name, "params": plan.params, "requested_by": str(plan.requested_by),
        "expires_at": plan.expires_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "resource_version": (plan.pre_state or {}).get("resourceVersion"), "resource_uid": (plan.pre_state or {}).get("uid"),
        "requires_approval": plan.requires_approval, "auto_recover": plan.auto_recover, "target_identity": plan.target_identity, "v": plan.hash_version,
    }, sort_keys=True, separators=(",", ":"))


def compute_hash(plan: ActionPlan, env_name: str) -> str:
    return hashlib.sha256(canonical(plan, env_name).encode()).hexdigest()


def write_target(row: Connector) -> dict[str, str]:
    """Stable identity of where a connector writes. There is no default: a connector without an
    executor section is read-only, so a plan can never land on the cluster hosting MOKS by accident."""
    cfg = row.config or {}
    if not cfg.get("executor"):
        raise PlanError(409, f"connector {row.name} has no write target: set config.executor before planning changes")
    material = json.dumps({"executor": cfg["executor"], "credential": cfg.get("executor_credential_ref")}, sort_keys=True)
    return {"connector_id": str(row.id), "write_target": hashlib.sha256(material.encode()).hexdigest()[:32]}


async def principal_of(db: AsyncSession, user_id) -> Principal:
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise PlanError(403, "user no longer exists or is disabled")
    rows = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == user_id))).scalars().all()
    return Principal(str(user.id), user.email, [Binding(r.role, r.environment, r.namespace) for r in rows])


def _require(principal: Principal, perm: P, env: Environment, ns: str) -> None:
    if not principal.can(perm, env.name, ns):
        POLICY_DENIALS.labels("action").inc()
        raise PlanError(403, f"{principal.email} lacks {perm} in {env.name}/{ns}")
    try:
        effective_scope(principal, env, perm).require(ns) if ns != "-" else None
    except ScopeError as exc:
        raise PlanError(403, str(exc)) from None


async def create_plan(db: AsyncSession, principal: Principal, env: Environment, *, namespace: str, action: str, target_name: str,
                      target_kind: str, params: dict, reason: str, evidence: list, incident_id=None, investigation_id=None,
                      auto_recover: bool = True) -> ActionPlan:
    spec = CATALOG.get(action)
    if spec is None:
        raise PlanError(400, f"{action!r} is not an allowlisted action ({', '.join(CATALOG)})")
    if target_kind not in spec.target_kinds:
        raise PlanError(400, f"{action} targets {', '.join(spec.target_kinds)}, not {target_kind}")
    if spec.cluster_wide != (namespace == "-"):
        raise PlanError(400, f"{action} is {'cluster-wide: its namespace must be -' if spec.cluster_wide else 'namespaced'}")
    _require(principal, P.ACTION_REQUEST, env, namespace)
    try:
        parsed = spec.params.model_validate(params)
    except Exception as exc:
        raise PlanError(422, f"invalid parameters: {exc}") from None
    connectors, _ = await registry.for_environment(db, env.id)
    pre: dict[str, Any] | None = None
    history = None
    identity: dict[str, Any] | None = None
    try:
        if target_kind == "Repository":
            git = connectors.get("git")
            if git is None:
                raise PlanError(409, "no git connector in this environment")
            git[1].check_repo(parsed.repository)  # type: ignore[attr-defined]
            target_name = parsed.repository  # type: ignore[attr-defined]
        else:
            k8s = connectors.get("kubernetes")
            if k8s is None:
                raise PlanError(409, "no kubernetes connector in this environment")
            identity = {**write_target(k8s[0]), "cluster_uid": await k8s[1].cluster_uid()}
            if target_kind in velero.KINDS:
                pre = await velero.preflight(k8s[1], action, target_name)
                auto_recover = False
            elif target_kind == "Deployment":
                meta = await k8s[1].deployment_meta(namespace, target_name)
                pre = {"resourceVersion": meta["resourceVersion"], "uid": meta["uid"], "generation": meta["generation"], "replicas": meta["replicas"],
                       "revision": meta["summary"].get("revision"), "images": meta["images"], "ready": meta["summary"].get("ready")}
                if action == "rollback_deployment":
                    history = await k8s[1].rollout_history(namespace, target_name)
            else:
                meta = await k8s[1].object_meta(namespace, target_kind, target_name)
                pre = {"resourceVersion": meta["resourceVersion"], "uid": meta["uid"], "spec": meta["spec"]}
                if target_kind == "Pod":  # a pod's resourceVersion moves with its status; the UID identifies it
                    pre = {"uid": meta["uid"], "owner": meta.get("owner")}
                    auto_recover = False
        check_policy(action, parsed, env, pre, target_kind)
        text = describe(action, parsed, f"{target_kind}/{target_name}", namespace, pre, history)
    except ConnectorError as exc:
        status = {"forbidden": 403, "invalid_args": 422, "conflict": 409, "bad_request": 409, "not_found": 409}.get(exc.kind, 502)
        raise PlanError(status, exc.message) from None
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan = ActionPlan(environment_id=env.id, namespace=namespace, action=action, target_kind=target_kind, target_name=target_name,
                      params=parsed.model_dump(exclude_none=True), reason=reason, evidence=evidence[:30],
                      expected_impact=text["impact"], blast_radius=text["blast_radius"], preconditions=text["preconditions"],
                      validation=text["validation"], rollback=text["rollback"], auto_recover=auto_recover, pre_state=pre,
                      requires_approval=needs_approval(spec, env) and not is_admin(principal), requested_by=uuid.UUID(principal.user_id),
                      expires_at=now + timedelta(minutes=env.plan_ttl_minutes), incident_id=incident_id,
                      investigation_id=investigation_id, status="pending", plan_hash="", hash_version=HASH_VERSION, target_identity=identity)
    db.add(plan)
    await db.flush()
    plan.plan_hash = compute_hash(plan, env.name)
    await audit.record(db, action="action.plan", actor_id=principal.user_id, actor_email=principal.email, resource_type="action_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=namespace, decision="allow",
                       details={"action": action, "target": f"{target_kind}/{target_name}", "params": plan.params, "hash": plan.plan_hash,
                                "requires_approval": plan.requires_approval})
    if needs_approval(spec, env) and is_admin(principal):
        await audit.record(db, action="action.approval_waived", actor_id=principal.user_id, actor_email=principal.email, resource_type="action_plan",
                           resource_id=str(plan.id), environment=env.name, namespace=namespace, decision="allow", details={"hash": plan.plan_hash, "reason": "requested by an administrator"})
    if incident_id:
        await add_event(db, incident_id, "action", {"text": f"Action plan {action} on {target_kind}/{target_name} proposed", "plan_id": str(plan.id)},
                        principal.user_id)
    return plan


async def _locked(db: AsyncSession, plan_id: uuid.UUID) -> ActionPlan:
    plan = (await db.execute(select(ActionPlan).where(ActionPlan.id == plan_id).with_for_update())).scalar_one_or_none()
    if plan is None:
        raise PlanError(404, "plan not found")
    return plan


def _expired(plan: ActionPlan) -> bool:
    return datetime.now(timezone.utc) >= plan.expires_at


def is_admin(principal: Principal) -> bool:
    """A global administrator. ADMIN_POLICIES only counts on a binding for every environment and namespace."""
    return principal.can(P.ADMIN_POLICIES, "*", "*")


async def decide(db: AsyncSession, principal: Principal, plan_id: uuid.UUID, approve: bool, comment: str, presented_hash: str) -> ActionPlan:
    plan = await _locked(db, plan_id)
    env = await db.get(Environment, plan.environment_id)
    if plan.status != "pending":
        raise PlanError(409, f"plan is {plan.status}, not pending")
    if _expired(plan):
        plan.status = "expired"
        raise PlanError(409, "plan expired; create a new one")
    own = str(plan.requested_by) == principal.user_id
    if own and not is_admin(principal):
        POLICY_DENIALS.labels("self_approval").inc()
        raise PlanError(403, "you cannot approve or reject your own plan")
    _require(principal, P.ACTION_APPROVE, env, plan.namespace)
    current = compute_hash(plan, env.name)
    if presented_hash != plan.plan_hash or current != plan.plan_hash:
        POLICY_DENIALS.labels("plan_hash").inc()
        raise PlanError(409, "the plan you reviewed differs from the stored plan (hash mismatch); reload and review again")
    db.add(Approval(plan_id=plan.id, approver_id=uuid.UUID(principal.user_id), decision="approve" if approve else "reject",
                    plan_hash=plan.plan_hash, comment=comment[:2000]))
    plan.status = "approved" if approve else "rejected"
    APPROVALS.labels("approve" if approve else "reject").inc()
    await audit.record(db, action="action.approve" if approve else "action.reject", actor_id=principal.user_id, actor_email=principal.email,
                       resource_type="action_plan", resource_id=str(plan.id), environment=env.name, namespace=plan.namespace,
                       decision="allow", details={"hash": plan.plan_hash, "comment": comment[:500], "self_approved": own})
    if plan.incident_id:
        await add_event(db, plan.incident_id, "approval", {"text": f"Plan {plan.action} {'approved' if approve else 'rejected'}",
                                                           "plan_id": str(plan.id)}, principal.user_id)
    return plan


async def force_approve(db: AsyncSession, principal: Principal, plan_id: uuid.UUID, reason: str, presented_hash: str) -> ActionPlan:
    """A global admin's override: approves any pending plan and brings back an expired one, with a
    written reason. It only replaces the approval step. The plan keeps its content, runs once, needs
    an explicit Execute, and the executor re-checks policy and the live object before acting."""
    plan = await _locked(db, plan_id)
    env = await db.get(Environment, plan.environment_id)
    if not is_admin(principal):
        POLICY_DENIALS.labels("force_approve").inc()
        await audit.record(db, action="action.force_approve", actor_id=principal.user_id, actor_email=principal.email,
                           resource_type="action_plan", resource_id=str(plan.id), environment=env.name, namespace=plan.namespace,
                           decision="deny", details={"reason": reason[:500]})
        raise PlanError(403, "only a global administrator can force approve a plan")
    _require(principal, P.ACTION_APPROVE, env, plan.namespace)
    if plan.status not in ("pending", "expired"):
        raise PlanError(409, f"plan is {plan.status}; only pending or expired plans can be force approved")
    if plan.hash_version != HASH_VERSION:
        raise PlanError(409, "plan was hashed by an older version; create a new plan")
    if presented_hash != plan.plan_hash or compute_hash(plan, env.name) != plan.plan_hash:
        POLICY_DENIALS.labels("plan_hash").inc()
        raise PlanError(409, "the plan you reviewed differs from the stored plan (hash mismatch); reload and review again")
    previous = {"status": plan.status, "hash": plan.plan_hash, "expires_at": plan.expires_at.isoformat()}
    if _expired(plan):  # the expiry is hashed, so a new window means a new hash
        plan.expires_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=env.plan_ttl_minutes)
        plan.plan_hash = compute_hash(plan, env.name)
    db.add(Approval(plan_id=plan.id, approver_id=uuid.UUID(principal.user_id), decision="approve", plan_hash=plan.plan_hash,
                    comment=f"{FORCED}{reason}"[:2000]))
    plan.status = "approved"
    APPROVALS.labels("force_approve").inc()
    await audit.record(db, action="action.force_approve", actor_id=principal.user_id, actor_email=principal.email,
                       resource_type="action_plan", resource_id=str(plan.id), environment=env.name, namespace=plan.namespace,
                       decision="allow", details={"hash": plan.plan_hash, "previous": previous, "expires_at": plan.expires_at.isoformat(),
                                                  "reason": reason[:500], "self_approved": str(plan.requested_by) == principal.user_id})
    if plan.incident_id:
        await add_event(db, plan.incident_id, "approval", {"text": f"Plan {plan.action} force approved by an administrator",
                                                           "plan_id": str(plan.id)}, principal.user_id)
    return plan


async def queue(db: AsyncSession, principal: Principal, plan_id: uuid.UUID, presented_hash: str) -> ActionPlan:
    plan = await _locked(db, plan_id)
    env = await db.get(Environment, plan.environment_id)
    _require(principal, P.ACTION_EXECUTE, env, plan.namespace)
    if plan.status in ("queued", "executing", "succeeded", "failed", "recovered", "reconciliation_required", "reconciled"):
        POLICY_DENIALS.labels("replay").inc()
        raise PlanError(409, f"plan already {plan.status}; plans execute at most once")
    if _expired(plan):
        plan.status = "expired"
        raise PlanError(409, "plan expired; create a new one")
    if plan.requires_approval and plan.status != "approved":
        raise PlanError(409, "plan needs an approval from someone else first")
    if not plan.requires_approval and plan.status not in ("pending", "approved"):
        raise PlanError(409, f"plan is {plan.status}")
    if presented_hash != plan.plan_hash or compute_hash(plan, env.name) != plan.plan_hash:
        POLICY_DENIALS.labels("plan_hash").inc()
        raise PlanError(409, "plan hash mismatch; the plan changed after review")
    plan.status, plan.executed_by, plan.queued_at = "queued", uuid.UUID(principal.user_id), datetime.now(timezone.utc)
    await audit.record(db, action="action.queue", actor_id=principal.user_id, actor_email=principal.email, resource_type="action_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=plan.namespace, decision="allow", details={"hash": plan.plan_hash})
    return plan


async def start_if_allowed(db: AsyncSession, principal: Principal, env: Environment, plan: ActionPlan) -> ActionPlan:
    """For work with no approval step: queue it at once for someone who may execute it. Anyone else
    leaves it pending for someone who can."""
    if plan.requires_approval or not principal.can(P.ACTION_EXECUTE, env.name, plan.namespace):
        return plan
    return await queue(db, principal, plan.id, plan.plan_hash)


async def cancel(db: AsyncSession, principal: Principal, plan_id: uuid.UUID) -> ActionPlan:
    plan = await _locked(db, plan_id)
    env = await db.get(Environment, plan.environment_id)
    if str(plan.requested_by) != principal.user_id:
        _require(principal, P.ACTION_APPROVE, env, plan.namespace)
    if plan.status not in ("pending", "approved", "queued"):
        raise PlanError(409, f"plan is {plan.status}")
    plan.status = "cancelled"
    await audit.record(db, action="action.cancel", actor_id=principal.user_id, actor_email=principal.email, resource_type="action_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=plan.namespace)
    return plan


async def verify_for_execution(db: AsyncSession, plan: ActionPlan, env: Environment) -> None:
    """Executor-side re-check of everything, immediately before acting, against current state:
    what was true at review or approval may have changed since."""
    if _expired(plan):
        raise PlanError(409, "plan expired before execution")
    if plan.hash_version != HASH_VERSION:
        raise PlanError(409, "plan was hashed by an older version; create a new plan")
    if compute_hash(plan, env.name) != plan.plan_hash:
        raise PlanError(409, "plan hash mismatch at execution")
    diagnostic = plan.action == "diagnostic_session"
    spec = CATALOG.get(plan.action)
    if spec is None and not diagnostic:
        raise PlanError(403, f"{plan.action} is no longer an allowlisted action")
    if spec is not None:
        try:
            check_policy(plan.action, spec.params.model_validate(plan.params), env, None, plan.target_kind)
        except ConnectorError as exc:
            raise PlanError(403, f"current policy refuses this plan: {exc.message}") from None
        except PlanError:
            raise
        except Exception as exc:
            raise PlanError(422, f"parameters no longer valid: {exc}") from None
    required = not diagnostic and needs_approval(spec, env)  # type: ignore[arg-type]
    try:
        requester = await principal_of(db, plan.requested_by)
        _require(requester, P.ACTION_REQUEST, env, plan.namespace)
    except PlanError as exc:
        raise PlanError(403, f"requester may no longer request this action: {exc.message}") from None
    if required and not plan.requires_approval and not is_admin(requester):
        raise PlanError(409, "this plan now needs an approval (environment or action policy changed, or the requester is no longer an administrator); create a new plan")
    executor = await principal_of(db, plan.executed_by)
    _require(executor, P.ACTION_EXECUTE, env, plan.namespace)
    if plan.target_identity:
        row = await db.get(Connector, uuid.UUID(plan.target_identity["connector_id"]))
        if row is None or not row.enabled or row.environment_id != env.id:
            raise PlanError(409, "the connector this plan was made for no longer exists or is disabled")
        if write_target(row)["write_target"] != plan.target_identity["write_target"]:
            raise PlanError(409, "the write target changed after this plan was made")
    if plan.requires_approval:
        approvals = (await db.execute(select(Approval).where(Approval.plan_id == plan.id, Approval.decision == "approve"))).scalars().all()
        valid = []
        for a in approvals:
            try:
                approver = await principal_of(db, a.approver_id)
                own_or_forced = a.approver_id == plan.requested_by or a.comment.startswith(FORCED)
                if a.plan_hash != plan.plan_hash or (own_or_forced and not is_admin(approver)):
                    continue
                _require(approver, P.ACTION_APPROVE, env, plan.namespace)
                valid.append(a)
            except PlanError:
                continue  # approver lost the permission since approving
        if not valid:
            raise PlanError(403, "no valid approval bound to this plan (approver changed, revoked, or hash differs)")


async def reconcile(db: AsyncSession, principal: Principal, plan_id: uuid.UUID, outcome: str, comment: str) -> ActionPlan:
    """A person records what really happened to a plan whose outcome MOKS could not establish."""
    plan = await _locked(db, plan_id)
    env = await db.get(Environment, plan.environment_id)
    _require(principal, P.ACTION_APPROVE, env, plan.namespace)
    if plan.status != "reconciliation_required":
        raise PlanError(409, f"plan is {plan.status}, not awaiting reconciliation")
    if outcome not in ("applied", "not_applied", "reverted"):
        raise PlanError(422, "outcome must be applied, not_applied or reverted")
    if not comment.strip():
        raise PlanError(422, "say how you verified the outcome")
    plan.status, plan.finished_at = "reconciled", datetime.now(timezone.utc)
    plan.result = {**(plan.result or {}), "reconciliation": {"outcome": outcome, "comment": comment[:2000], "by": principal.email}}
    await audit.record(db, action="action.reconcile", actor_id=principal.user_id, actor_email=principal.email, resource_type="action_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=plan.namespace, decision="allow", outcome=outcome,
                       details={"comment": comment[:500]})
    if plan.incident_id:
        await add_event(db, plan.incident_id, "validation", {"text": f"{plan.action} reconciled by a person: {outcome}", "plan_id": str(plan.id)}, principal.user_id)
    return plan


def plan_out(plan: ActionPlan, env_name: str, users: dict | None = None, approvals: list | None = None) -> dict[str, Any]:
    users = users or {}
    return {
        "id": str(plan.id), "environment": env_name, "namespace": plan.namespace, "action": plan.action,
        "title": CATALOG[plan.action].title if plan.action in CATALOG else plan.action, "operation": CATALOG[plan.action].operation if plan.action in CATALOG else "",
        "target": f"{plan.target_kind}/{plan.target_name}", "target_kind": plan.target_kind, "target_name": plan.target_name,
        "params": plan.params, "reason": plan.reason, "evidence": plan.evidence, "expected_impact": plan.expected_impact,
        "blast_radius": plan.blast_radius, "preconditions": plan.preconditions, "validation": plan.validation, "rollback": plan.rollback,
        "auto_recover": plan.auto_recover, "pre_state": {k: v for k, v in (plan.pre_state or {}).items() if k != "template"},
        "requires_approval": plan.requires_approval, "plan_hash": plan.plan_hash, "status": plan.status, "mutation_state": plan.mutation_state, "checkpoint": plan.checkpoint, "target_identity": plan.target_identity,
        "requested_by": users.get(plan.requested_by), "executed_by": users.get(plan.executed_by),
        "expires_at": plan.expires_at.isoformat(), "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "queued_at": plan.queued_at.isoformat() if plan.queued_at else None,
        "executed_at": plan.executed_at.isoformat() if plan.executed_at else None,
        "finished_at": plan.finished_at.isoformat() if plan.finished_at else None, "result": plan.result,
        "incident_id": str(plan.incident_id) if plan.incident_id else None,
        "investigation_id": str(plan.investigation_id) if plan.investigation_id else None,
        "approvals": approvals or [],
    }
