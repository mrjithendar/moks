"""Typed tool contracts.

A tool is the only way an investigation (LLM-driven or deterministic) reaches live systems.
Each tool declares its argument model, the connector it needs, whether it is read-only, and
any extra permission. The executor (moks.tools.executor) enforces all of that; the tool
handler itself never sees principals and cannot widen its scope.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..models import Environment
from ..rbac import P, Principal
from ..scope import Scope

K8S_NAME = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
K8S_NAME_RE = re.compile(K8S_NAME)
# Tool "connectors" served by MOKS itself (no external system to configure).
PSEUDO_CONNECTORS = {"knowledge"}


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass
class ToolResult:
    summary: str
    data: Any
    source: dict[str, Any] = field(default_factory=dict)  # exact query, links, API paths


@dataclass
class ToolContext:
    investigation_id: uuid.UUID | None
    environment: Environment
    scope: Scope
    principal: Principal
    time_from: datetime
    time_to: datetime
    connectors: dict[str, Any]  # connector type -> (Connector row, instance)
    max_window: timedelta
    guard: Any = None  # AccessGuard for long-running work; checked before every tool call

    def available(self, ctype: str) -> bool:
        return ctype in PSEUDO_CONNECTORS or ctype in self.connectors

    def connector(self, ctype: str):
        entry = self.connectors.get(ctype)
        if entry is None:
            from ..connectors.base import ConnectorError

            raise ConnectorError("config", f"no enabled {ctype} connector is configured for environment {self.environment.name!r}")
        return entry[1]

    def window(self, minutes: int | None) -> tuple[datetime, datetime]:
        """The investigation window, optionally narrowed to the last `minutes` of it, and never
        longer than the environment/global maximum."""
        end = self.time_to
        start = self.time_from
        if minutes:
            start = max(start, end - timedelta(minutes=minutes))
        if end - start > self.max_window:
            start = end - self.max_window
        return start, end


Handler = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    connector_type: str
    args: type[ToolArgs]
    handler: Handler
    read_only: bool = True
    # Privileged tools (exec, debug containers, scaling, restarts, package installs) are never
    # offered to the LLM planner and need an approved action plan.
    privileged: bool = False
    cluster_scoped: bool = False
    extra_permission: P | None = None
    # Name of the argument holding the target namespace, checked against scope.
    namespace_arg: str | None = "namespace"
    # Other connector types the tool needs besides connector_type.
    also_requires: tuple[str, ...] = ()

    def schema_for_llm(self) -> dict[str, Any]:
        schema = self.args.model_json_schema()
        props = {k: {kk: vv for kk, vv in v.items() if kk in ("type", "description", "enum", "maximum", "minimum", "default", "anyOf")}
                 for k, v in schema.get("properties", {}).items()}
        return {"name": self.name, "description": self.description, "args": props, "required": schema.get("required", [])}
