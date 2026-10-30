# Self-hosted GitHub Actions runners (not used)

> **MOKS CI does not use self-hosted runners.** `.github/workflows/ci.yml` runs on GitHub-hosted
> `ubuntu-24.04` (check) and `ubuntu-24.04-arm` (release), which are free for this public repository
> and start clean for every job. The release job signs in to AWS with the Terraform-managed OIDC role
> `moks-github-ci`; there are no stored keys.
>
> Do not point this public repository's workflows at a self-hosted runner: pull requests from forks
> would run untrusted code on that machine. Never install these runners on the mk4 cluster host
> (builds there froze it twice for jithendar.com). Remove any you installed with
> `bash runner/setup.sh remove`.

The rest of this file describes the script as copied from jithendar.com, with MOKS defaults
(`RUNNER_REPO=mrjithendar/moks`, `RUNNER_DIR=~/actions-runners/moks`).

`runner/setup.sh` installs runners for `mrjithendar/moks` as systemd services. One run sets up
**two runners**, so a pull-request check and a release build can run at the same time.

```bash
bash runner/setup.sh            # install and start 2 runners (safe to rerun: existing ones are kept)
bash runner/setup.sh status     # list them and whether their services are active
bash runner/setup.sh remove     # stop, unregister from GitHub and delete them
```

Run it on the CI host as the user the jobs should run as (with `sudo` rights), or as root (jobs then run as root).

## What it does

1. Downloads the official runner (v2.337.0) once and checks its SHA-256.
2. Installs the runner's OS libraries: on dnf/yum hosts `libicu openssl-libs krb5-libs zlib libstdc++ libgcc`
   (GitHub's `installdependencies.sh` also wants `lttng-ust`, which CentOS Stream does not ship); elsewhere GitHub's
   `bin/installdependencies.sh`. Then checks the runner starts before registering it.
3. For each runner: unpacks it into `~/actions-runners/moks/<n>`, registers it as `<hostname>-<n>` with a
   short-lived registration token, labels it for SELinux if enforcing, and installs and starts its systemd service.
4. Warns when the host cannot run this repository's jobs (see Requirements).

GitHub adds the labels `self-hosted`, `linux` and the CPU (`ARM64` or `X64`) automatically.

## GitHub token

Registration needs a token with **Administration: write** on the repository (fine-grained PAT), or a classic PAT
with `repo`. The first one found is used:

1. `GH_TOKEN` or `GITHUB_TOKEN` in the environment;
2. the GitHub CLI login (`gh auth login`);
3. the AWS Secrets Manager secret `mrjithendar-github-pat` in `us-east-1` (JSON key `github_token` or
   `github_pat`, or the bare token), read with the host's AWS credentials.

The token is only exchanged for registration/removal tokens (valid one hour). It is never written to disk, passed on a
command line or printed.

## Settings

| Variable | Default |
|---|---|
| `RUNNER_COUNT` | `2` (1–19). Lowering it later does not remove runners; use `remove` and reinstall |
| `RUNNER_REPO` | `mrjithendar/moks` |
| `RUNNER_DIR` | `~/actions-runners/moks` |
| `RUNNER_NAME` | short host name (runners are `<name>-1`, `<name>-2`) |
| `RUNNER_LABELS` | none (comma-separated extra labels) |
| `RUNNER_SECRET_NAME`, `AWS_REGION` | `mrjithendar-github-pat`, `us-east-1` |

## Requirements for this repository's jobs

- Linux with systemd; `curl`, `tar`, `python3`, `sha256sum`, and `git` for fast checkouts.
- **ARM64** for the release job (`runs-on: [self-hosted, linux, ARM64]`); the pull-request check runs on any runner.
- **Docker** usable by the runner user (the release job builds and pushes the image): add the user to the `docker`
  group, then restart the services: `sudo systemctl restart 'actions.runner.*'`.
- **AWS credentials** on the host (instance role or the runner user's profile) allowed to push to ECR repository
  `moks` in `us-east-1` (or the OIDC role `moks-github-ci`); the workflow logs in with `aws-actions/amazon-ecr-login`.

## Troubleshooting

- Runner shows **Offline** in *Settings → Actions → Runners*: `bash runner/setup.sh status`, then
  `journalctl -u actions.runner.<…>.service -e`.
- `GitHub refused a registration-token`: the token lacks Administration: write on this repository.
- Service fails with `Permission denied` on CentOS/RHEL: SELinux labels; rerun `bash runner/setup.sh`.
- A runner was deleted in GitHub but not on the host: `bash runner/setup.sh remove`, then install again.
