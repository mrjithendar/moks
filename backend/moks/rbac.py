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
    BACKUP_READ = "backup.read"              # cluster backup status (names and counts, no contents)
    INCIDENT_WRITE = "incident.write"        # create/update incidents and timelines
    TERMINAL_USE = "terminal.use"            # open a shell; granted only by the terminal role, never by admin
    TERMINAL_REVOKE = "terminal.revoke"      # end someone else's terminal session; never attach to it
    KNOWLEDGE_READ = "knowledge.read"        # read documents one binding covers on both axes (rag/access.py)
    KNOWLEDGE_TEST = "knowledge.test"        # compare retrieval modes in the test workspace
    KNOWLEDGE_REVIEW = "knowledge.review"    # review documents, change their lifecycle, read feedback; scoped by binding
    KNOWLEDGE_SOURCE_ADMIN = "knowledge.source_admin"  # create, change, sync and retire sources
    KNOWLEDGE_INDEX = "knowledge.index"      # index generations, outbox, backfill and retrieval diagnostics
    SKILL_REVIEW = "skills.review"           # record that a skill version was read and is fit to publish
    SKILL_PUBLISH = "skills.publish"         # publish and roll back generations; set skill policy
    ML_READ = "ml.read"                      # anomaly observations and similar incidents in the bound scope
    ML_FEEDBACK = "ml.feedback"              # judge a passage, similar incident, recommendation or observation
    LEARNING_REVIEW = "learning.review"      # confirm causes and resolutions, review judgments; scoped by binding
    LEARNING_EXPORT = "learning.export"      # build and export training and evaluation datasets
    ML_ADMIN = "ml.admin"                    # capability modes (the kill switch), training, model promotion and rollback
    SECRETS_DISCOVER = "secrets.discover"    # secret names, types, owners and sync status; never key names or values
    SECRETS_COMPARE = "secrets.compare"      # key names and masked differences; starts a read-only comparison
    SECRETS_MAP = "secrets.map"              # create, edit and retire mappings
    SECRETS_PROPOSE = "secrets.propose"      # propose export, import, rollback and deletion plans
    SECRETS_APPROVE = "secrets.approve"      # approve or reject secret plans (never one's own, as with actions)
    SECRETS_EXECUTE = "secrets.execute"      # run an approved secret plan and retry its failed items
    SECRETS_REVEAL = "secrets.reveal"        # see one secret's values, encrypted to the viewer's browser; every view is audited
    ADMIN_USERS = "admin.users"
    ADMIN_CONNECTORS = "admin.connectors"
    ADMIN_LLM = "admin.llm"
    ADMIN_POLICIES = "admin.policies"


_VIEWER = {P.OVERVIEW_READ, P.INVESTIGATION_READ, P.KNOWLEDGE_READ, P.ML_READ}
_INVESTIGATOR = _VIEWER | {P.INVESTIGATION_RUN, P.EXPLORER_READ, P.METRICS_RAW_QUERY, P.FINOPS_READ, P.INCIDENT_WRITE, P.BACKUP_READ, P.KNOWLEDGE_TEST, P.ML_FEEDBACK}
_OPERATOR = _INVESTIGATOR | {P.ACTION_REQUEST, P.ACTION_EXECUTE}
_APPROVER = _VIEWER | {P.ACTION_APPROVE, P.FINOPS_READ, P.BACKUP_READ}
_TERMINAL = {P.OVERVIEW_READ, P.INVESTIGATION_READ, P.KNOWLEDGE_READ, P.TERMINAL_USE}  # kept exactly this narrow: a shell grant carries nothing else
_KNOWLEDGE_REVIEWER = _VIEWER | {P.KNOWLEDGE_TEST, P.KNOWLEDGE_REVIEW}
_KNOWLEDGE_ADMIN = _KNOWLEDGE_REVIEWER | {P.KNOWLEDGE_SOURCE_ADMIN}
_INDEX_OPERATOR = _VIEWER | {P.KNOWLEDGE_TEST, P.KNOWLEDGE_INDEX}
_SKILL_PUBLISHER = _VIEWER | {P.SKILL_REVIEW, P.SKILL_PUBLISH}
_ML_REVIEWER = _VIEWER | {P.ML_FEEDBACK, P.LEARNING_REVIEW}
_ML_ADMIN = _ML_REVIEWER | {P.LEARNING_EXPORT, P.ML_ADMIN}
_SECRETS_VIEWER = {P.OVERVIEW_READ, P.SECRETS_DISCOVER, P.SECRETS_COMPARE}
_SECRETS_OPERATOR = _SECRETS_VIEWER | {P.SECRETS_MAP, P.SECRETS_PROPOSE, P.SECRETS_EXECUTE}
_SECRETS_APPROVER = _SECRETS_VIEWER | {P.SECRETS_APPROVE, P.SECRETS_REVEAL}
_ADMIN = set(P) - {P.TERMINAL_USE}  # a shell is arbitrary code: it is granted to a person on purpose, not bundled

