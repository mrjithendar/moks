# Knowledge sources

Knowledge is untrusted reference material. A retrieved document can inform an investigation, but it can
never become a skill, grant a permission, or stand in for live evidence. That holds even for a document
that looks exactly like a `SKILL.md`.

## Who can do what

| Permission | Roles | Allows | Scope |
| --- | --- | --- | --- |
| `knowledge.read` | every role that can read investigations | search, the Explorer, feedback | per binding |
| `knowledge.test` | investigator, operator, `knowledge_reviewer`, `knowledge_admin`, `index_operator` | the retrieval test page | per binding |
| `knowledge.review` | `knowledge_reviewer`, `knowledge_admin`, admin | see drafts and quarantined documents, review, change a document's lifecycle, read feedback | per binding |
| `knowledge.source_admin` | `knowledge_admin`, admin | create, change, sync and retire sources | global only |
| `knowledge.index` | `index_operator`, admin | index generations, outbox, backfill, retries, diagnostics | global only |

Administering a source and running the index are separate on purpose. A reviewer outside their binding gets 404, as for any document they cannot see.

## Who can read what

A document has the environment and namespace of its source. `*` means that axis is unrestricted.

- A person reads a document when **one binding** matches both axes. With `prod/team-a` and `dev/team-b`,
  `prod/team-b` is not readable.
- A filter of whole bindings runs before ranking wherever the backend can express it. PostgreSQL always
  can. OpenSearch can, except character classes. Pinecone cannot express patterns.
- Where a filter is incomplete, search widens its candidates within a limit of 400, so unreadable documents
  cannot crowd out readable ones. The response reports `candidates.filtered_before_ranking` and `expanded`.
- Every result is checked again against PostgreSQL. That check is never skipped.
- Asking for an environment or namespace you hold no binding on returns nothing, including shared documents.
- Investigations search only their own environment and namespace.

**Sharing globally.** A source scoped to `*/*` is readable by everyone with any knowledge access, so it must
be chosen with `shared_globally`. Confidential and restricted sources cannot be shared globally. Sources
that were already `*/*` before migration 0021 are recorded as shared, because they already were.

There is no retrieval result cache, so no cache key can outlive a revoked binding.

## Lifecycle

| State | Syncs | Searchable | Notes |
| --- | --- | --- | --- |
| draft | yes | no | Fills up so a reviewer can read it first. |
| active | yes | yes | |
| quarantined | no | no | Stops at once. Documents are kept for review. |
| archived | no | no | |
| deleted | no | no | Tombstones stay until the index has dropped them. The name can be reused. |

A document has its own `draft`, `quarantined`, `active`, `archived` or `deleted` state. `review_required` on a source holds new and changed documents in quarantine for a reviewer. Reviewing a document sets its next review date from the source's interval when no date is given.

`review_interval_days` makes a source stale when its review is overdue, and `stale_after_days` when a
document has not been confirmed against its source. `stale_policy: flag` marks results, and `exclude` drops them.
Example: [examples/knowledge-source.json](examples/knowledge-source.json).

## What a GitHub source reads

A source chooses **profiles** (`config.profiles`, in the source form as "What to index"):

| Profile | Reads |
|---|---|
| `documentation` | Markdown, MDX, reStructuredText, AsciiDoc, text, `docs/**`, `runbooks/**` |
| `infrastructure` | YAML, Helm charts and values, kustomizations, Terraform and HCL, Dockerfiles, CI workflows (GitHub, GitLab, Jenkins), Makefiles, shell |
| `application` | Python, Go, JavaScript, TypeScript, TSX, shell, Dockerfiles, `package.json`, `go.mod`, `pyproject.toml`, `requirements*.txt` |

Explicit `include` globs always count, and profiles add to them. A source with neither reads what every source
read before profiles existed (Markdown, `docs/`, `runbooks/`, YAML, Dockerfile, workflows, Helm, Terraform), so
an existing source is never broadened by an upgrade; application code is read only when someone ticks it.
`exclude` globs always narrow. Whatever a source includes, MOKS never indexes dependencies (`node_modules`,
`vendor`, virtualenvs), generated output (`dist`, `.next`, `__pycache__`, coverage, minified files, source maps,
lock files, Terraform state, generated protobuf), binaries, or files over 300 KB, and refuses files that look
like credentials (key files, `.env`, Secret manifests with data, text with many credential-like values),
recording them as excluded with the reason. `GET /knowledge/profiles` lists the exact patterns.

