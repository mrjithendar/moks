# Trustworthy reports and durable investigations

How MOKS decides what a report may claim, and what happens when a worker dies mid-run.

## Typed evidence

Every evidence row has a `kind`, set when it is collected:

| Kind | Meaning | Can it support a claim about the system? |
|---|---|---|
| `observation` | A live read that succeeded and was complete | Yes |
| `incomplete` | Succeeded, but cut short or partial | Yes for what it shows, never for absence or health |
| `tool_error` | The call failed | No, only a statement about the failure |
| `access_denied` | Policy refused the call | No, only a statement about the denial |
| `documentation` | Knowledge or a file in Git | No. It says what was written down |
| `historical` | Collected before a resume and past the freshness limit | Only labeled as earlier, and never as health |

## Claim validation

`orchestrator/report.py:validate_claims` runs on every report after schema validation. Each fact ends up accepted, qualified, moved to the hypotheses (`origin: demoted_fact`), recorded as missing evidence, or dropped. The checks are citation existence, evidence kind, namespace scope, freshness (`MOKS_EVIDENCE_MAX_AGE_SECONDS`, default 900), coverage (a failed or truncated source qualifies or blocks a claim of health), contradiction by a live finding, and whether identifiers named in the sentence occur in what it cites. Summary sentences have no citations, so they are held to all the evidence collected. Every decision is in `report.validation.decisions`.

**This is deterministic and it is not proof.** An identifier appearing in a citation shows the sentence is about the right thing, not that the passage says what the sentence claims. Numbers and paraphrase are not checked. No model takes part (`validation.model_assisted` is `false`).

## Verification

`verified_findings` lists checks that passed, each with its check, result, target, timestamp and evidence, and only from in-scope live evidence.

| Category | Meaning |
|---|---|
| `observed_failure` | The failure happened. Its cause stays open |
| `configuration_defect` | The wrong setting was read from the object. These alone appear in `verified_causes` |
| suspected cause | `suspected_causes`: never verified, whatever confidence the model gives |
| validated recovery | Only an action plan's validation after a change. A report never claims this |

## Output limits

A call asks for the smallest of the configured `max_output_tokens`, what the step wants, and what the investigation has left after the prompt. If that is under the step's minimum, the call is not made and the rule-based path takes over with a note. The Claude provider's 4000-token floor now yields to the configured cap, and reservations use what the provider really requests. A cap under 4000 leaves less room for Claude's thinking, so set `max_output_tokens` to at least 4000 for those models.

## Ownership

`worker.claim` gives each claim a new `execution_token` and a lease (`MOKS_INVESTIGATION_LEASE_SECONDS`, default 120). Writes check the token under `FOR SHARE` in the same transaction, so the reaper waits for them or they fail. `_finish` is the only way out of `running`, so one report, one reply and one audit record come out however many workers believed they owned the run. A worker stops when it loses the lease, or when it cannot reach the database for longer than the lease. A tool result that arrives after that is discarded and audited.

## Resuming

A checkpoint is written after each graph node, under the same fence. It holds decisions and progress: scope, completed nodes, evidence references, pinned skill digests, pending tool calls, budgets, model and prompt versions, connector identity and when authorization was last checked. It holds no evidence payloads, and it is redacted before it is written.

`investigations.attempts` is capped by `MOKS_INVESTIGATION_MAX_ATTEMPTS` (default 2). On a resume MOKS re-checks permissions and connectors first; evidence past the freshness limit, or read through a changed connector, becomes `historical`, and the basics are read again. The node after the checkpoint runs from its start. Every investigation tool is read-only, so repeating one mutates nothing; repeats count against the same tool budget, and LLM spend is re-read from `llm_usage`. Changes only happen in action plans, which have their own fencing.

This is an explicit design rather than LangGraph's `PostgresSaver`: that saver writes through its own connection, so it cannot share a transaction with the ownership check, and it needs a second driver plus `setup()` DDL that does not fit per-component database roles.
