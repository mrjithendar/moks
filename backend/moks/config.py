"""Process configuration, read once from the environment (prefix MOKS_).

Only deployment-level settings live here. Connectors, LLM providers, environments and
role bindings are application data in PostgreSQL, managed through the admin API. Secrets
are never stored in the database: rows hold *references* (see moks.connectors.credentials).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOKS_", env_file=None, extra="ignore")

    env: Literal["development", "test", "production"] = "production"

    # Public URL handling. Everything (SPA, API, docs, auth callbacks) is served under
    # base_path by the app itself; the ingress must NOT strip the prefix.
    public_url: str = "https://jithendar.com/moks"
    base_path: str = "/moks"

    database_url: str = "postgresql+asyncpg://moks@localhost:5432/moks"
    database_pool_size: int = 5

    # Authentication: oidc | local | dev. dev is refused unless env=development.
    auth_mode: Literal["oidc", "local", "dev"] = "oidc"
    session_secret: str = Field(default="", repr=False)
    session_idle_minutes: int = 480
    session_absolute_hours: int = 12
    cookie_secure: bool = True

    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = Field(default="", repr=False)
    oidc_scopes: str = "openid email profile"
    # Emails granted Administrator on first sign-in (comma separated). Everyone else starts
    # with no role bindings and therefore no access until an administrator grants one.
    bootstrap_admins: str = ""
    # Optional JSON map of IdP group -> list of "role:environment:namespace" grants, applied at sign-in.
    oidc_group_roles: str = "{}"
    oidc_groups_claim: str = "groups"
    # Restrict sign-in to these email domains (comma separated); empty allows any verified email.
    oidc_allowed_domains: str = ""

    # API docs at {base_path}/api/docs. Disabled by default in production.
    api_docs: bool = False

    # Static SPA build directory. Empty disables static serving (API-only, e.g. tests).
    static_dir: str = "/app/static"

    # Worker
    embedded_worker: bool = False  # run the investigation worker inside the API process (dev only)
    worker_concurrency: int = 2
    worker_poll_seconds: float = 1.0

    # Investigation limits (hard ceilings; per-environment settings can only lower them)
    max_tool_calls: int = 16
    max_llm_rounds: int = 4
    max_duration_seconds: int = 180
    max_query_window_minutes: int = 360
    default_query_window_minutes: int = 60
    max_result_bytes: int = 48_000
    max_tokens_per_investigation: int = 60_000
    investigation_lease_seconds: int = 120
    investigation_max_attempts: int = 2  # a crashed run resumes this many times in all
    # Tool calls per intent, under max_tool_calls (0 or absent: max_tool_calls). JSON in MOKS_INTENT_TOOL_BUDGETS.
    intent_tool_budgets: dict[str, int] = {"conceptual": 2, "deployment_guidance": 12, "repository": 10, "execution": 12}
    # Searches the knowledge stage may run per investigation (initial plus one refresh after triage). They are
    # counted in tool_calls but come from this allowance, so documented procedures never cost live evidence.
    knowledge_max_searches: int = 3
    # ML (docs/ML.md). Capability modes live in the ml_capabilities table; these bound the work each one may do.
    ml_disabled: bool = False                 # MOKS_ML_DISABLED: forces every capability off whatever the database says (the deploy-time kill switch)
    ml_learning_capture: bool = True          # write a learning record when an investigation finishes; capturing is not training
    ml_learning_retention_days: int = 365     # records past this are purged by `moks learning purge` and the daily job
    ml_similar_incidents_k: int = Field(default=5, ge=1, le=10)
    ml_similar_incidents_candidates: int = Field(default=300, ge=10, le=2000)
    ml_anomaly_step_seconds: int = Field(default=300, ge=60, le=3600)          # one sample per step, for training and inference alike
    ml_anomaly_window_minutes: int = Field(default=60, ge=15, le=360)          # the window an observation scores
    ml_anomaly_training_days: int = Field(default=14, ge=1, le=60)
    ml_anomaly_min_history_days: float = Field(default=7.0, ge=0.5, le=60)     # less than this is reported as insufficient history
    ml_anomaly_max_cohorts: int = Field(default=40, ge=1, le=500)               # per environment per tick, and per training run
    ml_anomaly_max_series: int = Field(default=200, ge=10, le=5000)             # per query; more is refused rather than truncated
    ml_anomaly_concurrency: int = Field(default=2, ge=1, le=8)
    ml_anomaly_interval_minutes: int = Field(default=15, ge=5, le=240)
    ml_anomaly_max_flags_per_day: float = Field(default=3.0, ge=0.1, le=100)   # promotion gate on the validation period
    ml_anomaly_namespaces: str = ""            # "env:namespace,env:namespace" the scheduled training covers; empty trains nothing
    ml_artifact_store: Literal["postgres", "s3"] = "postgres"
    ml_artifact_bucket: str = ""
    ml_artifact_prefix: str = "moks/ml-artifacts/"
    ml_artifact_endpoint: str = ""
    ml_artifact_region: str = ""
    ml_artifact_max_bytes: int = 5_000_000
    guidance_max_files: int = 6        # repository files read for one guidance answer
    intent_llm_classifier: bool = True  # ask the chat model only when the rules are unsure
    rag_rerank_enabled: bool = False  # stays off until docs/RERANKING.md's gates pass on real questions
    rag_rerank_url: str = ""          # internal Service only
    rag_rerank_model: str = ""        # must match what the service reports, or results keep their existing order
    rag_rerank_revision: str = ""
    rag_rerank_candidates: int = 30   # rag/rerank.py holds the ceilings, whatever is configured here
    rag_rerank_results: int = 12
    rag_rerank_timeout_ms: int = 1500
    rag_rerank_max_query_chars: int = 1000
    rag_rerank_max_passage_chars: int = 2000
    rag_decompose: bool = False  # off: the rag dataset measured no gain from it. Turn on only with a dataset that shows one.
    rag_activation: str = "strict"  # strict: a reindex with any incomplete document fails. degraded: it activates and says so.
    evidence_max_age_seconds: int = 900  # older evidence cannot back a claim about the current state without saying so
    # Opt-in CrewAI workload investigation (docs/crewai-investigation.md). Off unless MOKS_CREWAI_ENABLED is true and the
    # image was built with the crewai extra. The global ceilings (max_tool_calls, max_duration_seconds) still apply.
    crewai_enabled: bool = False
    crewai_timeout_seconds: int = Field(default=120, ge=10, le=900)
    crewai_tool_timeout_seconds: int = Field(default=30, ge=1, le=120)
    crewai_max_iterations: int = Field(default=5, ge=1, le=20)  # per agent
    crewai_max_tool_calls: int = Field(default=20, ge=1, le=100)  # across the whole crew
    crewai_max_concurrent: int = Field(default=2, ge=1, le=10)  # crews per worker process
    crewai_slot_wait_seconds: int = Field(default=20, ge=0, le=300)
    crewai_excerpt_chars: int = Field(default=2000, ge=200, le=20000)  # what one tool result shows the model and the report
    crewai_log_lines: int = Field(default=100, ge=10, le=500)

    # Rate limits (per process; see docs/SECURITY.md)
    rate_login_per_minute: int = 10
    rate_investigations_per_minute: int = 10
    rate_api_per_minute: int = 600

    # Retrieval: postgres (full-text, + pgvector when installed) or opensearch (BM25 + k-NN).
    rag_backend: Literal["postgres", "opensearch", "pinecone"] = "postgres"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    opensearch_url: str = ""
    opensearch_username: str = ""
    opensearch_password: str = Field(default="", repr=False)
    opensearch_verify_tls: bool = True
    opensearch_ca_file: str = ""       # PEM bundle for a private CA (the bundled moks-search cluster)
    # Separate identities, least privilege first. opensearch_username searches; these two exist only in
    # the worker and the migration Job, so an API pod holds nothing that can write. Unset: the search one.
    opensearch_index_username: str = ""
    opensearch_index_password: str = Field(default="", repr=False)
    opensearch_lifecycle_username: str = ""
    opensearch_lifecycle_password: str = Field(default="", repr=False)
    # Names the cluster an index generation lives in. Empty is the original cluster. Generations are kept per
    # cluster, so a second cluster can be filled, checked and reverted to without touching the first.
    opensearch_cluster: str = Field(default="", pattern=r"^[a-z0-9]{0,9}$")
    # true in the migration Job: this process fills a cluster that search does not use yet.
    opensearch_standby: bool = False
    opensearch_compare_url: str = ""   # the cluster `moks opensearch compare` reads as the old side
    opensearch_search_timeout_seconds: float = 3.0
    opensearch_snapshot_repository: str = ""
    opensearch_snapshot_policy: str = "moks-knowledge"
    rag_retired_retention_hours: int = 168  # a replaced generation stays this long, so it can be activated again
    # Amazon OpenSearch Service: sign requests with the AWS identity (SigV4) instead of a password.
    opensearch_aws_region: str = ""
    # Database backups: namespaces the executor may dump from (it needs pods/exec there).
    database_backup_namespaces: str = ""
    database_backup_dir: str = "/backup-work"
    database_backup_timeout_seconds: int = 1800
    rag_max_upload_bytes: int = 2_000_000
    rag_chunk_tokens: int = 350
    rag_chunk_overlap_tokens: int = 30

    # Executor and diagnostics
    process_role: Literal["api", "worker", "executor", "terminal"] = "api"
    sync_timezone: str = "UTC"  # shown beside knowledge sync schedules
    database_volume_gib: int = 10  # capacity of the database volume, for the size alert
    diagnostics_namespace: str = "moks-diagnostics"
    velero_namespace: str = "velero"
    # Terminal gateway. Profiles are JSON from the chart, so a change of IAM role is a reviewed change.
    terminal_namespace: str = "moks-terminals"
    terminal_image: str = ""
    terminal_image_pull_secrets: str = ""
    terminal_service_account: str = "moks-terminal-session"
    terminal_profiles: str = "[]"
    terminal_kube_namespaces: str = ""
    terminal_idle_minutes: int = 15
    terminal_max_minutes: int = 60
    terminal_user_sessions: int = 2
    terminal_cpu_limit: str = "500m"
    terminal_memory_limit: str = "512Mi"
    argocd_namespace: str = "argocd"
    build_commit: str = "dev"  # baked into the image at build time
    llm_user_daily_budget_usd: float | None = None      # None: no per-person limit
    llm_investigation_budget_usd: float | None = None   # None: no per-investigation limit
    # Which job classes this worker runs, and how many of each at once. Investigations are the
    # "interactive" class and use worker_concurrency.
    worker_classes: str = "detection,interactive,knowledge,maintenance"
    worker_capacity: str = "detection=2,knowledge=1,maintenance=1"
    job_queue_limit: int = 500          # queued jobs per class; beyond this new work is refused
    job_environment_limit: int = 2      # running jobs per environment within a class
    investigation_queue_limit: int = 200
    own_namespace: str = "moks"  # never a restore target: restoring over MOKS would stop the restore

    # Alerts: shared secret for the Alertmanager / Grafana webhook (Authorization: Bearer ...)
    alert_webhook_token: str = Field(default="", repr=False)
    alert_poll_seconds: int = 60

    # Observability
    metrics_port: int = 9464
    log_level: str = "INFO"
    otel_service_name: str = "moks"
    trace_sample_ratio: float = 1.0  # share of new traces kept; children always follow their parent

    @field_validator("base_path")
    @classmethod
    def _normalise_base_path(cls, v: str) -> str:
        v = "/" + v.strip("/")
        return "" if v == "/" else v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.auth_mode == "dev" and self.env != "development":
            raise ValueError("MOKS_AUTH_MODE=dev is only allowed with MOKS_ENV=development")
        if self.env == "production":
            if len(self.session_secret) < 32:
                raise ValueError("MOKS_SESSION_SECRET must be at least 32 characters in production")
            if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_client_id):
                raise ValueError("MOKS_OIDC_ISSUER and MOKS_OIDC_CLIENT_ID are required for auth_mode=oidc")
        if not self.session_secret:
            # Development/test only: a fixed, obviously insecure value.
            self.session_secret = "dev-insecure-session-secret-change-me-000000"
        return self

    @property
    def api_prefix(self) -> str:
        return f"{self.base_path}/api"

    @property
    def bootstrap_admin_set(self) -> set[str]:
        return {e.strip().lower() for e in self.bootstrap_admins.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
