"""Application RBAC.

Two independent controls decide whether MOKS touches a namespace:

1. Application RBAC (this module): a user's role bindings, each scoped to an
   environment pattern and a namespace pattern.
2. Infrastructure scope: the environment's allowed/denied namespaces, plus whatever the
   connector's own credentials permit (Kubernetes RBAC, IAM). See moks.scope.

Both must allow an operation. Checks run in the API and again in the tool executor, so a
request that bypasses the UI, or an LLM that asks for something else, is still bounded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase


class P(StrEnum):
    """Permissions. Roles are fixed bundles of these."""

    OVERVIEW_READ = "overview.read"
    INVESTIGATION_READ = "investigation.read"
    INVESTIGATION_RUN = "investigation.run"
    EXPLORER_READ = "explorer.read"          # live Kubernetes/metrics/log reads
    METRICS_RAW_QUERY = "metrics.raw_query"  # arbitrary PromQL; needs namespace '*' (see below)
    ACTION_REQUEST = "action.request"
    ACTION_EXECUTE = "action.execute"
    ACTION_APPROVE = "action.approve"
    AUDIT_READ = "audit.read"
    FINOPS_READ = "finops.read"              # cost, allocation and rightsizing data
    INCIDENT_WRITE = "incident.write"        # create/update incidents and timelines
    ADMIN_USERS = "admin.users"
    ADMIN_CONNECTORS = "admin.connectors"
    ADMIN_LLM = "admin.llm"
    ADMIN_POLICIES = "admin.policies"


_VIEWER = {P.OVERVIEW_READ, P.INVESTIGATION_READ}
_INVESTIGATOR = _VIEWER | {P.INVESTIGATION_RUN, P.EXPLORER_READ, P.METRICS_RAW_QUERY, P.FINOPS_READ, P.INCIDENT_WRITE}
_OPERATOR = _INVESTIGATOR | {P.ACTION_REQUEST, P.ACTION_EXECUTE}
_APPROVER = _VIEWER | {P.ACTION_APPROVE, P.FINOPS_READ}
_ADMIN = set(P)

ROLES: dict[str, frozenset[P]] = {
    "viewer": frozenset(_VIEWER),
    "investigator": frozenset(_INVESTIGATOR),
    "operator": frozenset(_OPERATOR),
    "approver": frozenset(_APPROVER),
    "admin": frozenset(_ADMIN),
}

ROLE_DESCRIPTIONS = {
    "viewer": "Read permitted dashboards, investigations and incidents.",
    "investigator": "Run read-only investigations, live reads, knowledge and FinOps queries; manage incidents.",
    "operator": "Request and execute permitted operational actions and diagnostic sessions.",
    "approver": "Approve or reject action plans within the assigned scope (never their own).",
    "admin": "Manage users, connectors, LLM providers and policies.",
}

# Permissions that only make sense globally: a binding grants them only when both its
# environment and namespace are '*'.
GLOBAL_PERMISSIONS = {P.ADMIN_USERS, P.ADMIN_CONNECTORS, P.ADMIN_LLM, P.ADMIN_POLICIES, P.AUDIT_READ}

# Raw PromQL cannot be reliably restricted to a namespace, so it requires a binding whose
# namespace is '*' for the environment. Namespace-scoped users get templated metric tools.
NAMESPACE_WIDE_PERMISSIONS = {P.METRICS_RAW_QUERY}


@dataclass(frozen=True)
class Binding:
    role: str
    environment: str = "*"
    namespace: str = "*"

    def matches(self, environment: str | None, namespace: str | None) -> bool:
        if environment is not None and not fnmatchcase(environment, self.environment):
            return False
        if namespace is not None and not fnmatchcase(namespace, self.namespace):
            return False
        return True


@dataclass
class Principal:
    user_id: str
    email: str
    bindings: list[Binding] = field(default_factory=list)

    def can(self, perm: P, environment: str | None = None, namespace: str | None = None) -> bool:
        for b in self.bindings:
            if perm not in ROLES.get(b.role, ()):
                continue
            if perm in GLOBAL_PERMISSIONS and (b.environment != "*" or b.namespace != "*"):
                continue
            if perm in NAMESPACE_WIDE_PERMISSIONS and b.namespace != "*":
                continue
            if b.matches(environment, namespace):
                return True
        return False

    def can_anywhere(self, perm: P) -> bool:
        """True if any binding grants perm in some scope (used for menu visibility only)."""
        return any(
            perm in ROLES.get(b.role, ())
            and not (perm in GLOBAL_PERMISSIONS and (b.environment != "*" or b.namespace != "*"))
            for b in self.bindings
        )

    def environments_for(self, perm: P, names: list[str]) -> list[str]:
        return [n for n in names if any(
            perm in ROLES.get(b.role, ()) and fnmatchcase(n, b.environment) for b in self.bindings
        )]

    def namespace_patterns(self, perm: P, environment: str) -> list[str]:
        """Namespace patterns this principal holds perm for in the environment."""
        return [
            b.namespace for b in self.bindings
            if perm in ROLES.get(b.role, ()) and fnmatchcase(environment, b.environment)
        ]

    @property
    def roles(self) -> set[str]:
        return {b.role for b in self.bindings}

    def permissions(self) -> set[str]:
        return {str(p) for b in self.bindings for p in ROLES.get(b.role, ())}


def parse_grant(text: str) -> Binding:
    """Parse 'role[:environment[:namespace]]', as used by OIDC group maps and the CLI."""
    parts = text.split(":")
    if not parts[0] or parts[0] not in ROLES or len(parts) > 3:
        raise ValueError(f"invalid grant {text!r}; expected role[:environment[:namespace]]")
    env = parts[1] if len(parts) > 1 and parts[1] else "*"
    ns = parts[2] if len(parts) > 2 and parts[2] else "*"
    return Binding(parts[0], env, ns)
