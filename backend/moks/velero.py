"""Cluster backups: Velero does the work, Cloudflare R2 holds the data, MOKS is the control plane.

Velero runs in each cluster and backs up every API object, Secrets included, plus PersistentVolume
contents through its node agent. MOKS never reads a Secret: its read role covers only Velero's own
objects, so nothing in an investigation, a tool result or an LLM prompt can contain Secret data.

Every write (location, schedule, backup, restore) is an action plan, so it has the same RBAC,
approval, hash binding, cluster identity check and audit trail as any other change. Only the
executor writes, and on Secrets it can only create or overwrite: it has no get or list.

What reaches R2 is encrypted with a key R2 does not keep (SSE-C), and volume data with the Kopia
repository password. Both keys must exist outside the cluster: losing them loses the backups.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .backup import BackupError, load_key
from .config import get_settings
from .connectors.base import ConnectorError, resolve_credential
from .models import BackupStore, Environment

GROUP = "velero.io"
VERSION = "v1"
LOCATION = "moks-r2"
CREDENTIALS_SECRET = "moks-r2-credentials"
SSE_SECRET = "moks-r2-sse"
REPO_SECRET = "velero-repo-credentials"  # Velero falls back to a public static password without it
KINDS = {"BackupStorageLocation", "Backup", "Schedule", "Restore"}
ACTIONS = {"velero_configure", "velero_backup", "velero_schedule", "velero_restore"}
CREDENTIAL_KEYS = ("access_key_id", "secret_access_key", "sse_key", "repo_password")
DONE = {"Completed", "PartiallyFailed", "Failed", "FailedValidation"}
NS_PATTERN = re.compile(r"^(\*|[a-z0-9*?]([-a-z0-9*?]{0,61}[a-z0-9*?])?)$")
STALE_HOURS = 26
KEEP_DAYS = 3650  # nothing is removed automatically unless someone chooses a shorter retention


def namespace() -> str:
    return get_settings().velero_namespace


def endpoint(store: BackupStore) -> str:
    region = ".eu" if store.jurisdiction == "eu" else ""
    return f"https://{store.account_id}{region}.r2.cloudflarestorage.com"


def env_prefix(store: BackupStore, env_name: str) -> str:
    return "/".join(p for p in (store.prefix.strip("/"), env_name) if p)


def fingerprint(store: BackupStore) -> str:
    """Binds a reviewed plan to the configuration it showed. Holds references, never values."""
    return hashlib.sha256(json.dumps({"account": store.account_id, "bucket": store.bucket, "prefix": store.prefix, "jurisdiction": store.jurisdiction,
                                      "credentials": store.credentials}, sort_keys=True).encode()).hexdigest()


async def get_store(db: AsyncSession) -> BackupStore | None:
    return (await db.execute(select(BackupStore).where(BackupStore.name == "r2"))).scalar_one_or_none()


# ---- Scope ---------------------------------------------------------------------------------------


def always_excluded() -> set[str]:
    return {namespace()}  # Velero's own namespace holds the R2 keys


def restore_excluded() -> set[str]:
    return always_excluded() | {get_settings().own_namespace}  # restoring over MOKS would stop the restore


def namespaces_for(env: Environment, requested: list[str], never: set[str]) -> tuple[list[str], list[str]]:
    """The environment's namespace policy applied to a request, so the plan shows exactly what runs."""
    allowed = env.allowed_namespaces or ["*"]
    denied = list(env.denied_namespaces or [])
    if "*" in requested and "*" not in allowed:
        included = sorted(set(allowed))
    else:
        included = sorted(set(requested))
    for ns in included:
        if ns != "*" and "*" not in allowed and not any(fnmatchcase(ns, a) for a in allowed):
            raise ConnectorError("forbidden", f"namespace {ns} is outside the {env.name} allowlist")
        if ns != "*" and any(fnmatchcase(ns, d) for d in denied):
            raise ConnectorError("forbidden", f"namespace {ns} is denied in {env.name}")
    return included, sorted(set(denied) | never)


