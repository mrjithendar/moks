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


@pytest.mark.parametrize("values", ["values-minikube.yaml", "values-eks.yaml"])
def test_default_render_changes_nothing_for_existing_installs(values):
    docs = render(values)
    by = dict(pods(docs))
    assert secrets(by["moks-api"]) == {"moks-secrets"} and by["moks-api"]["serviceAccountName"] == "moks"
    assert by["moks-api"]["initContainers"][0]["name"] == "migrate"
    assert not any(d["kind"] == "Job" for d in docs)


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


def test_eks_scales_each_class_separately():
    names = {n for n, _ in pods(render("values-eks.yaml"))}
    assert {"moks-worker-detection", "moks-worker-knowledge", "moks-worker-interactive", "moks-worker-maintenance"} <= names


def test_minikube_keeps_one_worker_with_reserved_capacity():
    by = dict(pods(render("values-minikube.yaml")))
    env = {e["name"]: e["value"] for e in by["moks-worker"]["containers"][0]["env"]}
    assert "detection=2" in env["MOKS_WORKER_CAPACITY"] and "moks-worker-detection" not in by


def test_production_runs_a_digest_and_latest_is_refused():
    by = dict(pods(render("values-minikube.yaml")))
    assert "@sha256:" in by["moks-api"]["containers"][0]["image"]
    done = subprocess.run(["helm", "template", "moks", str(CHART), "--set", "image.tag=latest", "--set", "image.digest="], capture_output=True, text=True)
    assert done.returncode != 0 and "mutable" in done.stderr


def test_the_chart_ships_the_consistent_dump():
    docs = render("values-minikube.yaml", *SPLIT)
    by = dict(pods(docs))
    dump = by["moks-backup"]["initContainers"][0]["command"][2]
    assert "consistent-dump.sql" in dump and "--manifest" in by["moks-backup"]["containers"][0]["args"]
    cm = next(d for d in docs if d["metadata"]["name"] == "moks-backup-scripts")
    assert "--snapshot" in cm["data"]["consistent-dump.sql"]
