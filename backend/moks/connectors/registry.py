"""Builds connector instances from database rows and caches them per configuration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Connector
from .base import ConnectorError, resolve_credential_path
from .cloudwatch import CloudWatchConnector
from .finops import AwsCostConnector, CostModelConnector
from .git import GitConnector
from .grafana import GrafanaConnector
from .kubernetes import KubernetesConnector
from .loki import LokiConnector
from .prometheus import PrometheusConnector

CONNECTOR_TYPES: dict[str, Any] = {
    "kubernetes": KubernetesConnector,
    "prometheus": PrometheusConnector,
    "loki": LokiConnector,
    "grafana": GrafanaConnector,
    "cloudwatch": CloudWatchConnector,
    "git": GitConnector,
    "aws_cost": AwsCostConnector,
    "cost_model": CostModelConnector,
}

_cache: dict[uuid.UUID, tuple[str, Any]] = {}
_closing: set[asyncio.Task] = set()
CLOSE_DELAY = 120
_lock = asyncio.Lock()


def _fingerprint(row: Connector) -> str:
    return hashlib.sha256(json.dumps([row.type, row.config, row.credential_ref], sort_keys=True, default=str).encode()).hexdigest()


def _build_key(row: Connector) -> str:
    """A mounted credential file can rotate with nothing else changing, so its age is part of the key."""
    stamp = None
    if (row.credential_ref or {}).get("type") == "file":
        try:
            stamp = resolve_credential_path(row.credential_ref).stat().st_mtime
        except (ConnectorError, OSError):
            pass
    return f"{_fingerprint(row)}:{stamp}"


async def _close_later(inst: Any) -> None:
    """Calls already using the old client finish first."""
    await asyncio.sleep(CLOSE_DELAY)
    try:
        await inst.aclose()
    except Exception:
        pass


def build(row: Connector) -> Any:
    cls = CONNECTOR_TYPES.get(row.type)
    if cls is None:
        raise ConnectorError("config", f"unknown connector type {row.type!r}")
    return cls(row.config or {}, row.credential_ref)


async def instance(row: Connector) -> Any:
    fp = _build_key(row)
    async with _lock:
        cached = _cache.get(row.id)
        if cached and cached[0] == fp:
            return cached[1]
        inst = build(row)  # first: if the new settings are bad, the running client stays
        if cached:
            task = asyncio.create_task(_close_later(cached[1]))
            _closing.add(task)
            task.add_done_callback(_closing.discard)
        _cache[row.id] = (fp, inst)
        return inst


async def for_environment(db: AsyncSession, environment_id: uuid.UUID) -> tuple[dict[str, tuple[Connector, Any]], dict[str, str]]:
    """Enabled connectors by type (first enabled one per type), plus per-type build errors."""
    rows = (await db.execute(
        select(Connector).where(Connector.environment_id == environment_id, Connector.enabled.is_(True)).order_by(Connector.created_at)
    )).scalars().all()
    out: dict[str, tuple[Connector, Any]] = {}
    errors: dict[str, str] = {}
    for row in rows:
        if row.type in out:
            continue
        try:
            out[row.type] = (row, await instance(row))
        except ConnectorError as exc:
            errors[row.type] = exc.message
    return out, errors


async def of_type(db: AsyncSession, environment_id: uuid.UUID, ctype: str) -> tuple[list[tuple[Connector, Any]], dict[str, str]]:
    """Every enabled connector of a type, for features that address connectors individually."""
    rows = (await db.execute(select(Connector).where(Connector.environment_id == environment_id, Connector.type == ctype, Connector.enabled.is_(True))
                             .order_by(Connector.name))).scalars().all()
    out: list[tuple[Connector, Any]] = []
    errors: dict[str, str] = {}
    for row in rows:
        try:
            out.append((row, await instance(row)))
        except ConnectorError as exc:
            errors[row.name] = exc.message
    return out, errors


def data_identity(row: Connector) -> str:
    """Two connectors are the same source only if type, endpoint, tenant, every other setting and
    the credential all match: one URL can serve different tenants to different tokens."""
    return _fingerprint(row)


async def test(row: Connector) -> tuple[bool, str]:
    try:
        inst = await instance(row)
        msg = await asyncio.wait_for(inst.test(), timeout=30)
        return True, msg
    except ConnectorError as exc:
        return False, f"{exc.kind}: {exc.message}"
    except asyncio.TimeoutError:
        return False, "timeout: connection test took longer than 30s"
    except Exception as exc:
        return False, f"error: {type(exc).__name__}"


def now() -> datetime:
    return datetime.now(timezone.utc)


async def close_all() -> None:
    for _, inst in list(_cache.values()):
        try:
            await inst.aclose()
        except Exception:
            pass
    _cache.clear()
