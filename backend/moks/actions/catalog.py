"""Allowlisted remediation actions. Anything not here cannot be planned or executed.

Each action validates its parameters, applies environment policy, and builds the reviewable
parts of the plan: exact target, API operation, preconditions, validation steps, expected impact
and blast radius, and the rollback/recovery approach.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..connectors.base import ConnectorError
from ..models import Environment

QTY_CPU = r"^([0-9]{1,3}(\.[0-9]{1,3})?|[0-9]{1,6}m)$"
QTY_MEM = r"^[0-9]{1,6}(Ki|Mi|Gi)$"
LABEL_KEY = re.compile(r"^([a-z0-9.-]{1,253}/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
PERCENT_OR_INT = re.compile(r"^[0-9]{1,3}%$")


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
    def _bounded(cls, v: dict) -> dict:
        if not v:
            raise ValueError("patch is empty")
        if len(json.dumps(v)) > 8000:
            raise ValueError("patch larger than 8 KB")
        return v


class PullRequestP(Params):
    repository: str = Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
    base: str = Field(default="main", pattern=r"^[A-Za-z0-9_./-]{1,100}$")
    path: str = Field(pattern=r"^[A-Za-z0-9_./@+-]{1,300}$")
    content: str = Field(max_length=200_000)
    title: str = Field(min_length=5, max_length=200)
    body: str = Field(default="", max_length=10_000)


# ---- Reviewed patches --------------------------------------------------------------------------
# A merge patch may set only these leaves, each with a strict value check. Merge patches replace
# arrays and delete on null, so neither is ever accepted. Replicas, images, commands and the pod
# security surface have no entry: they are changed through dedicated actions or not at all.


def _int(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: type(v) is int and lo <= v <= hi


def _surge(v: Any) -> bool:
    return (type(v) is int and 0 <= v <= 1000) or (isinstance(v, str) and bool(PERCENT_OR_INT.match(v)) and int(v[:-1]) <= 100)


def _labels(v: Any) -> bool:
    return isinstance(v, dict) and bool(v) and all(isinstance(k, str) and LABEL_KEY.match(k) and isinstance(x, str) and LABEL_VALUE.match(x) for k, x in v.items())


PATCH_PATHS: dict[str, dict[tuple[str, ...], Callable[[Any], bool]]] = {
    "Deployment": {
        ("spec", "minReadySeconds"): _int(0, 3600),
        ("spec", "progressDeadlineSeconds"): _int(60, 3600),
        ("spec", "revisionHistoryLimit"): _int(0, 100),
        ("spec", "strategy", "rollingUpdate", "maxSurge"): _surge,
        ("spec", "strategy", "rollingUpdate", "maxUnavailable"): _surge,
        ("spec", "template", "spec", "terminationGracePeriodSeconds"): _int(0, 3600),
    },
    "Service": {
        ("spec", "selector"): _labels,
        ("spec", "sessionAffinity"): lambda v: v in ("None", "ClientIP"),
        ("spec", "publishNotReadyAddresses"): lambda v: type(v) is bool,
    },
    "HorizontalPodAutoscaler": {
        ("spec", "minReplicas"): _int(0, 1000),
        ("spec", "maxReplicas"): _int(1, 1000),
        ("spec", "behavior", "scaleUp", "stabilizationWindowSeconds"): _int(0, 3600),
        ("spec", "behavior", "scaleDown", "stabilizationWindowSeconds"): _int(0, 3600),
    },
}


def patch_leaves(kind: str, patch: dict) -> dict[tuple[str, ...], Any]:
    """Leaves of a patch, where a supported map (such as spec.selector) counts as one leaf."""
    schema = PATCH_PATHS.get(kind, {})
    out: dict[tuple[str, ...], Any] = {}

    def walk(node: dict, prefix: tuple[str, ...]) -> None:
        for k, v in node.items():
            path = (*prefix, str(k))
            if isinstance(v, dict) and v and path not in schema:
                walk(v, path)
            else:
                out[path] = v
    walk(patch, ())
    return out


def get_path(obj: Any, path: tuple[str, ...] | list[str]) -> Any:
    for p in path:
        obj = obj.get(p) if isinstance(obj, dict) else None
    return obj


def apply_merge_patch(obj: Any, patch: Any) -> Any:
    """RFC 7386."""
    if not isinstance(patch, dict):
        return patch
    out = dict(obj) if isinstance(obj, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = apply_merge_patch(out.get(k), v)
    return out


def merge_diff(current: Any, wanted: Any) -> Any:
    """Merge patch that turns current into wanted (arrays and scalars replaced, removed keys nulled)."""
    if not isinstance(current, dict) or not isinstance(wanted, dict):
        return wanted
    out: dict[str, Any] = {k: None for k in current if k not in wanted}
    for k, v in wanted.items():
        if k not in current:
            out[k] = v
        elif current[k] != v:
            out[k] = merge_diff(current[k], v)
    return out


def paths_patch(paths: list[list[str]], current: dict, wanted: dict) -> dict:
    """Merge patch that sets only these paths of current to their value in wanted."""
    patch: dict[str, Any] = {}
    for path in paths:
        node = patch
        for p in path[:-1]:
            node = node.setdefault(p, {})
        node[path[-1]] = merge_diff(get_path(current, path), get_path(wanted, path))
    return patch


def check_patch(kind: str, patch: dict, env: Environment, current: dict | None = None) -> None:
    """Supported paths and strict values, then the effective object against environment policy."""
    schema = PATCH_PATHS.get(kind)
    if schema is None:
        raise ConnectorError("invalid_args", f"{kind} cannot be patched through MOKS")
    leaves = patch_leaves(kind, patch)
    for path, value in leaves.items():
        name = ".".join(path)
        if path not in schema:
            raise ConnectorError("invalid_args", f"{name} cannot be patched on a {kind}. Supported: {', '.join('.'.join(p) for p in schema)}")
        if value is None or isinstance(value, list) or not schema[path](value):
            raise ConnectorError("invalid_args", f"invalid value for {name}")
    spec = (apply_merge_patch(current or {}, {"spec": patch.get("spec") or {}}) if current is not None else patch).get("spec") or {}
    if kind == "HorizontalPodAutoscaler":
        low, high = spec.get("minReplicas"), spec.get("maxReplicas")
        if ("spec", "minReplicas") in leaves and low == 0 and not env.allow_scale_to_zero:
            raise ConnectorError("forbidden", f"scaling to zero is not allowed in {env.name}")
        if ("spec", "maxReplicas") in leaves and high > env.max_replicas:
            raise ConnectorError("forbidden", f"{high} replicas exceeds the {env.name} limit of {env.max_replicas}")
        if ("spec", "minReplicas") in leaves and low > env.max_replicas:
            raise ConnectorError("forbidden", f"{low} replicas exceeds the {env.name} limit of {env.max_replicas}")
        if low is not None and high is not None and low > high:
            raise ConnectorError("invalid_args", f"minReplicas {low} is above maxReplicas {high}")
    if kind == "Deployment" and current is not None and (spec.get("strategy") or {}).get("type") == "Recreate" and \
            any(p[:3] == ("spec", "strategy", "rollingUpdate") for p in leaves):
        raise ConnectorError("invalid_args", "rollingUpdate settings do not apply to a Recreate deployment")
    if kind == "Service" and ("spec", "selector") in leaves and current is not None and (current.get("spec") or {}).get("type") == "ExternalName":
        raise ConnectorError("invalid_args", "an ExternalName Service has no selector")


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


def check_policy(action: str, params: Params, env: Environment, pre: dict | None, target_kind: str | None = None) -> None:
    if action == "apply_config_patch":
        check_patch(target_kind or "", params.patch, env, {"spec": pre["spec"]} if pre and "spec" in pre else None)  # type: ignore[attr-defined]
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
                          "no pod of the current revision is in CrashLoopBackOff, ImagePullBackOff or newly restarting",
                          "any Service selecting these pods keeps ready endpoints"]
    recovery = "If validation fails and the changed fields still hold what MOKS applied, MOKS restores "
    manual = ". If anyone changed those fields meanwhile, MOKS stops and asks for manual reconciliation."
    if action == "restart_deployment":
        return {"impact": f"Pods of {target} are replaced one by one following its rolling-update strategy ({replicas} replicas).",
                "blast_radius": f"{replicas} pod(s) of {target} in {ns}; brief capacity dip if maxUnavailable > 0.",
                "preconditions": pre_conds + [f"{target} has at least one replica"], "validation": rollout_validation,
                "rollback": recovery + "the previous pod template" + manual}
    if action == "scale_deployment":
        r = params.replicas  # type: ignore[attr-defined]
        return {"impact": f"{target} goes from {replicas} to {r} replicas.",
                "blast_radius": f"{abs(r - (replicas or 0))} pod(s) {'added' if r > (replicas or 0) else 'removed'} in {ns}; cluster capacity affected.",
                "preconditions": pre_conds, "validation": [f"{r} replicas available within the timeout"] + rollout_validation[1:],
                "rollback": recovery + f"{replicas} replicas" + manual}
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
                "rollback": recovery + "the pre-rollback pod template" + manual}
    if action == "patch_resources":
        p: PatchResourcesP = params  # type: ignore[assignment]
        return {"impact": f"Container {p.container} of {target} gets requests {p.requests.model_dump(exclude_none=True)} "
                          f"and limits {p.limits.model_dump(exclude_none=True)}; pods are replaced.",
                "blast_radius": f"All {replicas} pod(s) of {target}; scheduling may change if requests grow.",
                "preconditions": pre_conds, "validation": rollout_validation,
                "rollback": recovery + "the previous pod template" + manual}
    if action == "apply_config_patch":
        return {"impact": f"JSON merge patch applied to {target}: {json.dumps(params.patch)[:600]}",  # type: ignore[attr-defined]
                "blast_radius": f"{target} in {ns} and whatever depends on it.",
                "preconditions": pre_conds,
                "validation": ["patched fields read back with the reviewed values", *(
                    rollout_validation if target.startswith("Deployment/") else
                    ["selected Services still have ready endpoints"] if target.startswith("Service/") else
                    ["the autoscaler did not lose its ability to scale"])],
                "rollback": recovery + "the previous values of the patched fields" + manual}
    if action == "open_pull_request":
        p2: PullRequestP = params  # type: ignore[assignment]
        return {"impact": f"A draft pull request to {p2.repository}@{p2.base} changing {p2.path}. Nothing is deployed.",
                "blast_radius": "None until a human reviews and merges the pull request.",
                "preconditions": [f"{p2.repository} is in the git connector allowlist", "the token may push branches and open PRs"],
                "validation": ["pull request exists and is a draft"], "rollback": "Close the pull request and delete the branch."}
    raise ConnectorError("bad_request", f"unknown action {action}")


SAFE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
Kind = Literal["Deployment", "Service", "HorizontalPodAutoscaler", "Repository"]
