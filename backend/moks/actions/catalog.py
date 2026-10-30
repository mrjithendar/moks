"""Allowlisted remediation actions. Anything not here cannot be planned or executed.

Each action validates its parameters, applies environment policy, and builds the reviewable
parts of the plan: exact target, API operation, preconditions, validation steps, expected impact
and blast radius, and the rollback/recovery approach.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..connectors.base import ConnectorError
from ..models import Environment

QTY_CPU = r"^([0-9]{1,3}(\.[0-9]{1,3})?|[0-9]{1,6}m)$"
QTY_MEM = r"^[0-9]{1,6}(Ki|Mi|Gi)$"
FORBIDDEN_KEYS = {"privileged", "hostNetwork", "hostPID", "hostIPC", "hostPath", "serviceAccountName", "serviceAccount",
                  "automountServiceAccountToken", "capabilities", "securityContext", "allowPrivilegeEscalation", "runAsUser",
                  "runAsNonRoot", "externalIPs", "selector", "nodeName", "volumes", "initContainers", "command", "args", "image"}


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestartP(Params):
    pass


class ScaleP(Params):
    replicas: int = Field(ge=0, le=1000)


class RollbackP(Params):
    to_revision: int = Field(ge=1)


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: str | None = Field(default=None, pattern=QTY_CPU)
    memory: str | None = Field(default=None, pattern=QTY_MEM)


class PatchResourcesP(Params):
    container: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    requests: Resources = Field(default_factory=Resources)
    limits: Resources = Field(default_factory=Resources)


class ConfigPatchP(Params):
    patch: dict[str, Any]

    @field_validator("patch")
    @classmethod
    def _safe(cls, v: dict) -> dict:
        if len(json.dumps(v)) > 8000:
            raise ValueError("patch larger than 8 KB")
        bad = sorted(_keys(v) & FORBIDDEN_KEYS)
        if bad:
            raise ValueError(f"patch touches fields that are never patched through MOKS: {', '.join(bad)}")
        return v


class PullRequestP(Params):
    repository: str = Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
    base: str = Field(default="main", pattern=r"^[A-Za-z0-9_./-]{1,100}$")
    path: str = Field(pattern=r"^[A-Za-z0-9_./@+-]{1,300}$")
    content: str = Field(max_length=200_000)
    title: str = Field(min_length=5, max_length=200)
    body: str = Field(default="", max_length=10_000)


def _keys(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    return out


@dataclass(frozen=True)
class ActionDef:
    name: str
    title: str
    target_kinds: tuple[str, ...]
    params: type[Params]
    approval_outside_production: bool
    operation: str


CATALOG: dict[str, ActionDef] = {a.name: a for a in [
    ActionDef("restart_deployment", "Restart a Deployment (rolling)", ("Deployment",), RestartP, False,
              "PUT apps/v1 Deployment (spec.template annotation kubectl.kubernetes.io/restartedAt)"),
    ActionDef("scale_deployment", "Scale a Deployment within policy", ("Deployment",), ScaleP, False,
              "PUT apps/v1 Deployment (spec.replicas)"),
    ActionDef("rollback_deployment", "Roll back to a previous revision", ("Deployment",), RollbackP, True,
              "PUT apps/v1 Deployment (spec.template from the revision's ReplicaSet)"),
    ActionDef("patch_resources", "Change container requests/limits", ("Deployment",), PatchResourcesP, False,
              "PUT apps/v1 Deployment (containers[].resources)"),
    ActionDef("apply_config_patch", "Apply a reviewed JSON merge patch", ("Deployment", "Service", "HorizontalPodAutoscaler"), ConfigPatchP, True,
              "PATCH (application/merge-patch+json, resourceVersion precondition)"),
    ActionDef("open_pull_request", "Open a draft pull request with a proposed change", ("Repository",), PullRequestP, True,
              "GitHub: create branch, commit file, open draft PR"),
]}


def needs_approval(action: ActionDef, env: Environment) -> bool:
    return env.is_production or action.approval_outside_production


def check_policy(action: str, params: Params, env: Environment, pre: dict | None) -> None:
    if action == "scale_deployment":
        r = params.replicas  # type: ignore[attr-defined]
        if r == 0 and not env.allow_scale_to_zero:
            raise ConnectorError("forbidden", f"scaling to zero is not allowed in {env.name}")
        if r > env.max_replicas:
            raise ConnectorError("forbidden", f"{r} replicas exceeds the {env.name} limit of {env.max_replicas}")
        if pre and pre.get("replicas") == r:
            raise ConnectorError("bad_request", f"the deployment already has {r} replicas")
    if action == "rollback_deployment":
        if pre and str(pre.get("revision")) == str(params.to_revision):  # type: ignore[attr-defined]
            raise ConnectorError("bad_request", "that revision is already running")
    if action == "patch_resources":
        p: PatchResourcesP = params  # type: ignore[assignment]
        if not any([p.requests.cpu, p.requests.memory, p.limits.cpu, p.limits.memory]):
            raise ConnectorError("bad_request", "specify at least one request or limit")


def describe(action: str, params: Params, target: str, ns: str, pre: dict | None, history: dict | None = None) -> dict[str, Any]:
    """Reviewable plan text: preconditions, validation, impact, blast radius, rollback."""
    replicas = (pre or {}).get("replicas")
    rv = (pre or {}).get("resourceVersion")
    pre_conds = [f"{target} in {ns} still has resourceVersion {rv} (unchanged since this plan was made)"] if rv else []
    rollout_validation = [f"rollout of {target} completes within the timeout (updated = available = desired replicas)",
                          "no pod of the deployment is in CrashLoopBackOff, ImagePullBackOff or newly restarting",
                          "any Service selecting these pods keeps ready endpoints"]
    if action == "restart_deployment":
        return {"impact": f"Pods of {target} are replaced one by one following its rolling-update strategy ({replicas} replicas).",
                "blast_radius": f"{replicas} pod(s) of {target} in {ns}; brief capacity dip if maxUnavailable > 0.",
                "preconditions": pre_conds + [f"{target} has at least one replica"], "validation": rollout_validation,
                "rollback": "Restarts are not reversed; if new pods fail, MOKS restores the previous pod template (identical spec)."}
    if action == "scale_deployment":
        r = params.replicas  # type: ignore[attr-defined]
        return {"impact": f"{target} goes from {replicas} to {r} replicas.",
                "blast_radius": f"{abs(r - (replicas or 0))} pod(s) {'added' if r > (replicas or 0) else 'removed'} in {ns}; cluster capacity affected.",
                "preconditions": pre_conds, "validation": [f"{r} replicas available within the timeout"] + rollout_validation[1:],
                "rollback": f"Automatic: scale back to {replicas} replicas if validation fails."}
    if action == "rollback_deployment":
        to = params.to_revision  # type: ignore[attr-defined]
        rev = next((x for x in (history or {}).get("revisions", []) if x["revision"] == to), None)
        if history is not None and rev is None:
            raise ConnectorError("not_found", f"revision {to} is not in the rollout history of {target}")
        return {"impact": f"{target} returns from revision {(pre or {}).get('revision')} to revision {to}"
                          + (f" (images {', '.join(rev['images'])}, created {rev['created']})" if rev else "") + ".",
                "blast_radius": f"All {replicas} pod(s) of {target} are replaced.",
                "preconditions": pre_conds + [f"revision {to} still exists (its ReplicaSet was not garbage-collected)"],
                "validation": rollout_validation,
                "rollback": "Automatic: restore the pre-rollback pod template if validation fails."}
    if action == "patch_resources":
        p: PatchResourcesP = params  # type: ignore[assignment]
        return {"impact": f"Container {p.container} of {target} gets requests {p.requests.model_dump(exclude_none=True)} "
                          f"and limits {p.limits.model_dump(exclude_none=True)}; pods are replaced.",
                "blast_radius": f"All {replicas} pod(s) of {target}; scheduling may change if requests grow.",
                "preconditions": pre_conds, "validation": rollout_validation,
                "rollback": "Automatic: restore the previous pod template if validation fails."}
    if action == "apply_config_patch":
        return {"impact": f"JSON merge patch applied to {target}: {json.dumps(params.patch)[:600]}",  # type: ignore[attr-defined]
                "blast_radius": f"{target} in {ns} and whatever depends on it.",
                "preconditions": pre_conds, "validation": rollout_validation if target.startswith("Deployment/") else
                ["object accepted by the API server", "Service still has ready endpoints" if target.startswith("Service/") else "object reads back"],
                "rollback": "Automatic: apply the inverse merge patch (previous values) if validation fails."}
    if action == "open_pull_request":
        p2: PullRequestP = params  # type: ignore[assignment]
        return {"impact": f"A draft pull request to {p2.repository}@{p2.base} changing {p2.path}. Nothing is deployed.",
                "blast_radius": "None until a human reviews and merges the pull request.",
                "preconditions": [f"{p2.repository} is in the git connector allowlist", "the token may push branches and open PRs"],
                "validation": ["pull request exists and is a draft"], "rollback": "Close the pull request and delete the branch."}
    raise ConnectorError("bad_request", f"unknown action {action}")


def inverse_merge_patch(original: dict, patch: dict) -> dict:
    """Merge patch that restores original's values for every key the patch touched."""
    inv: dict[str, Any] = {}
    for k, v in patch.items():
        if k == "metadata" and isinstance(v, dict):
            continue
        if isinstance(v, dict) and isinstance(original.get(k), dict):
            inv[k] = inverse_merge_patch(original[k], v)
        else:
            inv[k] = original.get(k)  # None -> remove the key again
    return inv


SAFE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
Kind = Literal["Deployment", "Service", "HorizontalPodAutoscaler", "Repository"]
