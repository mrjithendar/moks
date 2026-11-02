# Rolling out the knowledge and skill governance work

Schema head 0031. Nothing here has been deployed. All of it writes to mk4 and needs a push, so it is yours to run, and [RELEASES.md](RELEASES.md) covers how an image reaches the cluster.

## What changes for people

| Who | Change |
| --- | --- |
| Admins | Nothing lost: the admin role holds every new permission |
| Viewers and others | New Knowledge Explorer. `/knowledge/status` now shows only what they can read, and search responses leave out the index name and candidate counts |
| Holders of draft or quarantined text | Direct links now need `knowledge.review`. They used to work for any reader of that scope |
| Anyone publishing a custom skill | A version must now be reviewed before it can be published |

New roles: `knowledge_reviewer`, `knowledge_admin`, `index_operator`, `skill_publisher`. Grant the last three with environment and namespace `*`.

## Configuration

No new setting is required, and every default keeps today's behavior. With nothing else changed, the mk4 and EKS Helm renders are identical to before this work.

| Setting | Default | Note |
| --- | --- | --- |
| `rag.opensearch.secure.dashboards.enabled` | `false` | [OPENSEARCH.md](OPENSEARCH.md#opensearch-dashboards-for-administrators) |
| `rag.opensearch.secure.dashboards.ingress.enabled` | `false` | Refused while `publicRoute` holds the same path |

## Migration 0031

Additive, with no long lock on a table this size. It widens the role check, adds nullable and defaulted columns and one table, and repairs documents with no chunks that were recorded as missing vectors. Verified from 0030 with such a document in place, on PostgreSQL 17.

`retrieval_feedback` is a new table, so per-component database roles need `moks db-grants` to reach it. The migrate step runs it when `secrets.split` is on.

## Cost to plan for

**`PIPELINE_VERSION` went from 2 to 3,** because line numbers, redaction and identifiers changed. The next sync reads every GitHub and web document again, and each one is embedded again. mk4 held 1,942 chunks when last measured, at 3072 dimensions, so that is a full embedding run paid for at the provider. It takes several sync passes. Search keeps working from the existing index meanwhile. Uploads and postmortems stay on their old pipeline, and the status page counts them.

**A push is enough to start this.** mk4 runs `latest` with `pullPolicy: Always`, so once CI moves `latest`, any pod restart picks up this build, whatever you planned. Before pushing, either accept the cost or set each source's schedule to Off, and turn them back on when you are ready.

## Rollout

This follows [RELEASES.md](RELEASES.md). An Argo CD sync does not start a release.

1. Check embedding budget and the OpenSearch disk, which was at 96% when last measured. Rewriting every document needs room, and the flood-stage block stops writes.
2. Push, and wait for CI to move `latest`.
3. `kubectl --context mk4 -n moks rollout restart deploy/moks-api deploy/moks-worker deploy/moks-executor`. Migration 0031 runs as the new API pods start. With `secrets.split` on, it runs in the `moks-migrate` Job, followed by `moks db-grants`. With it off, nothing runs `db-grants` and nothing needs it.
4. Confirm: `curl https://jithendar.com/moks/api/health` shows the commit. Open **Knowledge Explorer**. As an admin, check Index health, and that outbox and vector coverage move during the next sync.
5. Check the Skills page says every process is current. Built-in skill files did not change, so `moks skills release` has nothing to publish for this work. Publish nothing using the new contract fields until every pod runs this build: an older process cannot parse them, keeps its last valid generation and reports it.
6. Grant the new roles to whoever should have them.
7. Run the `rag` and `skills` evaluations from **Evaluation**. Both are safe: they use the evaluation realm and clean up after themselves.

## Rollback

**To the previous image.** Point `latest` back at the earlier commit and restart, as [RELEASES.md](RELEASES.md#roll-back) describes. Leave the schema: 0031 is forward-only, and the old build ignores what it added.

- People holding only a new role lose access, because the old build does not know the role.
- Documents already on pipeline 3 are read again at the next sync, because the old build also treats a different version as out of date. That is a second embedding run, so set schedules to Off first if that matters.
- Feedback and proposal drafts stay in PostgreSQL, unused.

**A skill.** **Roll back** on its Versions list makes a new generation with the earlier version.

**An index generation.** `scripts/opensearch-migrate.sh activate <name>`, within the retention window. It now reports `degraded` if that generation missed anything, and `sync` fills it in.

**Dashboards.** Set `enabled: false`, push, sync, and delete the Deployment, Service, ConfigMap and NetworkPolicy by hand, because syncs here never prune.
