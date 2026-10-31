#!/usr/bin/env bash
# Proves terminal isolation on a real cluster: a kind cluster with kindnet, the network plugin mk4 runs.
# It applies the chart's terminal resources, starts a session pod as the gateway would, and runs
# moks-verify-isolation inside it. Never point this at a shared cluster: KUBECONFIG must be kind's.
#   IMAGE=moks-terminal:test scripts/terminal-isolation-test.sh
set -euo pipefail
: "${IMAGE:?set IMAGE to a locally built terminal image}"
[[ "$(kubectl config current-context)" == kind-* ]] || { echo "refusing: the current context is not a kind cluster" >&2; exit 1; }
here="$(cd "$(dirname "$0")/.." && pwd)"
kind load docker-image "$IMAGE" --name "${KIND_CLUSTER:-kind}"

api_ip="$(kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes -o jsonpath='{.items[0].endpoints[0].addresses[0]}')"
kubectl create namespace moks --dry-run=client -o yaml | kubectl apply -f -
helm template moks "$here/deploy/helm/moks" -n moks -s templates/terminal.yaml \
  --set image.tag=test --set terminal.enabled=true --set terminal.image.digest="sha256:$(printf 'a%.0s' {1..64})" \
  --set 'terminal.profiles[0].name=test' --set 'terminal.kubernetes.namespaces={moks}' --set terminal.network.apiServer=true \
  --set-json "networkPolicy.apiServer={\"cidrs\":[\"10.96.0.1/32\",\"$api_ip/32\"],\"ports\":[443,6443]}" \
  | awk 'BEGIN{RS="---\n"; ORS="---\n"} !/kind: (Deployment|Service)\n/ || /kind: ServiceAccount/' | kubectl apply -n moks -f -

# A stand-in for what a shell must not reach: the MOKS database and API, on their real ports.
kubectl -n moks run decoy --image="$IMAGE" --labels=app=moks-postgres --command -- sh -c 'while true; do nc -l -p 5432; done'
kubectl -n moks expose pod decoy --port=5432
kubectl -n moks wait pod/decoy --for=condition=Ready --timeout=120s
decoy_ip="$(kubectl -n moks get pod decoy -o jsonpath='{.status.podIP}')"
node_ip="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')"

python3 - "$IMAGE" <<'PY' | kubectl apply -f -
import json, sys, types
sys.path.insert(0, "backend")
import os
os.environ.update(MOKS_ENV="development", MOKS_AUTH_MODE="dev")
from datetime import datetime, timezone
from moks.terminal.pods import pod_manifest
s = types.SimpleNamespace(id="test", owner_id="ci", pod_name="moks-term-test", pod_namespace="moks-terminals", image=sys.argv[1], region="us-east-1",
                          aws_account="000000000000", environment="ci", lifetime_minutes=15, expires_at=datetime.now(timezone.utc))
print(json.dumps(pod_manifest(s)))
PY
kubectl -n moks-terminals wait pod/moks-term-test --for=condition=Ready --timeout=120s

# The same token request the gateway makes, bound to the pod.
token="$(kubectl -n moks-terminals create token moks-terminal-session --bound-object-kind Pod --bound-object-name moks-term-test --duration 15m)"
kubectl -n moks-terminals exec moks-term-test -- sh -c "umask 077; cat > /run/moks/kubeconfig" <<KUBE
apiVersion: v1
kind: Config
current-context: t
clusters: [{name: c, cluster: {server: "https://10.96.0.1:443", insecure-skip-tls-verify: true}}]
users: [{name: u, user: {token: "$token"}}]
contexts: [{name: t, context: {cluster: c, user: u, namespace: moks}}]
KUBE

kubectl -n moks-terminals exec moks-term-test -- env \
  MOKS_VERIFY_BLOCKED="moks-postgres.moks.svc.cluster.local:5432 decoy.moks.svc.cluster.local:5432 $decoy_ip:5432 $node_ip:10250 1.1.1.1:443" \
  MOKS_VERIFY_ALLOWED="10.96.0.1:443" \
  moks-verify-isolation
kubectl -n moks-terminals exec moks-term-test -- kubectl get pods -n moks >/dev/null && echo "ok    the session can read what it was granted"
if kubectl -n moks-terminals exec moks-term-test -- kubectl get pods -n kube-system >/dev/null 2>&1; then echo "FAIL  read outside its namespaces"; exit 1; else echo "ok    the session cannot read other namespaces"; fi

# Restricted Pod Security must refuse what a session pod must never be.
if kubectl -n moks-terminals run root-attempt --image="$IMAGE" --overrides='{"spec":{"hostNetwork":true}}' 2>/dev/null; then
  echo "FAIL  restricted Pod Security admitted a hostNetwork pod"; exit 1
fi
echo "ok    Pod Security refused a hostNetwork pod"

# A token bound to a pod must die with it.
kubectl -n moks-terminals delete pod moks-term-test --now
sleep 3
if kubectl --token="$token" --server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')" --insecure-skip-tls-verify get pods -n moks >/dev/null 2>&1; then
  echo "FAIL  the session token outlived its pod"; exit 1
fi
echo "ok    the session token died with its pod"
