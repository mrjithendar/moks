#!/bin/bash
# Checks this session's isolation from the inside, and exits non-zero if anything is open that should
# be closed. Used by CI on a kind cluster, and by an engineer after a deployment. Nothing it runs can
# change anything: AWS writes use --dry-run, and Kubernetes writes are `auth can-i` questions.
#   MOKS_VERIFY_BLOCKED="host:port host:port"  more destinations that must be unreachable
#   MOKS_VERIFY_ALLOWED="host:port"            destinations that must be reachable
set -u
fail=0
pass() { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fail=1; }
skip() { printf 'skip  %s\n' "$1"; }
unreachable() { # host port label
  if nc -z -w 3 "$1" "$2" 2>/dev/null; then bad "$3 is reachable ($1:$2)"; else pass "$3 is blocked ($1:$2)"; fi
}

[ "$(id -u)" != 0 ] && pass "not root (uid $(id -u))" || bad "running as root"
caps=$(awk '/^CapEff/ {print $2}' /proc/self/status)
[ "$caps" = 0000000000000000 ] && pass "no Linux capabilities" || bad "capabilities held: $caps"
[ "$(awk '/^NoNewPrivs/ {print $2}' /proc/self/status)" = 1 ] && pass "no privilege escalation" || bad "privilege escalation is possible"
touch /usr/local/moks-write-test 2>/dev/null && bad "root filesystem is writable" || pass "root filesystem is read-only"
[ ! -e /var/run/secrets/kubernetes.io/serviceaccount/token ] && pass "no mounted service account token" || bad "a service account token is mounted"
[ ! -S /var/run/docker.sock ] && [ ! -S /run/containerd/containerd.sock ] && pass "no container runtime socket" || bad "a container runtime socket is mounted"
[ -z "$(grep -E ' /(host|proc/1/root)' /proc/mounts)" ] && pass "no host mounts" || bad "a host path is mounted"
[ "$(stat -f -c %T /run/moks 2>/dev/null)" = tmpfs ] && pass "credentials are in memory" || bad "/run/moks is not memory-backed"

unreachable 169.254.169.254 80 "instance metadata"
unreachable 169.254.170.2 80 "ECS credentials endpoint"
unreachable 169.254.170.23 80 "EKS Pod Identity agent"
for target in ${MOKS_VERIFY_BLOCKED:-}; do unreachable "${target%:*}" "${target##*:}" "$target"; done
for target in ${MOKS_VERIFY_ALLOWED:-}; do
  nc -z -w 5 "${target%:*}" "${target##*:}" 2>/dev/null && pass "$target is reachable" || bad "$target should be reachable"
done

if [ -s "${AWS_SHARED_CREDENTIALS_FILE:-/nonexistent}" ]; then
  aws sts get-caller-identity --output text --query Arn 2>/dev/null | grep -q assumed-role && pass "AWS identity is the session role" || bad "AWS identity could not be verified"
  out=$(aws ec2 create-vpc --cidr-block 10.99.0.0/16 --dry-run 2>&1)
  case "$out" in
    *UnauthorizedOperation*) pass "IAM refuses a write" ;;
    *DryRunOperation*) bad "this role may write: ec2:CreateVpc would have succeeded" ;;
    *) skip "IAM write check was inconclusive: ${out:0:120}" ;;
  esac
else
  skip "no AWS credentials in this session"
fi

if [ -s "${KUBECONFIG:-/nonexistent}" ]; then
  for question in "create pods" "delete deployments" "get secrets" "create pods/exec"; do
    # shellcheck disable=SC2086
    kubectl auth can-i $question --all-namespaces >/dev/null 2>&1 && bad "Kubernetes allows: $question" || pass "Kubernetes refuses: $question"
  done
else
  skip "no Kubernetes credentials in this session"
fi
exit $fail
