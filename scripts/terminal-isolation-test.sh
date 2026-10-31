#!/usr/bin/env bash
# Proves terminal isolation on a real cluster: a kind cluster with kindnet, the network plugin mk4 runs,
# and two nodes (deploy/terminal/kind-isolation.yaml).
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
kubectl config set-context --current --namespace=moks   # a default, not -n: the chart also writes to moks-terminals
helm template moks "$here/deploy/helm/moks" -n moks -s templates/terminal.yaml \
  --set image.tag=test --set terminal.enabled=true --set terminal.image.digest="sha256:$(printf 'a%.0s' {1..64})" \
  --set 'terminal.profiles[0].name=test' --set 'terminal.kubernetes.namespaces={moks}' --set terminal.network.apiServer=true \
  --set-json "networkPolicy.apiServer={\"cidrs\":[\"10.96.0.1/32\",\"$api_ip/32\"],\"ports\":[443,6443]}" \
  | awk 'BEGIN{RS="---\n"; ORS="---\n"} !/kind: (Deployment|Service)\n/ || /kind: ServiceAccount/' | kubectl apply -f -

# A stand-in for what a shell must not reach: the MOKS database and API, on their real ports.
kubectl -n moks run decoy --image="$IMAGE" --labels=app=moks-postgres --command -- sh -c 'while true; do nc -lk 5432; done'
kubectl -n moks expose pod decoy --port=5432
kubectl -n moks wait pod/decoy --for=condition=Ready --timeout=120s
sleep 2
kubectl -n moks exec decoy -- nc -z -w 3 127.0.0.1 5432   # "blocked" below means nothing unless this listens
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
own_node="$(kubectl -n moks-terminals get pod moks-term-test -o jsonpath='{.status.hostIP}')"
[[ "$own_node" != "$node_ip" ]] || { echo "FAIL  the session runs on $node_ip, so that node cannot prove anything: NetworkPolicy never applies to a pod's own node"; exit 1; }

# The same token request the gateway makes, bound to the pod.
token="$(kubectl -n moks-terminals create token moks-terminal-session --bound-object-kind Pod --bound-object-name moks-term-test --duration 15m)"
kubectl -n moks-terminals exec -i moks-term-test -- sh -c "umask 077; cat > /run/moks/kubeconfig" <<KUBE
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
if kubectl -n moks-terminals exec moks-term-test -- kubectl get pods -n moks >/dev/null; then echo "ok    the session can read what it was granted"; else echo "FAIL  the session cannot read what it was granted"; exit 1; fi
if kubectl -n moks-terminals exec moks-term-test -- kubectl get pods -n kube-system >/dev/null 2>&1; then echo "FAIL  read outside its namespaces"; exit 1; else echo "ok    the session cannot read other namespaces"; fi

# NetworkPolicy cannot close a pod's own node, so its kubelet is reachable. Its own authorization must refuse.
check_kubelet() {  # label [curl arguments]
  local label="$1" code
  shift
  code="$(kubectl -n moks-terminals exec moks-term-test -- curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$@" "https://$own_node:10250/pods" || true)"
  if [[ "$code" == 401 || "$code" == 403 ]]; then echo "ok    its own node's kubelet refuses $label (HTTP $code)"; else echo "FAIL  its own node's kubelet answered $label with HTTP $code"; exit 1; fi
}
check_kubelet "an anonymous request"
check_kubelet "the session's token" -H "Authorization: Bearer $token"

# Restricted Pod Security must refuse what a session pod must never be.
if kubectl -n moks-terminals run root-attempt --image="$IMAGE" --overrides='{"spec":{"hostNetwork":true}}' 2>/dev/null; then
  echo "FAIL  restricted Pod Security admitted a hostNetwork pod"; exit 1
fi
echo "ok    Pod Security refused a hostNetwork pod"

# A token bound to a pod must die with it.
kubectl -n moks-terminals delete pod moks-term-test --now
# curl, because kubectl would also present kind's admin client certificate. The API server remembers a
# good token for about 10 seconds, so this waits that out.
server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
for _ in $(seq 1 15); do
  code="$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" "$server/api/v1/namespaces/moks/pods")"
  [[ "$code" == 401 ]] && break
  sleep 2
done
if [[ "$code" != 401 ]]; then echo "FAIL  the session token outlived its pod (HTTP $code)"; exit 1; fi
echo "ok    the session token died with its pod"
