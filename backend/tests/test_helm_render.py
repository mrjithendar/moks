"""What each component actually receives once credentials are split."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy/helm/moks"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm is not installed")


def render(values_file: str, *sets: str) -> list[dict]:
    args = ["helm", "template", "moks", str(CHART), "-n", "moks", "-f", str(CHART / values_file)] + ([] if values_file == "values-hetzner.yaml" else ["--set", "image.tag=test"])
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
# mk4 uses the shared CloudNativePG database and has no Velero yet (values-hetzner.yaml), so the checks of the bundled
# database, its backup CronJob and the Velero roles turn those on.
BUNDLED = "postgres.bundled=true"
VELERO = "velero.enabled=true"
# mk4 turns secret sync on; the checks of the posture without it switch it off again.
SYNC_OFF = "secretsSync.enabled=false"
TERMINAL = (
    "terminal.enabled=true",
    "terminal.image.digest=sha256:" + "0" * 64,
    "terminal.profiles[0].name=eks,terminal.profiles[0].label=eks,terminal.profiles[0].environment=roboshop-demo",
    "terminal.profiles[0].account=1,terminal.profiles[0].roleArn=arn,terminal.profiles[0].regions[0]=us-east-1",
)


@pytest.mark.parametrize("values", ["values-hetzner.yaml", "values-eks.yaml"])
def test_default_render_changes_nothing_for_existing_installs(values):
    docs = render(values)
    by = dict(pods(docs))
    assert secrets(by["moks-api"]) == {"moks-secrets"} and by["moks-api"]["serviceAccountName"] == "moks"
    assert by["moks-api"]["initContainers"][0]["name"] == "migrate"
    assert not any(d["kind"] == "Job" for d in docs)


def test_the_default_group_is_the_whole_chart():
    assert {(d["kind"], d["metadata"]["name"]) for d in render("values-hetzner.yaml")} == {
        (d["kind"], d["metadata"]["name"]) for d in render("values-hetzner.yaml", "resourceGroup=all")}


def test_a_mistyped_group_is_refused_rather_than_rendering_nothing():
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render("values-hetzner.yaml", "resourceGroup=frontend")
    assert "is not a resource group" in refused.value.stderr


def test_config_changes_still_roll_consumers_though_only_foundation_renders_the_configmaps():
    assert [d["metadata"]["name"] for d in render("values-hetzner.yaml", "resourceGroup=api") if d["kind"] == "ConfigMap"] == []
    assert {"moks-config", "moks-seed"} <= {d["metadata"]["name"] for d in render("values-hetzner.yaml", "resourceGroup=foundation") if d["kind"] == "ConfigMap"}

    def checksum(*sets):
        return of(render("values-hetzner.yaml", "resourceGroup=api", *sets), "Deployment", "moks-api")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    whole = of(render("values-hetzner.yaml"), "Deployment", "moks-api")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert checksum() == whole and checksum("config.logLevel=DEBUG") != whole


def test_each_component_gets_only_its_secret():
    by = dict(pods(render("values-hetzner.yaml", *SPLIT, SYNC_OFF, BUNDLED)))
    assert secrets(by["moks-api"]) == {"moks-api-secrets"}
    assert secrets(by["moks-worker"]) == {"moks-worker-secrets"}
    assert secrets(by["moks-executor"]) == {"moks-executor-secrets"}
    assert secrets(by["moks-backup"]) == {"moks-backup"}
    assert not any("moks-migrate-secrets" in secrets(p) for n, p in by.items() if not n.startswith("moks-migrate"))
    assert not any("moks-backup" in secrets(p) for n, p in by.items() if n != "moks-backup")
    assert not any("moks-executor-secrets" in secrets(p) for n, p in by.items() if n != "moks-executor")


def test_only_the_job_holds_the_schema_owner():
    by = dict(pods(render("values-hetzner.yaml", *SPLIT)))
    for name in ("moks-api", "moks-worker", "moks-executor"):
        assert [c["name"] for c in by[name]["initContainers"]] == ["wait-schema"]
    job = next(p for n, p in by.items() if n.startswith("moks-migrate"))
    assert job["automountServiceAccountToken"] is False and "db-grants" in job["containers"][0]["command"][2]


def test_identities_are_separate_and_nobody_reads_secrets():
    docs = render("values-hetzner.yaml", *SPLIT, SYNC_OFF)
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
    # The terminal is off on EKS (values-eks.yaml); the next test turns it on and checks its route comes first.
    assert [r["name"] for r in route["spec"]["http"]] == ["portal"]
    assert not any(d["kind"] in ("Ingress", "Middleware") for d in docs)


def test_istio_sends_terminal_traffic_to_the_gateway_first():
    route = next(d for d in render("values-eks.yaml", *TERMINAL) if d["kind"] == "VirtualService")
    first, second = route["spec"]["http"]
    assert first["match"] == [{"uri": {"prefix": "/moks/api/terminal"}}] and second["match"] == [{"uri": {"prefix": "/moks"}}]
    assert first["route"][0]["destination"]["host"] == "moks-terminal.moks.svc.cluster.local"


def test_minikube_is_strict_and_routed_by_delegates_without_gateway_policies():
    docs = render("values-hetzner.yaml")
    # Only the namespace's allow-known-callers (mesh.authorization), none of istio.mtls's gateway policies.
    assert [d["metadata"]["name"] for d in docs if d["kind"] == "AuthorizationPolicy"] == ["allow-known-callers"]
    assert not any(d["kind"] == "Ingress" or d["apiVersion"].startswith("traefik.io/") for d in docs)
    routes = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "VirtualService"}
    assert set(routes) == {"moks", "moks-opensearch", "moks-opensearch-dashboards"}
    assert not any("hosts" in r or "gateways" in r for r in routes.values())   # delegates of miniKube's routing table
    peer = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "PeerAuthentication"}
    assert peer["default"] == {"mtls": {"mode": "STRICT"}}
    # mk4: the Istio gateway is the only edge and Prometheus scrapes through its sidecar, so no port takes plain text.
    # Every name still renders (Argo CD never prunes here, so a dropped policy would keep its old exceptions live).
    assert set(peer) == {"default", "moks-metrics", "moks-reranker-metrics", "moks-opensearch-edge",
                         "moks-opensearch-dashboards-edge"}
    assert all("portLevelMtls" not in spec and spec["mtls"] == {"mode": "STRICT"} for spec in peer.values())
    assert peer["moks-metrics"]["selector"] == {"matchLabels": {"app": "moks"}}
    # An edge or a scraper outside the mesh gets exactly its ports back.
    legacy = {d["metadata"]["name"]: d["spec"] for d in render("values-hetzner.yaml", "mesh.edgeMtls=false", "mesh.metricsMtls=false")
              if d["kind"] == "PeerAuthentication"}
    assert legacy["moks-metrics"]["portLevelMtls"] == {9464: {"mode": "PERMISSIVE"}, 8080: {"mode": "PERMISSIVE"}}
    assert legacy["moks-reranker-metrics"]["portLevelMtls"] == {9464: {"mode": "PERMISSIVE"}}
    assert legacy["moks-opensearch-edge"]["portLevelMtls"] == {9200: {"mode": "PERMISSIVE"}}
    assert legacy["moks-opensearch-dashboards-edge"]["portLevelMtls"] == {5601: {"mode": "PERMISSIVE"}}
    policy = next(d for d in docs if d["kind"] == "AuthorizationPolicy")
    assert policy["metadata"]["annotations"] == {"istio.io/dry-run": "true"}
    assert policy["spec"] == {"action": "ALLOW", "rules": [{"from": [{"source": {"namespaces": ["moks", "istio-ingress", "monitoring"]}}]}]}
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    assert "traefik" not in str(policies)

def test_minikube_sidecars_reach_istiod_only_from_already_isolated_pods():
    docs = render("values-hetzner.yaml")
    policies = {d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "NetworkPolicy"}
    mesh = policies.pop("istio-sidecars")
    selected = set(mesh["podSelector"]["matchExpressions"][0]["values"])
    isolated = {p["podSelector"]["matchLabels"]["app"] for p in policies.values()
                if set(p.get("policyTypes", [])) == {"Ingress", "Egress"} and "app" in p["podSelector"].get("matchLabels", {})}
    # moks-postgres is always selected; with the shared database no such pod exists, so the selector matches nothing.
    assert isolated <= selected and selected - isolated <= {"moks-search", "moks-search-dashboards", "moks-postgres"}
    # DNS too: the proxy resolves istiod by name, and postgres, opensearch and reranker have no DNS rule of their own.
    assert mesh["egress"] == [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                       "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                               "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
                              {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "istio-system"}}}],
                               "ports": [{"port": 15012, "protocol": "TCP"}]}]
    assert {"port": 15090, "protocol": "TCP"} in mesh["ingress"][0]["ports"]


def test_mesh_and_gateway_mtls_are_exclusive():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-eks.yaml", "mesh.enabled=true")


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
    assert next(d for d in render("values-hetzner.yaml", BUNDLED) if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "moks-postgres")["spec"]["egress"] == []


def test_mtls_needs_a_gateway_identity():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-eks.yaml", "istio.gatewayPrincipal=")


def test_minikube_keeps_one_worker_with_reserved_capacity():
    by = dict(pods(render("values-hetzner.yaml")))
    env = {e["name"]: e["value"] for e in by["moks-worker"]["containers"][0]["env"]}
    assert "detection=2" in env["MOKS_WORKER_CAPACITY"] and "moks-worker-detection" not in by


def test_mk4_always_pulls_latest():
    for name, pod in pods(render("values-hetzner.yaml")):
        for c in pod["containers"]:
            if c["image"].split("/")[-1].startswith("moks"):
                assert c["image"].endswith(("/moks:latest", "/moks-crewai:latest", "/moks-reranker:latest")) and c["imagePullPolicy"] == "Always", name


def test_the_chart_ships_the_consistent_dump():
    docs = render("values-hetzner.yaml", *SPLIT, BUNDLED)
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
    docs = render("values-hetzner.yaml", VELERO)
    assert {"moks-velero-reader", "moks-executor-velero"} <= {d["metadata"]["name"] for d in docs if d["kind"] == "Role"}
    values = yaml.safe_load((CHART.parents[1] / "velero/values-r2-minikube.yaml").read_text())
    assert values["deployNodeAgent"] is False and values["configuration"]["defaultVolumesToFsBackup"] is False


def test_argo_can_deploy_velero_in_either_order():
    docs = render("values-hetzner.yaml", VELERO)
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


# The chart's own defaults (values.yaml). values-hetzner.yaml is mk4, which turns the terminal on on purpose:
# see test_mk4_opts_into_the_terminal_with_https_only.
def test_terminals_are_off_by_default():
    assert not [d for d in render("values.yaml") if "terminal" in d["metadata"]["name"]]


def test_a_bad_digest_no_image_or_no_profile_is_refused():
    with pytest.raises(subprocess.CalledProcessError):
        render("values.yaml", "terminal.enabled=true", "terminal.image.digest=latest", "terminal.profiles[0].name=ro")
    with pytest.raises(subprocess.CalledProcessError):
        render("values.yaml", "terminal.enabled=true", "terminal.image.tag=", "terminal.profiles[0].name=ro")
    with pytest.raises(subprocess.CalledProcessError):
        render("values.yaml", "terminal.enabled=true", "terminal.image.digest=sha256:" + "a" * 64)


def test_mk4_pulls_every_moks_image_from_harbor_as_latest():
    docs = render("values-hetzner.yaml")
    images, secrets = set(), set()
    for doc in docs:
        spec = doc.get("spec", {})
        pod = spec.get("jobTemplate", {}).get("spec", spec).get("template", {}).get("spec")
        if not pod:
            continue
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            for env in container.get("env", []):
                if env["name"] == "MOKS_TERMINAL_IMAGE":
                    images.add(env["value"])
            if "/jithendar/moks" in container["image"]:
                images.add(container["image"])
                secrets.add(tuple(s["name"] for s in pod.get("imagePullSecrets", [])))
    assert images and all(i.startswith("registry.jithendar.com/jithendar/moks") and i.endswith(":latest") for i in images), images
    assert "registry.jithendar.com/jithendar/moks-terminal:latest" in images
    assert secrets == {("harbor-pull",)}
    assert not any("dkr.ecr" in str(doc) or "ecr-pull" in str(doc) for doc in docs)


def test_mk4_opts_into_the_terminal_with_https_only():
    """mk4 runs terminals: moks-terminal:latest from Harbor, one read-only profile, and egress to public HTTPS only."""
    docs = render("values-hetzner.yaml")
    assert of(docs, "Namespace", "moks-terminals")["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    policy = of(docs, "NetworkPolicy", "terminals")["spec"]
    public = [r for r in policy["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]
    assert [r["ports"] for r in public] == [[{"port": 443, "protocol": "TCP"}]]
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"} <= set(public[0]["to"][0]["ipBlock"]["except"])


def test_the_gateway_is_routed_under_the_base_path_and_keeps_to_its_own_identity():
    docs = render("values-hetzner.yaml", *TERMINAL)
    terminal, portal = of(docs, "VirtualService", "moks")["spec"]["http"]
    # Both narrow the routing table's /moks prefix, so Istio's delegation accepts them.
    assert terminal["match"] == [{"uri": {"prefix": "/moks/api/terminal"}}] and terminal["route"][0]["destination"]["host"] == "moks-terminal.moks.svc.cluster.local"
    assert portal["match"] == [{"uri": {"prefix": "/moks"}}] and portal["route"][0]["destination"]["host"] == "moks-api.moks.svc.cluster.local"
    plain = render("values-hetzner.yaml", *TERMINAL, "istio.enabled=false", "ingress.enabled=true", "rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=false")
    paths = of(plain, "Ingress", "moks")["spec"]["rules"][0]["http"]["paths"]
    assert paths[0]["path"] == "/moks/api/terminal" and paths[0]["backend"]["service"]["name"] == "moks-terminal"
    assert paths[1]["path"] == "/moks" and paths[1]["backend"]["service"]["name"] == "moks-api"
    pod = dict(pods(docs))["moks-terminal"]
    assert pod["serviceAccountName"] == "moks-terminal" and secrets(pod) == {"moks-terminal-secrets"}
    assert not pod["containers"][0].get("volumeMounts", [])[1:]  # no credentials volume, no seed


def test_the_gateway_can_touch_only_the_terminal_namespace():
    docs = render("values-hetzner.yaml", *TERMINAL)
    assert not [d for d in docs if d["kind"] in ("ClusterRoleBinding", "RoleBinding") and any(s["name"] == "moks-terminal" for s in d.get("subjects", [])) and d["metadata"].get("namespace") != "moks-terminals"]
    rules = of(docs, "Role", "moks-terminal-gateway")["rules"]
    assert {r for rule in rules for r in rule["resources"]} == {"pods", "pods/exec", "serviceaccounts/token"}
    assert next(r for r in rules if "serviceaccounts/token" in r["resources"])["resourceNames"] == ["moks-terminal-session"]


def test_the_session_identity_is_read_only_and_never_sees_secrets():
    docs = render("values-hetzner.yaml", *TERMINAL)
    rules = of(docs, "ClusterRole", "moks-terminal-reader")["rules"]
    assert {v for r in rules for v in r["verbs"]} == {"get", "list", "watch"}
    assert not {"secrets", "configmaps", "pods/exec", "pods/attach", "pods/portforward"} & {x for r in rules for x in r["resources"]}
    binding = of(docs, "RoleBinding", "moks-terminal-reader")
    assert binding["metadata"]["namespace"] == "moks-lab" and binding["subjects"][0]["name"] == "moks-terminal-session"
    assert not [d for d in docs if d["kind"] == "ClusterRoleBinding" and "terminal" in d["metadata"]["name"]]


def test_the_terminal_namespace_is_restricted_and_closed_by_default():
    docs = render("values.yaml", *TERMINAL)
    ns = of(docs, "Namespace", "moks-terminals")["metadata"]["labels"]
    assert ns["pod-security.kubernetes.io/enforce"] == "restricted"
    policy = of(docs, "NetworkPolicy", "terminals")["spec"]
    assert policy["podSelector"] == {} and policy["ingress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    assert not [r for r in policy["egress"] for t in r["to"] if t.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]  # nothing beyond DNS and the API server
    quota = of(docs, "ResourceQuota", "terminals")["spec"]["hard"]
    assert {"pods", "limits.cpu", "limits.memory", "limits.ephemeral-storage"} <= set(quota)


def test_configured_egress_is_exactly_what_was_listed():
    docs = render("values-hetzner.yaml", *TERMINAL, "terminal.network.egress[0].cidr=0.0.0.0/0", "terminal.network.egress[0].except={10.0.0.0/8,169.254.0.0/16}", "terminal.network.egress[0].ports={443}")
    rule = of(docs, "NetworkPolicy", "terminals")["spec"]["egress"][-1]
    assert rule["to"][0]["ipBlock"]["except"] == ["10.0.0.0/8", "169.254.0.0/16"] and rule["ports"] == [{"port": 443, "protocol": "TCP"}]


RERANKER = ("reranker.enabled=true", "reranker.image.digest=sha256:" + "b" * 64)


def test_reranking_is_absent_when_off():
    docs = render("values-eks.yaml", "image.tag=ci")
    assert not [d for d in docs if "reranker" in d["metadata"]["name"]]
    assert not [k for d in docs if d["kind"] == "ConfigMap" for k in (d.get("data") or {}) if "RERANK" in k]


def test_deploying_the_service_does_not_turn_it_on_for_search():
    config = next(d for d in render("values-hetzner.yaml", *RERANKER, "reranker.client.enabled=false") if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]
    assert config["MOKS_RAG_RERANK_ENABLED"] == "false" and config["MOKS_RAG_RERANK_REVISION"] and config["MOKS_RAG_RERANK_URL"].startswith("http://moks-reranker.moks.svc")


def test_the_reranker_has_no_public_route_and_runs_restricted():
    docs = render("values-hetzner.yaml", *RERANKER)
    assert not [d for d in docs if d["kind"] in ("Ingress", "VirtualService") and "reranker" in str(d)]
    assert of(docs, "Service", "moks-reranker")["spec"]["type"] == "ClusterIP"
    pod = of(docs, "Deployment", "moks-reranker")["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True and pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["resources"]["limits"]["memory"] and "@sha256:" in c["image"] and c["imagePullPolicy"] == "IfNotPresent"
    assert {e["name"] for e in c["env"]} >= {"RERANKER_TOKEN", "RERANKER_THREADS"} and "envFrom" not in c  # never the database credentials


def test_only_the_api_and_worker_reach_it_and_it_reaches_nothing():
    docs = render("values-hetzner.yaml", *RERANKER)
    policy = of(docs, "NetworkPolicy", "moks-reranker")["spec"]
    assert policy["egress"] == [] and set(policy["policyTypes"]) == {"Ingress", "Egress"}
    callers = [p["podSelector"]["matchLabels"] for p in policy["ingress"][0]["from"]]
    assert callers == [{"app": "moks", "component": "api"}, {"app": "moks", "component": "worker"}]
    assert any(r.get("to") == [{"podSelector": {"matchLabels": {"app": "moks-reranker"}}}] for r in of(docs, "NetworkPolicy", "moks")["spec"]["egress"])


def test_a_tag_is_pulled_on_every_start_so_a_restart_gets_it():
    c = of(render("values-hetzner.yaml", "reranker.enabled=true", "reranker.image.tag=latest"), "Deployment", "moks-reranker")["spec"]["template"]["spec"]["containers"][0]
    assert c["image"].endswith("/moks-reranker:latest") and c["imagePullPolicy"] == "Always"


@pytest.mark.parametrize("bad", [("reranker.image.digest=latest",), ("reranker.image.tag=",)])
def test_an_unusable_image_reference_is_refused(bad):
    with pytest.raises(subprocess.CalledProcessError):
        render("values-hetzner.yaml", "reranker.enabled=true", *bad)


def test_the_mesh_allows_only_the_callers():
    docs = render("values-eks.yaml", *RERANKER)
    rule = of(docs, "AuthorizationPolicy", "moks-reranker")["spec"]["rules"][0]
    assert rule["to"][0]["operation"]["ports"] == ["8090"] and all(p.startswith("cluster.local/ns/moks/sa/") for p in rule["from"][0]["source"]["principals"])


def test_opensearch_public_route_is_a_delegate_that_strips_its_prefix():
    # The login and the read-only rule are the gateway's (miniKube deploy/minikube/istio/edge): this only routes.
    docs = render("values-hetzner.yaml")
    route = of(docs, "VirtualService", "moks-opensearch")["spec"]
    assert "hosts" not in route and "gateways" not in route
    (http,) = route["http"]
    assert "match" not in http and http["rewrite"] == {"uriRegexRewrite": {"match": "^/opensearch/?", "rewrite": "/"}}
    assert http["route"][0]["destination"] == {"host": "moks-opensearch.moks.svc.cluster.local", "port": {"number": 9200}}
    assert not any(d["apiVersion"].startswith("traefik.io/") for d in docs)
    rules = of(docs, "NetworkPolicy", "moks-opensearch")["spec"]["ingress"]
    assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "istio-ingress"}}} in rules[1]["from"]


def test_opensearch_routes_need_the_gateway_routing_table():
    # Without a delegating gateway there is nowhere for the login and the read-only rule to live.
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-hetzner.yaml", "istio.delegate=false", "istio.gateway=istio-ingress/public", "istio.host=jithendar.com")
    assert "needs istio.delegate" in e.value.stderr


def test_opensearch_dashboards_has_its_own_login_and_a_writable_config():
    docs = render("values-hetzner.yaml")
    pod = of(docs, "Deployment", "moks-opensearch-dashboards")["spec"]["template"]["spec"]
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["SERVER_BASEPATH"] == "/opensearch-dashboards" and env["SERVER_REWRITEBASEPATH"] == "true"
    assert env["OPENSEARCH_HOSTS"] == '["http://moks-opensearch:9200"]' and env["DISABLE_SECURITY_DASHBOARDS_PLUGIN"] == "true"
    assert ":" + "3.9.0" in pod["containers"][0]["image"] and ":3.9.0" in of(docs, "StatefulSet", "moks-opensearch")["spec"]["template"]["spec"]["containers"][0]["image"]
    assert not pod["containers"][0].get("volumeMounts") and pod["automountServiceAccountToken"] is False
    route = of(docs, "VirtualService", "moks-opensearch-dashboards")["spec"]
    assert "hosts" not in route and "rewrite" not in route["http"][0]   # Dashboards serves its base path itself
    assert route["http"][0]["route"][0]["destination"]["host"] == "moks-opensearch-dashboards.moks.svc.cluster.local"
    assert {"podSelector": {"matchLabels": {"app": "moks-opensearch-dashboards"}}} in of(docs, "NetworkPolicy", "moks-opensearch")["spec"]["ingress"][2]["from"]


@pytest.mark.parametrize("sets", [("rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=false"), ("rag.opensearch.url=https://d.example", "rag.opensearch.awsRegion=us-east-1")])
def test_opensearch_stays_private_unless_the_bundled_pod_is_published(sets):
    docs = render("values-hetzner.yaml", *sets)
    assert not any(d["kind"] == "IngressRoute" or d["metadata"]["name"].startswith("opensearch-") for d in docs)
    assert {d["metadata"]["name"] for d in docs if d["kind"] == "VirtualService"} == {"moks"}


def test_mk4_runs_opensearch_on_the_node_with_its_data():
    # On k3s the local-path volume's own node affinity places the pod; nodeName is for clusters where it does not.
    docs = render("values-hetzner.yaml", "rag.opensearch.nodeName=mk4-m02")
    spec = of(docs, "StatefulSet", "moks-opensearch")["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "mk4-m02"} and spec["initContainers"][0]["name"] == "volume-permissions"
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
    assert env["cluster.routing.allocation.disk.watermark.flood_stage"] == "1gb"
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]
    assert config["MOKS_RAG_BACKEND"] == "opensearch" and config["MOKS_OPENSEARCH_URL"] == "http://moks-opensearch:9200"


def test_watermarks_stay_out_of_the_chart_unless_asked_for():
    env = of(render("values-hetzner.yaml", "rag.opensearch.diskWatermarks=null"), "StatefulSet", "moks-opensearch")["spec"]["template"]["spec"]["containers"][0]["env"]
    assert not [e for e in env if "watermark" in e["name"]]



SECURE = ("rag.opensearch.secure.enabled=true",)
ACTIVE = (*SECURE, "rag.opensearch.secure.active=true", "rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=false")


def test_the_secured_cluster_is_off_on_mk4_until_asked_for():
    docs = render("values-hetzner.yaml")
    assert not any(d["metadata"]["name"].startswith("moks-search") for d in docs)
    assert of(docs, "ConfigMap", "moks-config")["data"]["MOKS_OPENSEARCH_URL"] == "http://moks-opensearch:9200"


def test_enabling_it_deploys_beside_the_old_pod_and_moves_nothing():
    docs = render("values-hetzner.yaml", *SECURE)
    assert of(docs, "StatefulSet", "moks-search") and of(docs, "StatefulSet", "moks-opensearch")
    assert of(docs, "ConfigMap", "moks-config")["data"]["MOKS_OPENSEARCH_URL"] == "http://moks-opensearch:9200"
    ops = of(docs, "CronJob", "moks-search-ops")["spec"]
    env = {e["name"]: e.get("value") for e in ops["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert ops["suspend"] is True and env["MOKS_OPENSEARCH_STANDBY"] == "true" and env["MOKS_OPENSEARCH_CLUSTER"] == "secure"


def test_the_secured_pod_is_hardened_and_never_disables_security():
    docs = render("values-hetzner.yaml", *SECURE, "rag.opensearch.secure.nodeName=mk4-m02")
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
    docs = render("values-hetzner.yaml", *SECURE)
    assert of(docs, "Service", "moks-search")["spec"]["type"] == "ClusterIP"
    assert not any(d["kind"] in ("Ingress", "IngressRoute") and "moks-search" in str(d["spec"]) for d in docs)
    np = of(docs, "NetworkPolicy", "moks-search")["spec"]
    who = np["ingress"][0]["from"]
    assert who[0]["podSelector"]["matchExpressions"][1]["values"] == ["api", "worker", "search-ops"] and np["egress"] == []
    assert {"to": [{"podSelector": {"matchLabels": {"app": "moks-search"}}}], "ports": [{"port": 9200, "protocol": "TCP"}]} in of(docs, "NetworkPolicy", "moks")["spec"]["egress"]


def test_at_cutover_each_component_gets_its_own_identity():
    docs = render("values-hetzner.yaml", *ACTIVE)
    data = of(docs, "ConfigMap", "moks-config")["data"]
    assert data["MOKS_OPENSEARCH_URL"] == "https://moks-search:9200" and data["MOKS_OPENSEARCH_CLUSTER"] == "secure"

    def names(deployment):
        return {e["name"] for e in of(docs, "Deployment", deployment)["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "MOKS_OPENSEARCH_PASSWORD" in names("moks-api") and "MOKS_OPENSEARCH_INDEX_PASSWORD" not in names("moks-api") and "MOKS_OPENSEARCH_LIFECYCLE_PASSWORD" not in names("moks-api")
    assert {"MOKS_OPENSEARCH_INDEX_PASSWORD", "MOKS_OPENSEARCH_LIFECYCLE_PASSWORD"} <= names("moks-worker")
    volume = next(v for v in of(docs, "Deployment", "moks-api")["spec"]["template"]["spec"]["volumes"] if v["name"] == "search-ca")
    assert volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]  # never tls.key


def test_the_original_pod_goes_only_after_cutover_and_only_when_asked():
    verified = "rag.opensearch.secure.cutoverVerified=moks-chunks-ab12cd34 validated 2026-11-20"
    docs = render("values-hetzner.yaml", *ACTIVE, "rag.opensearch.secure.retireOriginal=true", verified)
    assert not any(d["metadata"]["name"] == "moks-opensearch" for d in docs) and of(docs, "StatefulSet", "moks-search")
    with pytest.raises(subprocess.CalledProcessError) as e:  # active is not the same as verified
        render("values-hetzner.yaml", *ACTIVE, "rag.opensearch.secure.retireOriginal=true")
    assert "cutoverVerified" in str(e.value.stderr)
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-hetzner.yaml", *SECURE, "rag.opensearch.secure.retireOriginal=true")
    assert "set active first" in str(e.value.stderr)


def test_the_old_public_route_blocks_cutover():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-hetzner.yaml", *SECURE, "rag.opensearch.secure.active=true")
    assert "publicRoute" in str(e.value.stderr)


def test_the_old_dashboards_block_cutover():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-hetzner.yaml", *SECURE, "rag.opensearch.secure.active=true", "rag.opensearch.publicRoute.enabled=false", "rag.opensearch.dashboards.enabled=true")
    assert "rag.opensearch.dashboards" in str(e.value.stderr)


def test_snapshots_need_the_image_that_has_the_plugin():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("values-hetzner.yaml", *SECURE, "rag.opensearch.secure.snapshots.enabled=true")
    assert "pluginsInstalled" in str(e.value.stderr)
    docs = render("values-hetzner.yaml", *SECURE, "rag.opensearch.secure.snapshots.enabled=true", "rag.opensearch.secure.pluginsInstalled=true", "rag.opensearch.secure.snapshots.endpoint=https://x.r2.cloudflarestorage.com")
    spec = of(docs, "StatefulSet", "moks-search")["spec"]["template"]["spec"]
    assert [i["name"] for i in spec["initContainers"]] == ["volume-permissions", "keystore"]
    assert any(r.get("ports") == [{"port": 443, "protocol": "TCP"}] for r in of(docs, "NetworkPolicy", "moks-search")["spec"]["egress"])


def test_no_chart_file_holds_a_credential():
    from pathlib import Path
    for f in (Path(__file__).parents[2] / "deploy/helm/moks/files/opensearch").iterdir():
        assert "$2" not in f.read_text() and "hash:" not in f.read_text() and "BEGIN" not in f.read_text()


# ---- OpenSearch Dashboards on the secured cluster ------------------------------------------------


DASH = (*SECURE, "rag.opensearch.secure.dashboards.enabled=true")
ROUTED = (*DASH, "rag.opensearch.publicRoute.enabled=false", "rag.opensearch.secure.dashboards.ingress.enabled=true")


def dashboards_config(docs) -> dict:
    return yaml.safe_load(of(docs, "ConfigMap", "moks-search-dashboards")["data"]["opensearch_dashboards.yml"])


def test_secured_dashboards_are_off_by_default():
    for args in ((), SECURE):
        assert not [d for d in render("values-hetzner.yaml", *args) if d["metadata"]["name"].startswith("moks-search-dashboards")]


def test_secured_dashboards_need_the_secured_cluster():
    with pytest.raises(subprocess.CalledProcessError):
        render("values-hetzner.yaml", "rag.opensearch.secure.dashboards.enabled=true")


def test_secured_dashboards_are_internal_and_verify_tls():
    docs = render("values-hetzner.yaml", *DASH)
    assert of(docs, "Service", "moks-search-dashboards")["spec"]["type"] == "ClusterIP"
    assert not [d for d in docs if d["kind"] in ("IngressRoute", "Ingress") and d["metadata"]["name"] == "moks-search-dashboards"]
    assert of(docs, "NetworkPolicy", "moks-search-dashboards")["spec"]["ingress"] == []
    cfg = dashboards_config(docs)
    assert cfg["opensearch.ssl.verificationMode"] == "full" and cfg["opensearch.hosts"] == ["https://moks-search:9200"] and cfg["opensearch.ssl.certificateAuthorities"] == ["/etc/moks-dashboards/ca.crt"]
    assert cfg["server.basePath"] == "" and cfg["server.rewriteBasePath"] is False


def test_secured_dashboards_hold_their_own_identity_and_no_admin_credential():
    docs = render("values-hetzner.yaml", *DASH)
    pod = of(docs, "Deployment", "moks-search-dashboards")["spec"]["template"]["spec"]
    cfg = dashboards_config(docs)
    assert cfg["opensearch.username"] == "moks_dashboards" and cfg["opensearch.password"] == "${MOKS_DASHBOARDS_PASSWORD}"  # a reference, not a value
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["MOKS_DASHBOARDS_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "moks-search-dashboards"
    assert "moks-search-admin" not in json.dumps(pod) and "moks-search-clients" not in json.dumps(pod)
    secret = next(v for v in pod["volumes"] if v["name"] == "ca")["secret"]
    assert secret["items"] == [{"key": "ca.crt", "path": "ca.crt"}]  # the CA, never the node key
    assert pod["automountServiceAccountToken"] is False and pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "@sha256:" in pod["containers"][0]["image"]


def test_secured_dashboards_may_reach_moks_search_and_nothing_else_may_through_them():
    docs = render("values-hetzner.yaml", *DASH)
    allowed = of(docs, "NetworkPolicy", "moks-search")["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app": "moks-search-dashboards"}}} in allowed
    egress = of(docs, "NetworkPolicy", "moks-search-dashboards")["spec"]["egress"]
    assert egress[-1]["to"] == [{"podSelector": {"matchLabels": {"app": "moks-search"}}}] and egress[-1]["ports"] == [{"port": 9200, "protocol": "TCP"}]


def test_secured_dashboards_can_sit_behind_a_base_path():
    docs = render("values-hetzner.yaml", *ROUTED)
    cfg = dashboards_config(docs)
    assert cfg["server.basePath"] == "/opensearch" and cfg["server.rewriteBasePath"] is True and cfg["opensearch_security.cookie.secure"] is True
    pod = of(docs, "Deployment", "moks-search-dashboards")["spec"]["template"]["spec"]["containers"][0]
    assert pod["startupProbe"]["httpGet"]["path"] == "/opensearch/app/login"  # with rewriteBasePath the server answers under it
    # The delegate the gateway's routing table uses for /opensearch; nothing strips the prefix.
    route = of(docs, "VirtualService", "moks-opensearch")["spec"]
    assert "hosts" not in route and "rewrite" not in route["http"][0]
    assert route["http"][0]["route"][0]["destination"]["host"] == "moks-search-dashboards.moks.svc.cluster.local"
    assert of(docs, "NetworkPolicy", "moks-search-dashboards")["spec"]["ingress"]


def test_secured_dashboards_cannot_take_a_path_already_in_use():
    with pytest.raises(subprocess.CalledProcessError, match="") as e:
        render("values-hetzner.yaml", *DASH, "rag.opensearch.secure.dashboards.ingress.enabled=true")
    assert "taken by rag.opensearch.publicRoute" in e.value.stderr


def test_anonymous_access_stays_refused():
    config = (CHART / "files/opensearch/config.yml").read_text()
    assert "anonymous_auth_enabled: false" in config


def test_the_dashboards_identities_have_least_privilege():
    roles = yaml.safe_load((CHART / "files/opensearch/roles.yml").read_text())
    viewer = roles["moks_knowledge_viewer"]
    assert viewer["index_permissions"][0]["index_patterns"] == ["moks-chunks-*"] and viewer["index_permissions"][0]["fls"] == ["~embedding"]
    assert not {"write", "indices_all", "crud", "manage"} & set(viewer["index_permissions"][0]["allowed_actions"])
    mapping = yaml.safe_load((CHART / "files/opensearch/roles_mapping.yml").read_text())
    assert "moks_dashboards" not in mapping["all_access"]["users"] and "moks_viewer" not in mapping["all_access"]["users"]
    assert mapping["kibana_server"]["users"] == ["moks_dashboards"]


# ---- secret sync -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("values", ["values.yaml", "values-eks.yaml"])
def test_secret_sync_is_off_by_default_and_adds_nothing(values):
    docs = render(values)
    assert not [d for d in docs if d["metadata"]["name"] == "moks-secret-sync"]
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")
    assert config["data"]["MOKS_SECRETS_SYNC_ENABLED"] == "false"


@pytest.mark.parametrize("split", [False, True])
def test_only_the_executor_holds_the_secret_sync_secret_and_secret_verbs(split):
    sets = ["secretsSync.enabled=true", "secretsSync.namespaces={shop,pay}", "secretsSync.allowDelete=false"] + (list(SPLIT) if split else [])
    docs = render("values-hetzner.yaml", *sets)
    holders = {name for name, pod in pods(docs) if "moks-secret-sync" in secrets(pod)}
    assert holders == {"moks-executor"}
    roles = {d["metadata"]["namespace"]: d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "moks-secret-sync"}
    assert set(roles) == {"shop", "pay"}
    verbs = next(r["verbs"] for r in roles["shop"]["rules"] if r["resources"] == ["secrets"])
    assert verbs == ["get", "list", "create", "update"]  # no delete unless allowDelete
    bindings = [d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "moks-secret-sync"]
    assert {s["name"] for b in bindings for s in b["subjects"]} == {"moks-executor"}
    reader = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "moks-reader")
    assert not [r for r in reader["rules"] if "secrets" in r["resources"]]  # the read-only connector still cannot read Secrets


@pytest.mark.parametrize("values", ["values-hetzner.yaml", "values-eks.yaml"])
def test_secret_sync_is_manual_only_unless_scheduling_is_chosen(values):
    def policy(*sets):
        docs = render(values, "secretsSync.enabled=true", "secretsSync.namespaces={shop}", *sets)
        return next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "moks-config")["data"]["MOKS_SECRETS_SYNC_POLICY"]
    assert policy() == "manual"
    assert policy("secretsSync.policy=scheduled") == "scheduled"


def test_secret_delete_verb_needs_allow_delete():
    docs = render("values-hetzner.yaml", "secretsSync.enabled=true", "secretsSync.namespaces={shop}", "secretsSync.allowDelete=true")
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "moks-secret-sync")
    assert "delete" in next(r["verbs"] for r in role["rules"] if r["resources"] == ["secrets"])


def test_cnpg_status_is_readable_on_mk4_only_and_never_writable():
    def rule(values):
        reader = next(d for d in render(values) if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "moks-reader")
        return [r for r in reader["rules"] if r["apiGroups"] == ["postgresql.cnpg.io"]]
    assert rule("values-hetzner.yaml") == [{"apiGroups": ["postgresql.cnpg.io"], "resources": ["clusters", "backups", "scheduledbackups"], "verbs": ["get", "list"]}]
    assert rule("values-eks.yaml") == []  # off unless cnpg.enabled
