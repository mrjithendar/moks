# Releases

An image is built once, tagged with its commit, and deployed by digest. Git holds the digest, so what runs is reviewable and a rollback is a revert.

## Release

1. A merge to `master` passes CI, and CI builds one image tagged with the commit SHA. ECR tags are immutable.
2. CI opens a pull request named `Release <sha>` that changes `image.digest` in `values-minikube.yaml`.
3. Review it, including any migration it brings, and merge it.
4. Sync the `moks-portal` app in Argo CD by hand, without prune. Pods roll only because the digest changed.
5. Check the running build: `curl https://jithendar.com/moks/api/health` shows the commit, and the profile menu warns if components differ.

No `kubectl rollout restart` is involved, and the chart refuses `image.tag: latest` and any `image.digest` that is not a full `sha256:` digest.

Merging a release does not start another build: pushes that change only `values-minikube.yaml` skip CI, so a change to that file alone needs no new image. If Actions may not create pull requests, the build still succeeds and its summary says to open the pull request by hand from `release/<sha>`.

## Roll back

Revert the release commit, merge, and sync. That restores the previous digest, which ECR still holds (the newest `image_retention_count` images are kept).

The schema stays where it is, because migrations are forward-only. That is safe when releases follow the rule below.

## Migrations: expand, then contract

Old and new pods run side by side during a rollout, and a rollback runs old code on the new schema.

1. **Expand:** add columns, tables and indexes, nullable or with defaults. Old code keeps working.
2. **Migrate:** new code writes both forms, and backfills in small batches.
3. **Contract:** drop the old form only in a later release, once no running or rollback-target image reads it.

Never rename or drop something in the same release that stops using it. `moks wait-schema` lets a pod start on a schema newer than it knows, and holds a pod back on one that is older.

## Local development

Nothing here needs a registry: `dev/run-local.sh` runs from source and reports commit `dev`. To try a local image on Minikube, build with `--build-arg GIT_COMMIT=$(git rev-parse HEAD)` and install with `--set image.repository=moks,image.tag=$(git rev-parse --short HEAD),image.digest=`. The tag `latest` is refused.

## First rollout of this workflow

`values-minikube.yaml` pins the digest mk4 was running when this was written, so syncing it changes no code. Apply the Terraform change for immutable tags before the next release: the old `:latest` tag cannot be pushed again afterwards, and CI no longer does.
