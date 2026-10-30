"""Who may retrieve which knowledge.

A document carries an environment and a namespace (from its source, or "*" for knowledge that
applies everywhere). A principal may read it when one of their bindings grants
investigation.read and the binding matches the document's environment and namespace; "*" on the
document means "not restricted" on that axis. The same rule runs as a SQL pre-filter (cheap,
conservative) and as an authoritative post-filter on every result."""
from __future__ import annotations

from fnmatch import fnmatchcase

from ..rbac import ROLES, P, Principal

PERM = P.INVESTIGATION_READ


def can_read(principal: Principal, environment: str, namespace: str) -> bool:
    for b in principal.bindings:
        if PERM not in ROLES.get(b.role, ()):
            continue
        env_ok = environment == "*" or fnmatchcase(environment, b.environment)
        ns_ok = namespace == "*" or fnmatchcase(namespace, b.namespace)
        if env_ok and ns_ok:
            return True
    return False


def prefilter(principal: Principal) -> tuple[list[str] | None, list[str] | None]:
    """(environments, namespaces) to restrict the query to, or None for "no restriction" on that
    axis. Glob bindings disable that axis's prefilter; the post-filter still applies."""
    bindings = [b for b in principal.bindings if PERM in ROLES.get(b.role, ())]
    if not bindings:
        return [], []
    envs = None if any(any(c in b.environment for c in "*?[") for b in bindings) else sorted({b.environment for b in bindings})
    nss = None if any(any(c in b.namespace for c in "*?[") for b in bindings) else sorted({b.namespace for b in bindings})
    return envs, nss