ROLES: dict[str, frozenset[P]] = {
    "viewer": frozenset(_VIEWER),
    "investigator": frozenset(_INVESTIGATOR),
    "operator": frozenset(_OPERATOR),
    "approver": frozenset(_APPROVER),
    "terminal": frozenset(_TERMINAL),
    "knowledge_reviewer": frozenset(_KNOWLEDGE_REVIEWER),
    "knowledge_admin": frozenset(_KNOWLEDGE_ADMIN),
    "index_operator": frozenset(_INDEX_OPERATOR),
    "skill_publisher": frozenset(_SKILL_PUBLISHER),
    "ml_reviewer": frozenset(_ML_REVIEWER),
    "ml_admin": frozenset(_ML_ADMIN),
    "secrets_viewer": frozenset(_SECRETS_VIEWER),
    "secrets_operator": frozenset(_SECRETS_OPERATOR),
    "secrets_approver": frozenset(_SECRETS_APPROVER),
    "admin": frozenset(_ADMIN),
}

ROLE_DESCRIPTIONS = {
    "viewer": "Read permitted dashboards, investigations and incidents.",
    "investigator": "Run read-only investigations, live reads, knowledge and FinOps queries; manage incidents.",
    "operator": "Request and execute permitted operational actions and diagnostic sessions.",
    "approver": "Approve or reject action plans within the assigned scope (never their own).",
    "terminal": "Open a disposable Bash terminal with short-lived, read-only credentials. Needs namespace '*'. Not part of any other role, including admin.",
    "knowledge_reviewer": "Review documents in the bound scope, change their lifecycle, and read retrieval feedback.",
    "knowledge_admin": "Administer knowledge sources and review their documents. Needs environment and namespace '*'.",
    "index_operator": "Operate the search index: generations, outbox, backfill and retrieval diagnostics. Needs environment and namespace '*'.",
    "skill_publisher": "Review and publish skill versions, roll back generations and set skill policy. Needs environment and namespace '*'.",
    "ml_reviewer": "Confirm causes and resolutions on learning records and review operator judgments in the bound scope.",
    "ml_admin": "Switch ML capabilities (disabled, shadow, enabled), train, promote and roll back models, and export datasets. Needs environment and namespace '*'. Grants no infrastructure access.",
    "secrets_viewer": "See Kubernetes and AWS secret names, owners and sync status, and compare key names and masked differences in the bound namespaces. Never values.",
    "secrets_operator": "Manage secret mappings and propose and run reviewed sync, rollback and deletion plans in the bound namespaces. Not part of operator.",
    "secrets_approver": "Approve or reject secret plans and sync schedules in the bound namespaces (never their own), and view secret values there (audited). Not part of approver.",
    "admin": "Manage users, connectors, LLM providers and policies, and end anyone's terminal session. Their own action plans need no second approval.",
}

# Permissions that only make sense globally: a binding grants them only when both its
# environment and namespace are '*'.
GLOBAL_PERMISSIONS = {P.ADMIN_USERS, P.ADMIN_CONNECTORS, P.ADMIN_LLM, P.ADMIN_POLICIES, P.AUDIT_READ, P.TERMINAL_REVOKE,
                      P.KNOWLEDGE_SOURCE_ADMIN, P.KNOWLEDGE_INDEX, P.SKILL_REVIEW, P.SKILL_PUBLISH, P.LEARNING_EXPORT, P.ML_ADMIN}

# Raw PromQL cannot be reliably restricted to a namespace, so it requires a binding whose
# namespace is '*' for the environment. Namespace-scoped users get templated metric tools.
# A terminal's reach is set by its IAM role and Kubernetes RBAC, not by a MOKS namespace, so the same rule applies.
NAMESPACE_WIDE_PERMISSIONS = {P.METRICS_RAW_QUERY, P.TERMINAL_USE}


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