def check_scope(env: Environment, included: list[str], excluded: list[str]) -> None:
    """Re-run at execution: a policy tightened after review stops the plan."""
    allowed = env.allowed_namespaces or ["*"]
    missing = [d for d in (env.denied_namespaces or []) if d not in excluded]
    if missing:
        raise ConnectorError("forbidden", f"{', '.join(missing)} is denied in {env.name} but this plan does not exclude it")
    if "*" in included and "*" not in allowed:
        raise ConnectorError("forbidden", f"{env.name} allows only {', '.join(allowed)}")
    if "*" not in allowed:
        outside = [n for n in included if not any(fnmatchcase(n, a) or n == a for a in allowed)]
        if outside:
            raise ConnectorError("forbidden", f"{', '.join(outside)} is outside the {env.name} allowlist")


# ---- Manifests -----------------------------------------------------------------------------------


def _meta(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "namespace": namespace(), "labels": {"app.kubernetes.io/managed-by": "moks"}, **extra}


def location_body(store: BackupStore, env_name: str) -> dict[str, Any]:
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "BackupStorageLocation", "metadata": _meta(LOCATION), "spec": {
        "provider": "aws", "default": False, "accessMode": "ReadWrite",
        "objectStorage": {"bucket": store.bucket, "prefix": env_prefix(store, env_name)},
        "credential": {"name": CREDENTIALS_SECRET, "key": "cloud"},
        # R2 uses region auto and path-style URLs. An empty checksumAlgorithm is the Velero AWS
        # plugin's setting for S3-compatible stores that reject its default checksum headers.
        "config": {"region": "auto", "s3ForcePathStyle": "true", "s3Url": endpoint(store), "checksumAlgorithm": "",
                   "customerKeyEncryptionSecret": f"{SSE_SECRET}/key"},
    }}


def backup_spec(params: dict[str, Any]) -> dict[str, Any]:
    included = params["included_namespaces"]
    spec: dict[str, Any] = {"includedNamespaces": included, "excludedNamespaces": params["excluded_namespaces"], "storageLocation": LOCATION,
                            "ttl": f"{params['ttl_days'] * 24}h", "snapshotVolumes": False, "defaultVolumesToFsBackup": params["include_volumes"]}
    if "*" in included:
        spec["includeClusterResources"] = True
    return spec


def backup_body(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Backup", "metadata": _meta(name), "spec": backup_spec(params)}


def schedule_body(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Schedule", "metadata": _meta(name),
            "spec": {"schedule": params["cron"], "paused": params["paused"], "useOwnerReferencesInBackup": False, "template": backup_spec(params)}}


def restore_body(name: str, backup: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Restore", "metadata": _meta(name), "spec": {
        "backupName": backup, "includedNamespaces": params["included_namespaces"], "excludedNamespaces": params["excluded_namespaces"],
        "existingResourcePolicy": params["existing_resource_policy"], "restorePVs": params["restore_volumes"]}}


