"""Argo CD status: messages here are the real ones from mk4, so parsing is tested against what Argo sends."""
import uuid
from types import SimpleNamespace

import pytest
import yaml

from moks import argocd
from moks.connectors import registry
from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import Investigation
from moks.orchestrator.engine import Investigator
from moks.orchestrator.report import rules_report, verify_causes
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes

ROBOSHOP = "https://github.com/mrjithendar/roboshop-ai.git"
WRAP = "Failed to load target state: failed to generate manifest for source 1 of 1: rpc error: code = Unknown desc = Manifest generation error (cached): "
GONE = WRAP + "services/frontend: app path does not exist"
HELM = ("Failed to load target state: failed to generate manifest for source 1 of 2: rpc error: code = Unknown desc = Manifest generation error (cached): failed to execute helm template command: "
        "failed running helm: `helm template . --name-template cert-manager --namespace cert-manager --kube-version 1.37.0 --values <path to cached source>/minikube/cert-manager/values.yaml <api versions removed> --include-crds` "
        "failed exit status 1: Error: open <path to cached source>/minikube/cert-manager/values.yaml: no such file or directory")


def app(name="frontend", sync="Unknown", health="Healthy", conditions=(), op=None, namespace="roboshop-ai", **spec):
    return {"metadata": {"name": name}, "spec": {"project": "platform", "destination": {"namespace": namespace}, **(spec or {"source": {"repoURL": ROBOSHOP, "path": "services/frontend", "targetRevision": "master"}})},
            "status": {"sync": {"status": sync}, "health": {"status": health}, "resources": [{}, {}], "conditions": [{"type": t, "message": m} for t, m in conditions], "operationState": op or {}}}


FAILING = app(conditions=[("ComparisonError", GONE)], op={"phase": "Error", "message": "ComparisonError: " + GONE, "finishedAt": "2026-10-31T07:40:54Z"})
CHART = app("cert-manager", conditions=[("ComparisonError", HELM)], namespace="cert-manager", sources=[
    {"repoURL": "quay.io/jetstack/charts", "chart": "cert-manager", "targetRevision": "v1.20.4", "helm": {"parameters": [{"name": "password", "value": "hunter2"}], "values": "token: hunter2"}},
    {"repoURL": ROBOSHOP, "targetRevision": "master", "ref": "values"}])
GOOD = app("cart-service", sync="Synced")


class Git:
    repositories = ["mrjithendar/roboshop-ai", "mrjithendar/tools"]
    trees = {"mrjithendar/roboshop-ai": ["services/web/frontend/index.html", "services/web/nginx/nginx.conf"],
             "mrjithendar/tools": ["minikube/cert-manager/values.yaml", "observability/jaeger/deployment.yaml"]}

    async def list_paths(self, repo):
        return self.trees[repo]


class K8s:
    def __init__(self, items=(), error=None):
        self.items, self.error = list(items), error

    async def argocd_list(self):
        if self.error:
            raise self.error
        return self.items


def everything(ns):
    return True


def test_the_cause_is_taken_out_of_argos_wrapping():
    assert argocd.reason(GONE) == "services/frontend: app path does not exist"
    assert argocd.reason(HELM) == "Error: open <path to cached source>/minikube/cert-manager/values.yaml: no such file or directory"


def test_a_missing_path_and_a_missing_values_file_are_both_recognised():
    assert argocd.app_row(FAILING)["missing"] == {"path": "services/frontend", "repo": ROBOSHOP}
    assert argocd.app_row(CHART)["missing"] == {"path": "minikube/cert-manager/values.yaml", "repo": ROBOSHOP}


def test_helm_parameters_values_and_url_credentials_never_leave():
    row = argocd.app_row(app(source={"repoURL": "https://user:s3cr3t@github.com/o/r.git", "path": "p"}))
    assert "s3cr3t" not in str(row) and "hunter2" not in str(argocd.app_row(CHART))


def test_warnings_alone_are_not_a_failure():
    row = argocd.app_row(app(sync="Synced", conditions=[("OrphanedResourceWarning", "1 orphaned")]))
    assert row["errors"] == [] and not argocd.is_problem(row) and argocd.finding(row) is None