Every listing records how many files it saw, how many the policy left out and up to 200 of those paths with the
rule that decided, so "why is this file not searchable?" has an answer (see [Source readiness](#source-readiness)).

These always-excluded patterns are a little wider than before this release (virtualenvs, `.next`, coverage,
source maps, more lock files and binary types). A complete listing removes documents that no longer qualify, so
the first sync may delete a few documents of those kinds.

## Synchronization

Sync is daily by default, with Sync Now on the Sources table. Both go through one job, deduplicated per source, and `next_sync_at` is in PostgreSQL, so a restart loses nothing.

Each sync is one bounded pass of 500 files against a persistent inventory (`knowledge_sync_items`).

1. **Listing** pins one commit. A tree GitHub truncates is walked by directory, up to 2,000 requests.
2. **Reading** takes the least recently tried files first, so a file that keeps failing cannot starve others.
3. Another pass is scheduled in 30 seconds until the listing is finished. A new listing starts only then,
   or after 24 hours.

The state is reported separately: listing `complete | partial | failed`, content `in_progress | partial | complete`,
and counts of discovered, processed, excluded, failed and remaining files. **Deletions come only from a
complete listing.** On a web source that means a pass that visited everything: a 404 during an interrupted crawl deletes nothing, because a failing site looks the same. `last_success_at` moves only when both the listing and the content are complete, so stopping at a page limit is not a success.

**Sync is not indexing.** A pass that reads every file finishes the upstream half: `last_sync_status` and
`last_success_at` say so. With PostgreSQL search the chunks are searchable in the same transaction. With
OpenSearch or Pinecone the changes wait in the outbox, so the sync result and the Sources table carry a separate
`indexing` state (`complete | pending | failed`, with pending and dead counts). A source is searchable when both
halves are done and its documents are active; see [Source readiness](#source-readiness).

A file PostgreSQL refuses is recorded as failed, and the pass goes on. A page over 300 KB keeps its first 300 KB and says so wherever the document is shown.

`PIPELINE_VERSION` in `rag/chunking.py` is part of change detection. Raise it when chunking, redaction,
extraction or stored metadata change, and GitHub documents are read again even though their content did not change.
Code has its own `CODE_PIPELINE_VERSION` (4), so a change to how code is split re-reads code files only. The
first sync after this release re-reads and re-embeds the code files a source already holds (Dockerfiles, `.tf`,
shell), not its Markdown or YAML.
Uploads and postmortems keep no original text, so they stay on their old pipeline until uploaded or approved again. Status shows how many.

## Chunks and citations

Chunks are limited by estimated tokens (`MOKS_RAG_CHUNK_TOKENS`, default 350, and `MOKS_RAG_CHUNK_OVERLAP_TOKENS`,
default 30, at 4 characters a token, an estimate because no tokenizer ships with MOKS). Markdown splits on headings
and keeps fences together when they fit, YAML on documents and top-level keys. Code splits at top-level
definitions (Python `def`/`class`, Go `func`/`type`, JavaScript and TypeScript functions, classes, exported
constants, interfaces and types, shell functions, Terraform blocks, Dockerfile stages) once a section holds a
third of a chunk, and each chunk's heading names the definitions in it; otherwise at blank lines. These are
line heuristics, not parsers. A line too long for one chunk is split across several, so nothing is dropped.

Offsets refer to the stored, redacted text. A citation gives source, path, revision and line range.
`revision` is the last commit it was confirmed at, `content_changed_in` the commit its content last changed in. A link always names the same commit as `revision`, including when a file was confirmed unchanged.

Only newlines count as lines, as on a Git host, and redaction keeps every line break, so a citation's lines are the source's. Links have line anchors on Git hosts only, using the plain view for Markdown. Only `http` and `https` links leave MOKS.

## Index consistency

PostgreSQL is the authority. Each change adds an outbox event in its own transaction.

- One worker writes a document at a time (advisory lock). A worker that finds it locked defers.
- If a newer revision committed during a write, the event is recorded as superseded and the newer event rewrites from PostgreSQL.
- `document_index_state` records what each index generation holds.
- A document written while embedding failed has `vector_state: missing`. It is searchable by keyword, shown in
  status, and retried by `backfill_vectors` every minute, stopping at the first failure. The document that failed goes to the back of the queue, so one that always fails cannot hold up the rest. A document with no chunks has nothing to embed, and counts as complete for a strict reindex.
- `reconcile_index` requeues documents whose revisions disagree. `verify_index` compares chunk counts on a sample
  (OpenSearch only: Pinecone cannot count by metadata).
- Dead events are retried one document at a time or in a bounded batch, and every retry is audited.

Deletion and replacement run write-first: new chunks go in, fenced by content revision, then old ones are pruned, and every bulk item's result is read. `sync_generation` repairs a generation from `document_index_state` alone, which is also how a generation catches up after a cutover or rollback. [OPENSEARCH.md](OPENSEARCH.md) covers deployment, migration and recovery.

## Retrieval

Results are fused from keyword, vector and exact-identifier rankings with RRF, then adjusted after authorization by
small, visible `signals`: exact identifier, service, resource type, a document written for the scope asked about, source authority, stale, superseded. Duplicates collapse within
a scope. An RRF score only orders one result list. It is not a probability, and MOKS never presents it as confidence.

`mode` and `fallback_reason` say what actually ran. `evidence.state` is `supported`, `weak` or `none`, decided from what
the passages literally contain. `supported` means the terms are present, not that the answer is right. Search abstains when no identifier from the question appears, when the terms barely overlap, or when every passage that matches is stale or superseded, and it lists the identifiers it could not find.
Conflicts are flagged when two documents share a title and scope, and each result names the other. MOKS does not decide which is right.

Scores order one result list. They are not probabilities, and a citation existing does not show that it supports a claim. `claim_support` checks literal overlap in one passage, and says so. Documents are guidance: what is true of a cluster now needs live tool evidence.

## Index coverage

`document_index_state` records, per document and index generation, the revision, expected and written chunks and vectors, the model and dimensions, the last error and retries. An index is `complete` or `degraded` from that record, and a degraded one becomes complete only when the record shows every document whole. With Pinecone, written counts are what MOKS recorded, because Pinecone cannot count by metadata; OpenSearch is also sampled by `verify_index`.

## Document lifecycle and trust

A document is `draft`, `quarantined`, `active`, `archived` or `deleted`, and only `active` is searched. Search reads this from PostgreSQL, so a quarantine works at commit. `trust` is separate: a sync finding a file makes it `unreviewed`, and changed content loses a review. Only a reviewed document's source authority affects ranking.

Activating a retained generation recomputes its coverage: it comes back `degraded` if it missed anything, and documents' vector state is taken from its own record. Nothing is marked complete because a generation became active. Vector counts come from chunks that really received a vector.

## Knowledge Explorer

**Knowledge > Knowledge Explorer** lists what you can read, with filters by source, scope, service, resource type, document type, owner, team, lifecycle, classification, freshness, indexing and embedding. Sync, indexing and embedding are shown as three states per source, because they finish at different times. A document opens to its sanitized preview, original link, version and citation details, chunks with line and offset ranges, and which index generations hold it.

One predicate in `rag/explore.py` decides what a person sees. The list, each count, every filter option and each direct link use it, so an unreadable document does not appear in a total, does not offer a filter value, and answers exactly like one that does not exist. Excluded, draft and quarantined documents are for reviewers. Retries and errors are for `knowledge.index`.

Everything comes from PostgreSQL. The Explorer never calls OpenSearch, and there is no proxy to it. Embeddings are never shown: only the model, dimensions and coverage. Per-chunk membership is not recorded, so membership is per document, and `verify_index` samples what the index really holds.

## Retrieval test

**Knowledge > Retrieval test**, with `knowledge.test`, compares keyword, vector and hybrid search for one question, and reranking on and off when a reranker is enabled. It runs at most four searches and one embedding, under your own grants. Each column shows what actually ran, which can be less than what was asked for, and why. `knowledge.index` adds candidates and the index name. Token counts are estimates, because embedding providers report none.

Feedback (relevant, irrelevant, outdated, missing information) goes to **Knowledge review** for a person. Nothing reads it to train a model, change ranking or publish anything. It is redacted when stored, and a reviewer sees only what their binding covers.

## Evaluation isolation

Evaluation content lives in `realm = 'evaluation'`, enforced in every candidate query, in the authoritative check and by a composite foreign key that ties a document to its source's realm. No grant reaches across it, including a wildcard admin. A run uses its own backend name, so it never touches an external index or the outbox; it proves isolation before its first case and stops if it cannot. It removes its content in a `finally` block, and `cleanup_evaluations` collects what a dead run left. This is application-enforced in one database, not a separate database or role.

## Query understanding

Search reads identifiers, error names, resource types and time references without a model, and returns them as `understanding`. `MOKS_RAG_DECOMPOSE` splits multi-part questions into separate rankings. It is off: on the `rag` dataset it changed neither recall, MRR nor nDCG and added latency. That dataset is small and synthetic, so re-measure on real questions before turning it on.

## Knowledge in investigations

Retrieval is a stage of every investigation, not a planner's choice (`orchestrator/knowledge.py`):

    intake -> knowledge -> triage -> logs -> skills -> refresh -> plan/act or rules -> correlate -> report

- **knowledge** runs before anything live is read, with or without a model. It searches the original question and,
  when it adds anything, a second query built from what MOKS resolved: target, repository, namespace and, for a
  short follow-up such as "how do I fix it?", the words of the turn before.
- **refresh** runs once after triage, only when the findings name symptoms the first queries did not
  (CrashLoopBackOff, OOMKilled, a crash log line) and add at least two new terms.
- Guidance and concept questions use the same stage through `guidance.py`.

Every search is a `kb_search` tool call, so the executor applies RBAC, stores the result as evidence and audits
it. The scope is the investigation's environment and namespace, or the target's namespace once resolved, set
before the first search. The stage has its own allowance, `MOKS_KNOWLEDGE_MAX_SEARCHES` (default 3), counted in
`tool_calls` but not taken from the live-evidence budget. It stops near the time limit. A finished stage is in the
checkpoint and is not run again on resume; a search stored as evidence before a crash is reused, not repeated.
A failed search never stops the investigation: the report says the knowledge gap in `missing_evidence`.

**Relevance never widens access.** It is decided after search, on passages the person may already read. Each
passage gets one authority, with its reasons:

| Authority | When | Used |
|---|---|---|
| `target` | the source's `service` or `applies_to.services` matches the target or a service the question names, the repository is the target's, the document is scoped to the target's namespace, it names a resource from the findings, or it names the target | yes |
| `shared` | documentation with no service binding that shares enough of the request's terms (or ranks top 3 in hybrid search with some overlap) | yes, labeled general guidance |
| `unverified` | relevant, but from a repository or service binding not established as the target's | given to the model with that label; never used for guidance steps |
| `unrelated` | bound to another service | no, with the reason |
| `low_relevance` | shares too little with the request | no, with the reason |

Passages are judged again as context grows (triage names the workloads), so a billing runbook found before
triage is marked unrelated once the run knows it is about payments. To make a repository authoritative for a
service, set the source's **Service** (or `applies_to.services`) or the `services` path map.

Passages reach the planner and the report once, in a bounded block (9,000 characters, 1,200 per passage) inside
untrusted-data fences, labeled with authority, path, commit, lines and freshness. The prompts say documentation
describes intent and never proves current state; claim validation still turns a fact that cites only
documentation into a hypothesis. Without a model, or when the model cites none of the target's passages, MOKS
adds the documented procedures to next steps, labeled "Documented, not observed". Retrieved text grants no tools
and no scope: every call still goes through the executor's allowlist, and remediation is a proposal that needs an
action plan and its approvals.

The report's `knowledge` section (shown as **Knowledge used**) records status (`retrieved | partial | empty |
failed | not_attempted`), the scope searched, every query with its result count, mode and fallback, each passage
with repository, path, commit, lines, link, freshness, authority and why it was or was not used, and, when nothing
was found, hints about the sources the person can read (unfinished syncs, index backlog, documents awaiting
review). Hints count only sources the person can read and never name them. Logs carry stage, outcome, mode,
fallback, timing and counts, never passage text; `moks_knowledge_retrievals_total{stage,outcome}` counts stages.

## Source readiness

`GET /knowledge/sources/{id}/health` (source administrators) separates:

- **upstream**: last successful sync and revision, the pinned revision, and files listed, included, left out by
  policy (with a sample and the rule), excluded on read, failed and pending;
- **documents**: active, draft, quarantined and archived, and vector state;
- **indexing**: backend, mode, outbox pending and dead, documents behind;
- **searchable**: documents and chunks the active backend can return now;
- **reasons**, each with a code: `source_inactive`, `never_synced`, `sync_failed`, `sync_incomplete`, `sync_partial`,
  `no_active_index`, `indexing_pending`, `indexing_failed`, `awaiting_review`, `stale_excluded`, `no_searchable_documents`.

With `?path=docs/runbook.md` it says why one file is or is not searchable: `file_excluded` (and the rule),
`file_pending`, `file_failed`, `file_content_excluded`, `file_not_found`, `awaiting_review`, `indexing_pending`.

`POST /knowledge/sources/{id}/test-retrieval` (`knowledge.test`) runs the investigation's search, narrowed to the
source, under the caller's grants and the environment and namespace given. When nothing comes back it adds the
reasons above plus `outside_scope`, `no_access` or `no_results`. Someone who can neither read the source's scope nor
administer sources gets 404; a reader who is not an administrator sees only the scope reasons, not counts. The audit
record keeps the query length, not the query. Both are in the source's details drawer (Retrieval readiness, Test retrieval).

A GitHub knowledge source and a Git connector are different things. The source copies files into the index on a
schedule, and investigations search that copy under each person's knowledge access. The connector reads a
repository live during an investigation (deployed revision, commits, files) within its own allowlist. Neither
creates the other, and neither uses the other's credential.

## Verify end to end

With one GitHub repository and one investigation, on a test environment:

1. **Source.** Knowledge Base, Sources: add a GitHub source for `owner/repo` scoped to the environment and the
   namespace the service runs in, Service set to the service name, profiles Documentation and Infrastructure
   (add Application to index code). The first sync is queued.
2. **Sync.** When the row's Sync shows complete, open Details. Retrieval readiness should read *searchable*, with
   Sync complete, Index complete, and nonzero searchable documents and chunks. If not, the reasons say which half
   is unfinished. With OpenSearch, Index may read *indexing* for a minute while the outbox drains.
3. **One file.** Type the runbook's path into "Why is this file not found?" and press Explain: it should say
   Searchable, or name the rule, the review state or the index backlog.
4. **Test retrieval.** In the same drawer, ask what an engineer would ("why is <service> crash-looping") with the
   source's environment and namespace. The runbook's passage should come back with path, lines and commit. Sign in
   as someone without a binding on that namespace and the same test returns 404 or no results with `no_access`.
5. **Investigation.** Run "why is <service> crash-looping?" in that environment and namespace. In the report,
   **Knowledge used** shows status *retrieved*, the scope searched, the queries, and the runbook passage as
   *about the target* with a link to the commit and lines. Recommended next steps cite it (E-ref) as a documented
   procedure, separate from observed facts. The Evidence list shows the `kb_search` call before the first
   Kubernetes call.
6. **Follow-up.** Ask "how do I fix it?" in the same conversation: Queries shows a second query with the service
   name, and the new turn has its own `kb_search` evidence.
7. **Negative check.** Quarantine the runbook document in the Knowledge Explorer and ask again: the passage is
   gone, and Knowledge used gives the reason (nothing relevant, plus a hint that documents await review).
