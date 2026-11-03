"""Requests get the workflow and answer they ask for, about the target they name.

Regression for "How do I deploy jithendar.com?" turning into a cluster incident report about unrelated
roboshop-demo-* Argo CD errors. Fake Kubernetes, Git and Prometheus connectors and a real knowledge index,
through the real API, orchestrator, executor, validation and relevance check. No model unless a test scripts one.
"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import ActionPlan, AuditEvent, Investigation, InvestigationEvent, KnowledgeSource, LLMConfig
from moks.orchestrator import evidence as ev
from moks.orchestrator import intent, relevance
from moks.orchestrator.engine import Investigator
from moks.rag import ingest
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes, pod

EKS = "C0FFEE1234.gr7.eu-west-1.eks.amazonaws.com"
DNS_ERROR = (f"Failed to load target state: failed to get cluster info for \"https://{EKS}\": error synchronizing cache state : "
             f"Get \"https://{EKS}/version?timeout=32s\": dial tcp: lookup {EKS} on 10.96.0.10:53: no such host")


def argo_app(name, namespace, message=DNS_ERROR, server=f"https://{EKS}", path=None):
    return {"metadata": {"name": name}, "spec": {"project": "default", "destination": {"namespace": namespace, "server": server},
                                                 "source": {"repoURL": "https://github.com/mrjithendar/roboshop-ai.git", "path": path or f"eks/{name}", "targetRevision": "master"}},
            "status": {"sync": {"status": "Unknown"}, "health": {"status": "Healthy"}, "resources": [{}],
                       "conditions": [{"type": "ComparisonError", "message": message}]}}


ROBOSHOP_APPS = [argo_app("roboshop-demo-cart", "roboshop-demo-cart"), argo_app("roboshop-demo-web", "roboshop-demo-web"),
                 argo_app("roboshop-demo-db", "roboshop-demo-db"),
                 argo_app("roboshop-other", "roboshop-other", DNS_ERROR.replace(EKS, "OTHER9999.gr7.us-east-1.eks.amazonaws.com"), "https://OTHER9999.gr7.us-east-1.eks.amazonaws.com")]


def scenarios(jithendar_logs="GET / 200 3ms\n"):
    return {
        "jithendar": {"pods": [pod("jithendar-com-6d9f-abc", "jithendar", {"app": "jithendar-com"}), pod("jithendar-com-6d9f-def", "jithendar", {"app": "jithendar-com"})],
                      "services": [{"name": "jithendar-com", "selector": {"app": "jithendar-com"}}], "deployments": [{"name": "jithendar-com", "replicas": 2, "ready": 2}],
                      "logs": jithendar_logs, "ingresses": [{"name": "jithendar-com", "class": "traefik", "rules": [{"host": "jithendar.com", "path": "/", "service": "jithendar-com", "port": 80}]}]},
        "roboshop-ai": {"pods": [pod("cart-7f-xyz", "roboshop-ai", {"app": "cart"}, waiting="CrashLoopBackOff", last="Error", restarts=9)],
                        "services": [{"name": "cart", "selector": {"app": "cart"}}], "deployments": [{"name": "cart", "replicas": 1, "ready": 0}], "logs": "panic: redis unavailable\n", "ingresses": []},
        "roboshop-demo-web": {"pods": [], "services": [], "deployments": [], "logs": "", "ingresses": []},
    }


class Cluster(FakeKubernetes):
    scenarios: dict = {}
    argocd: list = []

    def _ns(self, ns):
        FakeKubernetes.calls.append(("ns", ns))
        if ns not in self.scenarios:
            raise ConnectorError("not_found", f"namespace {ns} not found")
        return self.scenarios[ns]

    async def list_namespaces(self):
        return sorted(self.scenarios) + ["argocd", "kube-system"]

    async def ingresses(self, namespace):
        return self._ns(namespace).get("ingresses", [])

    async def dns_health(self):
        FakeKubernetes.calls.append(("dns", "*"))
        return {"deployment": {"name": "coredns", "ready": 2, "desired": 2}, "services": [], "pods": []}


JITHENDAR_FILES = {
    "README.md": "# jithendar.com\n\nPersonal site and blog: a Next.js app in www/, Kubernetes manifests in k8s/.\n",
    "k8s/README.md": """# jithendar.com Kubernetes manifests

Everything jithendar.com runs on the cluster. Deployed with `kubectl apply -k k8s` (normally through
../deploy.sh), not Argo CD.