def _single_line(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value or "\n" in value or "\r" in value:
        raise ConnectorError("config", f"{what} is empty or spans several lines")
    return value


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def secret_payloads(store: BackupStore) -> list[tuple[str, dict[str, str], bool]]:
    """(name, data, overwrite). Executor only. The repository password is never overwritten, because
    changing it would make existing volume backups unreadable."""
    refs = store.credentials or {}
    key_id = _single_line(resolve_credential(refs.get("access_key_id")) or "", "the R2 access key id")
    secret = _single_line(resolve_credential(refs.get("secret_access_key")) or "", "the R2 secret access key")
    sse = _single_line(resolve_credential(refs.get("sse_key")) or "", "the encryption key")
    password = _single_line(resolve_credential(refs.get("repo_password")) or "", "the volume repository password")
    try:
        load_key(sse)
    except BackupError:
        raise ConnectorError("config", "the encryption key must be 32 random bytes, base64 encoded") from None
    if len(password) < 24:
        raise ConnectorError("config", "the volume repository password must have at least 24 characters")
    cloud = f"[default]\naws_access_key_id={key_id}\naws_secret_access_key={secret}\n"
    return [(CREDENTIALS_SECRET, {"cloud": _b64(cloud)}, True), (SSE_SECRET, {"key": sse}, True), (REPO_SECRET, {"repository-password": _b64(password)}, False)]


# ---- Reading what Velero reports -----------------------------------------------------------------


def backup_row(b: dict) -> dict[str, Any]:
    spec, st, meta = b.get("spec") or {}, b.get("status") or {}, b.get("metadata") or {}
    progress = st.get("progress") or {}
    return {"name": meta.get("name"), "uid": meta.get("uid"), "phase": st.get("phase") or "New", "started": st.get("startTimestamp"),
            "completed": st.get("completionTimestamp"), "expires": st.get("expiration"), "errors": st.get("errors") or 0,
            "warnings": st.get("warnings") or 0, "items": progress.get("itemsBackedUp"), "items_total": progress.get("totalItems"),
            "namespaces": spec.get("includedNamespaces") or ["*"], "volumes": bool(spec.get("defaultVolumesToFsBackup")),
            "schedule": (meta.get("labels") or {}).get("velero.io/schedule-name"),
            "failure": st.get("failureReason") or "; ".join(st.get("validationErrors") or []) or None}


def restore_row(r: dict) -> dict[str, Any]:
    spec, st, meta = r.get("spec") or {}, r.get("status") or {}, r.get("metadata") or {}
    return {"name": meta.get("name"), "backup": spec.get("backupName"), "phase": st.get("phase") or "New", "started": st.get("startTimestamp"),
            "completed": st.get("completionTimestamp"), "errors": st.get("errors") or 0, "warnings": st.get("warnings") or 0,
            "namespaces": spec.get("includedNamespaces") or ["*"], "policy": spec.get("existingResourcePolicy") or "none",
            "failure": st.get("failureReason") or "; ".join(st.get("validationErrors") or []) or None}


def schedule_row(s: dict) -> dict[str, Any]:
    spec, st, meta = s.get("spec") or {}, s.get("status") or {}, s.get("metadata") or {}
    return {"name": meta.get("name"), "cron": spec.get("schedule"), "paused": bool(spec.get("paused")), "last_backup": st.get("lastBackup"),
            "ttl": (spec.get("template") or {}).get("ttl"), "volumes": bool((spec.get("template") or {}).get("defaultVolumesToFsBackup"))}


def location_row(l: dict) -> dict[str, Any]:
    spec, st, meta = l.get("spec") or {}, l.get("status") or {}, l.get("metadata") or {}
    return {"name": meta.get("name"), "phase": st.get("phase") or "Unknown", "last_validated": st.get("lastValidationTime"), "message": st.get("message"),
            "bucket": (spec.get("objectStorage") or {}).get("bucket"), "prefix": (spec.get("objectStorage") or {}).get("prefix")}


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


def findings(state: dict[str, Any], now: datetime | None = None) -> list[dict[str, str]]:
    """Deterministic checks. The LLM may explain these; it does not decide them."""
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, str]] = []
    add = lambda severity, code, text: out.append({"severity": severity, "code": code, "text": text})  # noqa: E731
    if not state["installed"]:
        add("bad", "not_installed", state.get("error") or "Velero is not installed in this cluster.")
        return out
    loc = next((l for l in state["locations"] if l["name"] == LOCATION), None)
    if loc is None:
        add("bad", "no_location", "The R2 backup location is not configured in this cluster.")
    elif loc["phase"] != "Available":
        add("bad", "location_unavailable", f"The R2 backup location is {loc['phase']}" + (f": {loc['message']}" if loc.get("message") else "."))
    good = [b for b in state["backups"] if b["phase"] == "Completed"]
    if not good:
        add("bad", "no_backup", "This cluster has no completed backup.")
    else:
        last = max((_parse(b["completed"]) or datetime.min.replace(tzinfo=timezone.utc)) for b in good)
        hours = (now - last).total_seconds() / 3600
        if hours > STALE_HOURS:
            add("warn", "stale", f"The last completed backup is {hours:.0f} hours old.")
        if not any(b["volumes"] for b in good):
            add("warn", "no_volumes", "No completed backup includes PersistentVolume data.")
    if not state["schedules"]:
        add("warn", "no_schedule", "No backup schedule exists, so backups only run when someone starts one.")
    for s in state["schedules"]:
        if s["paused"]:
            add("warn", "paused", f"Schedule {s['name']} is paused.")
    latest = state["backups"][0] if state["backups"] else None
    if latest and latest["phase"] in ("Failed", "FailedValidation", "PartiallyFailed"):
        add("bad", "latest_failed", f"Backup {latest['name']} ended {latest['phase']} with {latest['errors']} errors"
                                    + (f": {latest['failure']}" if latest["failure"] else "."))
    return out


