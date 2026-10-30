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
from .base import ConnectorError
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

PLANNED_TYPES: set[str] = set()

_cache: dict[uuid.UUID, tuple[str, Any]] = {}
_lock = asyncio.Lock()


def _fingerprint(row: Connector) -> str:
    return hashlib.sha256(json.dumps([row.type, row.config, row.credential_ref], sort_keys=True, default=str).encode()).hexdigest()


def build(row: Connector) -> Any:
    cls = CONNECTOR_TYPES.get(row.type)
    if cls is None:
        if row.type in PLANNED_TYPES:
            raise ConnectorError("config", f"{row.type} connectors are planned for Phase 2 and not implemented yet")
        raise ConnectorError("config", f"unknown connector type {row.type!r}")
    return cls(row.config or {}, row.credential_ref)


async def instance(row: Connector) -> Any:
    fp = _fingerprint(row)
    async with _lock:
        cached = _cache.get(row.id)
        if cached and cached[0] == fp:
            return cached[1]
        if cached:
            try:
                await cached[1].aclose()
            except Exception:
                pass
        inst = build(row)
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