## Before the first deploy

These Secrets must exist in `jithendar`; they are never committed. `scripts/create-secrets.sh mk4` creates them.

- `jithendar-postgres` (key `password`), `jithendar-redis` (key `password`)
- `jithendar-com-env` (site configuration)

## Deploy

```sh
./deploy.sh          # the most recently pushed image
./deploy.sh <sha>    # a specific commit (also how you roll back)
```

It pins the image digest in `kustomization.yaml`, server dry-runs, applies, waits for the rollouts
(undoing the site's rollout if it fails) and runs `scripts/smoke.sh`.
""",
    "k8s/kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: jithendar\nresources:\n  - 00-namespace.yaml\n  - 30-jithendar-com.yaml\n",
    "k8s/00-namespace.yaml": "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: jithendar\n",
    ".github/workflows/ci-jithendar-com.yml": "name: ci-jithendar-com\non: {push: {branches: [master]}}\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: docker build -t site www\n      - run: ./deploy.sh ${{ github.sha }}\n",
    "deploy.sh": "#!/usr/bin/env bash\n# Deploy jithendar.com: pin the image digest, apply k8s/, wait for rollouts, smoke test.\nset -euo pipefail\n",
    "www/Dockerfile": "FROM node:26-alpine AS build\nFROM node:26-alpine\n",
}


class Repos:
    type = "git"
    repositories = ["mrjithendar/jithendar.com", "mrjithendar/roboshop-ai", "mrjithendar/tools"]
    files: dict[str, dict[str, str]] = {}
    calls: list[tuple] = []
    fail_reads = False

    def __init__(self, config, credential_ref=None):
        pass

    async def aclose(self):
        pass

    async def list_paths(self, repo, ref=None):
        Repos.calls.append(("list", repo))
        return list(self.files.get(repo, {}))

    async def read_file(self, repo, path, ref=None):
        Repos.calls.append(("read", repo, path))
        if self.fail_reads:
            raise ConnectorError("unavailable", "GitHub did not answer")
        if path not in self.files.get(repo, {}):
            raise ConnectorError("not_found", f"{path} not found")
        return {"repo": repo, "path": path, "ref": "master", "blob": "abc", "text": self.files[repo][path], "url": f"https://github.com/{repo}/blob/master/{path}"}

    async def recent_commits(self, repo, path, since, limit):
        return []


class Prometheus:
    type = "prometheus"
    calls: list[str] = []

    def __init__(self, config, credential_ref=None):
        pass

    async def aclose(self):
        pass

    async def alerts(self):
        Prometheus.calls.append("alerts")
        return [{"name": "KubePodCrashLooping", "state": "firing", "labels": {"severity": "critical"},
                 "active": [{"state": "firing", "labels": {"namespace": "roboshop-ai", "severity": "critical", "pod": "cart-7f-xyz"}, "value": "1"}]}]

    async def label_values(self, *a, **k):
        raise ConnectorError("unavailable", "not in this fake")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    Cluster.scenarios, Cluster.argocd = scenarios(), list(ROBOSHOP_APPS)
    Repos.files, Repos.calls, Repos.fail_reads = {"mrjithendar/jithendar.com": dict(JITHENDAR_FILES), "mrjithendar/roboshop-ai": {"README.md": "# RoboShop\n"}, "mrjithendar/tools": {}}, [], False
    Prometheus.calls = []
    FakeKubernetes.calls = []
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", Cluster)
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "git", Repos)
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "prometheus", Prometheus)
    registry._cache.clear()
    yield
    registry._cache.clear()


async def _env(git=True, prometheus=True) -> str:
    name = f"route-{uuid.uuid4().hex[:6]}"
    connectors = [{"type": "kubernetes", "name": "k8s", "config": {}}]
    if git:
        connectors.append({"type": "git", "name": "github", "config": {}})
    if prometheus:
        connectors.append({"type": "prometheus", "name": "prom", "config": {}})
    await make_env(name, connectors=connectors)
    await make_user(f"{name}@example.com", [f"investigator:{name}:*"])
    return name


async def _ask(client, env: str, question: str, **body) -> tuple[Investigation, list[dict]]:
    h = await login(client, f"{env}@example.com")
    r = await client.post("/moks/api/investigations", json={"question": question, "environment": env, **body}, headers=h)
    assert r.status_code == 201, r.text
    return await _run(r.json()["id"])


async def _run(inv_id: str) -> tuple[Investigation, list[dict]]:
    await Investigator(uuid.UUID(inv_id), "test-worker").run()
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(inv_id))
        events = [{"type": e.type, **e.payload} for e in (await db.execute(select(InvestigationEvent).where(InvestigationEvent.investigation_id == inv.id).order_by(InvestigationEvent.seq))).scalars()]
    assert inv.status == "completed", inv.error
    return inv, events


def tools(events: list[dict]) -> list[tuple[str, dict]]:
    return [(e["tool"], e["args"]) for e in events if e["type"] == "tool_call"]


async def _runbook(env: str, path: str, text: str, repository="mrjithendar/jithendar.com") -> None:
    async with session_scope() as db:
        src = KnowledgeSource(name=f"docs-{uuid.uuid4().hex[:6]}", type="upload", scope_environment=env, scope_namespace="*", config={"repository": repository})
        db.add(src)
        await db.flush()
        await ingest.add_upload(db, src, path, text.encode(), None)


# ---- the original question --------------------------------------------------------------------


async def test_how_do_i_deploy_returns_grounded_guidance_not_an_incident_report(client):
    env = await _env()
    inv, events = await _ask(client, env, "How do I deploy jithendar.com?")
    rep, called = inv.report, tools(events)
    names = [t for t, _ in called]

    assert rep["response_type"] == "deployment_guidance" and rep["request"]["primary"] == "deployment_guidance"
    # Target resolved from configuration and the repository, each with its provenance
    res = {r["field"]: r for r in rep["request"]["resolutions"]}
    assert res["repository"]["value"] == "mrjithendar/jithendar.com" and "allowlist" in res["repository"]["source"]
    assert res["namespace"]["value"] == "jithendar" and ("manifests declare" in res["namespace"]["source"] or "Ingress" in res["namespace"]["source"])
    # Steps, prerequisites and rollback come from k8s/README.md, quoted, with placeholders labeled
    a = rep["answer"]
    assert [s["command"] for s in a["steps"] if s["command"]][:2] == ["./deploy.sh", "./deploy.sh <sha>"]
    assert any(s["command"] == "./deploy.sh <sha>" for s in a["rollback"])
    assert any("jithendar-postgres" in p["text"] for p in a["prerequisites"])
    assert any("smoke" in v["text"] for v in a["verification"])
    assert all(i["sources"] and i["support"] == "quoted" for s in ("prerequisites", "steps", "verification", "rollback") for i in a[s])
    assert any("<sha>" in x for x in a["assumptions"])
    assert "not Argo CD" in a["overview"][0]["text"]
    assert any(c["kind"] == "ci" and "deploy.sh" in c["detail"] for c in a["configuration"])
    # Nothing about RoboShop, no incident sweep, no alerts, no all-apps Argo CD scan
    blob = json.dumps(rep).lower()
    assert "roboshop-demo" not in blob and "comparisonerror" not in blob and "kubepodcrashlooping" not in blob
    assert not set(names) & {"k8s_triage", "k8s_list_pods", "prom_alerts", "k8s_pod_logs", "k8s_nodes"}
    assert ("k8s_argocd_status", {}) not in called and Prometheus.calls == []
    assert all(ns == "jithendar" for t, a in called if (ns := a.get("namespace")))
    assert not any(c == ("ns", "roboshop-ai") for c in FakeKubernetes.calls)
    assert rep["verified_causes"] == [] and rep["suspected_causes"] == []
    assert inv.tool_calls <= 12
    assert rep["relevance"]["status"] == "passed", rep["relevance"]
    # Nothing was changed
    async with session_scope() as db:
        assert not (await db.execute(select(ActionPlan).where(ActionPlan.investigation_id == inv.id))).first()
        assert not (await db.execute(select(AuditEvent).where(AuditEvent.action.like("action.%"), AuditEvent.actor_email == f"{env}@example.com"))).first()
    # Routing is traced, without the question text
    routing = next(e for e in events if e["type"] == "routing")
    assert routing["primary"] == "deployment_guidance" and "question" not in routing
    assert any(t["purpose"] and t["tool"] == "git_read_file" for t in rep["trace"]["tools"])
    assert any(s["path"] == "k8s/README.md" and s["used"] for s in rep["trace"]["sources"])


async def test_missing_runbook_is_named_as_a_gap_and_no_steps_are_invented(client):
    Repos.files["mrjithendar/jithendar.com"] = {"src/main.py": "print('hi')\n"}
    env = await _env()
    inv, events = await _ask(client, env, "How do I deploy jithendar.com?")
    a = inv.report["answer"]
    assert inv.report["response_type"] == "deployment_guidance"
    assert a["steps"] == [] and a["rollback"] == []
    assert any(g["kind"] == "missing_source" and "mrjithendar/jithendar.com" in g["detail"] for g in a["gaps"])
    assert inv.report["relevance"]["status"] in ("passed", "corrected")
    assert "roboshop" not in json.dumps(inv.report).lower()


async def test_retrieval_and_tool_failures_are_not_reported_as_missing_documentation(client, monkeypatch):
    from moks.rag import service

    async def broken(*a, **k):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(service, "search", broken)
    Repos.fail_reads = True
    env = await _env()
    inv, _ = await _ask(client, env, "How do I deploy jithendar.com?")
    gaps = inv.report["answer"]["gaps"]
    kinds = {g["kind"] for g in gaps}
    assert "retrieval_failure" in kinds
    assert any("Knowledge search failed" in g["detail"] for g in gaps) and any("Could not read mrjithendar/jithendar.com:" in g["detail"] for g in gaps)
    assert not any(g["kind"] == "missing_source" for g in gaps)  # a failure is not proof that nothing is written down
    assert inv.report["answer"]["steps"] == []


async def test_conflicting_and_stale_documentation_is_surfaced(client):
    env = await _env()
    await _runbook(env, "runbooks/jithendar-com-deploy-old.md", "---\nstatus: deprecated\n---\n# Deploying jithendar.com\n\n## Deploy\n\n```sh\nargocd app sync jithendar-com\n```\n")
    inv, _ = await _ask(client, env, "How do I deploy jithendar.com?")
    a = inv.report["answer"]
    assert any(c["kind"] == "stale" and "jithendar-com-deploy-old.md" in c["detail"] for c in a["conflicts"])
    assert any(c["kind"] == "conflict" and "deployment commands" in c["detail"] for c in a["conflicts"])
    assert any(s["command"] == "./deploy.sh" for s in a["steps"])
    assert any(s["path"] == "runbooks/jithendar-com-deploy-old.md" for s in a["sources"])


async def test_ambiguous_target_asks_one_focused_question(client):
    Repos.repositories = ["acme/shop-api", "acme/shop-web"]
    Repos.files = {"acme/shop-api": {"README.md": "# API\n"}, "acme/shop-web": {"README.md": "# Web\n"}}
    try:
        env = await _env()
        inv, events = await _ask(client, env, "How do I deploy shop.example?")
        a = inv.report["answer"]
        assert inv.report["request"]["candidates"]["repository"] == ["acme/shop-api", "acme/shop-web"]
        assert "repository" in inv.report["request"]["unresolved"]
        assert a["question_for_user"] and "acme/shop-api" in a["question_for_user"] and "acme/shop-web" in a["question_for_user"]
        assert a["steps"] == []

        inv, events = await _ask(client, env, "How do I deploy it?")
        assert "target" in inv.report["request"]["unresolved"] and inv.report["answer"]["question_for_user"].startswith("Which application")
        assert not any(t.startswith(("k8s_", "prom_", "git_")) for t, _ in tools(events))
    finally:
        Repos.repositories = ["mrjithendar/jithendar.com", "mrjithendar/roboshop-ai", "mrjithendar/tools"]


async def test_an_unrelated_critical_alert_does_not_hijack_deployment_guidance(client):
    env = await _env(prometheus=True)
    inv, events = await _ask(client, env, "How do I deploy jithendar.com?")
    assert Prometheus.calls == [] and "prom_alerts" not in [t for t, _ in tools(events)]
    assert "KubePodCrashLooping" not in json.dumps(inv.report) and "cart-7f-xyz" not in json.dumps(inv.report)


# ---- the other request types -----------------------------------------------------------------


async def test_502_starts_with_the_target_application_and_its_ingress(client):
    env = await _env()
    inv, events = await _ask(client, env, "Why is jithendar.com returning 502?")
    called = tools(events)
    rep = inv.report
    assert rep["response_type"] == "troubleshooting"
    assert called[0] == ("k8s_ingresses", {"namespace": "jithendar"})  # which namespace serves the domain, from its Ingress
    assert ("k8s_triage", {"namespace": "jithendar"}) in called
    res = {r["field"]: r for r in rep["request"]["resolutions"]}
    assert res["namespace"]["value"] == "jithendar" and "Ingress in jithendar serves jithendar.com" in res["namespace"]["source"] and res["namespace"]["evidence"]
    assert inv.namespace == "jithendar"
    # Never another application's namespace, and the unrelated alert is filtered out of what the report says
    assert all(a.get("namespace") in (None, "jithendar") for _, a in called)
    assert not any(c == ("ns", "roboshop-ai") for c in FakeKubernetes.calls)
    facts = json.dumps(rep["observed_facts"])
    assert "roboshop-ai" not in facts and "KubePodCrashLooping" not in facts
    assert all(f.get("namespace") in (None, "jithendar") for f in rep["findings"])


async def test_a_shared_dependency_widens_scope_with_its_evidence(client):
    Cluster.scenarios = scenarios("dial tcp: lookup jithendar-postgres on 10.96.0.10:53: no such host\n")
    env = await _env()
    inv, events = await _ask(client, env, "Why is jithendar.com returning 502?")
    exp = inv.report["request"]["expansions"]
    assert any("k8s_dns_health" in x["tools"] and x["evidence"] and "name-resolution" in x["reason"] for x in exp)
    assert ("k8s_dns_health", {}) in tools(events) and ("dns", "*") in FakeKubernetes.calls
    assert not any(c == ("ns", "roboshop-ai") for c in FakeKubernetes.calls)  # widened to the dependency, not to everything


async def test_a_shared_argo_failure_is_kept_apart_from_the_target(client):
    Cluster.argocd = [*ROBOSHOP_APPS, argo_app("jithendar-com", "jithendar")]
    env = await _env()
    inv, events = await _ask(client, env, "Why is the Argo CD app for jithendar.com failing?")
    rep = inv.report
    assert [f["resources"] for f in rep["findings"] if f["kind"].startswith("argocd")] == [["application/jithendar-com"]]
    shared = rep["shared_findings"]
    assert len(shared) == 1 and shared[0]["kind"] == "argocd_shared_cause" and EKS.lower() in shared[0]["title"].lower()
    assert set(shared[0]["resources"]) == {"application/jithendar-com", "application/roboshop-demo-cart", "application/roboshop-demo-web", "application/roboshop-demo-db"}
    assert any(x["reason"].startswith("4 Argo CD applications") for x in rep["request"]["expansions"])
    assert any(x["count"] == 1 and "outside" in x["reason"] for x in rep["excluded"])  # roboshop-other fails for another reason


async def test_health_of_all_argo_apps_groups_genuinely_shared_failures(client):
    env = await _env()
    inv, events = await _ask(client, env, "Check the health of all Argo CD applications.")
    rep = inv.report
    assert rep["response_type"] == "health_check" and rep["request"]["scope"] == "cluster"
    assert any(t == "k8s_argocd_status" and a == {} for t, a in tools(events))
    groups = [f for f in rep["findings"] if f["kind"] == "argocd_shared_cause"]
    assert len(groups) == 1 and len(groups[0]["resources"]) == 3 and len(groups[0]["affected"]) == 3 and groups[0]["evidence"]
    # One error with a similar message but a different cluster is not folded in
    other = [f for f in rep["findings"] if f["kind"] == "argocd_comparison_error"]
    assert [f["resources"] for f in other] == [["application/roboshop-other"]]
    # A ComparisonError is an observed condition, never a verified configuration defect, and claims nothing about workloads
    assert not any("argocd" in c["check"] for c in rep["verified_causes"])
    assert {v["category"] for v in rep["verified_findings"] if "argocd" in v["check"]} == {"observed_condition"}
    assert "not affected" not in json.dumps(rep) and "unaffected" not in json.dumps(rep).lower()
    assert all("Fix what the condition names" not in (f.get("remediation_hint") or "") for f in rep["findings"])
    assert any("argocd cluster list" in c for c in groups[0]["next_checks"])


async def test_explain_argo_cd_answers_without_live_queries(client):
    env = await _env()
    inv, events = await _ask(client, env, "Explain Argo CD.")
    assert inv.report["response_type"] == "conceptual"
    assert {t for t, _ in tools(events)} <= {"kb_search"} and inv.tool_calls <= 2
    assert FakeKubernetes.calls == [] and Prometheus.calls == [] and Repos.calls == []


async def test_deploy_to_production_goes_through_the_authorization_workflow(client):
    env = await _env()
    inv, events = await _ask(client, env, "Deploy jithendar.com to production.")
    rep = inv.report
    ex = rep["answer"]["execution"]
    assert rep["response_type"] == "execution_plan" and rep["request"]["mode"] == "execution"
    assert ex["executed"] is False and ex["allowlisted"] is False and "no allowlisted deploy action" in ex["note"]
    assert any("action.approve" in s for s in ex["authorization"]) and {a["action"] for a in ex["actions"]} <= {"open_pull_request", "rollback_deployment"}
    assert any("not marked as production" in a for a in rep["answer"]["assumptions"])  # asked for production; this environment is not
    assert rep["summary"].startswith("MOKS has no allowlisted deploy action")
    assert any(s["command"] == "./deploy.sh" for s in rep["answer"]["steps"])  # steps for a person, from the sources
    assert all(not t.startswith(("k8s_triage", "k8s_pod_logs", "prom_")) for t, _ in tools(events))
    async with session_scope() as db:
        assert not (await db.execute(select(ActionPlan).where(ActionPlan.investigation_id == inv.id))).first()


async def test_a_roboshop_conversation_does_not_pin_the_next_question(client):
    env = await _env()
    h = await login(client, f"{env}@example.com")
    first = (await client.post("/moks/api/investigations", json={"question": "why is cart crashing?", "environment": env, "namespace": "roboshop-ai"}, headers=h)).json()
    await _run(first["id"])
    r = await client.post(f"/moks/api/conversations/{first['conversation_id']}/messages", json={"content": "How do I deploy jithendar.com?"}, headers=h)
    assert r.status_code == 201, r.text
    async with session_scope() as db:
        nxt = (await db.execute(select(Investigation).where(Investigation.conversation_id == uuid.UUID(first["conversation_id"]), Investigation.id != uuid.UUID(first["id"])))).scalar_one()
        queued = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == nxt.id, InvestigationEvent.seq == 0))).scalar_one()
    assert nxt.namespace != "roboshop-ai" and "conversation" not in queued["namespace_reason"]
    inv, events = await _run(str(nxt.id))
    assert inv.report["request"]["history_used"] is False and inv.report["request"]["namespaces"] == ["jithendar"]
    blob = json.dumps({k: inv.report[k] for k in ("summary", "answer", "observed_facts", "findings")})
    assert "cart" not in blob and "roboshop" not in blob.lower()
    assert all(a.get("namespace") in (None, "jithendar") for _, a in tools(events))

    # A follow-up that names nothing new stays in the conversation's namespace
    r = await client.post(f"/moks/api/conversations/{first['conversation_id']}/messages", json={"content": "why is it still restarting?"}, headers=h)
    async with session_scope() as db:
        last = (await db.execute(select(Investigation).where(Investigation.conversation_id == uuid.UUID(first["conversation_id"])).order_by(Investigation.created_at.desc()))).scalars().first()
    assert last.namespace == "roboshop-ai"


async def test_a_planner_cannot_wander_into_other_applications(client, monkeypatch):
    """With a model in the loop: a planner asking for another namespace is refused (and audited) unless evidence
    shows a dependency; invented guidance commands are removed."""
    from moks.llm import providers

    seen: list[dict] = []

    def llm(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        system = body["messages"][0]["content"]
        if "operational guidance" in system:
            content = {"summary": "Run the deploy script from the repository root.",
                       "steps": [{"text": "Deploy the latest image", "command": "./deploy.sh", "sources": ["E3", "E4", "E5", "E6"]},
                                 {"text": "Delete the namespace first", "command": "kubectl delete namespace jithendar", "sources": ["E4"]},
                                 {"text": "Uncited step", "command": "make deploy", "sources": []}]}
        elif '"tool_calls"' in system:
            content = {"tool_calls": [{"tool": "k8s_triage", "args": {"namespace": "roboshop-ai"}, "purpose": "look around"}],
                       "expand": {"namespaces": ["roboshop-ai"], "reason": "maybe related", "evidence": ["E1"]}, "done": len(seen) > 3}
        else:
            content = {"summary": "jithendar.com pods are serving.", "observed_facts": [], "suspected_causes": [], "missing_evidence": [], "next_steps": ["Fix what the condition names"], "proposed_remediation": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    real_init = providers.Provider.__init__
    monkeypatch.setattr(providers.Provider, "__init__", lambda self, cfg, transport=None: real_init(self, cfg, httpx.MockTransport(llm)))
    monkeypatch.setenv("MOKS_CRED_TEST_ROUTING", "sk-test-not-real-key-000000")
    async with session_scope() as db:
        for other in (await db.execute(select(LLMConfig).where(LLMConfig.is_active.is_(True)))).scalars():
            other.is_active = False
        db.add(LLMConfig(name=f"route-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="gpt-test", endpoint="http://llm.test/v1",
                         credential_ref={"type": "env", "name": "MOKS_CRED_TEST_ROUTING"}, is_active=True, input_price_per_mtok=1, output_price_per_mtok=2, daily_budget_usd=100))
    try:
        env = await _env()
        inv, events = await _ask(client, env, "Why is jithendar.com returning 502?")
        assert not any(c == ("ns", "roboshop-ai") for c in FakeKubernetes.calls)  # asked for, never reached the cluster
        assert any(r.get("tool") == "k8s_triage" and r.get("namespace") == "roboshop-ai" for r in inv.report["trace"]["scope_rejections"])
        # "expand" cited E1, which does not name roboshop-ai, so it was refused
        assert all("roboshop-ai" not in x["namespaces"] for x in inv.report["request"]["expansions"])
        assert any(r.get("namespaces") == ["roboshop-ai"] and "refused" in r["reason"] for r in inv.report["trace"]["scope_rejections"])
        async with session_scope() as db:
            refused = (await db.execute(select(AuditEvent).where(AuditEvent.resource_id == "k8s_triage", AuditEvent.decision == "deny"))).scalars().all()
        assert any(e.details.get("kind") == "request_scope" and e.namespace == "roboshop-ai" for e in refused)
        # The drafted generic step does not survive the relevance check
        assert "Fix what the condition names" not in inv.report["next_steps"]
        assert "actionable" in inv.report["trace"]["relevance"]["failed"]

        inv, _ = await _ask(client, env, "How do I deploy jithendar.com?")
        steps = inv.report["answer"]["steps"]
        assert [s["command"] for s in steps] == ["./deploy.sh", None]
        assert inv.report["generated_by"] == "llm" and any("does not occur in" in n for n in inv.report["notes"]) and any("cites no source" in n for n in inv.report["notes"])
    finally:
        async with session_scope() as db:
            for c in (await db.execute(select(LLMConfig).where(LLMConfig.is_active.is_(True)))).scalars():
                c.is_active = False


# ---- units: classification, claims, the relevance check ----------------------------------------


@pytest.mark.parametrize("question,primary,extra", [
    ("How do I deploy jithendar.com?", "deployment_guidance", None),
    ("Why is jithendar.com returning 502?", "troubleshooting", None),
    ("Check the health of all Argo CD applications.", "health_check", "cluster"),
    ("Explain Argo CD.", "conceptual", None),
    ("Deploy jithendar.com to production.", "execution", None),
    ("Can you restart the checkout deployment?", "execution", None),
    ("How do I roll back mrjithendar/jithendar.com?", "deployment_guidance", None),
    ("How do I deploy jithendar.com? It is returning 502", "deployment_guidance", "troubleshooting"),
    ("what is wrong", "troubleshooting", None),
    ("Show pods restarting in the last 30 minutes.", "troubleshooting", None),
    ("latency regression after the release in shop", "troubleshooting", None),
])
def test_requests_are_classified_by_what_they_ask(question, primary, extra):
    req = intent.build(question)
    assert req.primary == primary
    if extra == "cluster":
        assert req.scope == "cluster"
    elif extra:
        assert extra in req.intents
    assert req.mode != "execution" or primary == "execution"  # only a request to act is ever an execution


def test_a_model_cannot_turn_a_question_into_an_execution():
    scores, signals, _ = intent.classify("what about the jithendar.com rollout")
    req = intent.refine(intent.build("what about the jithendar.com rollout", scores=scores, signals=signals), {"primary": "execution", "intents": ["execution"]}, scores)
    assert req.primary != "execution" and req.mode != "execution" and any("change something" in a for a in req.ambiguities)


def test_file_names_are_not_targets_and_nothing_is_mapped_by_resemblance():
    assert [t.mention for t in intent.extract_targets("why does values.yaml in k8s/README.md differ for jithendar.com")] == ["jithendar.com"]
    t = intent.extract_targets("deploy jithendar.com")[0]
    assert intent.namespace_candidates(t, ["jithendar", "roboshop-ai"]) == ([], ["jithendar"])  # a candidate, not a resolution
    assert intent.match_repositories(t, ["mrjithendar/jithendar.com", "mrjithendar/tools"]) == (["mrjithendar/jithendar.com"], [])


def _ledger(*rows):
    return ev.Ledger.build([{"ref": f"E{i + 1}", "tool": t, "args": a, "ok": True, "summary": s, "data": d, "collected_at": None} for i, (t, a, s, d) in enumerate(rows)])


def test_workload_reassurance_needs_workload_evidence():
    argo = ("k8s_argocd_status", {}, "4 Argo CD applications, 3 with problems", {"problems": [{"name": "roboshop-demo-cart"}]})
    ledger = _ledger(argo)
    v = ev.judge("roboshop-demo-cart's running workloads are unaffected", ["E1"], ledger, [])
    assert v.disposition == "hypothesis" and any("no cited evidence reads those workloads" in r for r in v.reasons)
    assert ev.judge_sentence("The running workloads are not affected.", ledger, []) == (False, "claims workloads are unaffected, but no evidence read those workloads")
    ledger = _ledger(argo, ("k8s_workloads", {"namespace": "roboshop-demo-cart"}, "2 workloads in roboshop-demo-cart; 0 below desired replicas", [{"name": "cart", "ready": 1, "desired": 1}]))
    assert ev.judge("roboshop-demo-cart workloads keep running", ["E2"], ledger, []).disposition == "accepted"


def test_the_relevance_check_corrects_once_and_says_what_failed():
    req = intent.build("Why is jithendar.com returning 502?")
    req.resolve("namespace", "jithendar", "test")
    req.scope = "target"
    # Workload evidence exists, but only for another namespace: it cannot back a claim about this target's workloads
    ledger = _ledger(("k8s_triage", {"namespace": "roboshop-ai"}, "cart crashlooping", {}), ("k8s_ingresses", {"namespace": "jithendar"}, "2 pods ready", {}))
    payload = {"response_type": "troubleshooting", "summary": "jithendar.com returns 502. Running workloads are unaffected.",
               "observed_facts": [{"statement": "cart is crashlooping", "evidence": ["E1"]}, {"statement": "2 pods ready", "evidence": ["E2"]}],
               "findings": [{"title": "cart crashloop", "namespace": "roboshop-ai", "severity": "high", "evidence": ["E1"]}],
               "next_steps": ["Fix what the condition names", "Read the logs of jithendar-com-6d9f-abc"], "proposed_remediation": [], "missing_evidence": [], "answer": {}}
    out = relevance.check(payload, req, ledger, payload["findings"])
    assert out["status"] == "corrected" and out["attempts"] == 2
    failed = {c["name"] for c in out["first_pass"] if not c["passed"]}
    assert {"on_target", "actionable", "claims_supported"} <= failed
    assert [f["statement"] for f in payload["observed_facts"]] == ["2 pods ready"] and payload["findings"] == []
    assert payload["next_steps"] == ["Read the logs of jithendar-com-6d9f-abc"] and "unaffected" not in payload["summary"]
    assert payload["excluded"][0]["namespaces"] == ["roboshop-ai"]


def test_the_relevance_check_never_loops():
    req = intent.build("How do I deploy jithendar.com?")
    payload = {"response_type": "troubleshooting", "summary": "", "answer": {}, "observed_facts": [], "findings": [], "missing_evidence": []}
    out = relevance.check(payload, req, _ledger(), [])
    assert out["attempts"] == 2 and out["status"] in ("corrected", "failed")
    if out["status"] == "failed":
        assert payload["summary"].startswith("This answer did not pass")


async def test_the_routing_evaluation_dataset_scores_in_rules_mode():
    from moks.evals import runner

    rid = await runner.start("routing", "rules")
    out = await runner.execute(rid)
    async with session_scope() as db:
        from moks.models import EvalRun

        results = (await db.get(EvalRun, rid)).results
    assert out["cases"] == 4 and out["score"] == 1.0, [(r["case"], {k: v for k, v in r["checks"].items() if not v}) for r in results]
