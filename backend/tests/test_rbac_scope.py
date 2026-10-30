from moks.models import Environment
from moks.rbac import Binding, P, Principal, parse_grant
from moks.scope import ScopeError, effective_scope
import pytest


def env(name="prod", allowed=("*",), denied=()):
    return Environment(name=name, kind="eks", allowed_namespaces=list(allowed), denied_namespaces=list(denied))


def test_role_permissions_are_scoped_by_environment_and_namespace():
    p = Principal("u", "u@x", [Binding("investigator", "staging", "team-a")])
    assert p.can(P.INVESTIGATION_RUN, "staging", "team-a")
    assert not p.can(P.INVESTIGATION_RUN, "staging", "team-b")
    assert not p.can(P.INVESTIGATION_RUN, "prod", "team-a")
    assert not p.can(P.ACTION_EXECUTE, "staging", "team-a")  # investigators cannot execute


def test_glob_patterns():
    p = Principal("u", "u@x", [Binding("viewer", "prod-*", "shop-*")])
    assert p.can(P.INVESTIGATION_READ, "prod-eu", "shop-cart")
    assert not p.can(P.INVESTIGATION_READ, "staging", "shop-cart")


def test_admin_permissions_require_global_binding():
    scoped_admin = Principal("u", "u@x", [Binding("admin", "staging", "*")])
    assert scoped_admin.can(P.INVESTIGATION_RUN, "staging", "x")
    assert not scoped_admin.can(P.ADMIN_USERS, "*", "*")
    assert not scoped_admin.can(P.AUDIT_READ, "*", "*")
    assert Principal("a", "a@x", [Binding("admin")]).can(P.ADMIN_USERS, "*", "*")


def test_raw_promql_needs_namespace_wide_binding():
    ns_scoped = Principal("u", "u@x", [Binding("investigator", "prod", "team-a")])
    assert not ns_scoped.can(P.METRICS_RAW_QUERY, "prod", "*")
    assert Principal("u", "u@x", [Binding("investigator", "prod", "*")]).can(P.METRICS_RAW_QUERY, "prod", "*")


def test_approver_cannot_run_investigations_or_execute():
    p = Principal("u", "u@x", [Binding("approver")])
    assert p.can(P.ACTION_APPROVE, "prod", "x")
    assert not p.can(P.INVESTIGATION_RUN, "prod", "x")
    assert not p.can(P.ACTION_EXECUTE, "prod", "x")


def test_scope_is_intersection_of_rbac_and_environment():
    p = Principal("u", "u@x", [Binding("investigator", "*", "*")])
    s = effective_scope(p, env(allowed=("shop", "team-*"), denied=("team-secret",)), P.INVESTIGATION_RUN)
    assert s.allows("shop")
    assert s.allows("team-a")
    assert not s.allows("team-secret")  # denied by environment even though RBAC allows all
    assert not s.allows("kube-system")  # not in environment allowlist
    assert not s.cluster_wide
    with pytest.raises(ScopeError):
        s.require_cluster_wide("k8s_nodes")


def test_scope_without_binding_raises():
    with pytest.raises(ScopeError):
        effective_scope(Principal("u", "u@x", [Binding("viewer")]), env(), P.INVESTIGATION_RUN)


def test_parse_grant():
    assert parse_grant("operator:prod:shop") == Binding("operator", "prod", "shop")
    assert parse_grant("viewer") == Binding("viewer", "*", "*")
    with pytest.raises(ValueError):
        parse_grant("root:*:*")


def test_denied_namespaces_keep_environment_wide_reads_but_force_logql_pinning():
    p = Principal("u", "u@x", [Binding("investigator", "*", "*")])
    s = effective_scope(p, env(denied=("kube-system",)), P.INVESTIGATION_RUN)
    assert s.cluster_wide and not s.unrestricted
    assert not s.allows("kube-system") and s.allows("shop")
