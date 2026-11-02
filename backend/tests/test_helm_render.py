"""What each component actually receives once credentials are split."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy/helm/moks"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm is not installed")


def render(values_file: str, *sets: str) -> list[dict]:
    args = ["helm", "template", "moks", str(CHART), "-n", "moks", "-f", str(CHART / values_file)] + ([] if values_file == "values-minikube.yaml" else ["--set", "image.tag=test"])
    for s in sets:
        args += ["--set", s]
    return [d for d in yaml.safe_load_all(subprocess.run(args, check=True, capture_output=True, text=True).stdout) if d]


def pods(docs):
    for d in docs:
        spec = (d.get("spec") or {})
        tpl = (spec.get("jobTemplate", {}).get("spec", spec)).get("template")
        if tpl:
            yield d["metadata"]["name"], tpl["spec"]


def secrets(pod) -> set[str]:
    found = set()
    for c in pod.get("containers", []) + pod.get("initContainers", []):
        found |= {e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e}
        found |= {e["valueFrom"]["secretKeyRef"]["name"] for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})}
    return found


SPLIT = ("secrets.split=true", "backup.enabled=true", "backup.bucket=b", "velero.enabled=true")
TERMINAL = (
    "terminal.enabled=true",
    "terminal.image.digest=sha256:" + "0" * 64,
    "terminal.profiles[0].name=eks,terminal.profiles[0].label=eks,terminal.profiles[0].environment=roboshop-demo",
    "terminal.profiles[0].account=1,terminal.profiles[0].roleArn=arn,terminal.profiles[0].regions[0]=us-east-1",
)


@pytest.mark.parametrize("values", ["values-minikube.yaml", "values-eks.yaml"])
def test_default_render_changes_nothing_for_existing_installs(values):
    docs = render(values)
    by = dict(pods(docs))
    assert secrets(by["moks-api"]) == {"moks-secrets"} and by["moks-api"]["serviceAccountName"] == "moks"
    assert by["moks-api"]["initContainers"][0]["name"] == "migrate"
    assert not any(d["kind"] == "Job" for d in docs)


def test_the_default_group_is_the_whole_chart():
    assert {(d["kind"], d["metadata"]["name"]) for d in render("values-minikube.yaml")} == {
        (d["kind"], d["metadata"]["name"]) for d in render("values-minikube.yaml", "resourceGroup=all")}


def test_a_mistyped_group_is_refused_rather_than_rendering_nothing():
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render("values-minikube.yaml", "resourceGroup=frontend")
    assert "is not a resource group" in refused.value.stderr


def test_config_changes_still_roll_consumers_though_only_foundation_renders_the_configmaps():
    assert [d["metadata"]["name"] for d in render("values-minikube.yaml", "resourceGroup=api") if d["kind"] == "ConfigMap"] == []
    assert {"moks-config", "moks-seed"} <= {d["metadata"]["name"] for d in render("values-minikube.yaml", "resourceGroup=foundation") if d["kind"] == "ConfigMap"}

    def checksum(*sets):
        return of(render("values-minikube.yaml", "resourceGroup=api", *sets), "Deployment", "moks-api")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    whole = of(render("values-minikube.yaml"), "Deployment", "moks-api")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert checksum() == whole and checksum("config.logLevel=DEBUG") != whole


def test_each_component_gets_only_its_secret():
    by = dict(pods(render("values-minikube.yaml", *SPLIT)))
    assert secrets(by["moks-api"]) == {"moks-api-secrets"}
    assert secrets(by["moks-worker"]) == {"moks-worker-secrets"}
    assert secrets(by["moks-executor"]) == {"moks-executor-secrets"}
    assert secrets(by["moks-backup"]) == {"moks-backup"}
    assert not any("moks-migrate-secrets" in secrets(p) for n, p in by.items() if not n.startswith("moks-migrate"))
    assert not any("moks-backup" in secrets(p) for n, p in by.items() if n != "moks-backup")
    assert not any("moks-executor-secrets" in secrets(p) for n, p in by.items() if n != "moks-executor")


def test_only_the_job_holds_the_schema_owner():
    by = dict(pods(render("values-minikube.yaml", *SPLIT)))
    for name in ("moks-api", "moks-worker", "moks-executor"):
        assert [c["name"] for c in by[name]["initContainers"]] == ["wait-schema"]
    job = next(p for n, p in by.items() if n.startswith("moks-migrate"))
    assert job["automountServiceAccountToken"] is False and "db-grants" in job["containers"][0]["command"][2]


def test_identities_are_separate_and_nobody_reads_secrets():
    docs = render("values-minikube.yaml", *SPLIT)
    by = dict(pods(docs))
    assert len({by[n]["serviceAccountName"] for n in ("moks-api", "moks-worker", "moks-executor")}) == 3
    for d in docs:
        if d["kind"] in ("Role", "ClusterRole") and d["metadata"]["name"].startswith("moks"):
            for rule in d["rules"]:
                if "secrets" in rule.get("resources", []):
                    assert not {"get", "list", "watch"} & set(rule["verbs"]), d["metadata"]["name"]


def test_chart_renders_for_eks_split():
    assert any(d["kind"] == "Job" for d in render("values-eks.yaml", "secrets.split=true"))


def test_worker_pools_scale_each_class_separately():
    pools = [f"worker.pools[{i}].name={n},worker.pools[{i}].classes={n},worker.pools[{i}].replicas=1" for i, n in enumerate(["detection", "knowledge"])]
    names = {n for n, _ in pods(render("values-eks.yaml", *pools))}
    assert {"moks-worker-detection", "moks-worker-knowledge"} <= names and "moks-worker" not in names


def test_eks_routes_through_istio_without_traefik():
    docs = render("values-eks.yaml")
    route = next(d for d in docs if d["kind"] == "VirtualService")
    assert route["spec"]["gateways"] == ["istio-ingress/public"] and route["spec"]["hosts"] == ["eks.jithendar.com"]
    assert [r["name"] for r in route["spec"]["http"]] == ["terminal", "portal"]
    assert not any(d["kind"] in ("Ingress", "Middleware") for d in docs)


def test_istio_sends_terminal_traffic_to_the_gateway_first():
    route = next(d for d in render("values-eks.yaml", *TERMINAL) if d["kind"] == "VirtualService")
    first, second = route["spec"]["http"]
    assert first["match"] == [{"uri": {"prefix": "/moks/api/terminal"}}] and second["match"] == [{"uri": {"prefix": "/moks"}}]
    assert first["route"][0]["destination"]["host"] == "moks-terminal.moks.svc.cluster.local"


def test_minikube_renders_no_mesh_objects():
    kinds = {d["kind"] for d in render("values-minikube.yaml")}
    assert not kinds & {"VirtualService", "PeerAuthentication", "AuthorizationPolicy"}


def test_eks_is_strict_and_denies_by_default():
    docs = render("values-eks.yaml")
    assert next(d for d in docs if d["kind"] == "PeerAuthentication")["spec"]["mtls"]["mode"] == "STRICT"
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "AuthorizationPolicy"}
    assert policies["allow-nothing"] == {}
    api, db = policies["moks"]["rules"][0], policies["moks-postgres"]["rules"][0]
    assert api["from"][0]["source"]["principals"] == ["cluster.local/ns/istio-ingress/sa/istio-ingressgateway"]
    assert api["to"][0]["operation"]["ports"] == ["8080"]
    assert db["from"][0]["source"]["namespaces"] == ["moks"] and db["to"][0]["operation"]["ports"] == ["5432"]


def test_meshed_postgres_can_reach_istiod_when_policy_is_enforced():
    docs = render("values-eks.yaml")
    egress = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "moks-postgres")["spec"]["egress"]
    assert {"port": 15012, "protocol": "TCP"} in egress[1]["ports"]
    assert next(d for d in render("values-minikube.yaml") if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "moks-postgres")["spec"]["egress"] == []


def test_mtls_needs_a_gateway_identity():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-eks.yaml", "istio.gatewayPrincipal=")


def test_minikube_keeps_one_worker_with_reserved_capacity():
    by = dict(pods(render("values-minikube.yaml")))
    env = {e["name"]: e["value"] for e in by["moks-worker"]["containers"][0]["env"]}
    assert "detection=2" in env["MOKS_WORKER_CAPACITY"] and "moks-worker-detection" not in by


def test_mk4_always_pulls_latest():
    for name, pod in pods(render("values-minikube.yaml")):
        for c in pod["containers"]:
            if c["image"].split("/")[-1].startswith("moks"):
                assert c["image"].endswith(("/moks:latest", "/moks-reranker:latest")) and c["imagePullPolicy"] == "Always", name


def test_the_chart_ships_the_consistent_dump():
    docs = render("values-minikube.yaml", *SPLIT)
    by = dict(pods(docs))
    dump = by["moks-backup"]["initContainers"][0]["command"][2]
    assert "consistent-dump.sql" in dump and "--manifest" in by["moks-backup"]["containers"][0]["args"]
    cm = next(d for d in docs if d["metadata"]["name"] == "moks-backup-scripts")
    assert "--snapshot" in cm["data"]["consistent-dump.sql"]


@pytest.mark.parametrize("digest", ["latest", "sha256:abc", "99f94e21"])
def test_a_malformed_digest_is_refused(digest):
    done = subprocess.run(["helm", "template", "moks", str(CHART), "--set", f"image.digest={digest}"], capture_output=True, text=True)
    assert done.returncode != 0 and "image.digest must be" in done.stderr


def test_mk4_has_velero_access_and_no_node_agent():
    docs = render("values-minikube.yaml")
    assert {"moks-velero-reader", "moks-executor-velero"} <= {d["metadata"]["name"] for d in docs if d["kind"] == "Role"}
    values = yaml.safe_load((CHART.parents[1] / "velero/values-r2-minikube.yaml").read_text())
    assert values["deployNodeAgent"] is False and values["configuration"]["defaultVolumesToFsBackup"] is False


def test_argo_can_deploy_velero_in_either_order():
    docs = render("values-minikube.yaml")
    ns = next(d for d in docs if d["kind"] == "Namespace" and d["metadata"]["name"] == "velero")
    assert ns["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"  # before the Roles inside it
    assert "Prune=false" in ns["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
    project, app = yaml.safe_load_all((CHART.parents[1] / "argocd/velero.yaml").read_text())
    assert app["spec"]["project"] == project["metadata"]["name"] and "automated" not in app["spec"]["syncPolicy"]
    values = app["spec"]["sources"][0]["helm"]["valueFiles"][0].replace("$values/", "")
    assert (CHART.parents[2] / values).exists()
    assert {s["repoURL"] for s in app["spec"]["sources"]} <= set(project["spec"]["sourceRepos"])
    assert yaml.safe_load((CHART.parents[2] / values).read_text())["upgradeCRDs"] is False


def test_velero_project_allows_everything_the_chart_renders():
    out = subprocess.run(["helm", "template", "velero", "velero", "--repo", "https://vmware-tanzu.github.io/helm-charts", "--version", "12.2.2", "-n", "velero",
                          "--include-crds", "-f", str(CHART.parents[1] / "velero/values-r2-minikube.yaml")], capture_output=True, text=True)
    if out.returncode:
        pytest.skip("the Velero chart repository is unreachable")
    project = next(yaml.safe_load_all((CHART.parents[1] / "argocd/velero.yaml").read_text()))
    allowed = {w["kind"] for w in project["spec"]["clusterResourceWhitelist"] + project["spec"]["namespaceResourceWhitelist"]}
    rendered = {d["kind"] for d in yaml.safe_load_all(out.stdout) if d}
    assert rendered <= allowed, rendered - allowed


TERMINAL = ("terminal.enabled=true", "terminal.image.digest=sha256:" + "a" * 64, "terminal.profiles[0].name=ro", "terminal.kubernetes.namespaces={moks-lab}", "terminal.network.apiServer=true")


def of(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_terminals_are_off_by_default():
    assert not [d for d in render("values-minikube.yaml") if "terminal" in d["metadata"]["name"]]


def test_a_tag_or_no_profile_is_refused():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-minikube.yaml", "terminal.enabled=true", "terminal.profiles[0].name=ro")
    with pytest.raises(subprocess.CalledProcessError):
        render("values-minikube.yaml", "terminal.enabled=true", "terminal.image.digest=sha256:" + "a" * 64)


def test_the_gateway_is_routed_under_the_base_path_and_keeps_to_its_own_identity():
    docs = render("values-minikube.yaml", *TERMINAL)
    paths = of(docs, "Ingress", "moks")["spec"]["rules"][0]["http"]["paths"]
    assert paths[0]["path"] == "/moks/api/terminal" and paths[0]["backend"]["service"]["name"] == "moks-terminal"
    assert paths[1]["path"] == "/moks" and paths[1]["backend"]["service"]["name"] == "moks-api"
    pod = dict(pods(docs))["moks-terminal"]
    assert pod["serviceAccountName"] == "moks-terminal" and secrets(pod) == {"moks-terminal-secrets"}
    assert not pod["containers"][0].get("volumeMounts", [])[1:]  # no credentials volume, no seed


def test_the_gateway_can_touch_only_the_terminal_namespace():
    docs = render("values-minikube.yaml", *TERMINAL)
    assert not [d for d in docs if d["kind"] in ("ClusterRoleBinding", "RoleBinding") and any(s["name"] == "moks-terminal" for s in d.get("subjects", [])) and d["metadata"].get("namespace") != "moks-terminals"]
    rules = of(docs, "Role", "moks-terminal-gateway")["rules"]
    assert {r for rule in rules for r in rule["resources"]} == {"pods", "pods/exec", "serviceaccounts/token"}
    assert next(r for r in rules if "serviceaccounts/token" in r["resources"])["resourceNames"] == ["moks-terminal-session"]


def test_the_session_identity_is_read_only_and_never_sees_secrets():
    docs = render("values-minikube.yaml", *TERMINAL)
    rules = of(docs, "ClusterRole", "moks-terminal-reader")["rules"]
    assert {v for r in rules for v in r["verbs"]} == {"get", "list", "watch"}
    assert not {"secrets", "configmaps", "pods/exec", "pods/attach", "pods/portforward"} & {x for r in rules for x in r["resources"]}
    binding = of(docs, "RoleBinding", "moks-terminal-reader")
    assert binding["metadata"]["namespace"] == "moks-lab" and binding["subjects"][0]["name"] == "moks-terminal-session"
    assert not [d for d in docs if d["kind"] == "ClusterRoleBinding" and "terminal" in d["metadata"]["name"]]


def test_the_terminal_namespace_is_restricted_and_closed_by_default():
    docs = render("values-minikube.yaml", *TERMINAL)
    ns = of(docs, "Namespace", "moks-terminals")["metadata"]["labels"]
    assert ns["pod-security.kubernetes.io/enforce"] == "restricted"
    policy = of(docs, "NetworkPolicy", "terminals")["spec"]
    assert policy["podSelector"] == {} and policy["ingress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    assert not [r for r in policy["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]  # nothing beyond DNS and the API server
    quota = of(docs, "ResourceQuota", "terminals")["spec"]["hard"]
    assert {"pods", "limits.cpu", "limits.memory", "limits.ephemeral-storage"} <= set(quota)


def test_configured_egress_is_exactly_what_was_listed():
    docs = render("values-minikube.yaml", *TERMINAL, "terminal.network.egress[0].cidr=0.0.0.0/0", "terminal.network.egress[0].except={10.0.0.0/8,169.254.0.0/16}", "terminal.network.egress[0].ports={443}")
    rule = of(docs, "NetworkPolicy", "terminals")["spec"]["egress"][-1]
    assert rule["to"][0]["ipBlock"]["except"] == ["10.0.0.0/8", "169.254.0.0/16"] and rule["ports"] == [{"port": 443, "protocol": "TCP"}]


RERANKER = ("reranker.enabled=true", "reranker.image.digest=sha256:" + "b" * 64)


def test_reranking_is_absent_when_off():
    docs = render("values-eks.yaml", "image.tag=ci")
    assert not [d for d in docs if "reranker" in d["metadata"]["name"]]
    assert not [k for d in docs if d["kind"] == "ConfigMap" for k in (d.get("data") or {}) if "RERANK" in k]


def test_deploying_the_service_does_not_turn_it_on_for_search():
    config = next(d for d in render("values-minikube.yaml", *RERANKER, "reranker.client.enabled=false") if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]
    assert config["MOKS_RAG_RERANK_ENABLED"] == "false" and config["MOKS_RAG_RERANK_REVISION"] and config["MOKS_RAG_RERANK_URL"].startswith("http://moks-reranker.moks.svc")


def test_the_reranker_has_no_public_route_and_runs_restricted():
    docs = render("values-minikube.yaml", *RERANKER)
    assert not [d for d in docs if d["kind"] in ("Ingress", "VirtualService") and "reranker" in str(d)]
    assert of(docs, "Service", "moks-reranker")["spec"]["type"] == "ClusterIP"
    pod = of(docs, "Deployment", "moks-reranker")["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"]["memory"] and "@sha256:" in c["image"] and c["imagePullPolicy"] == "IfNotPresent"
    assert {e["name"] for e in c["env"]} >= {"RERANKER_TOKEN", "RERANKER_THREADS"} and "envFrom" not in c  # never the database credentials


def test_only_the_api_and_worker_reach_it_and_it_reaches_nothing():
    docs = render("values-minikube.yaml", *RERANKER)
    policy = of(docs, "NetworkPolicy", "moks-reranker")["spec"]
    assert policy["egress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    callers = [p["podSelector"]["matchLabels"] for p in policy["ingress"][0]["from"]]
    assert callers == [{"app": "moks", "component": "api"}, {"app": "moks", "component": "worker"}]
    assert any(r.get("to") == [{"podSelector": {"matchLabels": {"app": "moks-reranker"}}}] for r in of(docs, "NetworkPolicy", "moks")["spec"]["egress"])


def test_a_tag_is_pulled_on_every_start_so_a_restart_gets_it():
    c = of(render("values-minikube.yaml", "reranker.enabled=true", "reranker.image.tag=latest"), "Deployment", "moks-reranker")["spec"]["template"]["spec"]["containers"][0]
    assert c["image"].endswith("/moks-reranker:latest") and c["imagePullPolicy"] == "Always"


@pytest.mark.parametrize("bad", [("reranker.image.digest=latest",), ("reranker.image.tag=",)])
def test_an_unusable_image_reference_is_refused(bad):
    with pytest.raises(subprocess.CalledProcessError):
        render("values-minikube.yaml", "reranker.enabled=true", *bad)


def test_the_mesh_allows_only_the_callers():
    docs = render("values-eks.yaml", *RERANKER)
    rule = of(docs, "AuthorizationPolicy", "moks-reranker")["spec"]["rules"][0]
    assert rule["to"][0]["operation"]["ports"] == ["8090"] and all(p.startswith("cluster.local/ns/moks/sa/") for p in rule["from"][0]["source"]["principals"])


def test_opensearch_public_route_is_read_only_behind_a_login():
    docs = render("values-minikube.yaml")
    route = of(docs, "IngressRoute", "opensearch")["spec"]
    match = route["routes"][0]["match"]
    assert route["entryPoints"] == ["websecure"] and "tls" in route
    assert [m["name"] for m in route["routes"][0]["middlewares"]] == ["opensearch-auth", "opensearch-strip-prefix"]
    assert "PUT" not in match and "DELETE" not in match and "(search|msearch|count)$" in match
    assert of(docs, "Middleware", "opensearch-auth")["spec"]["basicAuth"] == {"secret": "moks-opensearch-auth", "removeHeader": True}
    rules = of(docs, "NetworkPolicy", "moks-opensearch")["spec"]["ingress"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}} in rules[1]["from"]


def test_opensearch_dashboards_has_its_own_login_and_a_writable_config():
    docs = render("values-minikube.yaml")
    pod = of(docs, "Deployment", "moks-opensearch-dashboards")["spec"]["template"]["spec"]
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["SERVER_BASEPATH"] == "/opensearch-dashboards" and env["SERVER_REWRITEBASEPATH"] == "true"
    assert env["OPENSEARCH_HOSTS"] == '["http://moks-opensearch:9200"]' and env["DISABLE_SECURITY_DASHBOARDS_PLUGIN"] == "true"
    assert ":" + "3.9.0" in pod["containers"][0]["image"] and ":3.9.0" in of(docs, "StatefulSet", "moks-opensearch")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert not pod["containers"][0].get("volumeMounts") and pod["automountServiceAccountToken"] is False
    route = of(docs, "IngressRoute", "opensearch-dashboards")["spec"]["routes"][0]
    assert [m["name"] for m in route["middlewares"]] == ["opensearch-dashboards-auth"]
    assert of(docs, "Middleware", "opensearch-dashboards-auth")["spec"]["basicAuth"]["secret"] == "moks-opensearch-dashboards-auth"
    assert {"podSelector": {"matchLabels": {"app": "moks-opensearch-dashboards"}}} in of(docs, "NetworkPolicy", "moks-opensearch")["spec"]["ingress"][2]["from"]


@pytest.mark.parametrize("sets", [("rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=false"), ("rag.opensearch.url=https://d.example", "rag.opensearch.awsRegion=us-east-1")])
def test_opensearch_stays_private_unless_the_bundled_pod_is_published(sets):
    docs = render("values-minikube.yaml", *sets)
    assert not any(d["kind"] == "IngressRoute" or d["metadata"]["name"].startswith("opensearch-") for d in docs)


def test_mk4_runs_opensearch_on_the_node_with_its_data():
    docs = render("values-minikube.yaml")
    spec = of(docs, "StatefulSet", "moks-opensearch")["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "mk4-m02"} and spec["initContainers"][0]["name"] == "volume-permissions"
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
    assert env["cluster.routing.allocation.disk.watermark.flood_stage"] == "1gb"
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]
    assert config["MOKS_RAG_BACKEND"] == "opensearch" and config["MOKS_OPENSEARCH_URL"] == "http://moks-opensearch:9200"


def test_watermarks_stay_out_of_the_chart_unless_asked_for():
    env = of(render("values-minikube.yaml", "rag.opensearch.diskWatermarks=null"), "StatefulSet", "moks-opensearch")["spec"]["template"]["spec"]["containers"][0]["env"]
    assert not [e for e in env if "watermark" in e["name"]]



SECURE = ("rag.opensearch.secure.enabled=true",)
ACTIVE = (*SECURE, "rag.opensearch.secure.active=true", "rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=false")


def test_the_secured_cluster_is_off_on_mk4_until_asked_for():
    docs = render("values-minikube.yaml")
    assert not any(d["metadata"]["name"].startswith("moks-search") for d in docs)
    assert of(docs, "ConfigMap", "moks-config")["data"]["MOKS_OPENSEARCH_URL"] == "http://moks-opensearch:9200"


def test_enabling_it_deploys_beside_the_old_pod_and_moves_nothing():
    docs = render("values-minikube.yaml", *SECURE)
    assert of(docs, "StatefulSet", "moks-search") and of(docs, "StatefulSet", "moks-opensearch")
    assert of(docs, "ConfigMap", "moks-config")["data"]["MOKS_OPENSEARCH_URL"] == "http://moks-opensearch:9200"
    ops = of(docs, "CronJob", "moks-search-ops")["spec"]
    env = {e["name"]: e.get("value") for e in ops["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert ops["suspend"] is True and env["MOKS_OPENSEARCH_STANDBY"] == "true" and env["MOKS_OPENSEARCH_CLUSTER"] == "secure"


def test_the_secured_pod_is_hardened_and_never_disables_security():
    docs = render("values-minikube.yaml", *SECURE)
    sts = of(docs, "StatefulSet", "moks-search")["spec"]
    spec = sts["template"]["spec"]
    c = spec["containers"][0]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert "DISABLE_SECURITY_PLUGIN" not in env and env["DISABLE_INSTALL_DEMO_CONFIG"] == "true"
    assert c["startupProbe"]["httpGet"]["scheme"] == "HTTPS" and c["readinessProbe"] and c["livenessProbe"]["tcpSocket"]
    assert "@sha256:" in c["image"] and c["securityContext"]["capabilities"]["drop"] == ["ALL"] and spec["securityContext"]["runAsNonRoot"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "mk4-m02"} and spec["terminationGracePeriodSeconds"] >= 60
    assert sts["persistentVolumeClaimRetentionPolicy"] == {"whenDeleted": "Retain", "whenScaled": "Retain"}
    assert not any(i.get("securityContext", {}).get("privileged") for i in spec["initContainers"])
    config = of(docs, "ConfigMap", "moks-search-config")["data"]["opensearch.yml"]
    assert "plugins.security.ssl.http.enabled: true" in config and "plugins.security.disabled" not in config and "action.auto_create_index: false" in config


def test_the_secured_service_is_internal_and_reachable_only_by_those_who_use_it():
    docs = render("values-minikube.yaml", *SECURE)
    assert of(docs, "Service", "moks-search")["spec"]["type"] == "ClusterIP"
    assert not any(d["kind"] in ("Ingress", "IngressRoute") and "moks-search" in str(d["spec"]) for d in docs)
    np = of(docs, "NetworkPolicy", "moks-search")["spec"]
    who = np["ingress"][0]["from"]
    assert who[0]["podSelector"]["matchExpressions"][1]["values"] == ["api", "worker", "search-ops"] and np["egress"] == []
    assert {"to": [{"podSelector": {"matchLabels": {"app": "moks-search"}}}], "ports": [{"port": 9200, "protocol": "TCP"}]} in of(docs, "NetworkPolicy", "moks")["spec"]["egress"]


def test_at_cutover_each_component_gets_its_own_identity():
    docs = render("values-minikube.yaml", *ACTIVE)
    data = of(docs, "ConfigMap", "moks-config")["data"]
    assert data["MOKS_OPENSEARCH_URL"] == "https://moks-search:9200" and data["MOKS_OPENSEARCH_CLUSTER"] == "secure"

    def names(deployment):
        return {e["name"] for e in of(docs, "Deployment", deployment)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "MOKS_OPENSEARCH_PASSWORD" in names("moks-api") and "MOKS_OPENSEARCH_INDEX_PASSWORD" not in names("moks-api") and "MOKS_OPENSEARCH_LIFECYCLE_PASSWORD" not in names("moks-api")
    assert {"MOKS_OPENSEARCH_INDEX_PASSWORD", "MOKS_OPENSEARCH_LIFECYCLE_PASSWORD"} <= names("moks-worker")
    volume = next(v for v in of(docs, "Deployment", "moks-api")["spec"]["template"]["spec"]["volumes"] if v["name"] == "search-ca")
    assert volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]  # never tls.key


def test_the_original_pod_goes_only_after_cutover_and_only_when_asked():
    docs = render("values-minikube.yaml", *ACTIVE, "rag.opensearch.secure.retireOriginal=true")
    assert not any(d["metadata"]["name"] == "moks-opensearch" for d in docs) and of(docs, "StatefulSet", "moks-search")
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-minikube.yaml", *SECURE, "rag.opensearch.secure.retireOriginal=true")
    assert "set active first" in str(e.value.stderr)


def test_the_old_public_route_blocks_cutover():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-minikube.yaml", *SECURE, "rag.opensearch.secure.active=true")
    assert "publicRoute" in str(e.value.stderr)


def test_the_old_dashboards_block_cutover():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-minikube.yaml", *SECURE, "rag.opensearch.secure.active=true", "rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=true")
    assert "rag.opensearch.dashboards" in str(e.value.stderr)


def test_snapshots_need_the_image_that_has_the_plugin():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-minikube.yaml", *SECURE, "rag.opensearch.secure.snapshots.enabled=true")
    assert "pluginsInstalled" in str(e.value.stderr)
    docs = render("values-minikube.yaml", *SECURE, "rag.opensearch.secure.snapshots.enabled=true", "rag.opensearch.secure.pluginsInstalled=true", "rag.opensearch.secure.snapshots.endpoint=https://x.r2.cloudflarestorage.com")
    spec = of(docs, "StatefulSet", "moks-search")["spec"]["template"]["spec"]
    assert [i["name"] for i in spec["initContainers"]] == ["volume-permissions", "keystore"]
    assert any(r.get("ports") == [{"port": 443, "protocol": "TCP"}] for r in of(docs, "NetworkPolicy", "moks-search")["spec"]["egress"])


def test_no_chart_file_holds_a_credential():
    from pathlib import Path
    for f in (Path(__file__).parents[2] / "deploy/helm/moks/files/opensearch").iterdir():
        assert "$2" not in f.read_text() and "hash:" not in f.read_text() and "BEGIN" not in f.read_text()
