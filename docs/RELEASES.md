# Releases

An image is built once and tagged with its commit, and CI moves `latest` to it. mk4 always runs `latest` with `pullPolicy: Always`, and that setting does not change between releases.

Git does not record which build is running. Check it with `/api/health`.

## What was released, and with what

Each `master` build uploads a **release record** as a CI artifact, kept 400 days:

- `release-moks-<sha>/moks.json`: `{component, commit, repository, tag, digest, run}`
- `release-moks-reranker-<sha>/moks-reranker.json`: the same, plus `model`, the reranker's `model_id@revision`
  from `reranker/model.lock.json`

Together with what MOKS records at runtime, that links a running build to its model and its evaluation:

| Question | Where |
|---|---|
| Which commit is each pod running? | `/api/health` and the profile menu (`component_versions`), from the commit baked into the image |
| Which digest is that commit? | the release record for that commit, or `aws ecr describe-images --repository-name moks --image-ids imageTag=<sha>` |
| Which reranker model revision? | the reranker release record, `reranking.revision` on each search, and `MOKS_RAG_RERANK_REVISION` |
| Which evaluation backed it? | `eval_runs.versions` (dataset, skills, model, index) for knowledge and rerank evaluations; `ml_models.metrics` and `ml_models.provenance.commit` for ML models |

Images stay on `:latest` with `pullPolicy: Always` (the rule since 2026-11-03), so nothing here needs a values change
to release. The chart still accepts `image.digest` and `reranker.image.digest` if a release must be held still,
for example during a rollback investigation. Set the digest from a release record, never typed by hand, and remove
it again afterwards so CI releases resume.

## Release

1. A pull request passes the `check` job, which runs on pull requests only. Merging it to `master` builds one image, tags it with the commit SHA and moves `latest` to it. A failed image build or smoke test leaves `latest` where it was. A direct push to `master` is released without `check`, so require `check` in branch protection.
2. Check any migration the release brings. Migrations run when the new API pods start, or in the `moks-migrate` Job once `secrets.split` is true. A mutable tag never changes that Job's name, so it needs the steps in [ARGOCD_APP_SPLIT.md](ARGOCD_APP_SPLIT.md#upgrades-and-migrations) before this restart.
3. Restart the pods, which pull `latest`:

   ```bash
   kubectl --context mk4 -n moks rollout restart deploy/moks-api deploy/moks-worker deploy/moks-executor
   ```

4. Check the running build: `curl https://jithendar.com/moks/api/health` shows the commit, and the profile menu warns if components differ.

An Argo CD sync does not start a release. The manifests do not change between builds, so the MOKS Applications stay Synced and no pod restarts. Sync one only when the chart or its values change, by hand and without prune. Each component is its own Application, and a shared setting changes several: see [ARGOCD_APP_SPLIT.md](ARGOCD_APP_SPLIT.md#changing-shared-configuration).

A pod that restarts for any other reason, such as a node restart, also pulls `latest`. It can run a newer build than the others until the rest are restarted, and a sync that changes a pod template rolls that component onto the newest build too.

## Roll back

Point `latest` back at an earlier commit, then restart as in step 3:

```bash
COMMIT=<commit-sha>
MANIFEST="$(aws ecr batch-get-image --region us-east-1 --repository-name moks --image-ids imageTag=$COMMIT --query 'images[0].imageManifest' --output text)"
aws ecr put-image --region us-east-1 --repository-name moks --image-tag latest --image-manifest "$MANIFEST"
```

The next successful `master` build moves `latest` forward again. Revert the bad commit first if it would break again.

To hold a component on an earlier build instead (for instance, while `master` keeps building), pin it from its
release record: `image.digest: sha256:...` (or `reranker.image.digest`) in `values-minikube.yaml`, push, and sync that
one Application. Remove the pin once the fix is released. ML models roll back separately and without a release:
see [ML.md](ML.md#roll-back).

The schema stays where it is, because migrations are forward-only. That is safe when releases follow the rule below.

## Migrations: expand, then contract

Old and new pods run side by side during a rollout, and a rollback runs old code on the new schema.

1. **Expand:** add columns, tables and indexes, nullable or with defaults. Old code keeps working.
2. **Migrate:** new code writes both forms, and backfills in small batches.
3. **Contract:** drop the old form only in a later release, once no running or rollback-target image reads it.

Never rename or drop something in the same release that stops using it. `moks wait-schema` lets a pod start on a schema newer than it knows, and holds a pod back on one that is older.

## Local development

Nothing here needs a registry: `dev/run-local.sh` runs from source and reports commit `dev`. To try a local image on Minikube, build with `--build-arg GIT_COMMIT=$(git rev-parse HEAD)` and install with `--set image.repository=moks,image.tag=$(git rev-parse --short HEAD),image.digest=`. The tag `latest` is refused.
