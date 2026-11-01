#!/usr/bin/env bash
# Clear an Application's tracking annotation from the resources it used to own, so another Application
# can adopt them under FailOnSharedResource=true (docs/ARGOCD_APP_SPLIT.md, section 4). Changes metadata
# only: no pod restarts. Touches nothing tracked by a different Application, and prints what it would
# do unless --apply is given. Assumes Argo CD's annotation tracking, the default on mk4.
#
#   scripts/argocd-clear-tracking.sh <kube-context> <application> <resources.json> [--apply]
#
# resources.json is a list of {group, kind, namespace, name}, as in an Application's .status.resources.
set -euo pipefail
CONTEXT="${1:?kube context}"
APPLICATION="${2:?application name}"
RESOURCES="${3:?resources.json}"
APPLY="${4:-}"
[[ -z "$APPLY" || "$APPLY" == --apply ]] || { echo "unknown option: $APPLY" >&2; exit 2; }
ANNOTATION=argocd.argoproj.io/tracking-id

cleared=0 skipped=0 foreign=0
while IFS='|' read -r kind namespace name; do
  args=(--context "$CONTEXT")
  [[ -n "$namespace" ]] && args+=(-n "$namespace")
  tracking="$(kubectl "${args[@]}" get "$kind/$name" --ignore-not-found -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}')"
  if [[ -z "$tracking" ]]; then
    skipped=$((skipped + 1))
  elif [[ "$tracking" != "$APPLICATION:"* ]]; then
    echo "LEFT ALONE  $kind/$name ${namespace:+in $namespace}: tracked as $tracking" >&2
    foreign=$((foreign + 1))
  elif [[ "$APPLY" == --apply ]]; then
    kubectl "${args[@]}" annotate "$kind/$name" "$ANNOTATION-" >/dev/null
    echo "cleared     $kind/$name ${namespace:+in $namespace}"
    cleared=$((cleared + 1))
  else
    echo "would clear $kind/$name ${namespace:+in $namespace}"
    cleared=$((cleared + 1))
  fi
done < <(jq -r '.[] | [(if (.group // "") == "" then .kind else "\(.kind).\(.group)" end), (.namespace // ""), .name] | join("|")' "$RESOURCES")

echo "$APPLICATION on $CONTEXT: ${APPLY:-dry run}: $cleared to clear or cleared, $skipped already untracked, $foreign left alone"
[[ "$foreign" -eq 0 ]]
