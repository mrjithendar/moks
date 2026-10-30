#!/usr/bin/env bash
# Logical backup of the bundled MOKS database to a local, timestamped, compressed file.
#   scripts/backup.sh [kube-context] [output-dir]
# Restore: see docs/OPERATOR_GUIDE.md "Backup and restore".
set -euo pipefail
CONTEXT="${1:-mk4}"; OUT="${2:-.}"
FILE="$OUT/moks-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
kubectl --context "$CONTEXT" -n moks exec statefulset/moks-postgres -- \
  pg_dump -U moks -d moks --no-owner --format=plain | gzip -9 > "$FILE"
echo "wrote $FILE ($(du -h "$FILE" | cut -f1))"
