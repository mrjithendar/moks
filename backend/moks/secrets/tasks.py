"""Executor jobs: secret_discover, secret_compare and secret_execute (job class "secrets", claimed only by the
executor process). Values exist here only inside one function call and are never returned, stored or logged.

Execution, per item, under a lease on its mapping (one MOKS operation per mapping at a time):
  1. The mapping's version and baseline must be the ones the plan was compared against.
  2. Both sides are read again. A side that changed since the comparison makes the item stale, unless this item's
     own earlier attempt is what changed it (its write state says so and the content is what was approved).
  3. The content to write is rebuilt from the sources and must have the approved digest.
  4. Each destination is written once: skipped when it already holds that content, so no redundant AWS version is
     created. AWS updates are staged and then swapped conditionally (see aws.py); Kubernetes updates carry the
     approved resourceVersion. The intent is committed before the call, so an ambiguous outcome (timeout,
     connection lost) is resolved on retry by reading what landed, never by writing blindly again.
  5. The destination is read back and must hold the approved digest. Only then does the baseline move, with
     compare-and-set on the mapping's version and baseline sequence.

The race that remains: AWS has no precondition on PutSecretValue, so a client outside MOKS can write between the
staged put and the conditional swap. The swap then fails and the item is stale, but that client's own write stays.
Kubernetes is fully conditional. docs/SECRETS_SYNC.md describes both.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import delete as sql_delete, select, text

from .. import audit
from ..config import get_settings
from ..connectors.base import ConnectorError
from ..db import session_scope
from ..models import Connector, Environment, SecretInventory, SecretInventoryScope, SecretMapping, SecretPlan, SecretPlanItem
from ..observability import span
from . import compare, service
from .aws import UNCERTAIN, AwsSecretsConnector, SecretMeta
from .content import AwsValue, Content, FormatError, assemble, content_digest, from_aws, key_id, summarize, to_aws
from .kube import SecretsKube, consumers_of
from .ownership import MAPPING_TAG, MOKS_TAG, aws_manager

log = logging.getLogger("moks.secrets")


async def _default_backends(kube_row: Connector | None, aws_row: Connector | None) -> tuple[Any, Any]:
    kube = SecretsKube(kube_row.config or {}, kube_row.credential_ref) if kube_row is not None else None
    aws = AwsSecretsConnector(aws_row.config or {}, aws_row.credential_ref) if aws_row is not None else None
    return kube, aws


# Tests replace this with in-memory Kubernetes and a fake Secrets Manager client.
backends_factory: Callable[..., Any] = _default_backends


def digest_key() -> bytes:
    key = get_settings().secrets_digest_key
    if len(key) < 32:
        raise ConnectorError("config", "MOKS_SECRETS_DIGEST_KEY is not set (32+ characters) in the executor; comparisons cannot run")
    return key.encode()


class Backends:
    """One client per connector for the length of a job."""

    def __init__(self) -> None:
        self._built: dict[tuple, tuple[Any, Any]] = {}

    async def get(self, kube_row: Connector | None, aws_row: Connector | None) -> tuple[Any, Any]:
        key = (kube_row.id if kube_row else None, aws_row.id if aws_row else None)
        if key not in self._built:
            self._built[key] = await backends_factory(kube_row, aws_row)
        return self._built[key]

    async def close(self) -> None:
        for kube, aws in self._built.values():
            for c in (kube, aws):
                if c is not None:
                    try:
                        await c.aclose()
                    except Exception:
                        pass


def _err(exc: Exception) -> str:
    return getattr(exc, "message", None) or f"{type(exc).__name__}"


# ---- discovery ----------------------------------------------------------------------------------------------


async def discover(payload: dict[str, Any]) -> dict[str, Any]:
    env_id = uuid.UUID(payload["environment_id"])
    job_id = uuid.UUID(str(payload["_job_id"])) if payload.get("_job_id") else None
    scopes: list[dict[str, Any]] = []
    async with session_scope() as db:
        env = await db.get(Environment, env_id)
        kube_row = await db.get(Connector, uuid.UUID(payload["kube_connector_id"])) if payload.get("kube_connector_id") else None
        aws_row = await db.get(Connector, uuid.UUID(payload["aws_connector_id"])) if payload.get("aws_connector_id") else None
    backends = Backends()
    try:
        kube, aws = await backends.get(kube_row, aws_row)
        if kube is not None:
            try:
                uid = await kube.cluster_uid()
            except ConnectorError as exc:
                uid = None
                for ns in payload.get("namespaces") or []:
                    scopes.append(await _store_scope(env_id, "kubernetes", kube_row.id, ns, [], False, _err(exc), job_id, None))
            if uid:
                for ns in payload.get("namespaces") or []:
                    with span("secrets.discover.kubernetes", **{"moks.namespace": ns}):
                        try:
                            rows, complete = await kube.list(ns)
                            scopes.append(await _store_scope(env_id, "kubernetes", kube_row.id, ns, [_kube_row(r) for r in rows], complete, None, job_id, uid))
                        except ConnectorError as exc:
                            scopes.append(await _store_scope(env_id, "kubernetes", kube_row.id, ns, [], False, _err(exc), job_id, uid))
        if aws is not None:
            key = f"{aws.account_id}/{aws.region}"
            with span("secrets.discover.aws", **{"moks.aws.region": aws.region}):
                try:
                    metas, complete, error = await aws.list_secrets(get_settings().secrets_discovery_max_pages)
                    scopes.append(await _store_scope(env_id, "aws", aws_row.id, key, [_aws_row(m) for m in metas], complete, error, job_id, None))
                except ConnectorError as exc:
                    scopes.append(await _store_scope(env_id, "aws", aws_row.id, key, [], False, _err(exc), job_id, None))
    finally:
        await backends.close()
    async with session_scope() as db:
        await audit.record(db, action="secrets.discover", actor_id=payload.get("requested_by"), resource_type="secret_inventory", resource_id=str(job_id),
                           environment=env.name, namespace="*", decision="allow", outcome="success" if all(s["complete"] for s in scopes) else "partial",
                           details={"scopes": scopes})
    return {"scopes": scopes}


def _kube_row(r: dict[str, Any]) -> dict[str, Any]:
    owner = r["owner"]
    return {"name": r["name"], "namespace": r["namespace"], "type": r["type"], "key_names": r["keys"], "owner": {"manager": owner["manager"], "detail": owner["detail"]},
            "syncable": owner["syncable"], "excluded": owner["excluded"], "last_modified": r["last_modified"], "version": r["resource_version"],
            "extra": {"uid": r["uid"], "immutable": r["immutable"], "last_applied": r["last_applied"]}}


def _aws_row(m: SecretMeta) -> dict[str, Any]:
    owner = aws_manager(m.out())
    return {"name": m.name, "namespace": None, "arn": m.arn, "type": None, "key_names": None, "owner": {"manager": owner["manager"], "detail": owner["detail"]},
            "syncable": m.deleted_date is None, "excluded": "scheduled for deletion" if m.deleted_date else None, "last_modified": m.last_changed,
            "version": m.version_id, "tags": m.tags, "extra": {"kms_key_id": m.kms_key_id, "rotation_enabled": m.rotation_enabled, "deleted_date": m.deleted_date}}


async def _store_scope(env_id: uuid.UUID, side: str, connector_id: uuid.UUID, scope_key: str, rows: list[dict[str, Any]], complete: bool, error: str | None,
                       job_id: uuid.UUID | None, cluster_uid: str | None) -> dict[str, Any]:
    """A complete listing replaces the scope; a partial one adds and updates, and keeps rows it did not reach."""
    async with session_scope() as db:
        if complete:
            await db.execute(sql_delete(SecretInventory).where(SecretInventory.environment_id == env_id, SecretInventory.side == side,
                                                               SecretInventory.connector_id == connector_id, SecretInventory.scope_key == scope_key))
        else:
            names = [r["name"] for r in rows]
            if names:
                await db.execute(sql_delete(SecretInventory).where(SecretInventory.environment_id == env_id, SecretInventory.side == side, SecretInventory.connector_id == connector_id,
                                                                   SecretInventory.scope_key == scope_key, SecretInventory.name.in_(names)))
        for r in rows:
            db.add(SecretInventory(environment_id=env_id, side=side, connector_id=connector_id, scope_key=scope_key, cluster_uid=cluster_uid, job_id=job_id,
                                   **{k: r.get(k) for k in ("name", "namespace", "arn", "type", "key_names", "owner", "syncable", "excluded", "last_modified", "version")},
                                   tags=r.get("tags") or {}, extra=r.get("extra") or {}))
        scope = (await db.execute(select(SecretInventoryScope).where(SecretInventoryScope.environment_id == env_id, SecretInventoryScope.side == side,
                                                                     SecretInventoryScope.connector_id == connector_id, SecretInventoryScope.scope_key == scope_key))).scalar_one_or_none()
        if scope is None:
            scope = SecretInventoryScope(environment_id=env_id, side=side, connector_id=connector_id, scope_key=scope_key)
            db.add(scope)
        scope.complete, scope.count, scope.error, scope.job_id, scope.discovered_at = complete, len(rows), error, job_id, datetime.now(timezone.utc)
        if cluster_uid:
            scope.cluster_uid = cluster_uid
    return {"side": side, "scope": scope_key, "count": len(rows), "complete": complete, "error": error}


# ---- reading one side -------------------------------------------------------------------------------------


async def read_kube(kube: SecretsKube, m: SecretMapping, dkey: bytes) -> tuple[dict[str, Any], Content | None]:
    try:
        got = await kube.get(m.namespace, m.secret_name)
    except ConnectorError as exc:
        return {"exists": None, "error": _err(exc)}, None
    if got is None:
        return {"exists": False}, None
    meta, content = got
    keys = summarize(dkey, content)
    state = {"exists": True, "uid": meta["uid"], "resource_version": meta["resource_version"], "type": content.type, "keys": keys,
             "digest": content_digest(content.type, keys), "owner": meta["owner"], "immutable": meta["immutable"], "last_applied": meta["last_applied"],
             "last_modified": meta["last_modified"]}
    if meta["owner"].get("excluded"):
        state["unsupported"] = f"not syncable: {meta['owner']['excluded']}"
    elif m.payload_format != "moks-k8s-v1" and content.type != m.k8s_type:
        state["unsupported"] = f"the Secret is {content.type} but the mapping says {m.k8s_type}, and the {m.payload_format} format does not store the type; edit the mapping"
    return state, content


async def read_aws(aws: AwsSecretsConnector, m: SecretMapping, dkey: bytes, version_id: str | None = None) -> tuple[dict[str, Any], Content | None, SecretMeta | None, AwsValue | None]:
    try:
        meta = await aws.describe(m.aws_secret_name)
        if meta is None:
            return {"exists": False, "name": m.aws_secret_name}, None, None, None
        state: dict[str, Any] = {"exists": True, "arn": meta.arn, "name": meta.name, "version_id": meta.version_id, "kms_key_id": meta.kms_key_id,
                                 "rotation_enabled": meta.rotation_enabled, "deleted_date": meta.deleted_date, "last_modified": meta.last_changed,
                                 "owner": {k: v for k, v in aws_manager(meta.out()).items() if k in ("manager", "detail")}}
        if meta.deleted_date:
            return {**state, "keys": {}, "digest": None, "unsupported": "the AWS secret is scheduled for deletion"}, None, meta, None
        value = await aws.get_value(meta, version_id=version_id) if version_id else await aws.get_value(meta)
    except ConnectorError as exc:
        return {"exists": None, "error": _err(exc), "name": m.aws_secret_name}, None, None, None
    try:
        content = from_aws(value, m.payload_format, m.key_mapping, m.k8s_type)
    except FormatError as exc:
        return {**state, "version_id": value.version_id, "keys": {}, "digest": None, "unsupported": exc.message, "unsupported_keys": exc.keys}, None, meta, value
    keys = summarize(dkey, content)
    return {**state, "version_id": value.version_id, "type": content.type, "keys": keys, "digest": content_digest(content.type, keys)}, content, meta, value


# ---- comparison ---------------------------------------------------------------------------------------------


async def compare_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = uuid.UUID(payload["plan_id"])
    async with session_scope() as db:
        plan = await db.get(SecretPlan, plan_id)
        if plan is None or plan.status != "comparing":
            return {"skipped": "plan is not waiting for a comparison"}
        env = await db.get(Environment, plan.environment_id)
        items = await service.items_of(db, plan)
        mappings = await service.plan_mappings(db, plan)
        rows = {r.id: r for r in (await db.execute(select(Connector).where(Connector.id.in_(
            {m.kube_connector_id for m in mappings.values()} | {m.aws_connector_id for m in mappings.values()})))).scalars().all()}
        kind, request = plan.kind, dict(plan.request or {})
    try:
        dkey = digest_key()
    except ConnectorError as exc:
        async with session_scope() as db:
            plan = await db.get(SecretPlan, plan_id)
            plan.status, plan.error, plan.finished_at = "compare_failed", exc.message, datetime.now(timezone.utc)
        return {"error": exc.message}
    kid = key_id(dkey)
    backends = Backends()
    results: dict[uuid.UUID, dict[str, Any]] = {}
    try:
        for item in items:
            m = mappings[item.mapping_id]
            with span("secrets.compare.item", **{"moks.namespace": m.namespace}):
                results[item.id] = await _compare_item(backends, rows, m, item, kind, request, dkey, kid)
    finally:
        await backends.close()
    counts: dict[str, int] = {}
    audit_items = []
    async with session_scope() as db:
        plan = await db.get(SecretPlan, plan_id, with_for_update=True)
        if plan.status != "comparing":
            return {"skipped": f"plan became {plan.status} during the comparison"}
        for item in await service.items_of(db, plan):
            r = results[item.id]
            m = await db.get(SecretMapping, item.mapping_id)
            for k in ("classification", "first_sync", "proposal", "kube_state", "aws_state", "source_state", "baseline", "impact", "rollback_version", "baseline_seq"):
                if k in r:
                    setattr(item, k, r[k])
            item.warnings = [f"compare: {w}" for w in r.get("warnings", [])]
            if r.get("stale"):
                item.status, item.error, item.blocked = "stale", r["stale"], r["stale"]
            else:
                service.rederive(item, m, kind, request, AwsSecretsConnector(rows[m.aws_connector_id].config or {}).allow_delete)
                item.error = r.get("reason")
            counts[item.classification or "stale"] = counts.get(item.classification or "stale", 0) + 1
            if m.version == item.mapping_version and kind == "sync" and item.classification:
                m.last_status, m.last_compared_at = item.classification, datetime.now(timezone.utc)
            audit_items.append({"mapping": str(m.id), "secret": f"{m.namespace}/{m.secret_name}", "aws": m.aws_secret_name, "classification": item.classification,
                                "kube_version": (item.kube_state or {}).get("resource_version"), "aws_version": (item.aws_state or {}).get("version_id"),
                                "keys": sorted(set((item.kube_state or {}).get("keys") or {}) | set((item.aws_state or {}).get("keys") or {}))})
        plan.status, plan.summary = "draft", {**(plan.summary or {}), "classifications": counts}
        plan.version += 1
        await audit.record(db, action="secrets.compare", actor_id=plan.requested_by, resource_type="secret_plan", resource_id=str(plan.id), environment=env.name,
                           namespace="*", decision="allow", details={"kind": kind, "items": audit_items})
    return {"classifications": counts}


async def _compare_item(backends: Backends, rows: dict, m: SecretMapping, item: SecretPlanItem, kind: str, request: dict[str, Any], dkey: bytes, kid: str) -> dict[str, Any]:
    if m.status != "active" or m.version != item.mapping_version:
        return {"stale": "the mapping changed after this plan was proposed; refresh the plan"}
    kube_row, aws_row = rows.get(m.kube_connector_id), rows.get(m.aws_connector_id)
    if kube_row is None or aws_row is None or not kube_row.enabled or not aws_row.enabled:
        return {"classification": "failed", "kube_state": {"error": "connector removed or disabled"}, "aws_state": {}, "reason": "a connector of this mapping is gone or disabled"}
    try:
        kube, aws = await backends.get(kube_row, aws_row)
        uid = await kube.cluster_uid()
    except ConnectorError as exc:
        return {"classification": "failed", "kube_state": {"error": _err(exc)}, "aws_state": {}, "reason": _err(exc)}
    if uid != m.cluster_uid:
        reason = "the Kubernetes connector now points at a different cluster than this mapping was made for"
        return {"classification": "failed", "kube_state": {"error": reason}, "aws_state": {}, "reason": reason}
    if aws.account_id != m.aws_account or aws.region != m.aws_region:
        reason = "the AWS connector now points at a different account or region than this mapping was made for"
        return {"classification": "failed", "kube_state": {}, "aws_state": {"error": reason}, "reason": reason}
    kube_state, _ = await read_kube(kube, m, dkey)
    aws_state, _, _, _ = await read_aws(aws, m, dkey)
    out: dict[str, Any] = {"kube_state": kube_state, "aws_state": aws_state, "warnings": []}
    baseline = m.baseline
    if baseline and baseline.get("kid") != kid:
        out["warnings"].append("the digest key changed since the last sync, so the old baseline no longer applies")
    if baseline and kube_state.get("exists") and baseline.get("kube_uid") and baseline["kube_uid"] != kube_state.get("uid"):
        out["warnings"].append("the Kubernetes Secret was deleted and recreated since the last sync")
    if m.aws_secret_arn and aws_state.get("exists") and aws_state.get("arn") != m.aws_secret_arn:
        out["warnings"].append("the AWS secret was deleted and recreated since the last sync (its ARN changed)")
    for side, st in (("kubernetes", kube_state), ("aws", aws_state)):
        found = (st.get("owner") or {}).get("manager") if st.get("exists") else None
        expected = (m.expected_owner or {}).get(side) or "none"
        if found not in (None, "moks") and expected != found:
            out["warnings"].append(f"{side}: {found} manages this secret, but the mapping expects {expected}")
    if kind == "rollback":
        spec = (request.get("rollback") or {}).get(str(m.id)) or {}
        wanted = spec.get("version") or "AWSPREVIOUS"
        source, version = {"exists": False}, None
        try:
            meta = await aws.describe(m.aws_secret_name)
            if meta is not None:
                if wanted in ("AWSPREVIOUS", "AWSCURRENT"):
                    version = next((v["version_id"] for v in await aws.versions(meta) if wanted in v["stages"]), None)
                else:
                    version = wanted
                if version:
                    st, _, _, _ = await read_aws(aws, m, dkey, version_id=version)
                    source = {**st, "version_id": version}
        except ConnectorError as exc:
            source = {"exists": None, "error": _err(exc)}
        out.update(source_state=source, rollback_version=version or wanted)
        if source.get("unsupported"):
            source["exists"] = False
        out["classification"] = "failed" if source.get("error") else ("in_sync" if source.get("digest") and source["digest"] == aws_state.get("digest") else "changed_aws")
        out["reason"] = source.get("error") or (None if source.get("exists") else f"version {wanted} could not be read")
    elif kind == "delete":
        cls, first, proposal, reason = compare.classify(kube_state, aws_state, baseline, kid)
        out.update(classification=cls, first_sync=first, proposal=proposal, reason=reason)
    else:
        cls, first, proposal, reason = compare.classify(kube_state, aws_state, baseline, kid)
        out.update(classification=cls, first_sync=first, proposal=proposal, reason=reason)
        if first and cls == "changed_both":
            out["warnings"].append("first sync: there is no verified baseline yet and the two sides differ, so choose the source explicitly")
        if cls == "in_sync":  # an observed agreement is as good as a verified sync
            seq = await _adopt_baseline(m, item, kube_state, aws_state, kid)
            if seq is not None:
                out["baseline_seq"] = seq
    if kind != "delete" and (m.direction in ("import", "both") or kind == "rollback") and kube_state.get("exists") is not None:
        try:
            out["impact"] = consumers_of(m.secret_name, await kube.workloads(m.namespace))
        except ConnectorError:
            out["impact"] = []
    out["baseline"] = baseline
    return out


async def _adopt_baseline(m: SecretMapping, item: SecretPlanItem, kube_state: dict, aws_state: dict, kid: str) -> int | None:
    if m.baseline and m.baseline.get("kid") == kid and m.baseline.get("digest") == kube_state["digest"] and \
            m.baseline.get("kube_rv") == kube_state.get("resource_version") and m.baseline.get("aws_version") == aws_state.get("version_id"):
        return None
    baseline = {"kid": kid, "digest": kube_state["digest"], "keys": kube_state["keys"], "type": kube_state["type"], "kube_uid": kube_state.get("uid"),
                "kube_rv": kube_state.get("resource_version"), "aws_version": aws_state.get("version_id"), "aws_arn": aws_state.get("arn"),
                "at": datetime.now(timezone.utc).isoformat(), "source": "observed"}
    return await _cas_baseline(m.id, item.mapping_version, item.baseline_seq, baseline, aws_state.get("arn"), "in_sync")


async def _cas_baseline(mapping_id: uuid.UUID, version: int, seq: int, baseline: dict | None, arn: str | None, status: str) -> int | None:
    import json

    async with session_scope() as db:
        row = (await db.execute(text("""UPDATE secret_mappings SET baseline = CAST(:b AS jsonb), baseline_seq = baseline_seq + 1, last_status = :s,
                                            aws_secret_arn = COALESCE(:arn, aws_secret_arn), last_compared_at = now()
                                        WHERE id = :id AND version = :v AND baseline_seq = :seq AND status = 'active' RETURNING baseline_seq"""),
                                   {"b": json.dumps(baseline) if baseline is not None else None, "s": status, "arn": arn, "id": mapping_id, "v": version, "seq": seq})).first()
    return row[0] if row else None


# ---- execution ----------------------------------------------------------------------------------------------


class Stale(Exception):
    pass


class LeaseBusy(Exception):
    pass


async def _lease(mapping_id: uuid.UUID, owner: uuid.UUID) -> bool:
    async with session_scope() as db:
        row = (await db.execute(text("""UPDATE secret_mappings SET lease_owner = :o, lease_expires_at = now() + make_interval(secs => :s)
                                        WHERE id = :id AND (lease_owner IS NULL OR lease_owner = :o OR lease_expires_at < now()) RETURNING id"""),
                                {"o": owner, "s": get_settings().secrets_lease_seconds, "id": mapping_id})).first()
    return row is not None


async def acquire(mapping_id: uuid.UUID, owner: uuid.UUID) -> None:
    deadline = asyncio.get_running_loop().time() + get_settings().secrets_lease_wait_seconds
    while not await _lease(mapping_id, owner):
        if asyncio.get_running_loop().time() >= deadline:
            raise LeaseBusy("another MOKS operation on this mapping is running; retry when it finishes")
        await asyncio.sleep(0.25)


async def release(mapping_id: uuid.UUID, owner: uuid.UUID) -> None:
    async with session_scope() as db:
        await db.execute(text("UPDATE secret_mappings SET lease_owner = NULL, lease_expires_at = NULL WHERE id = :id AND lease_owner = :o"), {"id": mapping_id, "o": owner})


async def _still_ours(mapping_id: uuid.UUID, owner: uuid.UUID) -> None:
    """Renew right before each write. Losing the lease means someone else may be acting: write nothing."""
    if not await _lease(mapping_id, owner):
        raise LeaseBusy("the mapping lease was lost before writing; nothing was written")


async def _save_item(item_id: uuid.UUID, **values: Any) -> None:
    async with session_scope() as db:
        item = await db.get(SecretPlanItem, item_id, with_for_update=True)
        for k, v in values.items():
            setattr(item, k, v)
        item.updated_at = datetime.now(timezone.utc)


async def _write_state(item_id: uuid.UUID, target: str, state: str, **result: Any) -> None:
    async with session_scope() as db:
        item = await db.get(SecretPlanItem, item_id, with_for_update=True)
        item.write_state = {**(item.write_state or {}), target: state}
        if result:
            item.result = {**(item.result or {}), **result}
        item.updated_at = datetime.now(timezone.utc)


async def execute_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan_id = uuid.UUID(payload["plan_id"])
    owner = uuid.UUID(str(payload.get("_job_id") or uuid.uuid4()))
    async with session_scope() as db:
        plan = await db.get(SecretPlan, plan_id, with_for_update=True)
        if plan is None or plan.status != "queued":
            return {"skipped": "plan is not queued"}
        env = await db.get(Environment, plan.environment_id)
        items = await service.items_of(db, plan)
        mappings = await service.plan_mappings(db, plan)
        try:
            await service.verify_for_execution(db, plan, env, items, mappings)
        except service.SyncError as exc:
            plan.status, plan.error, plan.finished_at = "failed", exc.message, datetime.now(timezone.utc)
            for i in items:
                if i.status == "pending":
                    i.status, i.error = "failed", exc.message
            await audit.record(db, action="secrets.execute", actor_id=plan.executed_by, resource_type="secret_plan", resource_id=str(plan.id),
                               environment=env.name, decision="deny", outcome="failed", details={"error": exc.message})
            return {"error": exc.message}
        plan.status = "executing"
        todo = [i.id for i in service.runnable(items)]
        rows = {r.id: r for r in (await db.execute(select(Connector).where(Connector.id.in_(
            {m.kube_connector_id for m in mappings.values()} | {m.aws_connector_id for m in mappings.values()})))).scalars().all()}
        env_name, executed_by = env.name, plan.executed_by
    backends = Backends()
    outcomes: dict[str, int] = {}
    try:
        dkey = digest_key()
        for item_id in todo:
            with span("secrets.execute.item"):
                status = await _execute_item(backends, rows, item_id, plan_id, owner, dkey, env_name, executed_by)
            outcomes[status] = outcomes.get(status, 0) + 1
    except ConnectorError as exc:  # no digest key: nothing ran
        outcomes["failed"] = len(todo)
        for item_id in todo:
            await _save_item(item_id, status="failed", error=exc.message)
    finally:
        await backends.close()
    async with session_scope() as db:
        plan = await db.get(SecretPlan, plan_id, with_for_update=True)
        items = await service.items_of(db, plan)
        attempted = [i for i in items if i.included and i.targets and i.status in ("succeeded", "failed", "stale", "conflict", "running")]
        done = [i for i in attempted if i.status == "succeeded"]
        plan.status = "succeeded" if attempted and len(done) == len(attempted) else "partial" if done else "failed"
        plan.finished_at = datetime.now(timezone.utc)
        plan.summary = {**(plan.summary or {}), "outcomes": {s: sum(1 for i in attempted if i.status == s) for s in {i.status for i in attempted}}}
        await audit.record(db, action="secrets.execute", actor_id=executed_by, resource_type="secret_plan", resource_id=str(plan.id), environment=env_name,
                           decision="allow", outcome=plan.status, details={"outcomes": plan.summary["outcomes"], "hash": plan.plan_hash})
    return {"outcomes": outcomes}


async def _execute_item(backends: Backends, rows: dict, item_id: uuid.UUID, plan_id: uuid.UUID, owner: uuid.UUID, dkey: bytes, env_name: str, executed_by) -> str:
    async with session_scope() as db:
        item = await db.get(SecretPlanItem, item_id)
        m = await db.get(SecretMapping, item.mapping_id)
        plan = await db.get(SecretPlan, plan_id)
        item.status, item.attempts, item.updated_at = "running", item.attempts + 1, datetime.now(timezone.utc)
        snap = {k: getattr(item, k) for k in ("mapping_version", "baseline_seq", "targets", "actions", "kube_state", "aws_state", "source_state", "expected",
                                               "resolution", "proposal", "key_choices", "rollback_version", "write_state", "idempotency_token", "diff")}
        kind = plan.kind
    status, error = "failed", None
    try:
        await acquire(m.id, owner)
        try:
            await _run_item(backends, rows, m, item_id, snap, kind, owner, dkey)  # it saves its result as it goes
            status = "succeeded"
        finally:
            await release(m.id, owner)
    except Stale as exc:
        status, error = "stale", str(exc)
    except LeaseBusy as exc:
        status, error = "failed", str(exc)
    except FormatError as exc:
        status, error = "failed", exc.message
    except ConnectorError as exc:
        status = "failed"
        error = exc.message + (" (the outcome is not known: retrying first checks what landed)" if exc.kind in UNCERTAIN else "")
    except Exception as exc:  # a bug: say so without the details, which could include anything a library put in them
        log.exception("secret sync item failed", extra={"event": "secret_sync_item_error"})
        status, error = "failed", f"internal error ({type(exc).__name__})"
    await _save_item(item_id, status=status, error=error)
    async with session_scope() as db:
        item = await db.get(SecretPlanItem, item_id)
        await audit.record(db, action="secrets.item_execute", actor_id=executed_by, resource_type="secret_plan_item", resource_id=str(item_id), environment=env_name,
                           namespace=m.namespace, decision="allow", outcome=status, details={**service._item_audit(item, m), "error": error, "result": item.result})
    return status


def _same(now: dict[str, Any], then: dict[str, Any], side: str) -> bool:
    if bool(now.get("exists")) != bool(then.get("exists")):
        return False
    if not now.get("exists"):
        return True
    version = "resource_version" if side == "kubernetes" else "version_id"
    return now.get("digest") == then.get("digest") and now.get(version) == then.get(version)


async def _run_item(backends: Backends, rows: dict, m: SecretMapping, item_id: uuid.UUID, snap: dict[str, Any], kind: str, owner: uuid.UUID, dkey: bytes) -> dict[str, Any]:
    async with session_scope() as db:
        fresh = await db.get(SecretMapping, m.id)
        if fresh.status != "active" or fresh.version != snap["mapping_version"]:
            raise Stale("the mapping was edited after the comparison; refresh the plan")
        if fresh.baseline_seq != snap["baseline_seq"]:
            raise Stale("another sync of this mapping finished after the comparison; refresh the plan")
    kube_row, aws_row = rows.get(m.kube_connector_id), rows.get(m.aws_connector_id)
    if kube_row is None or aws_row is None or not kube_row.enabled or not aws_row.enabled:
        raise Stale("a connector of this mapping was removed or disabled")
    kube, aws = await backends.get(kube_row, aws_row)
    if await kube.cluster_uid() != m.cluster_uid:
        raise Stale("the Kubernetes connector now points at a different cluster")
    write_state = dict(snap["write_state"] or {})
    expected = snap["expected"] or {}

    kube_now, kube_content = await read_kube(kube, m, dkey)
    aws_now, aws_content, aws_meta, aws_value = await read_aws(aws, m, dkey)
    for side, now in (("kubernetes", kube_now), ("aws", aws_now)):
        if now.get("error"):
            raise ConnectorError("unavailable", f"{side} could not be read before writing: {now['error']}", retryable=True)
    for side, now, then in (("kubernetes", kube_now, snap["kube_state"]), ("aws", aws_now, snap["aws_state"])):
        if _same(now, then, side):
            continue
        ours = side in snap["targets"] and write_state.get(side) in ("intent", "applied", "verified") and (
            (expected.get("absent") and not now.get("exists")) or (now.get("exists") and now.get("digest") == expected.get("digest")))
        if not ours:
            raise Stale(f"{side} changed after the comparison; refresh the plan to compare and approve again")

    # what to write, rebuilt from real values and checked against the approved digest
    content: Content | None = None
    if kind == "sync":
        source = snap["resolution"] or snap["proposal"]
        if source == "kubernetes":
            content = kube_content
        elif source == "aws":
            content = aws_content
        elif source == "edited":
            content = assemble(kube_content, aws_content, snap["key_choices"] or {}, expected.get("type") or m.k8s_type)
        # a target that already holds the result was written by an earlier attempt: its content equals expected
        if content is None and expected.get("digest"):
            content = kube_content if kube_now.get("digest") == expected["digest"] else aws_content if aws_now.get("digest") == expected["digest"] else None
    elif kind == "rollback":
        version = snap["rollback_version"]
        if aws_meta is None:
            raise Stale("the AWS secret no longer exists")
        st, content, _, _ = await read_aws(aws, m, dkey, version_id=version)
        if st.get("digest") != (snap["source_state"] or {}).get("digest"):
            raise Stale("the version to roll back to no longer holds what was reviewed")
    if kind != "delete":
        if content is None:
            raise Stale("the source no longer holds the reviewed content")
        got = content_digest(content.type, summarize(dkey, content))
        if got != expected.get("digest"):
            raise Stale("the content to write differs from what was approved")

    result: dict[str, Any] = {"aws_version_before": snap["aws_state"].get("version_id"), "kube_version_before": snap["kube_state"].get("resource_version")}
    token = str(snap["idempotency_token"])
    for target in ("aws", "kubernetes"):
        if target not in snap["targets"] or write_state.get(target) == "verified":
            continue
        action = snap["actions"][target]
        now = aws_now if target == "aws" else kube_now
        holds = (not now.get("exists")) if action == "delete" else (now.get("exists") and now.get("digest") == expected.get("digest") and
                                                                    (action != "restage" or now.get("version_id") == snap["rollback_version"]))
        if holds:
            result[f"{target}_unchanged"] = True  # nothing to write: no redundant version
        else:
            await _still_ours(m.id, owner)
            await _write_state(item_id, target, "intent")
            write_state[target] = "intent"
            try:
                applied = await (_write_aws if target == "aws" else _write_kube)(aws if target == "aws" else kube, m, action, content, snap, aws_meta, aws_value, token)
            except ConnectorError as exc:
                if exc.kind not in UNCERTAIN:
                    await _write_state(item_id, target, "none")  # refused: nothing landed
                    if exc.kind == "conflict":
                        raise Stale(f"{target} changed while writing: {exc.message}") from None
                raise
            result.update(applied)
            await _write_state(item_id, target, "applied", **applied)
        verified = await _verify(aws if target == "aws" else kube, m, target, action, expected, dkey, snap)
        result.update(verified)
        await _write_state(item_id, target, "verified", **result)
        write_state[target] = "verified"

    # the baseline moves only now, after every destination was read back
    if kind == "delete":
        baseline, status = None, "missing_aws" if "aws" in snap["targets"] else "missing_kubernetes"
    else:
        kube_after, _ = await read_kube(kube, m, dkey)
        aws_after, _, after_meta, _ = await read_aws(aws, m, dkey)
        baseline = {"kid": key_id(dkey), "digest": expected["digest"], "keys": expected["keys"], "type": expected["type"], "kube_uid": kube_after.get("uid"),
                    "kube_rv": kube_after.get("resource_version"), "aws_version": aws_after.get("version_id"), "aws_arn": aws_after.get("arn"),
                    "at": datetime.now(timezone.utc).isoformat(), "source": "verified_sync"}
        both = kube_after.get("digest") == expected["digest"] and aws_after.get("digest") == expected["digest"]
        status = "in_sync" if both else "changed_kubernetes" if aws_after.get("digest") == expected["digest"] else "changed_aws"
        if not both:
            baseline = None  # a rollback of one side leaves them different; there is no agreement to record
    seq = await _cas_baseline(m.id, snap["mapping_version"], snap["baseline_seq"], baseline, result.get("aws_arn"), status) if baseline is not None or kind == "delete" else None
    if seq is None and (baseline is not None or kind == "delete"):
        raise ConnectorError("conflict", "written and verified, but the mapping was edited or synced meanwhile, so the baseline was not advanced; compare again")
    result["baseline_seq"] = seq
    await _save_item(item_id, result=result)
    return result


async def _write_aws(aws: AwsSecretsConnector, m: SecretMapping, action: str, content: Content | None, snap: dict, meta: SecretMeta | None,
                     current: AwsValue | None, token: str) -> dict[str, Any]:
    if action == "delete":
        return {"aws_deletion_date": await aws.delete(meta)}
    if action == "restage":
        await aws.restage(meta, snap["rollback_version"], snap["aws_state"]["version_id"])
        return {"aws_version_after": snap["rollback_version"]}
    value = to_aws(content, m.payload_format, m.key_mapping, current)
    if action == "create":
        if meta is not None:
            raise ConnectorError("conflict", "the AWS secret appeared after the comparison")
        try:
            meta = await aws.create(m.aws_secret_name, value, token, {MOKS_TAG: "moks", MAPPING_TAG: str(m.id)},
                                    f"Synced by MOKS from Kubernetes {m.cluster_name}/{m.namespace}/{m.secret_name}")
        except ConnectorError as exc:
            if exc.kind != "conflict":
                raise
            meta = await aws.describe(m.aws_secret_name)  # an earlier attempt may have created it: only ours counts
            if meta is None or meta.tags.get(MAPPING_TAG) != str(m.id) or meta.version_id != token:
                raise
        return {"aws_version_after": meta.version_id, "aws_arn": meta.arn}
    vid = await aws.put_pending(meta, value, token)
    try:
        await aws.promote(meta, vid, snap["aws_state"]["version_id"])
    except ConnectorError as exc:
        if exc.kind == "conflict":
            try:
                await aws.clear_pending(meta, vid)
            except ConnectorError:
                pass
        raise
    return {"aws_version_after": vid, "aws_arn": meta.arn}


async def _write_kube(kube: SecretsKube, m: SecretMapping, action: str, content: Content | None, snap: dict, meta, current, token: str) -> dict[str, Any]:
    ks = snap["kube_state"]
    if action == "delete":
        await kube.delete(m.namespace, m.secret_name, ks["uid"], ks["resource_version"])
        return {"kube_deleted": True}
    aws_version = (snap["aws_state"] or {}).get("version_id")
    if action == "create":
        out = await kube.create(m.namespace, m.secret_name, content, str(m.id), aws_version)
    else:
        out = await kube.replace(m.namespace, m.secret_name, content, ks["resource_version"], ks["uid"], str(m.id), aws_version)
    return {"kube_version_after": out["resource_version"], "kube_uid": out["uid"]}


async def _verify(client, m: SecretMapping, target: str, action: str, expected: dict, dkey: bytes, snap: dict) -> dict[str, Any]:
    if target == "aws":
        st, _, meta, _ = await read_aws(client, m, dkey)
        if action == "delete":
            if st.get("exists") and not st.get("deleted_date"):
                raise ConnectorError("conflict", "verification failed: the AWS secret is not scheduled for deletion")
            return {"aws_verified": True}
        if st.get("digest") != expected["digest"]:
            raise ConnectorError("conflict", "verification failed: AWS does not hold the approved content; the baseline was not advanced")
        return {"aws_verified": True, "aws_version_after": st.get("version_id"), "aws_arn": st.get("arn")}
    st, _ = await read_kube(client, m, dkey)
    if action == "delete":
        if st.get("exists"):
            raise ConnectorError("conflict", "verification failed: the Secret still exists")
        return {"kube_verified": True}
    if st.get("digest") != expected["digest"]:
        raise ConnectorError("conflict", "verification failed: Kubernetes does not hold the approved content; the baseline was not advanced")
    return {"kube_verified": True, "kube_version_after": st.get("resource_version")}


async def reconcile_orphans() -> int:
    """Plans whose job is no longer running (the executor stopped, the worker's reaper failed the job) would stay
    comparing or executing forever. Items that were running keep their write state, so a retry resolves them."""
    async with session_scope() as db:
        rows = (await db.execute(text("""SELECT p.id, COALESCE(p.execute_job_id, p.compare_job_id) FROM secret_plans p
                                         LEFT JOIN jobs j ON j.id = CASE WHEN p.status = 'comparing' THEN p.compare_job_id ELSE p.execute_job_id END
                                         WHERE p.status IN ('comparing', 'queued', 'executing') AND (j.id IS NULL OR j.status NOT IN ('queued', 'running'))
                                           AND COALESCE(p.queued_at, p.created_at) < now() - interval '30 seconds'"""))).all()
    for plan_id, job_id in rows:
        async with session_scope() as db:
            plan = await db.get(SecretPlan, plan_id, with_for_update=True)
            if plan is None or plan.status not in ("comparing", "queued", "executing"):
                continue
            if plan.status == "comparing":
                plan.status, plan.error = "compare_failed", "the comparison stopped before it finished; refresh the plan"
            else:
                items = await service.items_of(db, plan)
                for i in items:
                    if i.status == "running":
                        i.status, i.error = "failed", "the executor stopped while writing; the outcome is not known: retrying first checks what landed"
                plan.status = "partial" if any(i.status == "succeeded" for i in items) else "failed"
                plan.error = "the executor stopped before the plan finished"
            plan.finished_at = datetime.now(timezone.utc)
            if job_id:
                await db.execute(text("UPDATE secret_mappings SET lease_owner = NULL, lease_expires_at = NULL WHERE lease_owner = :o"), {"o": job_id})
    return len(rows)


# ---- scheduled discovery ------------------------------------------------------------------------------------

_last_auto: float | None = None


async def schedule_discovery() -> int:
    """Every secrets_discovery_interval_minutes (and once when the executor starts), queue a read-only listing of the
    secretsSync namespaces for each cluster with a write target, and of each AWS connector. Only discovery: it
    never compares or syncs. Returns the number of jobs queued; a listing already queued or running is not repeated."""
    global _last_auto
    import time

    from .. import jobs

    s = get_settings()
    if not s.secrets_discovery_interval_minutes or (_last_auto is not None and time.monotonic() - _last_auto < s.secrets_discovery_interval_minutes * 60):
        return 0
    _last_auto = time.monotonic()
    async with session_scope() as db:
        rows = (await db.execute(select(Connector).where(Connector.enabled.is_(True), Connector.type.in_(("kubernetes", "aws_secrets"))))).scalars().all()
    queued = 0
    for env_id in {r.environment_id for r in rows}:
        kubes = [r for r in rows if r.environment_id == env_id and r.type == "kubernetes" and (r.config or {}).get("executor")]
        scopes = []
        for r in rows:
            if r.environment_id == env_id and r.type == "aws_secrets":
                try:
                    scopes.append((r, service.aws_scope(r)))
                except ConnectorError as exc:
                    log.warning("aws_secrets connector skipped by scheduled discovery", extra={"event": "secrets_auto_discover_skipped", "error": exc.message})
        if not scopes:
            continue  # no namespace is synced in this environment
        namespaces = [ns for ns in s.secrets_namespace_list if any(a.namespace_allowed(ns) for _, a in scopes)]
        base = {"environment_id": str(env_id), "requested_by": None, "trigger": "schedule"}
        work = [({**base, "kube_connector_id": str(k.id), "namespaces": namespaces}, f"secret_discover:auto:{k.id}") for k in kubes if namespaces]
        work += [({**base, "aws_connector_id": str(r.id), "namespaces": []}, f"secret_discover:auto:{r.id}") for r, _ in scopes]
        for payload, key in work:
            try:
                queued += await jobs.enqueue("secret_discover", payload, dedupe_key=key) is not None
            except jobs.QueueFull:
                log.warning("secrets queue full; scheduled discovery skipped", extra={"event": "secrets_auto_discover_skipped"})
                return queued
    return queued