async def test_it_names_each_failing_app_and_where_git_has_it_now():
    s = await argocd.status(K8s([FAILING, CHART, GOOD]), everything, Git())
    assert s["total"] == 3 and s["healthy"] == ["cart-service"] and [f["kind"] for f in s["findings"]] == ["argocd_source_missing"] * 2
    by_app = {f["resources"][0]: f for f in s["findings"]}
    assert "mrjithendar/roboshop-ai:services/web/frontend" in by_app["application/frontend"]["detail"]
    assert "mrjithendar/tools:minikube/cert-manager/values.yaml" in by_app["application/cert-manager"]["detail"]
    assert all("without prune" in f["remediation_hint"] for f in s["findings"])
    assert "not affected" in by_app["application/frontend"]["detail"] and by_app["application/frontend"]["severity"] == "medium"


async def test_exact_paths_in_another_repository_rank_first():
    paths = {"mrjithendar/roboshop-ai": ["services/mongodb/Dockerfile"], "mrjithendar/tools": ["minikube/mongodb/service.yaml"]}
    git = Git()
    git.trees = paths
    row = app("mongodb", conditions=[("ComparisonError", WRAP + "minikube/mongodb: app path does not exist")], source={"repoURL": ROBOSHOP, "path": "minikube/mongodb"})
    s = await argocd.status(K8s([row]), everything, git)
    assert [c["repository"] for c in s["problems"][0]["missing"]["candidates"]] == ["mrjithendar/tools", "mrjithendar/roboshop-ai"]


async def test_a_repository_git_cannot_list_is_reported_and_the_rest_still_works():
    class Partial(Git):
        async def list_paths(self, repo):
            if repo.endswith("tools"):
                raise ConnectorError("too_large", "tree too large")
            return await super().list_paths(repo)

    s = await argocd.status(K8s([FAILING]), everything, Partial())
    assert "tree too large" in s["notes"][0] and s["findings"][0]["kind"] == "argocd_source_missing"


async def test_without_git_it_still_gives_the_argo_error():
    s = await argocd.status(K8s([FAILING]), everything)
    assert "services/frontend" in s["findings"][0]["detail"]


async def test_failed_sync_unhealthy_and_out_of_sync_are_told_apart():
    failed = app("a", sync="OutOfSync", op={"phase": "Failed", "message": "one or more objects failed", "syncResult": {"resources": [{"kind": "Deployment", "name": "x", "status": "SyncFailed", "message": "forbidden"}]}})
    down = app("b", sync="Synced", health="Degraded")
    drift = app("c", sync="OutOfSync")
    s = await argocd.status(K8s([failed, down, drift]), everything)
    assert {f["resources"][0]: (f["kind"], f["severity"]) for f in s["findings"]} == {
        "application/a": ("argocd_sync_failed", "medium"), "application/b": ("argocd_unhealthy", "high"), "application/c": ("argocd_out_of_sync", "low")}
    assert "Deployment/x: forbidden" in s["findings"][0]["detail"]


async def test_apps_outside_the_scope_are_hidden_along_with_their_errors():
    s = await argocd.status(K8s([FAILING, CHART]), lambda ns: ns != "cert-manager")
    assert s["total"] == 1 and "cert-manager" not in str(s)


async def test_one_application_can_be_asked_for():
    assert (await argocd.status(K8s([FAILING, GOOD]), everything, application="cart-service"))["problems"] == []


@pytest.mark.parametrize("kind,text", [("forbidden", "argocd.enabled"), ("not_found", "not installed")])
async def test_no_access_says_so_rather_than_looking_clean(kind, text):
    s = await argocd.status(K8s(error=ConnectorError(kind, "x")), everything)
    assert s["installed"] is False and text in s["error"]


async def test_other_errors_are_not_hidden():
    with pytest.raises(ConnectorError):
        await argocd.status(K8s(error=ConnectorError("timeout", "x")), everything)


def test_the_tool_is_read_only_and_needs_environment_wide_access():
    from moks.tools.builtin import REGISTRY

    spec = REGISTRY["k8s_argocd_status"]
    assert spec.read_only and not spec.privileged and spec.cluster_scoped


def test_the_skill_runs_argo_first_on_how_people_ask():
    from moks import skills

    sk = skills.load("").skills["gitops-drift-argocd"]
    assert sk.meta.steps[0].tool == "k8s_argocd_status"
    for question in ("why are 7 argocd applications sync failing", "why did this Argo app fail", "syncs failing in gitops"):
        assert "gitops-drift-argocd" in [s.name for s, _, _ in skills.select(question, set())]


def test_unhealthy_pods_do_not_push_out_the_skill_the_question_asks_for():
    from moks import skills

    many = {"crashloop", "oomkilled", "service_selector_mismatch", "service_no_ready_endpoints", "workload_unavailable", "image_pull", "unschedulable"}
    chosen = [s.name for s, _, _ in skills.select("why are argocd applications sync failing", many)]
    assert len(chosen) == 3 and "gitops-drift-argocd" in chosen


