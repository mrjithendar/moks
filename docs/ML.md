# Machine learning in MOKS

MOKS uses ML to suggest things: similar past incidents, unusual metric behavior and skill recommendations. ML never
grants a permission, opens an incident, changes a severity or runs an action, and deterministic detection runs
exactly as before. Every capability can be switched off. When it is off, or when its model is missing or invalid,
MOKS does what it did without it.

Code: `backend/moks/ml/`. API: `/moks/api/ml/...`. Schema: migration 0032. UI: **ML insights** (Monitor) and
**Machine learning** (Admin).

## Status

| Capability | Built | Checked here (PostgreSQL, fake connectors) | Shadow-tested on a cluster | Validated in production |
|---|---|---|---|---|
| Passage-level citations | yes | yes | no | no |
| Relevance: explicit relationships before the namespace | yes | yes | no | no |
| Readiness counts that match search | yes | yes | no | no |
| Learning records, reviews, judgments, datasets | yes | yes | no | no |
| Similar incidents | yes, **enabled** by default | yes (synthetic incidents) | no | no |
| Anomaly detection | yes, **disabled** by default, shadow only | yes (synthetic Prometheus series) | no | no |
| Semantic skill recommendations | yes, **disabled** by default | yes | no | no |
| Supervised skill ranking | **not built**: waits for reviewed outcomes | n/a | n/a | n/a |

