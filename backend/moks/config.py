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
    rag_max_upload_bytes: int = 2_000_000
    rag_chunk_tokens: int = 350
    rag_chunk_overlap_tokens: int = 30

    # Executor and diagnostics
    process_role: Literal["api", "worker", "executor"] = "api"
    sync_timezone: str = "UTC"  # shown beside knowledge sync schedules
    database_volume_gib: int = 10  # capacity of the database volume, for the size alert
    diagnostics_namespace: str = "moks-diagnostics"
    velero_namespace: str = "velero"
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