def test_an_argo_question_leads_with_argo_even_beside_worse_pod_findings():
    pod = {"kind": "oomkilled", "severity": "high", "title": "controller was OOMKilled", "detail": "old", "resources": ["pod/x"], "evidence": ["E1"]}
    argo = [{**argocd.finding(argocd.app_row(a)), "evidence": ["E2"]} for a in (FAILING, CHART)]
    report = rules_report("Why are 7 argocd applications' syncs failing?", [pod, *argo], [], [])
    assert report.summary.startswith("Argo CD reports problems with 2 applications: frontend, cert-manager. 2 read a path")
    assert "frontend" in report.observed_facts[0].statement and "OOMKilled" not in report.summary


def test_other_questions_keep_their_order():
    pod = {"kind": "oomkilled", "severity": "high", "title": "t", "detail": "d", "resources": ["pod/x"]}
    argo = argocd.finding(argocd.app_row(FAILING))
    report = rules_report("why is checkout slow", [pod, argo], [], [])
    assert "Most severe: t." in report.summary


def test_a_runtime_kill_is_not_blamed_on_the_last_log_line():
    run = Investigator.__new__(Investigator)
    run.findings, run.notes = [], []
    logged = SimpleNamespace(data={"lines": ['level=error msg="services/frontend: app path does not exist"']}, evidence_ref="E8")
    run._crash_reason("argocd", "repo-server", "repo-server", {"reason": "Unknown", "exitCode": 255}, logged)
    assert run.findings == [] and "node or runtime restart" in run.notes[0]
    run._crash_reason("argocd", "repo-server", "repo-server", {"reason": "Error", "exitCode": 1}, logged)
    assert run.findings[0]["kind"] == "crash_log_reason"


def test_a_detected_failure_is_a_verified_cause_in_the_report():
    row = argocd.app_row(FAILING)
    findings = [{**argocd.finding(row), "evidence": ["E1"]}]
    report = rules_report("q", findings, [], [])
    verify_causes(report, findings)
    assert [c["check"] for c in report.verified_causes] == ["deterministic detector: argocd_source_missing"]


async def test_an_investigation_reports_the_argo_error_when_every_pod_is_healthy(client, monkeypatch):
    class Cluster(FakeKubernetes):
        argocd = [FAILING, GOOD]

    class Repos(Git):
        type = "git"

        def __init__(self, config, credential_ref=None):
            pass

        async def aclose(self):
            pass

    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", Cluster)
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "git", Repos)
    registry._cache.clear()
    name = f"argo-{uuid.uuid4().hex[:6]}"
    await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {}}, {"type": "git", "name": "git", "config": {}}])
    await make_user(f"{name}@example.com", [f"investigator:{name}:*"])
    h = await login(client, f"{name}@example.com")
    inv = (await client.post("/moks/api/investigations", json={"question": "why are argocd applications sync failing", "environment": name, "namespace": "shop"}, headers=h)).json()
    await Investigator(uuid.UUID(inv["id"]), "t").run()
    async with session_scope() as db:
        rep = (await db.get(Investigation, uuid.UUID(inv["id"]))).report
    assert "gitops-drift-argocd" in [s["name"] for s in rep["skills"]]
    assert [c["check"] for c in rep["verified_causes"] if "argocd" in c["check"]] == ["deterministic detector: argocd_source_missing"]
    assert any("services/frontend" in f["statement"] and "services/web/frontend" in f["statement"] for f in rep["observed_facts"])


def test_the_chart_grants_read_only_argo_access_and_only_when_enabled():
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("helm"):
        pytest.skip("helm is not installed")
    chart = Path(__file__).resolve().parents[2] / "deploy/helm/moks"

    def rules(*sets):
        args = ["helm", "template", "moks", str(chart), "-n", "moks", "-f", str(chart / "values-minikube.yaml")]
        for s in sets:
            args += ["--set", s]
        docs = yaml.safe_load_all(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
        return next(d for d in docs if d and d["kind"] == "ClusterRole" and d["metadata"]["name"] == "moks-reader")["rules"]

    argo = [r for r in rules() if "argoproj.io" in r["apiGroups"]]
    assert argo == [{"apiGroups": ["argoproj.io"], "resources": ["applications"], "verbs": ["get", "list"]}]
    assert not [r for r in rules("argocd.enabled=false") if "argoproj.io" in r["apiGroups"]]