Nothing here has run against mk4, real Prometheus, a real embedding model or real incidents. The only evaluation
numbers are from synthetic fixtures written for the tests (see [Evaluation](#evaluation)). They show the code does
what it was designed to do, and say nothing about real-world quality.

## Modes and the kill switch

Each capability has a mode in the `ml_capabilities` table. It is read on every use, so a change takes effect on the
next investigation or tick, with no restart.

| Capability | Modes | Default | Shadow | Enabled |
|---|---|---|---|---|
| `similar_incidents` | disabled, shadow, enabled | enabled | computed and stored, not shown | shown in investigations |
| `anomaly_detection` | disabled, shadow | disabled | observations shown on ML insights and judged | not offered |
| `skill_recommendations` | disabled, shadow, enabled | disabled | ranking recorded beside the rules' choice | may fill skill places the rules left empty; never removes or reorders |

- **Switch:** Admin > Machine learning, or `PUT /ml/capabilities/{name}` with `{mode, reason}`. Needs `ml.admin`. The
  change is audited (`ml.capability`).
- **Kill everything:** set `ml.disabled: true` in Helm values (`MOKS_ML_DISABLED`). Every capability then reads as
  disabled, whatever the database says. Use this when the database itself is suspect.
- **Fail closed:** an unknown capability, or a mode that cannot be read, counts as disabled.

## Roles

| Permission | In | Allows |
|---|---|---|
| `ml.read` | viewer and every role above it | observations and similar incidents within the reader's bindings |
| `ml.feedback` | investigator, operator, `ml_reviewer`, `ml_admin` | judge a passage, similar incident, recommendation or observation |
| `learning.review` | `ml_reviewer`, `ml_admin` (scoped) | confirm causes and resolutions; mark judgments reviewed |
| `learning.export` | `ml_admin` (global) | build and export datasets |
| `ml.admin` | `ml_admin` (global) | modes, training, promotion, rollback, rejection |

`admin` holds all of these. Scoped reads are filtered row by row, so a reader never sees another scope's rows or
counts. None of these permissions reach infrastructure.

## Learning data

A **learning record** is written when an investigation finishes, in its own transaction after the answer is stored,
so it can never fail the investigation. Another is written when a postmortem is approved. A record holds:

- identifiers and kinds, never evidence text;
- scope: environment, namespace, service and resource;
- the time window and recent rollouts;
- passage ids by state;
- skills that were eligible, recommended and executed, with versions and step outcomes;
- model, feature-schema and pipeline versions.

Error messages are kept as normalized signatures: identifiers, numbers, addresses and paths become placeholders, and
the text is redacted.

The rules:

- *Completed* is not *resolved*. `outcome.resolution` stays `unknown` until a reviewer confirms it or the
  postmortem is approved.
- An executed skill is not a successful one. `skills.executed[].outcome` says only whether its steps ran
  (`steps_ran`, `steps_failed`, `not_run`, `stopped`). Whether it helped is a judgment someone records.
- Labels come from a reviewer (Admin > Machine learning > Confirm outcomes) or an approved postmortem. Nothing is
  labeled automatically.
- Judgments are one per person per target and context. A changed verdict goes back to `open`, and nothing uses a
  judgment until a reviewer marks it `reviewed`.
- Evaluation runs (`trigger=eval`) are written in the `evaluation` realm and never mix with operational records.
- Retention: `ml.learningRetentionDays` (365). The daily `learning_purge` job, or `moks ml purge`, deletes expired
  records and datasets. Approved incident records have no expiry.

A **dataset** (`POST /ml/learning/datasets`, `learning.export`) is a frozen, versioned selection:

- It includes approved records only.
- Duplicates by content hash are dropped first.
- Every record of one incident, deployment or cited document family lands on one side of the split. `chronological`
  puts the latest groups in evaluation.
- MOKS refuses to save a dataset if any group or content hash would appear in both train and evaluation.
- Export is JSONL with features and labels only, redacted again, and is audited.
- **Contents are frozen at build time.** Each item keeps the redacted record exactly as exported, with its SHA-256.
  The checksum (`checksum_version` 2) covers the export schema and fields, the purpose, realm, filters and split
  settings, and every item's record id, split, group and payload hash. Export reads the snapshot, never today's
  records, and refuses (409) if the snapshot no longer matches the checksum. One `name@version` and checksum
  therefore always mean the same rows. The response carries `X-MOKS-Dataset-Checksum` and `X-MOKS-Dataset-Provenance`.
- **When sources change.** A label edited while the record stays approved does not touch existing versions; build
  a new version to take it in. A label withdrawn (approved to rejected or unreviewed) or a record deleted (retention
  or its investigation deleted) **invalidates** every dataset holding it: a database trigger (migration 0035) sets
  `state = invalidated` with the reason and clears that dataset's snapshot, and export answers 410. Item rows stay
  as references (there is no longer a foreign key that would silently drop them). A dataset's own `expires_at`
  deletes it with its snapshot.
- **Legacy datasets** (built before 0035, `checksum_version` 1) have no snapshot. They are listed as
  `legacy_live_records`, not reproducible: their export reads today's records, says `"provenance": "live"` on each
  row, and their checksum covers record ids and splits only. Rebuild them to get a frozen version.

Nothing in MOKS trains on conversations or on feedback automatically.

## Similar incidents

Candidates are approved incidents (postmortem approved) in the reader's realm. Access is checked first, per
incident: the reader must hold knowledge access and `ml.read` in the incident's environment and namespace, and the
incident must be inside the request's scope. The comparison has two parts:

- **Structure:** symptoms (finding kinds), error-signature tokens, service, resource kind and whether a rollout
  preceded both, compared as sets. The weights are in `ml/similar.py`.
- **Text:** the existing knowledge search, limited to the `incident-postmortems` source. It uses embeddings when the
  active index has them, and keyword search otherwise.

If the text search fails, structure alone still ranks, and the result says so. With keyword-only text it says
"embeddings unavailable".

In an investigation this is one bounded call after triage, through the executor (tool `similar_incidents`, evidence
kind `documentation`), with its own one-call allowance. Each result shows:

- what matched, what differs, and how long ago it happened;
- whether the cause was verified, the cause, and the resolution;
- a link to the incident.

The section always says that historical similarity does not show the current cause. Claim validation treats this
evidence like documentation, so it never stands as an observation, and a cause backed only by it stays low
confidence.

Existing approved postmortems: run `moks ml index-incidents` once in the worker pod, so incidents approved before
this release get records.

## Anomaly detection (shadow)

**Features** are computed per workload, with pods folded into their workload in PromQL so rollouts do not create new
cohorts. They are sampled on one aligned grid (`ml.anomaly.stepSeconds`, 300):

- request rate;
- error ratio, which is missing below 0.05 req/s rather than 0;
- p95 latency;
- CPU throttling;
- memory use as a share of the limit, which is missing when no limit is set;
- restarts per hour, from the raw counter, with resets handled.

A feature with no series is `absent`, and absent is never treated as healthy. Reads go through the automation
principal's executor (`prom_query`), in chunks under the connector's 25-series and 240-point limits; a query that
hits the series cap is reported as incomplete.

The grid comes from one function, `features.align_window`, which training, validation and scoring all use. Both
ends of a window are floored to a multiple of the step (UTC epoch seconds) and the window is half-open,
`[start, end)`. Chunk boundaries fall on the grid, and each request ends one second before the next chunk starts,
because Prometheus includes `end`. That way each timestamp is requested exactly once. A window shorter than one step
is `insufficient_window`; it is never scored as 0% coverage.

Each chunk of each query is recorded as `success`, `failed` (an error, a timeout, a Prometheus status other than
success) or `partial` (warnings, a truncated result, the series cap), and the counts are in `data_quality.chunks`.
An error ratio inside a failed or partial chunk is unknown, not 0. Zero errors are inferred only from a complete
chunk with traffic and no 5xx series, because `errors_rate` is declared `absent_means_zero` in
`features.METRIC_DEFINITIONS`. Coverage counts known values only. Request metrics default to `http_requests_total` and
`http_request_duration_seconds_bucket`; restarts use kube-state-metrics, which mk4 does not run, so expect that
feature to be absent there.

**Training** is offline: the CronJob, `moks ml train`, or Admin > Train. It covers `ml.anomaly.trainingDays` (14) of
history for each workload in the given namespaces.

1. Cohorts with less than `minHistoryDays` (7) of usable history are refused as `insufficient_history`. Usable means
   instants with at least two features.
2. The model is fitted on the earlier 80% and evaluated on the rest. Preprocessing (medians and IQR scaling) is
   fitted on the training part only.
3. Two algorithms are trained:
   - the seasonal baseline: median and MAD per hour of week, falling back to hour of day;
   - an Isolation Forest, implemented in `ml/iforest.py` so its artifact is JSON.
4. Both are registered as `candidate` with gates (history, validation windows, flags per day, feature coverage) and
   a like-for-like comparison with the current champion on the same validation windows.
5. Incident recall and detection delay are reported only when approved incidents fall in the validation period;
   otherwise MOKS says recall cannot be measured.

**Inference** is the `ml_anomaly_tick` job. It is scheduled per environment with an active model, at most every
`intervalMinutes` (15), and is bounded by `maxCohorts` and `concurrency`. It scores the champion and any shadow
challengers over the last `windowMinutes` (60). It writes `ml_observations` and nothing else. Besides `scored`, an
observation can be:

- `insufficient_history`;
- `no_telemetry`;
- `maintenance` (an active maintenance window for the namespace with rule `*`, `ml` or `anomaly_detection`: scored
  for the record, never flagged);
- `model_invalid` (artifact checks failed).

Each scored observation lists the features that moved, observed against typical, and their direction. It does not
say why, and the score is not a probability.

## Models and artifacts

- **Artifacts** are canonical JSON, stored by SHA-256: in PostgreSQL by default, or in S3/R2 with
  `ml.artifactStore: s3`, using the backup client. Only MOKS's own training writes them, and there is no upload path.
- **Before every use** MOKS checks the checksum, format, library major version, algorithm, feature schema, feature
  list, sampling step and structure. Any failure falls back to deterministic behavior. A pickle is refused because it
  is not JSON.
- **Provenance** on each model: app commit, job, training window and cutoff, data quality, `dataset_version`
  (`prometheus:<cohort>@<from>..<cutoff>`) and library versions.
- **Statuses:**
  - `candidate`
  - `shadow`: scored as a challenger
  - `champion`: one per workload, enforced by a unique index
  - `retired`
  - `rejected`
- **Promotion** is explicit (Admin > Machine learning > Models, `POST /ml/models/{id}/promote`) and audited. A
  champion needs passed gates and the capability not disabled. Promoting a new champion retires the old one.
- **Rollback:** `POST /ml/rollback {capability, cohort}`. The champion steps down and the most recently promoted
  retired model returns. If there is none, the cohort has no champion and shows nothing.

## Skill recommendations

Order is fixed:

1. Publication, enablement, environment policy, connectors, telemetry and permissions decide which skills are
   eligible (`skills/selection.py`).
2. The deterministic ranker picks its skills.
3. Only then are the eligible skills ranked by similarity to the symptoms. This uses embeddings when the knowledge
   index has an embedding model, and TF-IDF otherwise.

An operator's chosen skill stays first. In `enabled` mode only skills above similarity 0.18 that the rules did not
pick can fill the remaining places, up to 3 in total. Any error falls back to the rules' choice
(`moks_ml_fallbacks_total{capability="skill_recommendations"}`). The ranking, method and additions are recorded in
the report and the learning record, so they can be judged and evaluated later.

A supervised ranker is deliberately not built: there are no reviewed outcomes yet, and training on executed skills
would teach "it ran" as "it worked". Build it from a `skill_ranking` dataset once enough approved records and
reviewed `skill_recommendation` judgments exist.

## Evaluation

| What | How | Where |
|---|---|---|
| Retrieval Recall@k, MRR, nDCG | `ml/evaluation.py` `ranking()`; the existing `rag` and `rerank` datasets | `moks eval rag`, `moks eval rerank` |
| Citation correctness and coverage | `citations()` over report `knowledge` sections | offline |
| Similar incidents | `moks eval similar_incidents` (synthetic, with hard negatives); `moks ml evaluate-similar` (reviewed judgments) | eval runs, CLI |
| Skill recommendation usefulness | reviewed `skill_recommendation` judgments | `skill_ranking` datasets |
| Anomaly flags per cohort per day, judged false positives, incident recall, detection delay | `flags_per_day`, `false_positives_per_day`, `detection` | training metrics; judged observations |
| Inference latency, fallbacks, missing features, drift | `moks_ml_inference_seconds`, `moks_ml_fallbacks_total`, `moks_ml_missing_features_total`, `moks_ml_feature_drift_psi` | Prometheus |

Telemetry evaluation is chronological. Datasets are grouped so one incident, deployment or document family never
spans both sides.

The synthetic `similar_incidents` dataset (5 cases, 3 with hard negatives, written for this work) scored Recall@3,
MRR and nDCG@3 of 1.0, with no hard negative above the true match. That shows the scoring separates the cases it was
designed to separate. It is not a quality measure.

## Setup on mk4

This is yours to run. It writes to mk4, and Argo CD reads `origin/master`.

1. **Ship the code.** Push, wait for the image, then restart:
   `kubectl --context mk4 -n moks rollout restart deploy/moks-api deploy/moks-worker`. Migration 0032 runs on start.
   The config map gains `MOKS_ML_*` keys, so the next Argo CD sync of the API and worker also restarts them.
2. **Backfill incidents:** `kubectl --context mk4 -n moks exec deploy/moks-worker -- moks ml index-incidents`.
3. **Grant roles** if someone other than an admin will review: `moks grant <email> ml_reviewer:<env>:<ns>`.
4. **Train, without changing anything live:**
   `kubectl --context mk4 -n moks exec deploy/moks-worker -- moks ml train --environment mk4 --namespace <ns>`.
   Read the output for each cohort: `insufficient_history`, or two candidates with gates.
5. **Turn on shadow mode:** Admin > Machine learning > anomaly detection > shadow, with a reason. Then pick a champion
   per workload under Models. Observations appear on ML insights within `intervalMinutes`.
6. **Optionally schedule training:** set `ml.training.enabled: true` and
   `ml.anomaly.namespaces: "mk4:<ns>,mk4:<ns2>"`, then push and sync the worker app. The CronJob requests 100m CPU and
   256Mi memory, is limited to 1 CPU and 768Mi, has a 1-hour deadline, and is weekly by default.

## Verify

- An investigation shows **Knowledge used** with "Retrieved n · eligible n · given to the model n · cited n", and
  each cited passage's button reads `E3.2` and opens evidence `E3`.
- Knowledge > source > Retrieval readiness shows "n eligible for retrieval" and its exclusions. Test retrieval on the
  same source should return no more documents than that.
- After step 2, an investigation like a past approved incident shows **Similar incidents** with the matched
  symptoms and the note that similarity is not a cause.
- After step 5:
  - `moks_ml_observations_total{status="scored"}` rises;
  - no incident, detection state or action plan appears that would not have appeared otherwise;
  - `moks_ml_fallbacks_total{reason="model_invalid"}` stays 0.
- The kill switch: set anomaly detection to disabled. The next tick logs `anomaly_detection is disabled` and no new
  observations appear.

## Roll back

| What | How |
|---|---|
| One capability | set its mode to `disabled` |
| All of ML | `ml.disabled: true`, push, sync, restart API and worker |
| One model | Admin > Models > Roll back this cohort, or `POST /ml/rollback` |
| The release | as in [RELEASES.md](RELEASES.md). Migration 0032 is additive, so the previous image runs on the new schema; its tables are simply unused |

## Limitations

- Nothing has been exercised against mk4, real Prometheus, a real embedding model or real incidents. Feature PromQL
  has been rendered and parsed by tests, never executed by Prometheus.
- On mk4 the restart feature has no source (no kube-state-metrics). Request and latency features depend on
  applications exporting the default metric names.
- The Isolation Forest is a small pure-Python implementation, chosen so artifacts are data. It is slower than
  scikit-learn's, which is acceptable for at most 40 cohorts of a few thousand rows.
- Similar incidents compares at most `ml.similarIncidents.candidates` (300) of the most recent approved incidents.
- Anomaly drift (PSI) is computed per scored window against training deciles. It is a monitoring signal, not a gate.
- `moks_ml_feature_drift_psi` is labeled by cohort, so its series count grows with `maxCohorts × 6`.
