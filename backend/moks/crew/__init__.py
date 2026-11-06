"""Opt-in CrewAI workload investigation (docs/crewai-investigation.md).

The LangGraph orchestrator stays the default. This package runs one flow, "why is this workload
unhealthy", as three sequential CrewAI agents, and only when MOKS_CREWAI_ENABLED is true and the
image carries the crewai extra (backend/requirements-crewai.txt). Nothing here imports crewai at
module import, so the default image and the flag-off path never load it.

CrewAI reaches nothing on its own: its model is MOKS's configured provider through llm.budget, its
tools are MOKS tools through the ToolExecutor, and its telemetry, AMP tracing and on-disk task store
are switched off before it is imported (prepare_environment).
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from importlib import metadata
from typing import Any

PINNED = "1.15.36"  # backend/requirements-crewai.txt; checked against that version's docs and source

# Set before crewai is imported. CREWAI_DISABLE_TELEMETRY and CREWAI_DISABLE_TRACKING stop the anonymous usage
# spans to telemetry.crewai.com:4319; CREWAI_TRACING_ENABLED=false stops AMP trace upload (docs v1.15.36 telemetry.mdx).
# OTEL_SDK_DISABLED is deliberately not used: it would switch MOKS's own tracing off as well.
TELEMETRY_OFF = {"CREWAI_DISABLE_TELEMETRY": "true", "CREWAI_DISABLE_TRACKING": "true", "CREWAI_TRACING_ENABLED": "false"}


class CrewUnavailable(Exception):
    """The run cannot use CrewAI here. Its message is shown as the investigation's error."""


def installed_version() -> str | None:
    if importlib.util.find_spec("crewai") is None:
        return None
    try:
        return metadata.version("crewai")
    except metadata.PackageNotFoundError:
        return None


def prepare_environment() -> None:
    """Forces telemetry off before crewai is imported. CrewAI also writes settings and a credentials
    directory under HOME at import time, and the import fails when HOME is read-only, as it is in MOKS
    pods (readOnlyRootFilesystem, no home directory). There HOME moves to a private directory under /tmp,
    which nothing else in the worker reads; a writable HOME (a laptop with a kubeconfig) is left alone."""
    os.environ.update(TELEMETRY_OFF)
    os.environ["CREWAI_STORAGE_DIR"] = "moks-crewai"
    home = os.environ.get("HOME") or ""
    if not (home and os.path.isdir(home) and os.access(home, os.W_OK)):
        os.environ["HOME"] = os.environ.get("MOKS_CREWAI_HOME") or tempfile.mkdtemp(prefix="moks-crewai-")


def capability(settings: Any, llm_cfg: Any) -> dict[str, Any]:
    """Whether a CrewAI investigation can run here and, when not, why. `llm_cfg` is the active chat LLM or None."""
    version = installed_version()
    out: dict[str, Any] = {"enabled": bool(settings.crewai_enabled), "available": False, "reason": None, "version": version,
                           "limits": {"timeout_seconds": settings.crewai_timeout_seconds, "tool_timeout_seconds": settings.crewai_tool_timeout_seconds,
                                      "max_iterations_per_agent": settings.crewai_max_iterations,
                                      "max_tool_calls": min(settings.crewai_max_tool_calls, settings.max_tool_calls),
                                      "max_concurrent_per_worker": settings.crewai_max_concurrent}}
    if not settings.crewai_enabled:
        out["reason"] = "disabled: MOKS_CREWAI_ENABLED is not true"
    elif version is None:
        out["reason"] = "the crewai package is not installed in this image (build with --build-arg MOKS_CREWAI=true)"
    elif version != PINNED:
        out["reason"] = f"crewai {version} is installed, but MOKS was checked against {PINNED}"
    elif llm_cfg is None:
        out["reason"] = "no active chat LLM is configured; CrewAI uses MOKS's configured model and nothing else"
    else:
        out["available"] = True
        out["llm"] = f"{llm_cfg.provider}/{llm_cfg.model}"
    return out
