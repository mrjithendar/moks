#!/usr/bin/env bash
# GitHub Actions self-hosted runners for this repository, as systemd services.
#
#   bash runner/setup.sh            # install and start 2 runners (safe to rerun)
#   bash runner/setup.sh status     # show them
#   bash runner/setup.sh remove     # stop, unregister and delete them
#
# Settings (environment): RUNNER_COUNT (2), RUNNER_REPO (mrjithendar/moks),
# RUNNER_DIR (~/actions-runners/moks), RUNNER_NAME (short hostname), RUNNER_LABELS (extra labels).
#
# GitHub token, first one found: GH_TOKEN / GITHUB_TOKEN, `gh auth token`, or the AWS Secrets Manager secret
# $RUNNER_SECRET_NAME (default mrjithendar-github-pat, region $AWS_REGION or us-east-1; JSON key github_token or
# github_pat, or the bare token). It needs Administration: write on the repository. It is only exchanged for
# short-lived registration tokens and is never written to disk or shown on a command line.
set -euo pipefail
umask 077

REPO=${RUNNER_REPO:-mrjithendar/moks}
COUNT=${RUNNER_COUNT:-2}
BASE=${RUNNER_DIR:-$HOME/actions-runners/moks}
NAME=${RUNNER_NAME:-$(hostname -s)}
LABELS=${RUNNER_LABELS:-}
SECRET=${RUNNER_SECRET_NAME:-mrjithendar-github-pat}
REGION=${AWS_REGION:-us-east-1}
VERSION=2.337.0
ACTION=${1:-install}

die() { echo "error: $*" >&2; exit 1; }
say() { echo "→ $*"; }

[[ $ACTION =~ ^(install|status|remove)$ ]] || die "usage: bash runner/setup.sh [install|status|remove]"
[[ $(uname -s) == Linux && -d /run/systemd/system ]] || die "this needs a Linux host running systemd"
[[ $COUNT =~ ^([1-9]|1[0-9])$ ]] || die "RUNNER_COUNT must be between 1 and 19"
[[ $REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "RUNNER_REPO must look like OWNER/REPOSITORY"
for cmd in curl tar sha256sum python3 systemctl; do command -v "$cmd" >/dev/null || die "missing command: $cmd"; done
if [[ $EUID == 0 ]]; then
  sudo() { "$@"; }
  export RUNNER_ALLOW_RUNASROOT=1 # config.sh refuses root without it; jobs then run as root
else
  command -v sudo >/dev/null || die "sudo is required (or run as root)"
fi

slot_dir() { echo "$BASE/$1"; }
slot_name() { echo "$NAME-$1"; }
service_of() { [[ -f $1/.service ]] && cat "$1/.service"; }
repo_of() { python3 -c 'import json,sys; print(json.loads(open(sys.argv[1], encoding="utf-8-sig").read()).get("gitHubUrl", ""))' "$1/.runner"; }

# --- GitHub tokens ------------------------------------------------------------------------------------

GH_ADMIN_TOKEN=""
github_token() {
  [[ -n $GH_ADMIN_TOKEN ]] && return
  GH_ADMIN_TOKEN=${GH_TOKEN:-${GITHUB_TOKEN:-}}
  if [[ -z $GH_ADMIN_TOKEN ]] && command -v gh >/dev/null; then GH_ADMIN_TOKEN=$(gh auth token --hostname github.com 2>/dev/null || true); fi
  if [[ -z $GH_ADMIN_TOKEN ]]; then
    command -v aws >/dev/null || die "no GitHub token: set GH_TOKEN, run 'gh auth login', or install the AWS CLI to read secret $SECRET"
    GH_ADMIN_TOKEN=$(aws secretsmanager get-secret-value --secret-id "$SECRET" --region "$REGION" --query SecretString --output text 2>/dev/null |
      python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    value = json.loads(raw)
except ValueError:
    value = raw
if isinstance(value, dict):
    value = next((value[k] for k in ("github_token", "github_pat", "password") if value.get(k)), "")
print(value if isinstance(value, str) else "")') || true
    [[ -n $GH_ADMIN_TOKEN ]] || die "could not read a GitHub token from AWS secret $SECRET in $REGION (check AWS credentials and secretsmanager:GetSecretValue)"
  fi
}

# Short-lived token from GitHub: registration-token or remove-token. The admin token goes in via a header file.
runner_token() {
  github_token
  curl -fsS -X POST --max-time 30 \
    -H @<(printf 'Authorization: Bearer %s\n' "$GH_ADMIN_TOKEN") \
    -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/$REPO/actions/runners/$1" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' ||
    die "GitHub refused a $1 for $REPO; the token needs Administration: write on that repository"
}

# --- Install ------------------------------------------------------------------------------------------

download() {
  local arch sum url
  case $(uname -m) in
    x86_64) arch=x64 sum=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613 ;;
    aarch64 | arm64) arch=arm64 sum=9b1dc70626422526e3c94767cf024896beb15da5342a3f4819bf2feac13e0393 ;;
    *) die "unsupported CPU $(uname -m) (x86_64 or arm64 only)" ;;
  esac
  TARBALL="$BASE/.cache/actions-runner-linux-$arch-$VERSION.tar.gz"
  if [[ -f $TARBALL ]] && echo "$sum  $TARBALL" | sha256sum -c --status; then return; fi
  url="https://github.com/actions/runner/releases/download/v$VERSION/actions-runner-linux-$arch-$VERSION.tar.gz"
  say "Downloading GitHub runner $VERSION ($arch)"
  mkdir -p "$BASE/.cache"
  curl -fL --retry 3 --connect-timeout 15 -o "$TARBALL.part" "$url"
  echo "$sum  $TARBALL.part" | sha256sum -c --status || { rm -f "$TARBALL.part"; die "checksum mismatch for $url"; }
  mv "$TARBALL.part" "$TARBALL"
}

