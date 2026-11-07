"""Mappings and secret plans: the parts the API runs. Nothing here reads a secret value or calls AWS.

A plan's lifecycle: comparing (the executor reads both sides) -> draft (the requester includes, excludes and
resolves items) -> pending (hash fixed, expiry starts) -> approved -> queued -> executing -> succeeded | partial |
failed. Approval binds to the plan hash: SHA-256 over the environment, kind, requester, expiry, and for every item
whether it is included, the mapping and its version, the baseline it was compared against, the source and
destination versions and digests, the resolution, the actions, the expected digest and the changed key names.
Anything that moves after approval (a side, the mapping, the baseline) fails the executor's re-check, and the item
goes stale; a fresh comparison and approval are needed.

Approval follows the action-plan rules: never one's own plan, except a global administrator, whose plans need no
second approval (recorded as secrets.approval_waived). Deletion plans are stronger: they always need someone else's
approval, with the confirmation phrase typed back.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..actions.service import is_admin, principal_of
from ..models import Connector, Environment, SecretInventory, SecretInventoryScope, SecretMapping, SecretPlan, SecretPlanApproval, SecretPlanItem
from ..observability import POLICY_DENIALS
from ..rbac import P, Principal
from ..scope import ScopeError, effective_scope
from . import compare
from .content import FORMATS, SUPPORTED_TYPES

HASH_VERSION = 1
K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
K8S_KEY = re.compile(r"^[-._a-zA-Z0-9]{1,253}$")
AWS_NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")
EDITABLE = ("draft",)
FINAL = ("succeeded", "rejected", "cancelled", "expired", "compare_failed")


class SyncError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def require(principal: Principal, perm: P, env: Environment, namespace: str) -> None:
    if not principal.can(perm, env.name, namespace):
        POLICY_DENIALS.labels("secrets").inc()
        raise SyncError(403, f"{principal.email} lacks {perm} in {env.name}/{namespace}")
    try:
        effective_scope(principal, env, perm).require(namespace)
    except ScopeError as exc:
        raise SyncError(403, str(exc)) from None


def can(principal: Principal, perm: P, env: Environment, namespace: str | None) -> bool:
    """namespace None: cluster-wide (every namespace), which AWS-only listings need."""
    try:
        scope = effective_scope(principal, env, perm)
    except ScopeError:
        return False
    return scope.cluster_wide if namespace is None else scope.allows(namespace)


# ---- connectors and identity --------------------------------------------------------------------------------


async def connector(db: AsyncSession, env: Environment, connector_id: uuid.UUID | str, ctype: str) -> Connector:
    row = await db.get(Connector, uuid.UUID(str(connector_id)))
    if row is None or row.environment_id != env.id or row.type != ctype or not row.enabled:
        raise SyncError(404, f"no enabled {ctype} connector {connector_id} in {env.name}")
    if ctype == "kubernetes" and not (row.config or {}).get("executor"):
        raise SyncError(409, f"connector {row.name} has no write target (config.executor), so the executor cannot read or write its Secrets")
    return row


def aws_scope(row: Connector):
    from .aws import AwsSecretsConnector

    return AwsSecretsConnector(row.config or {}, row.credential_ref)  # validates; never calls AWS from here


async def cluster_identity(db: AsyncSession, env: Environment, kube: Connector) -> str:
    """The cluster UID the executor recorded at discovery. A mapping is bound to it, so a connector repointed at
    another cluster cannot write there under an old mapping."""
    uid = (await db.execute(select(SecretInventoryScope.cluster_uid).where(SecretInventoryScope.environment_id == env.id, SecretInventoryScope.connector_id == kube.id,
                                                                            SecretInventoryScope.side == "kubernetes", SecretInventoryScope.cluster_uid.is_not(None))
                            .order_by(SecretInventoryScope.discovered_at.desc()).limit(1))).scalar_one_or_none()
    if not uid:
        raise SyncError(409, f"discover Secrets in {kube.name} first, so MOKS knows which cluster it is")
    return uid


def default_aws_name(template: str, env: Environment, cluster: str, namespace: str, name: str) -> str:
    raw = template.format(environment=env.name, cluster=cluster, namespace=namespace, name=name)
    return re.sub(r"[^A-Za-z0-9/_+=.@-]", "-", raw)[:512]


# ---- mappings -----------------------------------------------------------------------------------------------


def check_key_mapping(fmt: str, key_mapping: dict[str, Any]) -> dict[str, Any]:
    key_mapping = dict(key_mapping or {})
    if fmt in ("text", "binary"):
        key = key_mapping.get("key")
        if not isinstance(key, str) or not K8S_KEY.match(key):
            raise SyncError(422, f"the {fmt} format needs key_mapping.key: the Kubernetes key that holds the whole value")
        return {"key": key}
    if fmt == "json":
        keys = key_mapping.get("keys") or {}
        if not isinstance(keys, dict) or not all(isinstance(a, str) and isinstance(k, str) and 0 < len(a) <= 512 and K8S_KEY.match(k) for a, k in keys.items()):
            raise SyncError(422, "key_mapping.keys maps AWS JSON keys to Kubernetes keys (letters, digits, -._)")
        if len(set(keys.values())) != len(keys):
            raise SyncError(422, "two AWS keys map to the same Kubernetes key")
        return {"keys": keys} if keys else {}
    if fmt == "moks-k8s-v1":
        return {}
    raise SyncError(422, f"payload_format must be one of {', '.join(FORMATS)}")


async def create_mapping(db: AsyncSession, principal: Principal, env: Environment, body: dict[str, Any]) -> SecretMapping:
    ns, name = body["namespace"], body["secret_name"]
    if not K8S_NAME.match(name) or not K8S_NAME.match(ns):
        raise SyncError(422, "namespace and secret_name must be Kubernetes names")
    require(principal, P.SECRETS_MAP, env, ns)
    kube = await connector(db, env, body["kube_connector_id"], "kubernetes")
    aws_row = await connector(db, env, body["aws_connector_id"], "aws_secrets")
    aws = aws_scope(aws_row)
    if not aws.namespace_allowed(ns):
        raise SyncError(403, f"connector {aws_row.name} does not sync namespace {ns} (kubernetes_namespaces: {', '.join(aws.namespaces)})")
    aws_name = body.get("aws_secret_name") or default_aws_name(aws.name_template, env, kube.name, ns, name)
    if not AWS_NAME.match(aws_name) or not aws.name_allowed(aws_name):
        raise SyncError(422, f"AWS secret name {aws_name!r} must use letters, digits and /_+=.@- and start with one of {', '.join(aws.prefixes)}")
    if re.search(r"-[A-Za-z0-9]{6}$", aws_name):
        raise SyncError(422, "AWS secret names that end in a hyphen and six characters are confused with ARN suffixes; choose another name")
    fmt = body.get("payload_format") or "moks-k8s-v1"
    k8s_type = body.get("k8s_type") or "Opaque"
    if k8s_type not in SUPPORTED_TYPES:
        raise SyncError(422, f"k8s_type must be one of {', '.join(SUPPORTED_TYPES)}")
    direction = body.get("direction") or "export"
    if direction not in compare.DIRECTION_ALLOWS:
        raise SyncError(422, "direction must be export, import or both")
    seen = (await db.execute(select(SecretInventory).where(SecretInventory.environment_id == env.id, SecretInventory.connector_id == kube.id,
                                                           SecretInventory.namespace == ns, SecretInventory.name == name))).scalar_one_or_none()
    if seen is not None and seen.excluded:
        raise SyncError(422, f"{ns}/{name} cannot be synced: {seen.excluded}")
    m = SecretMapping(environment_id=env.id, kube_connector_id=kube.id, cluster_uid=await cluster_identity(db, env, kube), cluster_name=kube.name,
                      namespace=ns, secret_name=name, aws_connector_id=aws_row.id, aws_account=aws.account_id, aws_region=aws.region,
                      aws_secret_name=aws_name, payload_format=fmt, key_mapping=check_key_mapping(fmt, body.get("key_mapping") or {}), k8s_type=k8s_type,
                      direction=direction, expected_owner=_owners(body.get("expected_owner")), management_policy=_policy(body.get("management_policy")),
                      created_by=uuid.UUID(principal.user_id), updated_by=uuid.UUID(principal.user_id))
    db.add(m)
    try:
        async with db.begin_nested():
            await db.flush()
    except Exception as exc:
        if "secret_mappings_kube" in str(exc) or "secret_mappings_aws" in str(exc):
            raise SyncError(409, "an active mapping already uses this Kubernetes Secret or this AWS secret") from None
        raise
    await audit.record(db, action="secrets.mapping_create", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_mapping",
                       resource_id=str(m.id), environment=env.name, namespace=ns, decision="allow", details=_mapping_audit(m))
    return m


def _owners(v: Any) -> dict[str, str]:
    v = v or {}
    return {"kubernetes": str(v.get("kubernetes") or "none")[:60], "aws": str(v.get("aws") or "none")[:60]}


def _policy(v: Any) -> str:
    if v in (None, "", "refuse_managed"):
        return "refuse_managed"
    if v == "override":
        return "override"
    raise SyncError(422, "management_policy must be refuse_managed or override")


def _mapping_audit(m: SecretMapping) -> dict[str, Any]:
    return {"kubernetes": f"{m.cluster_name}/{m.namespace}/{m.secret_name}", "cluster_uid": m.cluster_uid, "aws": f"{m.aws_account}/{m.aws_region}/{m.aws_secret_name}",
            "format": m.payload_format, "direction": m.direction, "type": m.k8s_type, "policy": m.management_policy, "expected_owner": m.expected_owner, "version": m.version}


async def update_mapping(db: AsyncSession, principal: Principal, env: Environment, mapping_id: uuid.UUID, body: dict[str, Any]) -> SecretMapping:
    m = await db.get(SecretMapping, mapping_id, with_for_update=True)
    if m is None or m.environment_id != env.id or m.status != "active":
        raise SyncError(404, "mapping not found")
    require(principal, P.SECRETS_MAP, env, m.namespace)
    if body.get("version") != m.version:
        raise SyncError(409, f"the mapping changed since you loaded it (version {m.version}); reload and edit again")
    if m.lease_owner and m.lease_expires_at and m.lease_expires_at > datetime.now(timezone.utc):
        raise SyncError(409, "an operation on this mapping is running; edit it when it finishes")
    before = _mapping_audit(m)
    if "aws_secret_name" in body and body["aws_secret_name"] != m.aws_secret_name:
        if m.aws_secret_arn:
            raise SyncError(409, "the AWS secret already exists under this mapping; retire the mapping and create a new one to point elsewhere")
        aws = aws_scope(await connector(db, env, m.aws_connector_id, "aws_secrets"))
        if not AWS_NAME.match(body["aws_secret_name"]) or not aws.name_allowed(body["aws_secret_name"]):
            raise SyncError(422, f"AWS secret name must start with one of {', '.join(aws.prefixes)}")
        m.aws_secret_name = body["aws_secret_name"]
    if "payload_format" in body or "key_mapping" in body:
        fmt = body.get("payload_format", m.payload_format)
        m.key_mapping = check_key_mapping(fmt, body.get("key_mapping", m.key_mapping))
        m.payload_format = fmt
    if "k8s_type" in body:
        if body["k8s_type"] not in SUPPORTED_TYPES:
            raise SyncError(422, f"k8s_type must be one of {', '.join(SUPPORTED_TYPES)}")
        m.k8s_type = body["k8s_type"]
    if "direction" in body:
        if body["direction"] not in compare.DIRECTION_ALLOWS:
            raise SyncError(422, "direction must be export, import or both")
        m.direction = body["direction"]
    if "expected_owner" in body:
        m.expected_owner = _owners(body["expected_owner"])
    if "management_policy" in body:
        m.management_policy = _policy(body["management_policy"])
    if any(k in body for k in ("payload_format", "key_mapping", "k8s_type")) and m.baseline:
        m.baseline, m.baseline_seq = None, m.baseline_seq + 1  # digests under the old format no longer describe the agreement
    m.version += 1
    m.updated_by, m.updated_at = uuid.UUID(principal.user_id), datetime.now(timezone.utc)
    await audit.record(db, action="secrets.mapping_update", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_mapping",
                       resource_id=str(m.id), environment=env.name, namespace=m.namespace, decision="allow", details={"before": before, "after": _mapping_audit(m)})
    return m


async def retire_mapping(db: AsyncSession, principal: Principal, env: Environment, mapping_id: uuid.UUID, version: int) -> SecretMapping:
    m = await db.get(SecretMapping, mapping_id, with_for_update=True)
    if m is None or m.environment_id != env.id or m.status != "active":
        raise SyncError(404, "mapping not found")
    require(principal, P.SECRETS_MAP, env, m.namespace)
    if version != m.version:
        raise SyncError(409, "the mapping changed since you loaded it; reload first")
    m.status, m.version, m.updated_at = "retired", m.version + 1, datetime.now(timezone.utc)
    await audit.record(db, action="secrets.mapping_retire", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_mapping",
                       resource_id=str(m.id), environment=env.name, namespace=m.namespace, decision="allow", details=_mapping_audit(m))
    return m


def mapping_dict(m: SecretMapping) -> dict[str, Any]:
    """The mapping as compare.derive reads it."""
    return {"direction": m.direction, "payload_format": m.payload_format, "key_mapping": m.key_mapping or {}, "k8s_type": m.k8s_type,
            "expected_owner": m.expected_owner or {}, "management_policy": m.management_policy}


# ---- plans --------------------------------------------------------------------------------------------------


async def plan_mappings(db: AsyncSession, plan: SecretPlan) -> dict[uuid.UUID, SecretMapping]:
    ids = (await db.execute(select(SecretPlanItem.mapping_id).where(SecretPlanItem.plan_id == plan.id))).scalars().all()
    rows = (await db.execute(select(SecretMapping).where(SecretMapping.id.in_(ids)))).scalars().all() if ids else []
    return {m.id: m for m in rows}


async def create_plan(db: AsyncSession, principal: Principal, env: Environment, *, kind: str, mapping_ids: list[uuid.UUID], reason: str,
                      rollback: dict[str, dict[str, Any]] | None = None, targets: list[str] | None = None, source_plan_id: uuid.UUID | None = None) -> SecretPlan:
    from ..config import get_settings

    if kind not in ("sync", "rollback", "delete"):
        raise SyncError(422, "kind must be sync, rollback or delete")
    ids = list(dict.fromkeys(mapping_ids))
    if not ids:
        raise SyncError(422, "select at least one mapping")
    if len(ids) > get_settings().secrets_max_plan_items:
        raise SyncError(422, f"a plan holds at most {get_settings().secrets_max_plan_items} mappings")
    rows = (await db.execute(select(SecretMapping).where(SecretMapping.id.in_(ids), SecretMapping.environment_id == env.id, SecretMapping.status == "active"))).scalars().all()
    found = {m.id: m for m in rows}
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise SyncError(404, f"no active mapping {missing[0]} in {env.name}")
    for m in rows:
        require(principal, P.SECRETS_PROPOSE, env, m.namespace)
        require(principal, P.SECRETS_COMPARE, env, m.namespace)
    if kind == "delete":
        targets = [t for t in (targets or []) if t in compare.SIDES]
        if not targets:
            raise SyncError(422, "a deletion plan names its targets: aws, kubernetes or both")
    request: dict[str, Any] = {"mapping_ids": [str(i) for i in ids], "targets": targets}
    if kind == "rollback":
        rollback = rollback or {}
        for i in ids:
            spec = rollback.get(str(i)) or {}
            version = spec.get("version") or "AWSPREVIOUS"
            if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", str(version)):
                raise SyncError(422, "rollback version must be a version id or AWSPREVIOUS")
            spec_targets = [t for t in spec.get("targets") or ["aws"] if t in compare.SIDES]
            rollback[str(i)] = {"version": version, "targets": spec_targets or ["aws"]}
        request["rollback"] = rollback
    plan = SecretPlan(environment_id=env.id, kind=kind, status="comparing", reason=reason[:4000], requested_by=uuid.UUID(principal.user_id),
                      request=request, hash_version=HASH_VERSION, source_plan_id=source_plan_id)
    db.add(plan)
    await db.flush()
    for pos, i in enumerate(ids):
        m = found[i]
        db.add(SecretPlanItem(plan_id=plan.id, mapping_id=m.id, position=pos, mapping_version=m.version, baseline_seq=m.baseline_seq,
                              rollback_version=(request.get("rollback") or {}).get(str(i), {}).get("version")))
    await audit.record(db, action="secrets.plan_create", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=_namespaces(rows), decision="allow",
                       details={"kind": kind, "mappings": [_label(m) for m in rows], "targets": targets, "rollback": request.get("rollback")})
    return plan


def _label(m: SecretMapping) -> str:
    return f"{m.namespace}/{m.secret_name} <-> {m.aws_secret_name}"


def _namespaces(rows) -> str:
    ns = sorted({m.namespace for m in rows})
    return ns[0] if len(ns) == 1 else "*"


async def load_plan(db: AsyncSession, plan_id: uuid.UUID, env: Environment, lock: bool = False) -> SecretPlan:
    q = select(SecretPlan).where(SecretPlan.id == plan_id)
    if lock:
        q = q.with_for_update()
    plan = (await db.execute(q)).scalar_one_or_none()
    if plan is None or plan.environment_id != env.id:
        raise SyncError(404, "plan not found")
    return plan


async def items_of(db: AsyncSession, plan: SecretPlan) -> list[SecretPlanItem]:
    return list((await db.execute(select(SecretPlanItem).where(SecretPlanItem.plan_id == plan.id).order_by(SecretPlanItem.position))).scalars().all())


def rederive(item: SecretPlanItem, m: SecretMapping, kind: str, request: dict[str, Any], aws_allow_delete: bool) -> None:
    """Recompute what an item writes from its compared states and its resolution."""
    targets = request.get("targets") if kind == "delete" else ((request.get("rollback") or {}).get(str(m.id)) or {}).get("targets")
    d = compare.derive(kind=kind, mapping=mapping_dict(m), kube=item.kube_state or {}, aws=item.aws_state or {}, source=item.source_state or None,
                       classification=item.classification or "failed", proposal=item.proposal, resolution=item.resolution, key_choices=item.key_choices or {},
                       targets_requested=targets, aws_allow_delete=aws_allow_delete)
    item.targets, item.actions, item.expected, item.diff, item.blocked = d["targets"], d["actions"], d["expected"], d["diff"], d["blocked"]
    item.warnings = _keep_warnings(item.warnings) + [w for w in dict.fromkeys(d["warnings"])]
    item.status = d["status"]


def _keep_warnings(ws: list[str] | None) -> list[str]:
    """Warnings the comparison found (first sync, ownership drift) stay; derived ones are recomputed."""
    return [w for w in ws or [] if w.startswith("compare: ")]


async def update_item(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID, item_id: uuid.UUID, body: dict[str, Any]) -> SecretPlan:
    plan = await load_plan(db, plan_id, env, lock=True)
    if plan.status not in EDITABLE:
        raise SyncError(409, f"plan is {plan.status}; only a draft can be edited")
    if body.get("version") != plan.version:
        raise SyncError(409, "the plan changed since you loaded it; reload and review again")
    if str(plan.requested_by) != principal.user_id:
        raise SyncError(403, "only the person who proposed this plan can edit it")
    item = await db.get(SecretPlanItem, item_id)
    if item is None or item.plan_id != plan.id:
        raise SyncError(404, "item not found")
    m = await db.get(SecretMapping, item.mapping_id)
    require(principal, P.SECRETS_PROPOSE, env, m.namespace)
    if "included" in body:
        item.included = bool(body["included"])
    if "resolution" in body:
        if body["resolution"] not in (None, "kubernetes", "aws", "edited"):
            raise SyncError(422, "resolution must be kubernetes, aws or edited")
        if plan.kind != "sync":
            raise SyncError(422, "only sync plans have resolutions")
        item.resolution = body["resolution"]
    if "key_choices" in body:
        choices = body["key_choices"] or {}
        if not isinstance(choices, dict) or not all(v in ("kubernetes", "aws", "omit") for v in choices.values()):
            raise SyncError(422, "key_choices maps each key to kubernetes, aws or omit")
        item.key_choices = {str(k): v for k, v in choices.items()}
    rederive(item, m, plan.kind, plan.request or {}, aws_scope(await connector(db, env, m.aws_connector_id, "aws_secrets")).allow_delete)
    item.updated_at = datetime.now(timezone.utc)
    plan.version += 1
    await audit.record(db, action="secrets.plan_edit", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan", resource_id=str(plan.id),
                       environment=env.name, namespace=m.namespace, decision="allow",
                       details={"item": str(item.id), "mapping": _label(m), "included": item.included, "resolution": item.resolution, "key_choices": item.key_choices})
    return plan


def canonical(plan: SecretPlan, env_name: str, items: list[SecretPlanItem]) -> str:
    def side(st: dict[str, Any]) -> dict[str, Any]:
        return {k: st.get(k) for k in ("exists", "uid", "resource_version", "arn", "version_id", "digest", "type")}

    def names(d: dict[str, Any]) -> dict[str, Any]:
        return {t: {"added": [x["key"] for x in v.get("added", [])], "removed": [x["key"] for x in v.get("removed", [])],
                    "changed": [x["key"] for x in v.get("changed", [])], "type_change": v.get("type_change")} for t, v in (d or {}).items()}

    return json.dumps({
        "v": plan.hash_version, "environment": env_name, "plan": str(plan.id), "kind": plan.kind, "requested_by": str(plan.requested_by),
        "expires_at": plan.expires_at.astimezone(timezone.utc).isoformat(timespec="seconds") if plan.expires_at else None,
        "requires_approval": plan.requires_approval,
        "items": [{"mapping": str(i.mapping_id), "included": i.included, "mapping_version": i.mapping_version, "baseline_seq": i.baseline_seq,
                   "kube": side(i.kube_state or {}), "aws": side(i.aws_state or {}), "source": side(i.source_state or {}), "rollback_version": i.rollback_version,
                   "resolution": i.resolution, "key_choices": i.key_choices or {}, "targets": list(i.targets or []), "actions": i.actions or {},
                   "expected": (i.expected or {}).get("digest") if not (i.expected or {}).get("absent") else "absent", "diff": names(i.diff)}
                  for i in sorted(items, key=lambda x: x.position)],
    }, sort_keys=True, separators=(",", ":"))


def compute_hash(plan: SecretPlan, env_name: str, items: list[SecretPlanItem]) -> str:
    return hashlib.sha256(canonical(plan, env_name, items).encode()).hexdigest()


def runnable(items: list[SecretPlanItem]) -> list[SecretPlanItem]:
    return [i for i in items if i.included and i.status == "pending" and i.targets]


def confirm_phrase(plan: SecretPlan, items: list[SecretPlanItem]) -> str:
    return f"delete {len(runnable(items))} secret{'s' if len(runnable(items)) != 1 else ''}"


async def submit(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID, version: int) -> SecretPlan:
    plan = await load_plan(db, plan_id, env, lock=True)
    if str(plan.requested_by) != principal.user_id:
        raise SyncError(403, "only the person who proposed this plan can submit it")
    if plan.status != "draft":
        raise SyncError(409, f"plan is {plan.status}, not draft")
    if version != plan.version:
        raise SyncError(409, "the plan changed since you loaded it; reload and review again")
    items = await items_of(db, plan)
    mappings = await plan_mappings(db, plan)
    for i in items:
        require(principal, P.SECRETS_PROPOSE, env, mappings[i.mapping_id].namespace)
    unresolved = [i for i in items if i.included and i.status == "conflict"]
    if unresolved:
        raise SyncError(409, f"{len(unresolved)} included item(s) still need a resolution; resolve or exclude them")
    if not runnable(items):
        raise SyncError(409, "nothing to do: every included item is in sync, blocked or excluded")
    for i in items:
        if not i.included or i.status != "pending":
            if i.status == "pending":
                i.status = "skipped"
    admin = is_admin(principal)
    plan.requires_approval = plan.kind == "delete" or not admin
    now = datetime.now(timezone.utc).replace(microsecond=0)
    plan.expires_at = now + timedelta(minutes=env.plan_ttl_minutes)
    plan.submitted_at = now
    plan.summary = {**(plan.summary or {}), "runnable": len(runnable(items)), **({"confirm": confirm_phrase(plan, items)} if plan.kind == "delete" else {})}
    plan.plan_hash = compute_hash(plan, env.name, items)
    plan.status = "pending" if plan.requires_approval else "approved"
    plan.version += 1
    await audit.record(db, action="secrets.plan_submit", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan", resource_id=str(plan.id),
                       environment=env.name, namespace=_namespaces(mappings.values()), decision="allow",
                       details={"hash": plan.plan_hash, "kind": plan.kind, "requires_approval": plan.requires_approval, "items": [_item_audit(i, mappings[i.mapping_id]) for i in runnable(items)]})
    if not plan.requires_approval:
        await audit.record(db, action="secrets.approval_waived", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan",
                           resource_id=str(plan.id), environment=env.name, decision="allow", details={"hash": plan.plan_hash, "reason": "requested by an administrator"})
    return plan


def _item_audit(i: SecretPlanItem, m: SecretMapping) -> dict[str, Any]:
    """Names, key names and versions: what an auditor needs, and never a value or a digest."""
    keys = sorted({x["key"] for d in (i.diff or {}).values() for part in ("added", "removed", "changed") for x in d.get(part, [])})
    return {"mapping": str(m.id), "kubernetes": f"{m.cluster_name}/{m.namespace}/{m.secret_name}", "aws": f"{m.aws_account}/{m.aws_region}/{m.aws_secret_name}",
            "targets": list(i.targets or []), "actions": i.actions, "resolution": i.resolution, "keys": keys,
            "kube_version": (i.kube_state or {}).get("resource_version"), "aws_version": (i.aws_state or {}).get("version_id"), "rollback_version": i.rollback_version}


def _expired(plan: SecretPlan) -> bool:
    return plan.expires_at is not None and datetime.now(timezone.utc) >= plan.expires_at


async def decide(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID, approve: bool, comment: str, presented_hash: str, confirm: str | None) -> SecretPlan:
    plan = await load_plan(db, plan_id, env, lock=True)
    if plan.status != "pending":
        raise SyncError(409, f"plan is {plan.status}, not pending")
    if _expired(plan):
        plan.status = "expired"
        raise SyncError(409, "plan expired; refresh it to compare again")
    own = str(plan.requested_by) == principal.user_id
    if own and (plan.kind == "delete" or not is_admin(principal)):
        POLICY_DENIALS.labels("self_approval").inc()
        raise SyncError(403, "you cannot approve or reject your own plan" + (" (deletion always needs someone else)" if plan.kind == "delete" else ""))
    items = await items_of(db, plan)
    mappings = await plan_mappings(db, plan)
    for i in items:
        require(principal, P.SECRETS_APPROVE, env, mappings[i.mapping_id].namespace)
    if presented_hash != plan.plan_hash or compute_hash(plan, env.name, items) != plan.plan_hash:
        POLICY_DENIALS.labels("plan_hash").inc()
        raise SyncError(409, "the plan you reviewed differs from the stored plan (hash mismatch); reload and review again")
    if approve and plan.kind == "delete" and (confirm or "").strip() != (plan.summary or {}).get("confirm"):
        raise SyncError(422, f"type \"{(plan.summary or {}).get('confirm')}\" to approve this deletion")
    db.add(SecretPlanApproval(plan_id=plan.id, approver_id=uuid.UUID(principal.user_id), decision="approve" if approve else "reject", plan_hash=plan.plan_hash, comment=comment[:2000]))
    plan.status = "approved" if approve else "rejected"
    if not approve:
        plan.finished_at = datetime.now(timezone.utc)
    await audit.record(db, action="secrets.approve" if approve else "secrets.reject", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=_namespaces(mappings.values()), decision="allow",
                       details={"hash": plan.plan_hash, "comment": comment[:500], "self_approved": own, "kind": plan.kind})
    return plan


async def queue(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID, presented_hash: str) -> SecretPlan:
    plan = await load_plan(db, plan_id, env, lock=True)
    items = await items_of(db, plan)
    mappings = await plan_mappings(db, plan)
    for i in items:
        require(principal, P.SECRETS_EXECUTE, env, mappings[i.mapping_id].namespace)
    if plan.status != "approved":
        raise SyncError(409, f"plan is {plan.status}; only an approved plan runs" + (" (it needs someone else's approval first)" if plan.status == "pending" else ""))
    if _expired(plan):
        plan.status = "expired"
        raise SyncError(409, "plan expired; refresh it to compare again")
    if presented_hash != plan.plan_hash or compute_hash(plan, env.name, items) != plan.plan_hash:
        POLICY_DENIALS.labels("plan_hash").inc()
        raise SyncError(409, "plan hash mismatch; the plan changed after review")
    plan.status, plan.executed_by, plan.queued_at = "queued", uuid.UUID(principal.user_id), datetime.now(timezone.utc)
    await audit.record(db, action="secrets.execute_request", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan",
                       resource_id=str(plan.id), environment=env.name, namespace=_namespaces(mappings.values()), decision="allow", details={"hash": plan.plan_hash})
    return plan


async def retry(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID, item_ids: list[uuid.UUID] | None) -> tuple[SecretPlan, int]:
    """Failed items run again under the same approval, while it is valid. The executor re-reads both sides first:
    anything that moved makes the item stale, and a stale item needs a fresh comparison and approval."""
    plan = await load_plan(db, plan_id, env, lock=True)
    if plan.status not in ("partial", "failed"):
        raise SyncError(409, f"plan is {plan.status}; only a partial or failed plan has items to retry")
    if _expired(plan):
        raise SyncError(409, "the approval for this plan expired; refresh it to compare and approve again")
    items = await items_of(db, plan)
    mappings = await plan_mappings(db, plan)
    for i in items:
        require(principal, P.SECRETS_EXECUTE, env, mappings[i.mapping_id].namespace)
    wanted = {uuid.UUID(str(x)) for x in item_ids} if item_ids else None
    again = [i for i in items if i.included and i.status == "failed" and (wanted is None or i.id in wanted)]
    if not again:
        raise SyncError(409, "no failed items to retry (stale items need a refreshed plan)")
    for i in again:
        i.status, i.error = "pending", None
    plan.status, plan.executed_by, plan.queued_at, plan.finished_at = "queued", uuid.UUID(principal.user_id), datetime.now(timezone.utc), None
    await audit.record(db, action="secrets.retry", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan", resource_id=str(plan.id),
                       environment=env.name, namespace=_namespaces(mappings.values()), decision="allow", details={"items": [str(i.id) for i in again]})
    return plan, len(again)


async def cancel(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID) -> SecretPlan:
    plan = await load_plan(db, plan_id, env, lock=True)
    if plan.status not in ("comparing", "draft", "pending", "approved"):
        raise SyncError(409, f"plan is {plan.status}")
    mappings = await plan_mappings(db, plan)
    if str(plan.requested_by) != principal.user_id:
        for m in mappings.values():
            require(principal, P.SECRETS_APPROVE, env, m.namespace)
    plan.status, plan.finished_at = "cancelled", datetime.now(timezone.utc)
    await audit.record(db, action="secrets.plan_cancel", actor_id=principal.user_id, actor_email=principal.email, resource_type="secret_plan", resource_id=str(plan.id),
                       environment=env.name, namespace=_namespaces(mappings.values()), decision="allow")
    return plan


async def refresh(db: AsyncSession, principal: Principal, env: Environment, plan_id: uuid.UUID) -> SecretPlan:
    """A new plan for the same mappings, compared afresh. The old plan keeps its history."""
    old = await load_plan(db, plan_id, env)
    req = old.request or {}
    return await create_plan(db, principal, env, kind=old.kind, mapping_ids=[uuid.UUID(i) for i in req.get("mapping_ids") or []], reason=old.reason,
                             rollback=req.get("rollback"), targets=req.get("targets"), source_plan_id=old.id)


# ---- executor-side re-check -----------------------------------------------------------------------------------


async def verify_for_execution(db: AsyncSession, plan: SecretPlan, env: Environment, items: list[SecretPlanItem], mappings: dict[uuid.UUID, SecretMapping]) -> None:
    """Everything the approval relied on, checked again by the executor against current permissions."""
    if plan.hash_version != HASH_VERSION:
        raise SyncError(409, "plan was hashed by an older version; refresh it")
    if _expired(plan):
        raise SyncError(409, "plan expired before execution")
    if compute_hash(plan, env.name, items) != plan.plan_hash:
        raise SyncError(409, "plan hash mismatch at execution")
    namespaces = {m.namespace for m in mappings.values()}
    try:
        requester = await principal_of(db, plan.requested_by)
        for ns in namespaces:
            require(requester, P.SECRETS_PROPOSE, env, ns)
    except Exception as exc:
        raise SyncError(403, f"the requester may no longer propose this plan: {getattr(exc, 'message', exc)}") from None
    executor = await principal_of(db, plan.executed_by)
    for ns in namespaces:
        require(executor, P.SECRETS_EXECUTE, env, ns)
    if (plan.request or {}).get("schedule_id"):
        await _scheduled_approval(db, plan, env, mappings)
        return
    if not plan.requires_approval:
        if plan.kind == "delete" or not is_admin(requester):
            raise SyncError(409, "this plan now needs an approval (the requester is no longer an administrator); refresh it")
        return
    approvals = (await db.execute(select(SecretPlanApproval).where(SecretPlanApproval.plan_id == plan.id, SecretPlanApproval.decision == "approve"))).scalars().all()
    for a in approvals:
        try:
            approver = await principal_of(db, a.approver_id)
            own = a.approver_id == plan.requested_by
            if a.plan_hash != plan.plan_hash or (own and (plan.kind == "delete" or not is_admin(approver))):
                continue
            for ns in namespaces:
                require(approver, P.SECRETS_APPROVE, env, ns)
            return
        except Exception:
            continue
    raise SyncError(403, "no valid approval bound to this plan (approver changed, revoked, or hash differs)")


async def _scheduled_approval(db: AsyncSession, plan: SecretPlan, env: Environment, mappings: dict[uuid.UUID, SecretMapping]) -> None:
    """A scheduled run's approval is the schedule's: still valid, for the hash the run was made under, and covering
    every mapping in the plan, which the schedule's creator requested."""
    from . import schedules
    from ..models import SecretSchedule

    s = await db.get(SecretSchedule, uuid.UUID(plan.request["schedule_id"]))
    if s is None or s.environment_id != env.id:
        raise SyncError(403, "the schedule behind this run is gone")
    await schedules.check(db, s, env)
    if plan.request.get("schedule_hash") != s.schedule_hash or plan.requested_by != s.requested_by:
        raise SyncError(403, "the schedule changed after this run started")
    if not {str(m) for m in mappings} <= set(s.mapping_ids or []):
        raise SyncError(403, "this run includes a mapping the schedule does not cover")
