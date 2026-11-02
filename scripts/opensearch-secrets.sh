#!/usr/bin/env bash
# Create the Secrets for the secured moks-search cluster: a private CA, node and admin certificates, and a
# generated password per identity. Values leave this machine only in the Secrets, and are printed nowhere.
#
#   scripts/opensearch-secrets.sh [kube-context]        create them; refuses if any exists
#   scripts/opensearch-secrets.sh --files DIR           write them to DIR instead (tests)
#
# The human admin password is the one value you need later:
#   kubectl -n moks get secret moks-search-admin-login -o jsonpath='{.data.password}' | base64 -d
#
# Certificates last CERT_DAYS (default 1095). docs/OPENSEARCH.md has rotation.
set -euo pipefail
CERT_DAYS="${CERT_DAYS:-1095}"
NS=moks
FILES=""
CONTEXT=mk4
if [[ "${1:-}" == --files ]]; then FILES="${2:?usage: $0 --files DIR}"; else CONTEXT="${1:-mk4}"; fi
k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
command -v openssl > /dev/null && command -v htpasswd > /dev/null || { echo "needs openssl and htpasswd" >&2; exit 1; }

SECRETS=(moks-search-tls moks-search-admin-tls moks-search-users moks-search-clients moks-search-admin-login)
if [[ -z "$FILES" ]]; then
  for s in "${SECRETS[@]}"; do
    if k get secret "$s" > /dev/null 2>&1; then echo "Secret $NS/$s exists; leaving everything unchanged." >&2; exit 1; fi
  done
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
umask 077

openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout ca.key -out ca.crt -days "$CERT_DAYS" \
  -subj "/O=moks/CN=moks-search-ca" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2> /dev/null

issue() {  # name subject extensions. PKCS#8, the only key format OpenSearch reads.
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$1.key" 2> /dev/null
  openssl req -new -key "$1.key" -subj "$2" -out "$1.csr" 2> /dev/null
  printf '%b' "$3" > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days "$CERT_DAYS" -sha256 -extfile "$1.ext" -out "$1.crt" 2> /dev/null
}
issue node "/O=moks/CN=moks-search" \
  "subjectAltName=DNS:moks-search,DNS:moks-search.moks,DNS:moks-search.moks.svc,DNS:moks-search.moks.svc.cluster.local,DNS:moks-search-0.moks-search.moks.svc.cluster.local,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\n"
issue admin "/O=moks/CN=moks-search-admin" "extendedKeyUsage=clientAuth\n"

password() { openssl rand -hex 32; }
bcrypt() { htpasswd -nbBC 12 "" "$1" | tr -d ':\n'; }
SEARCH="$(password)"; INDEX="$(password)"; LIFECYCLE="$(password)"; MONITOR="$(password)"; ADMIN="$(password)"
cat > internal_users.yml <<YAML
_meta: {type: internalusers, config_version: 2}
moks_search: {hash: "$(bcrypt "$SEARCH")", description: API pods}
moks_indexer: {hash: "$(bcrypt "$INDEX")", description: Worker}
moks_lifecycle: {hash: "$(bcrypt "$LIFECYCLE")", description: Index generations and snapshots}
moks_monitor: {hash: "$(bcrypt "$MONITOR")", description: Prometheus}
moks_admin: {hash: "$(bcrypt "$ADMIN")", description: People}
YAML

if [[ -n "$FILES" ]]; then
  mkdir -p "$FILES"
  cp ca.crt node.crt node.key admin.crt admin.key internal_users.yml "$FILES/"
  printf '%s\n' "search=$SEARCH" "index=$INDEX" "lifecycle=$LIFECYCLE" "monitor=$MONITOR" "admin=$ADMIN" > "$FILES/passwords"
  exit 0
fi

# The CA key is deliberately kept nowhere: renewing means issuing a new CA, which docs/OPENSEARCH.md covers.
k create secret generic moks-search-tls --from-file=ca.crt=ca.crt --from-file=tls.crt=node.crt --from-file=tls.key=node.key
k create secret generic moks-search-admin-tls --from-file=ca.crt=ca.crt --from-file=tls.crt=admin.crt --from-file=tls.key=admin.key
k create secret generic moks-search-users --from-file=internal_users.yml=internal_users.yml
k create secret generic moks-search-clients \
  --from-literal=search-password="$SEARCH" --from-literal=index-password="$INDEX" \
  --from-literal=lifecycle-password="$LIFECYCLE" --from-literal=monitor-password="$MONITOR"
k create secret generic moks-search-admin-login --from-literal=username=moks_admin --from-literal=password="$ADMIN"
echo "Created ${SECRETS[*]} in $NS. Next: docs/OPENSEARCH.md, step 3."
