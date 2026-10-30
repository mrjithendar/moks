"""SRE skills (SKILL.md): validation, selection, policy-checked execution and remediation hints."""
import uuid

import pytest

from moks import skills
from moks.connectors import registry
from moks.db import session_scope
from moks.models import Investigation
from moks.orchestrator.engine import Investigator
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes

GOOD = """---
name: demo-skill
description: "Demo: check pods and events."
triggers: {keywords: [demo], findings: [crashloop]}
steps:
  - {tool: k8s_events, args: {namespace: "{namespace}"}, purpose: events}
---
# Demo
Body.
"""


def test_builtin_skills_all_load():
    lib = skills.load(extra_dir="")
    assert lib.errors == [] and len(lib.skills) >= 13
    assert {"crashloop-triage", "oomkilled-memory", "http-5xx-service-routing", "cost-spike-finops"} <= set(lib.skills)


@pytest.mark.parametrize("mutation,error", [
    ("{tool: scale_deployment, args: {}}", "unknown tool"),
    ("{tool: k8s_events, args: {namespace: \"{secret}\"}}", "unknown template variables"),
])
def test_skills_cannot_mutate_or_template_freely(mutation, error):
    bad = GOOD.replace('{tool: k8s_events, args: {namespace: "{namespace}"}, purpose: events}', mutation)
    with pytest.raises(ValueError, match=error):
        skills.parse(bad, "x", "custom")


def test_remediation_must_be_allowlisted():
    bad = GOOD.replace("---\n# Demo", "remediation:\n  - {when: crashloop, action: delete_namespace}\n---\n# Demo")
    with pytest.raises(ValueError, match="not allowlisted"):
        skills.parse(bad, "x", "custom")


def test_custom_skills_load_and_override(tmp_path):
    (tmp_path / "demo.skill.md").write_text(GOOD)
    (tmp_path / "broken.skill.md").write_text("no frontmatter")
    lib = skills.load(extra_dir=str(tmp_path))
    assert lib.skills["demo-skill"].source == "custom" and any("broken" in e["path"] for e in lib.errors)


def test_selection_prefers_findings_over_keywords():
    picked = skills.select("why does the payments pod keep crashing", {"crashloop"})
    assert picked[0][0].name == "crashloop-triage" and "findings: crashloop" in picked[0][2]
    assert skills.select("hello world", set()) == []


async def test_investigation_runs_matching_skill_and_suggests_allowlisted_actions(client, monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    name = f"sk-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}])
    await make_user(f"sk-{name}@example.com", [f"investigator:{name}:*"])
    h = await login(client, f"sk-{name}@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "Why is payments crashing?", "environment": name, "namespace": "shop"},
                             headers=h)).json()
    await Investigator(uuid.UUID(inv["id"]), "t").run()
    async with session_scope() as db:
        done = await db.get(Investigation, uuid.UUID(inv["id"]))
    rep = done.report
    assert "crashloop-triage" in [s["name"] for s in rep["skills"]]
    events = (await client.get(f"/moks/api/investigations/{inv['id']}/events")).text
    assert "[crashloop-triage] output of the crashed container" in events  # skill step ran through the executor
    actions = {(a["action"], a["target_name"]) for a in rep["skill_actions"]}
    assert ("rollback_deployment", "payments") in actions
    assert all(a["action"] in ("rollback_deployment", "open_pull_request", "patch_resources", "apply_config_patch") for a in rep["skill_actions"])


async def test_skills_api(client):
    await make_user("skview@example.com", ["viewer"])
    await login(client, "skview@example.com")
    lst = (await client.get("/moks/api/skills")).json()
    assert any(s["name"] == "dns-resolution" for s in lst["skills"]) and lst["errors"] == []
    one = (await client.get("/moks/api/skills/dns-resolution")).json()
    assert one["steps"] and "CoreDNS" in one["body"]


def test_config_audit_never_exposes_values_and_finds_risks():
    from moks.diagnosis.config_audit import audit, findings_from

    pod = {"metadata": {"name": "api-7d9f-x", "namespace": "shop", "labels": {"pod-template-hash": "7d9f"},
                        "ownerReferences": [{"kind": "ReplicaSet", "name": "api-7d9f"}]},
           "spec": {"containers": [{"name": "app", "image": "registry/api:latest",
                                    "env": [{"name": "DB_PASSWORD", "value": "hunter2-literal"},
                                            {"name": "MODE", "value": "prod"},
                                            {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "api", "key": "token", "optional": True}}}],
                                    "envFrom": [{"secretRef": {"name": "all-keys"}}, {"configMapRef": {"name": "api-config"}}],
                                    "securityContext": {"privileged": True}}],
                    "volumes": [{"name": "h", "hostPath": {"path": "/var/run"}}]}}
    events = [{"type": "Warning", "reason": "Failed", "object": "Pod/api-7d9f-x",
               "message": 'Error: couldn\'t find key DB_URL in ConfigMap shop/api-config', "lastSeen": "now"}]
    r = audit([pod], events)
    import json

    assert "hunter2-literal" not in json.dumps(r)
    kinds = {i["kind"] for i in r["workloads"][0]["issues"]}
    assert {"plaintext_secret_env", "missing_requests", "missing_memory_limit", "missing_readiness_probe", "mutable_image_tag",
            "privileged_container", "host_path_volume", "optional_secret_ref", "secret_envfrom"} <= kinds
    assert r["workloads"][0]["workload"] == "Deployment/api" and r["workloads"][0]["references"]["secrets"] == ["all-keys", "api"]
    assert r["missing_references"][0]["key"] == "DB_URL"
    f = {x["kind"] for x in findings_from(r, "shop", "E1")}
    assert {"config_ref_missing", "plaintext_secret_env", "privileged_container"} <= f and "optional_secret_ref" not in f  # low severity not promoted


def test_rightsizing_suggestions_carry_valid_patch_parameters():
    from moks.actions.catalog import PatchResourcesP
    from moks.connectors.finops import fmt_mem, rightsize

    assert fmt_mem(2.5 * 1024**3) == "2560Mi"  # integer Mi only: accepted by patch_resources
    recs = rightsize([{"namespace": "shop", "workload": "Deployment/big", "container": "app", "replicas": 2, "cpu_request": 2.0, "cpu_limit": 0,
                       "mem_request": 8 * 1024**3, "mem_limit": 8 * 1024**3, "cpu_p95": 0.2, "mem_max": 2.4 * 1024**3}], 0.04, 0.005)
    rec = recs[0]["recommended"]
    PatchResourcesP.model_validate({"container": "app", "requests": {"cpu": rec["cpu_request"], "memory": rec["mem_request"]},
                                    "limits": {"memory": rec["mem_limit"]} if rec["mem_limit"] else {}})
