"""ORM models. The schema itself is owned by Alembic migrations (moks/migrations)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    auth_provider: Mapped[str] = mapped_column(String(20))  # oidc | local | dev
    subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoleBinding(Base):
    __tablename__ = "role_bindings"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(40))
    environment: Mapped[str] = mapped_column(String(100), default="*")
    namespace: Mapped[str] = mapped_column(String(253), default="*")
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | oidc-group | bootstrap
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    id_token_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Environment(Base):
    __tablename__ = "environments"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20))  # minikube | eks
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)
    # Infrastructure scope MOKS may touch in this environment, independent of user RBAC.
    allowed_namespaces: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    denied_namespaces: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    max_query_window_minutes: Mapped[int] = mapped_column(Integer, default=360)
    grafana_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Connector(Base):
    __tablename__ = "connectors"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    environment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(30))  # kubernetes | prometheus | loki | ...
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    credential_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMConfig(Base):
    __tablename__ = "llm_configs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    purpose: Mapped[str] = mapped_column(String(20))  # chat | embedding
    provider: Mapped[str] = mapped_column(String(20))  # openai | gemini | bedrock | ollama
    model: Mapped[str] = mapped_column(String(200))
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(String(40), nullable=True)
    credential_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=2000)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("5"))
    input_price_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    output_price_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    llm_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_configs.id", ondelete="SET NULL"))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Investigation(Base):
    __tablename__ = "investigations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    environment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("environments.id"))
    namespace: Mapped[str] = mapped_column(String(253))
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    time_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    time_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="llm")  # llm | deterministic
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvestigationEvent(Base):
    __tablename__ = "investigation_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    investigation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"))
    ref: Mapped[str] = mapped_column(String(10))  # short citation id within the investigation, e.g. E3
    tool: Mapped[str] = mapped_column(String(60))
    connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    connector_type: Mapped[str] = mapped_column(String(30))
    args: Mapped[dict] = mapped_column(JSONB, default=dict)
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[dict] = mapped_column(JSONB, default=dict)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(100), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    decision: Mapped[str] = mapped_column(String(10), default="n/a")  # allow | deny | n/a
    outcome: Mapped[str] = mapped_column(String(20), default="success")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Filled by the database trigger (migration 0001), never by the application.
    chain_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="")