async def status(k8s: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"installed": True, "locations": [], "backups": [], "restores": [], "schedules": []}
    try:
        locations, backups, restores, schedules = await asyncio.gather(*(k8s.velero_list(p) for p in ("backupstoragelocations", "backups", "restores", "schedules")))
    except ConnectorError as exc:
        if exc.kind not in ("not_found", "forbidden"):
            raise
        state["installed"] = False
        state["error"] = "Velero is not installed in this cluster." if exc.kind == "not_found" else "MOKS has no read access to Velero here (set velero.enabled in the chart)."
    else:
        newest = lambda row: row.get("started") or ""  # noqa: E731
        state["locations"] = [location_row(l) for l in locations]
        state["backups"] = sorted((backup_row(b) for b in backups), key=newest, reverse=True)
        state["restores"] = sorted((restore_row(r) for r in restores), key=newest, reverse=True)
        state["schedules"] = [schedule_row(s) for s in schedules]
    state["findings"] = findings(state)
    return state


async def preflight(k8s: Any, action: str, target_name: str) -> dict[str, Any]:
    """What a person reviews, and what the plan hash binds to."""
    locations = {l["metadata"]["name"]: location_row(l) for l in await k8s.velero_list("backupstoragelocations")}
    if action == "velero_configure":
        return {"location": locations.get(LOCATION, {}).get("phase", "not configured")}
    loc = locations.get(LOCATION)
    if loc is None or loc["phase"] != "Available":
        raise ConnectorError("conflict", f"the R2 backup location is {loc['phase'] if loc else 'not configured'}; configure it first")
    if action != "velero_restore":
        return {"location": loc["phase"]}
    backup = backup_row(await k8s.velero_get("backups", target_name))
    if backup["phase"] not in ("Completed", "PartiallyFailed"):
        raise ConnectorError("conflict", f"backup {target_name} is {backup['phase']}, so it cannot be restored")
    return {"uid": backup["uid"], "location": loc["phase"], "backup_phase": backup["phase"], "backup_errors": backup["errors"],
            "backup_completed": backup["completed"], "backup_namespaces": backup["namespaces"], "backup_volumes": backup["volumes"]}


# ---- Testing the bucket --------------------------------------------------------------------------


def _s3_client(store: BackupStore) -> Any:
    import boto3
    from botocore.config import Config

    refs = store.credentials or {}
    return boto3.client("s3", endpoint_url=endpoint(store), region_name="auto", aws_access_key_id=resolve_credential(refs.get("access_key_id")),
                        aws_secret_access_key=resolve_credential(refs.get("secret_access_key")),
                        config=Config(signature_version="s3v4", request_checksum_calculation="when_required", response_checksum_validation="when_required",
                                      connect_timeout=10, read_timeout=20, retries={"max_attempts": 2}))


def probe(store: BackupStore) -> dict[str, Any]:
    """Writes, reads and removes one small object with the real encryption key. Blocking."""
    refs = store.credentials or {}
    sse = resolve_credential(refs.get("sse_key")) or ""
    try:
        raw = load_key(sse)
    except BackupError:
        raise ConnectorError("config", "the encryption key must be 32 random bytes, base64 encoded") from None
    s3 = _s3_client(store)
    key = "/".join(p for p in (store.prefix.strip("/"), f".moks-probe-{uuid.uuid4().hex}") if p)
    args = {"SSECustomerAlgorithm": "AES256", "SSECustomerKey": raw}
    body = uuid.uuid4().bytes
    s3.put_object(Bucket=store.bucket, Key=key, Body=body, **args)
    try:
        if s3.get_object(Bucket=store.bucket, Key=key, **args)["Body"].read() != body:
            raise ConnectorError("unavailable", "the object read back from R2 differs from what was written")
    finally:
        s3.delete_object(Bucket=store.bucket, Key=key)
    return {"ok": True, "endpoint": endpoint(store)}


def expiry_days(ts: str | None) -> float | None:
    at = _parse(ts)
    return (at - datetime.now(timezone.utc)) / timedelta(days=1) if at else None