# Runner files must be executable under SELinux (CentOS/RHEL enforce it); label only the runner directory.
selinux_label() {
  command -v getenforce >/dev/null && [[ $(getenforce) == Enforcing ]] || return 0
  command -v semanage >/dev/null || sudo dnf install -y policycoreutils-python-utils
  sudo semanage fcontext -a -t bin_t "$1(/.*)?" 2>/dev/null || sudo semanage fcontext -m -t bin_t "$1(/.*)?"
  sudo restorecon -RF "$1"
}

# The .NET libraries the runner needs. On dnf/yum hosts they are installed directly: GitHub's
# installdependencies.sh also requires lttng-ust, which CentOS Stream / RHEL do not ship (the runner does not need it).
install_deps() {
  local pm
  for pm in dnf yum; do
    if command -v "$pm" >/dev/null; then
      sudo "$pm" install -y libicu openssl-libs krb5-libs zlib libstdc++ libgcc >/dev/null
      return
    fi
  done
  sudo "$1/bin/installdependencies.sh" >/dev/null
}

install_slot() {
  local dir name reg
  dir=$(slot_dir "$1")
  name=$(slot_name "$1")
  if [[ -f $dir/.runner ]]; then
    [[ $(repo_of "$dir") == "https://github.com/$REPO" ]] || die "$dir is registered to $(repo_of "$dir"), not $REPO; set RUNNER_DIR"
    say "Runner $name already registered"
  else
    say "Registering runner $name"
    mkdir -p "$dir"
    [[ -x $dir/config.sh ]] || tar --no-same-owner -xzf "$TARBALL" -C "$dir"
    if [[ ! -f $BASE/.deps-installed ]]; then
      say "Installing the runner's OS libraries"
      install_deps "$dir"
      touch "$BASE/.deps-installed"
    fi
    # Fail here, before asking GitHub for a token, if the runner cannot start on this host.
    (cd "$dir" && ./config.sh --version >/dev/null) || die "the runner cannot start on this host (missing libraries?); see the output above"
    reg=$(runner_token registration-token)
    # --replace takes over a runner of the same name left in GitHub by an earlier install.
    (cd "$dir" && ACTIONS_RUNNER_INPUT_TOKEN=$reg ./config.sh --unattended --replace \
      --url "https://github.com/$REPO" --name "$name" --work _work ${LABELS:+--labels "$LABELS"})
  fi
  selinux_label "$dir"
  (cd "$dir" && { [[ -f .service ]] || sudo ./svc.sh install "$(id -un)"; } && sudo ./svc.sh start >/dev/null)
  systemctl is-active --quiet "$(service_of "$dir")" || die "service for $name did not start: journalctl -u $(service_of "$dir")"
}

install() {
  mkdir -p "$BASE"
  exec 9>"$BASE/.lock"
  command -v flock >/dev/null && { flock -n 9 || die "another setup is running"; }
  download
  for ((i = 1; i <= COUNT; i++)); do install_slot "$i"; done

  # Checks for what this repository's jobs need on the host.
  [[ $(uname -m) =~ ^(aarch64|arm64)$ ]] || echo "warning: the release job needs an ARM64 runner (runs-on: [self-hosted, linux, ARM64]); these are $(uname -m)"
  docker info >/dev/null 2>&1 || echo "warning: $(id -un) cannot use Docker; the release job builds images (add the user to the docker group, then: sudo systemctl restart 'actions.runner.*')"
  command -v git >/dev/null || echo "warning: git is not installed; checkouts fall back to the slower REST API"
  echo
  status
  echo "Check they show Idle in https://github.com/$REPO/settings/actions/runners"
}

# --- Status / remove ----------------------------------------------------------------------------------

slots() { [[ -d $BASE ]] && find "$BASE" -mindepth 1 -maxdepth 1 -type d ! -name '.*' | sort -V; }

status() {
  local dir found=0
  while IFS= read -r dir; do
    [[ -n $dir ]] || continue
    found=1
    if [[ -f $dir/.runner ]]; then
      printf '%-28s %-10s %s\n' "$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1], encoding="utf-8-sig").read()).get("agentName", "?"))' "$dir/.runner")" \
        "$(systemctl is-active "$(service_of "$dir")" 2>/dev/null || echo stopped)" "$dir"
    else
      printf '%-28s %-10s %s\n' "(not registered)" "-" "$dir"
    fi
  done < <(slots)
  ((found)) || echo "No runners installed in $BASE"
}

remove() {
  local dir svc
  while IFS= read -r dir; do
    [[ -n $dir ]] || continue
    say "Removing $dir"
    svc=$(service_of "$dir" || true)
    if [[ -n $svc ]]; then (cd "$dir" && { sudo ./svc.sh stop >/dev/null || true; } && sudo ./svc.sh uninstall >/dev/null); fi
    if [[ -f $dir/.runner ]]; then
      local tok
      tok=$(runner_token remove-token)
      (cd "$dir" && ACTIONS_RUNNER_INPUT_TOKEN=$tok ./config.sh remove)
    fi
    rm -rf -- "$dir"
  done < <(slots)
  rm -rf -- "$BASE/.cache" "$BASE/.deps-installed" "$BASE/.lock"
  rmdir -- "$BASE" 2>/dev/null || true
  say "Done"
}

"$ACTION"
