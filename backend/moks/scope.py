"""Effective scope: the intersection of application RBAC and environment infrastructure scope."""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from .models import Environment
from .rbac import P, Principal


class ScopeError(PermissionError):
    """Raised when an operation falls outside the effective scope."""


@dataclass(frozen=True)
class Scope:
    environment: str
    # Namespace patterns: the user may touch ns when it matches one of user_patterns AND
    # one of env_allowed, and none of env_denied.
    user_patterns: tuple[str, ...]
    env_allowed: tuple[str, ...]
    env_denied: tuple[str, ...]

    def allows(self, namespace: str) -> bool:
        if any(fnmatchcase(namespace, d) for d in self.env_denied):
            return False
        if not any(fnmatchcase(namespace, a) for a in self.env_allowed):
            return False
        return any(fnmatchcase(namespace, u) for u in self.user_patterns)

    @property
    def cluster_wide(self) -> bool:
        """Environment-wide access: may read non-namespaced data (nodes, scrape targets, alert
        rules without a namespace label). Denied namespaces stay protected by allows()."""
        return "*" in self.user_patterns and "*" in self.env_allowed

    @property
    def unrestricted(self) -> bool:
        """Every namespace is reachable, so free-form queries need no namespace pinning."""
        return self.cluster_wide and not self.env_denied

    def require(self, namespace: str) -> None:
        if not self.allows(namespace):
            raise ScopeError(f"namespace {namespace!r} is outside your scope in environment {self.environment!r}")

    def require_cluster_wide(self, what: str) -> None:
        if not self.cluster_wide:
            raise ScopeError(f"{what} is cluster-scoped and needs access to all namespaces in {self.environment!r}")

    def filter(self, namespaces: list[str]) -> list[str]:
        return [n for n in namespaces if self.allows(n)]


def effective_scope(principal: Principal, env: Environment, perm: P) -> Scope:
    patterns = tuple(principal.namespace_patterns(perm, env.name))
    if not patterns:
        raise ScopeError(f"you have no {perm} access in environment {env.name!r}")
    allowed = tuple(env.allowed_namespaces or ["*"])
    return Scope(env.name, patterns, allowed, tuple(env.denied_namespaces or []))
