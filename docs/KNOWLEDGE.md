# Knowledge sources

Knowledge is untrusted reference material. A retrieved document can inform an investigation, but it can
never become a skill, grant a permission, or stand in for live evidence. That holds even for a document
that looks exactly like a `SKILL.md`.

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
| active | yes | yes | |
| quarantined | no | no | Stops at once. Documents are kept for review. |
| archived | no | no | |
| deleted | no | no | Tombstones stay until the index has dropped them. The name can be reused. |

`review_interval_days` makes a source stale when its review is overdue, and `stale_after_days` when a
document has not been confirmed against its source. `stale_policy: flag` marks results, and `exclude` drops them.
Example: [examples/knowledge-source.json](examples/knowledge-source.json).

## Synchronization

Each sync is one bounded pass of 500 files against a persistent inventory (`knowledge_sync_items`).

1. **Listing** pins one commit. A tree GitHub truncates is walked by directory, up to 2,000 requests.
2. **Reading** takes the least recently tried files first, so a file that keeps failing cannot starve others.
3. Another pass is scheduled in 30 seconds until the listing is finished. A new listing starts only then,
   or after 24 hours.

The state is reported separately: listing `complete | partial | failed`, content `in_progress | partial | complete`,
and counts of discovered, processed, excluded, failed and remaining files. **Deletions come only from a
complete listing.** `last_success_at` moves only when both the listing and the content are complete.

`PIPELINE_VERSION` in `rag/chunking.py` is part of change detection. Raise it when chunking, redaction,
extraction or stored metadata change, and GitHub documents are read again even though their content did not change.
Uploads and postmortems keep no original text, so they stay on their old pipeline until uploaded or approved again. Status shows how many.

## Chunks and citations

Chunks are limited by estimated tokens (`MOKS_RAG_CHUNK_TOKENS`, default 350, and `MOKS_RAG_CHUNK_OVERLAP_TOKENS`,
default 30, at 4 characters a token, an estimate because no tokenizer ships with MOKS). Markdown splits on headings
and keeps fences together when they fit, YAML on documents and top-level keys, code on blank lines. These are
line heuristics, not parsers. A line too long for one chunk is split across several, so nothing is dropped.

Offsets refer to the stored, redacted text. A citation gives source, path, revision and line range.
`revision` is the last commit it was confirmed at, `content_changed_in` the commit its content last changed in.

## Index consistency

PostgreSQL is the authority. Each change adds an outbox event in its own transaction.

- One worker writes a document at a time (advisory lock). A worker that finds it locked defers.
- If a newer revision committed during a write, the event is recorded as superseded and the newer event rewrites from PostgreSQL.
- `document_index_state` records what each index generation holds.
- A document written while embedding failed has `vector_state: missing`. It is searchable by keyword, shown in
  status, and retried by `backfill_vectors` every minute, stopping at the first failure.
- `reconcile_index` requeues documents whose revisions disagree. `verify_index` compares chunk counts on a sample
  (OpenSearch only: Pinecone cannot count by metadata).
- Dead events are retried one document at a time or in a bounded batch, and every retry is audited.

## Retrieval

Results are fused from keyword, vector and exact-identifier rankings with RRF, then adjusted after authorization by
small, visible `signals`: exact identifier, service, source authority, stale, superseded. Duplicates collapse within
a scope. An RRF score only orders one result list. It is not a probability, and MOKS never presents it as confidence.

`mode` and `fallback_reason` say what actually ran. `evidence.state` is `supported`, `weak` or `none`, decided from what
the passages literally contain. `supported` means the terms are present, not that the answer is right.
Conflicts are flagged when two documents share a title and scope. MOKS does not decide which is right.
