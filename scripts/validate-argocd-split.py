#!/usr/bin/env python3
"""Static proof that the child Argo CD Applications add up to exactly the MOKS chart.

Renders every resource group across a matrix of values, and fails on a resource with two owners or
none, an unexpected addition, or any change to a rendered object. Then checks the Application
manifests: paths, value files, projects, destinations and the safety settings of docs/ARGOCD_APP_SPLIT.md.
No cluster is touched, so a pass says nothing about what is live.

  scripts/validate-argocd-split.py                          the checks CI runs
  scripts/validate-argocd-split.py --baseline <chart dir>   also compare with a copy of the chart from before the split
  scripts/validate-argocd-split.py --compare-live eks app.json    compare with `kubectl get application -o json`
  scripts/validate-argocd-split.py --ownership-table        print the table in docs/ARGOCD_APP_SPLIT.md
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/moks"
ARGOCD = ROOT / "deploy/argocd"
GROUPS = ["foundation", "database", "migrate", "opensearch", "api", "worker", "executor", "terminal", "reranker", "routing", "backup"]
CLUSTER_SCOPED = {"Namespace", "ClusterRole", "ClusterRoleBinding"}
REPO = "https://github.com/mrjithendar/moks.git"
TERMINAL_ON = ["--set", "terminal.enabled=true", "--set", "terminal.image.digest=sha256:" + "a" * 64, "--set", "terminal.profiles[0].name=ro",
               "--set", "terminal.kubernetes.namespaces={moks-lab}", "--set", "terminal.network.apiServer=true"]
POOLS = ["--set-json", 'worker.pools=[{"name":"detection","classes":"detection","capacity":"detection=2","replicas":1},'
                       '{"name":"knowledge","classes":"knowledge,maintenance","capacity":"knowledge=1,maintenance=1","replicas":2}]']
OPENSEARCH = ["--set", "rag.backend=opensearch"]
RERANKER = ["--set", "reranker.enabled=true", "--set", "reranker.image.digest=sha256:" + "b" * 64]
BACKUP = ["--set", "backup.enabled=true", "--set", "backup.bucket=ci"]
SPLIT = ["--set", "secrets.split=true"]
# name -> (extra helm arguments, groups that must render something, groups that must render nothing)
SCENARIOS = {
    "base": ([], {"foundation", "api", "worker"}, set()),   # the rest depends on the values; check_environment matches it to apps/
    "split": (SPLIT, {"migrate"}, set()),
    "terminal-on": (TERMINAL_ON, {"terminal"}, set()),
    "terminal-on-split": (TERMINAL_ON + SPLIT, {"terminal", "migrate"}, set()),
    "terminal-off": (["--set", "terminal.enabled=false"], set(), {"terminal"}),
    "external-postgres": (["--set", "postgres.bundled=false"], set(), {"database"}),
    "external-postgres-split-backup": (["--set", "postgres.bundled=false"] + SPLIT + BACKUP, {"migrate"}, {"database", "backup"}),
    "rag-postgres": (["--set", "rag.backend=postgres"], set(), {"opensearch"}),
    "rag-pgvector": (["--set", "rag.backend=pgvector", "--set", "postgres.image=pgvector/pgvector:0.8.7-pg17", "--set", "postgres.uid=999"], {"database"}, {"opensearch"}),
    "rag-pinecone": (["--set", "rag.backend=pinecone"], set(), {"opensearch"}),
    "rag-opensearch-bundled": (OPENSEARCH, {"opensearch"}, set()),
    "rag-opensearch-managed": (OPENSEARCH + ["--set", "rag.opensearch.url=https://search.example", "--set", "rag.opensearch.awsRegion=us-east-1"], set(), {"opensearch"}),
    "worker-pools": (POOLS, {"worker"}, set()),
    "worker-pools-split": (POOLS + SPLIT, {"worker", "migrate"}, set()),
    "backup": (BACKUP, {"backup"}, set()),
    "backup-split": (BACKUP + SPLIT, {"backup", "migrate"}, set()),
    "reranker-on": (RERANKER, {"reranker"}, set()),
    "reranker-off": (["--set", "reranker.enabled=false"], set(), {"reranker"}),
    "custom-skills": (["--set", "customSkills.runbook=body"], {"foundation"}, set()),
    "namespaced-rbac": (["--set", "rbac.clusterWide=false", "--set", "rbac.namespaces={moks}"], set(), set()),
    "no-network-policy": (["--set", "networkPolicy.enabled=false"], set(), set()),
    "velero": (["--set", "velero.enabled=true"], {"foundation", "executor"}, set()),
    "executor-off": (["--set", "executor.enabled=false"], set(), {"executor"}),
    "everything": (TERMINAL_ON + SPLIT + POOLS + OPENSEARCH + BACKUP + ["--set", "customSkills.runbook=body"],
                   {"terminal", "migrate", "opensearch", "backup", "database"}, set()),
}
# What each optional Application needs before it may leave optional/, for the project checks.
OPTIONAL_SCENARIO = {"migrate": "split", "opensearch": "rag-opensearch-bundled", "backup": "backup", "reranker": "reranker-on"}
ENVIRONMENTS = {
    "minikube": {
        "prefix": "moks", "project": "moks", "projectFile": "minikube/project.yaml", "values": "values-minikube.yaml", "legacy": "legacy/application.yaml",
        "old": "moks-portal", "destination": {"server": "https://kubernetes.default.svc", "namespace": "moks"},
        "namespaceLabels": {"app.kubernetes.io/part-of": "moks", "pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/warn": "restricted"},
    },
    "eks": {
        "prefix": "roboshop-demo-moks", "project": "moks-eks", "projectFile": "eks/project.yaml", "values": "values-eks.yaml", "legacy": "legacy/application-eks.yaml",
        "old": "roboshop-demo-moks-portal", "destination": {"name": "roboshop-demo", "namespace": "moks"},
        "namespaceLabels": {"app.kubernetes.io/part-of": "moks", "istio-injection": "enabled",
                            "pod-security.kubernetes.io/enforce": "privileged", "pod-security.kubernetes.io/warn": "restricted"},
    },
}
RESERVED_APP_NAMES = {"moks", "roboshop-demo-moks"}   # the RoboShop shopping widget, unrelated to this portal

failures: list[str] = []
passed: Counter = Counter()


def check(ok: bool, message: str, category: str = "other") -> bool:
    passed[category] += bool(ok)
    if not ok:
        failures.append(message)
    return ok


def render(chart: Path, environment: str, extra: list[str], group: str | None = None) -> list[dict]:
    args = ["helm", "template", "moks", str(chart), "-n", "moks", "-f", str(chart / ENVIRONMENTS[environment]["values"]), *extra]
    if group:
        args += ["--set", f"resourceGroup={group}"]
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"helm template failed ({environment}, {group or 'whole chart'}, {' '.join(extra)}):\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def key(doc: dict) -> tuple[str, str, str, str]:
    group = doc["apiVersion"].rpartition("/")[0]
    namespace = "" if doc["kind"] in CLUSTER_SCOPED else doc["metadata"].get("namespace", "moks")
    return group, doc["kind"], namespace, doc["metadata"]["name"]


def label(k: tuple[str, str, str, str]) -> str:
    return f"{k[0] or 'core'}/{k[1]}/{k[2] or '-'}/{k[3]}"


def index(docs: list[dict]) -> dict:
    return {key(d): d for d in docs}


def pod_spec(doc: dict) -> tuple[dict, dict] | None:
    spec = doc.get("spec") or {}
    template = spec.get("jobTemplate", {}).get("spec", spec).get("template")
    return (template.get("metadata") or {}, template["spec"]) if template else None


def references(pod: dict) -> dict:
    """Every ConfigMap and Secret a pod names, and its identity."""
    configmaps, secrets = set(), set()
    for c in pod.get("containers", []) + pod.get("initContainers", []):
        for e in c.get("envFrom", []):
            configmaps |= {e["configMapRef"]["name"]} if "configMapRef" in e else set()
            secrets |= {e["secretRef"]["name"]} if "secretRef" in e else set()
        for e in c.get("env", []):
            source = e.get("valueFrom", {})
            configmaps |= {source["configMapKeyRef"]["name"]} if "configMapKeyRef" in source else set()
            secrets |= {source["secretKeyRef"]["name"]} if "secretKeyRef" in source else set()
    for v in pod.get("volumes", []):
        configmaps |= {v["configMap"]["name"]} if "configMap" in v else set()
        secrets |= {v["secret"]["secretName"]} if "secret" in v else set()
    return {"configMaps": sorted(configmaps), "secrets": sorted(secrets), "serviceAccount": pod.get("serviceAccountName"),
            "automountToken": pod.get("automountServiceAccountToken"), "imagePullSecrets": pod.get("imagePullSecrets")}


def facts(docs: dict) -> dict[str, dict]:
    """The properties the migration must not change, by category, so a failure says which one moved."""
    found: dict[str, dict] = defaultdict(dict)
    for k, d in docs.items():
        name, spec = label(k), d.get("spec") or {}
        if k[1] in ("Deployment", "StatefulSet", "PodDisruptionBudget", "Service"):
            found["selectors"][name] = spec.get("selector")
        if k[1] == "NetworkPolicy":
            found["networkPolicies"][name] = spec
        if k[1] in ("PeerAuthentication", "AuthorizationPolicy", "VirtualService", "Ingress", "IngressRoute", "Middleware"):
            found["meshAndRouting"][name] = spec
        if k[1] in ("Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"):
            found["rbac"][name] = {f: d.get(f) for f in ("rules", "roleRef", "subjects")}
        if k[1] == "ServiceAccount":
            found["serviceAccounts"][name] = {"annotations": d["metadata"].get("annotations"), "automount": d.get("automountServiceAccountToken")}
        if k[1] == "Namespace":
            found["namespaces"][name] = {"labels": d["metadata"].get("labels"), "annotations": d["metadata"].get("annotations")}
        if k[1] == "StatefulSet":
            found["storage"][name] = {"serviceName": spec["serviceName"], "volumeClaimTemplates": spec["volumeClaimTemplates"]}
        pod = pod_spec(d)
        if pod:
            found["podReferences"][name] = references(pod[1])
            found["podLabels"][name] = pod[0].get("labels")
            found["checksums"][name] = pod[0].get("annotations")
    return found


def check_scenario(environment: str, name: str, baseline: Path | None) -> dict:
    """Returns {resource key: owning group} for this scenario."""
    extra, must_render, must_be_empty = SCENARIOS[name]
    where = f"{environment}/{name}"
    whole = render(CHART, environment, extra)
    check(len(index(whole)) == len(whole), f"{where}: the chart renders one resource twice", "uniqueness")
    owners: dict = {}
    union: dict = {}
    for group in GROUPS:
        docs = render(CHART, environment, extra, group)
        if group in must_render:
            check(bool(docs), f"{where}: group {group} rendered nothing", "groups")
        if group in must_be_empty:
            check(not docs, f"{where}: group {group} should be empty", "groups")
        for d in docs:
            k = key(d)
            check(k not in owners, f"{where}: {label(k)} is in {owners.get(k)} and {group}", "duplicates")
            owners[k] = group
            union[k] = d
    expected = index(whole)
    for k in expected.keys() - union.keys():
        check(False, f"{where}: {label(k)} has no owner", "missing")
    for k in union.keys() - expected.keys():
        check(False, f"{where}: {label(k)} is not in the whole chart", "additions")
    for k in expected.keys() & union.keys():
        check(expected[k] == union[k], f"{where}: {label(k)} differs between its group and the whole chart", "identical")
    if baseline:
        original = index(render(baseline, environment, extra))
        for k in original.keys() - union.keys():
            check(False, f"{where}: {label(k)} from the original chart is missing", "missing")
        for k in union.keys() - original.keys():
            check(False, f"{where}: {label(k)} was added since the original chart", "additions")
        passed["inventory"] += original.keys() == union.keys()
        for k in original.keys() & union.keys():
            check(original[k] == union[k], f"{where}: {label(k)} differs from the original chart", "identical")
        before, after = facts(original), facts(union)
        for category in before.keys() | after.keys():
            check(before.get(category) == after.get(category), f"{where}: {category} differ from the original chart", category)
    check_invariants(where, union, owners)
    return owners


def check_invariants(where: str, docs: dict, owners: dict) -> None:
    names = {(k[1], k[3]) for k in docs}
    for k, d in docs.items():
        check(k[1] != "Secret", f"{where}: {label(k)} renders a Secret", "secrets")
        selector = (d.get("spec") or {}).get("selector")
        text = json.dumps(selector) if selector else ""
        check("app.kubernetes.io/instance" not in text and "argocd.argoproj.io" not in text, f"{where}: {label(k)} selects on a tracking label", "selectorsAvoidTracking")
        pod = pod_spec(d)
        if pod:
            refs = references(pod[1])
            for configmap in refs["configMaps"]:
                check(("ConfigMap", configmap) in names, f"{where}: {label(k)} needs ConfigMap {configmap}, which no group renders", "references")
            if refs["serviceAccount"]:
                check(("ServiceAccount", refs["serviceAccount"]) in names, f"{where}: {label(k)} needs ServiceAccount {refs['serviceAccount']}, which no group renders", "references")
    checksums = {pod_spec(d)[0].get("annotations", {}).get("checksum/config") for d in docs.values() if pod_spec(d) and d["kind"] == "Deployment" and references(pod_spec(d)[1])["configMaps"]}
    check(len(checksums) <= 1, f"{where}: consumers disagree on checksum/config", "checksums")
    for name in ("moks-config", "moks-seed", "moks-skills"):
        k = ("", "ConfigMap", "moks", name)
        if k in owners:
            check(owners[k] == "foundation", f"{where}: {name} belongs to {owners[k]}", "sharedConfig")
    for k, group in owners.items():
        if k[1] == "Namespace":
            check(group in ("foundation", "executor", "terminal"), f"{where}: {label(k)} belongs to {group}", "namespaces")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def source(app: dict) -> dict:
    return app["spec"]["source"]


def group_of(app: dict) -> str | None:
    return next((p["value"] for p in source(app).get("helm", {}).get("parameters", []) if p["name"] == "resourceGroup"), None)


def check_common(path: Path, app: dict) -> None:
    where = str(path.relative_to(ROOT))
    policy = app["spec"].get("syncPolicy", {})
    check(app.get("kind") == "Application" and app["metadata"].get("namespace") == "argocd", f"{where}: not an Application in argocd", "applications")
    check("automated" not in policy, f"{where}: automated sync is on", "manualSync")
    check(not app["metadata"].get("finalizers"), f"{where}: a finalizer would make deletion cascade", "noFinalizers")
    check("FailOnSharedResource=true" in policy.get("syncOptions", []), f"{where}: FailOnSharedResource=true is missing", "failOnShared")
    check("PruneLast=true" in policy.get("syncOptions", []), f"{where}: PruneLast=true is missing", "applications")
    check("Prune=false,Delete=false" == app["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-options"), f"{where}: not protected from the root", "protection")
    check(app["metadata"]["name"] not in RESERVED_APP_NAMES, f"{where}: that name is the RoboShop shopping widget", "applications")
    check(source(app)["repoURL"] == REPO and source(app)["targetRevision"] == "master", f"{where}: source repository or revision changed", "applications")
    check((ROOT / source(app)["path"]).is_dir(), f"{where}: path {source(app)['path']} does not exist", "paths")


def allowed(project: dict, key_: tuple[str, str, str, str]) -> bool:
    spec = project["spec"]
    listed = spec.get("clusterResourceWhitelist" if key_[1] in CLUSTER_SCOPED else "namespaceResourceWhitelist") or []
    return any(w["group"] == key_[0] and w["kind"] == key_[1] for w in listed)


def check_project_admits(where: str, project: dict, destination: dict, docs: list[dict]) -> None:
    for d in docs:
        k = key(d)
        check(allowed(project, k), f"{where}: project {project['metadata']['name']} refuses {label(k)}", "projects")
        if k[2]:
            cluster = {f: destination[f] for f in ("server", "name") if f in destination}
            check({**cluster, "namespace": k[2]} in project["spec"]["destinations"], f"{where}: project {project['metadata']['name']} has no destination for {label(k)}", "projects")


def check_environment(name: str) -> None:
    env = ENVIRONMENTS[name]
    directory = ARGOCD / name
    project = load(ARGOCD / env["projectFile"])
    root_project = load(ARGOCD / "project-root.yaml")
    extra: list[str] = []
    active = {group for group in GROUPS if render(CHART, name, extra, group)}

    root = load(directory / "root.yaml")
    check_common(directory / "root.yaml", root)
    check(root["metadata"]["name"] == f"{env['prefix']}-root" and root["spec"]["project"] == root_project["metadata"]["name"], f"{name}: root name or project", "root")
    check(source(root)["path"] == f"deploy/argocd/{name}/apps" and not source(root).get("directory", {}).get("recurse"), f"{name}: root must read apps/ only", "root")
    check(root["spec"]["destination"] in root_project["spec"]["destinations"], f"{name}: root destination is outside its project", "root")
    check(source(root)["repoURL"] in root_project["spec"]["sourceRepos"], f"{name}: root repository is outside its project", "root")

    apps = {group_of(load(p)): p for p in sorted((directory / "apps").glob("*.yaml"))}
    optional = {group_of(load(p)): p for p in sorted((directory / "optional").glob("*.yaml"))}
    check(active == set(apps), f"{name}: groups that render {sorted(active)} differ from Applications {sorted(str(a) for a in apps)}", "appsMatchGroups")
    check(not set(apps) & set(optional), f"{name}: an Application is in both apps/ and optional/", "appsMatchGroups")
    check(set(apps) | set(optional) <= set(GROUPS), f"{name}: unknown group", "appsMatchGroups")
    for group, path in {**apps, **optional}.items():
        app = load(path)
        where = f"{name}/{group}"
        check_common(path, app)
        spec, helm = app["spec"], source(app)["helm"]
        check(app["metadata"]["name"] == f"{env['prefix']}-{group}" == path.stem, f"{where}: name", "applications")
        check(spec["project"] == env["project"] == project["metadata"]["name"], f"{where}: project", "projects")
        check(spec["destination"] == env["destination"], f"{where}: destination", "destinations")
        check(spec["destination"] in project["spec"]["destinations"], f"{where}: destination is outside the project", "destinations")
        check(source(app)["repoURL"] in project["spec"]["sourceRepos"], f"{where}: repository is outside the project", "projects")
        check(source(app)["path"] == "deploy/helm/moks" and helm["releaseName"] == "moks", f"{where}: chart path or release name", "releaseName")
        check(helm["valueFiles"] == [env["values"]] and (ROOT / source(app)["path"] / env["values"]).is_file(), f"{where}: value files", "valueFiles")
        is_foundation = group == "foundation"
        check(("CreateNamespace=true" in spec["syncPolicy"]["syncOptions"]) == is_foundation, f"{where}: only foundation creates the namespace", "namespaceOwner")
        check(("managedNamespaceMetadata" in spec["syncPolicy"]) == is_foundation, f"{where}: only foundation sets namespace metadata", "namespaceOwner")
        if is_foundation:
            check(spec["syncPolicy"]["managedNamespaceMetadata"] == {"labels": env["namespaceLabels"]}, f"{where}: namespace labels changed", "namespaceOwner")
        scenario = OPTIONAL_SCENARIO.get(group, "base")
        check_project_admits(where, project, env["destination"], render(CHART, name, SCENARIOS[scenario][0], group))

    legacy = ARGOCD / env["legacy"]
    if legacy.exists():
        check(load(legacy)["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"] == env["namespaceLabels"], f"{name}: namespace labels differ from {env['old']}", "namespaceOwner")
    check(all(d.get("namespace") != "argocd" for d in project["spec"]["destinations"]), f"{name}: project {project['metadata']['name']} reaches argocd", "projects")
    check(all(w["kind"] != "Secret" for w in project["spec"]["namespaceResourceWhitelist"]), f"{name}: project admits Secrets", "secrets")
    check(all(w["group"] != "argoproj.io" for w in project["spec"]["namespaceResourceWhitelist"]), f"{name}: project admits Applications", "projects")


def check_root_project() -> None:
    project = load(ARGOCD / "project-root.yaml")["spec"]
    check(project["namespaceResourceWhitelist"] == [{"group": "argoproj.io", "kind": "Application"}], "moks-root may write more than Applications", "root")
    check(not project.get("clusterResourceWhitelist"), "moks-root may write cluster-scoped kinds", "root")
    check(project["destinations"] == [{"server": "https://kubernetes.default.svc", "namespace": "argocd"}], "moks-root reaches beyond argocd", "root")
    check(project["sourceRepos"] == [REPO], "moks-root trusts another repository", "root")


def check_eks_terminal() -> None:
    check(load(CHART / "values-eks.yaml")["terminal"]["enabled"] is False, "values-eks.yaml turns the terminal on", "eksTerminalOff")
    check(not list((ARGOCD / "eks").rglob("*terminal*")), "eks has a terminal Application", "eksTerminalOff")


def compare_live(environment: str, path: Path) -> int:
    """Read-only preflight on a saved Application: would adoption leave anything behind?"""
    app = json.loads(path.read_text())
    live = {(r.get("group", ""), r["kind"], r.get("namespace", ""), r["name"]) for r in app["status"].get("resources", [])}
    planned = {group: index(render(CHART, environment, [], group)) for group in GROUPS}
    wanted = {k for docs in planned.values() for k in docs}
    for k in sorted(live - wanted):
        print(f"ORPHAN  tracked live, no new owner: {label(k)}")
    for k in sorted(wanted - live):
        print(f"NEW     rendered, not tracked live: {label(k)}")
    print(f"{environment}: {len(live)} tracked live, {len(wanted)} rendered, {len(live & wanted)} matched")
    return 0 if live == wanted else 1


def ownership_table() -> None:
    rows: dict = {}
    for environment in ENVIRONMENTS:
        active = {k for group in GROUPS for k in index(render(CHART, environment, [], group))}
        for scenario in SCENARIOS:
            for group in GROUPS:
                for k in index(render(CHART, environment, SCENARIOS[scenario][0], group)):
                    name = re.sub(r"^moks-migrate-[0-9a-f]{10}$", "moks-migrate-<hash>", k[3])
                    row = rows.setdefault((group, k[1], k[2], name), {})
                    row[environment] = ENVIRONMENTS[environment]["old"] if k in active else row.get(environment, "if enabled")
    print("| New group | Kind | Namespace | Name | Old owner on mk4 | Old owner on EKS |\n|---|---|---|---|---|---|")
    for (group, kind, namespace, name), row in sorted(rows.items(), key=lambda r: (GROUPS.index(r[0][0]), r[0][1], r[0][2], r[0][3])):
        print(f"| {group} | {kind} | {namespace or '(cluster)'} | `{name}` | {row.get('minikube', '-')} | {row.get('eks', '-')} |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", type=Path, help="copy of the chart from before the split")
    parser.add_argument("--compare-live", nargs=2, metavar=("ENV", "APP_JSON"))
    parser.add_argument("--ownership-table", action="store_true")
    args = parser.parse_args()
    if args.compare_live:
        return compare_live(args.compare_live[0], Path(args.compare_live[1]))
    if args.ownership_table:
        ownership_table()
        return 0
    for environment in ENVIRONMENTS:
        for scenario in SCENARIOS:
            check_scenario(environment, scenario, args.baseline)
        check_environment(environment)
    check_root_project()
    check_eks_terminal()
    print(f"{len(ENVIRONMENTS)} environments, {len(SCENARIOS)} scenarios each, baseline: {args.baseline or 'not compared'}")
    for category, count in sorted(passed.items()):
        print(f"  {category}: {count} passed")
    if failures:
        print(f"\nFAILED, {len(failures)} problems:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("OK. Rendered and static checks only: nothing here looked at a cluster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
