"""Kubernetes Secret <-> AWS Secrets Manager discovery, comparison and reviewed manual sync (docs/SECRETS_SYNC.md).

Where values live. Secret values are read and written only by the executor (MOKS_PROCESS_ROLE=executor), inside
secret_* jobs, and exist only in that process's memory while one job runs. PostgreSQL, job payloads and results,
audit details, logs and traces hold names, key names, version identifiers and keyed digests (HMAC under
MOKS_SECRETS_DIGEST_KEY, which only the executor has). The API never reads a value and has no credential that could.

Nothing here runs on a schedule. Discovery and comparison are read-only and start when a person asks; every write
follows discover -> select -> compare -> review -> approve -> execute -> verify. None of it is an investigation
tool: no LLM or CrewAI agent can reach these modules (tests/test_secret_sync.py checks the tool registry).
"""
