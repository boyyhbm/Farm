import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    String,
    Text,
    Integer,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Numeric,
    UniqueConstraint,
    CheckConstraint,
    Index,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base, utcnow


def uid():
    return str(uuid.uuid4())


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)


class Created:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Role(Base):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(String(20), primary_key=True)


class User(Identified, Created, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(ForeignKey("roles.name"), default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Identified, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AccountGroup(Identified, Base):
    __tablename__ = "account_groups"
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Node(Identified, Created, Base):
    __tablename__ = "nodes"
    transport_security: Mapped[str] = mapped_column(String(40), default="unknown")
    transport_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hostname: Mapped[str] = mapped_column(String(255))
    os_version: Mapped[str] = mapped_column(String(255), default="")
    hardware: Mapped[dict] = mapped_column(JSON, default=dict)
    agent_version: Mapped[str] = mapped_column(String(30), default="")
    credential_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="offline", index=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(100), nullable=True)


class EnrollmentToken(Identified, Base):
    __tablename__ = "enrollment_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NodeMetric(Identified, Created, Base):
    __tablename__ = "node_metrics"
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    metrics: Mapped[dict] = mapped_column(JSON)


class SteamAccount(Identified, Created, Base):
    __tablename__ = "steam_accounts"
    steam_id: Mapped[str] = mapped_column(String(17), unique=True, index=True)
    nickname: Mapped[str | None] = mapped_column(String(200), nullable=True)
    profile_url: Mapped[str] = mapped_column(String(255))
    prime_status: Mapped[str] = mapped_column(String(20), default="unknown")
    inventory_visibility: Mapped[str] = mapped_column(String(20), default="unknown")
    assigned_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id"), nullable=True, index=True
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_groups.id"), nullable=True, index=True
    )
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    estimated_value: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 2), nullable=True
    )
    last_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_drop: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("prime_status IN ('unknown','confirmed','unconfirmed')"),
    )


class Job(Identified, Created, Base):
    __tablename__ = "jobs"
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("steam_accounts.id"), nullable=True, index=True
    )
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    __table_args__ = (
        CheckConstraint("max_attempts BETWEEN 1 AND 10"),
        CheckConstraint("kind IN ('test','steam','cs2')"),
    )


class Session(Identified, Base):
    __tablename__ = "sessions"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("steam_accounts.id"), nullable=True, index=True
    )
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="STARTING", index=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resource_usage: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index(
            "one_active_session_per_node",
            "node_id",
            unique=True,
            postgresql_where=text(
                "status IN ('STARTING','RUNNING','STOPPING','UNKNOWN')"
            ),
            sqlite_where=text("status IN ('STARTING','RUNNING','STOPPING','UNKNOWN')"),
        ),
    )


class JobAttempt(Identified, Base):
    __tablename__ = "job_attempts"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), unique=True)
    attempt: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Command(Identified, Created, Base):
    __tablename__ = "commands"
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id"), nullable=True, index=True
    )
    operation: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ack_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    ack: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Schedule(Identified, Base):
    __tablename__ = "schedules"
    name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    next_run: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    job_template: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("interval_seconds >= 60"),)


class Control(Base):
    __tablename__ = "control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    policy: Mapped[str] = mapped_column(String(20), default="priority")
    last_node_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class InventorySnapshot(Identified, Base):
    __tablename__ = "inventory_snapshots"
    account_id: Mapped[str] = mapped_column(ForeignKey("steam_accounts.id"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    total_value: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="complete")


class InventoryItem(Identified, Base):
    __tablename__ = "inventory_items"
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("inventory_snapshots.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(String(40))
    class_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(500))
    market_hash_name: Mapped[str] = mapped_column(String(500), default="")
    amount: Mapped[int] = mapped_column(Integer, default=1)
    tradable: Mapped[bool] = mapped_column(Boolean, default=False)
    marketable: Mapped[bool] = mapped_column(Boolean, default=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    __table_args__ = (UniqueConstraint("snapshot_id", "asset_id"),)


class InventoryChange(Identified, Base):
    __tablename__ = "inventory_changes"
    account_id: Mapped[str] = mapped_column(ForeignKey("steam_accounts.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("inventory_snapshots.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(String(40))
    change_type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(500))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class MarketPrice(Identified, Base):
    __tablename__ = "market_prices"
    market_hash_name: Mapped[str] = mapped_column(String(500), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    price: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    source: Mapped[str] = mapped_column(String(100), default="steam_community_market")


class WeeklyPeriod(Identified, Base):
    __tablename__ = "weekly_periods"
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text)
    __table_args__ = (CheckConstraint("ends_at > starts_at"),)


class DropRecord(Identified, Base):
    __tablename__ = "drop_records"
    account_id: Mapped[str] = mapped_column(ForeignKey("steam_accounts.id"), index=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("weekly_periods.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="NOT_RECORDED")
    item_name: Mapped[str] = mapped_column(String(500), default="")
    value: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    source: Mapped[str] = mapped_column(Text, default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        UniqueConstraint("account_id", "period_id"),
        CheckConstraint(
            "status IN ('NOT_RECORDED','PENDING_REVIEW','CONFIRMED','NOT_RECEIVED')"
        ),
    )


class Expense(Identified, Base):
    __tablename__ = "expenses"
    category: Mapped[str] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    description: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    __table_args__ = (
        CheckConstraint("amount >= 0"),
        CheckConstraint("currency = 'USD'"),
    )


class Revenue(Identified, Base):
    __tablename__ = "revenues"
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    fees: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    description: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    __table_args__ = (
        CheckConstraint("amount >= 0 AND fees >= 0 AND fees <= amount"),
        CheckConstraint("currency = 'USD'"),
    )


class Notification(Identified, Created, Base):
    __tablename__ = "notifications"
    event: Mapped[str] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class AuditLog(Identified, Created, Base):
    __tablename__ = "audit_logs"
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class AccountRisk(Base):
    __tablename__ = "account_risks"
    account_id: Mapped[str] = mapped_column(
        ForeignKey("steam_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    vac_banned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    community_banned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    game_bans: Mapped[int | None] = mapped_column(Integer, nullable=True)
    economy_ban: Mapped[str | None] = mapped_column(String(30), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    check_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    source: Mapped[str] = mapped_column(String(200), default="")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    auth_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id"), nullable=True
    )
    auth_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auth_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auth_source: Mapped[str] = mapped_column(String(200), default="")
    manual_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("game_bans IS NULL OR game_bans >= 0"),)


class RiskCheck(Identified, Base):
    __tablename__ = "risk_checks"
    account_id: Mapped[str] = mapped_column(
        ForeignKey("steam_accounts.id", ondelete="CASCADE"), index=True
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    status: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(200))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RiskIncident(Identified, Created, Base):
    __tablename__ = "risk_incidents"
    active_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )
    code: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("steam_accounts.id"), nullable=True, index=True
    )
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id"), nullable=True, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id"), nullable=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(200))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    acknowledged_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution: Mapped[str] = mapped_column(Text, default="")
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        CheckConstraint("severity IN ('LOW','MEDIUM','HIGH','CRITICAL')"),
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')"),
    )


class ProtectionPolicy(Base):
    __tablename__ = "protection_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    require_fresh_checks: Mapped[bool] = mapped_column(Boolean, default=False)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=21600)
    max_check_age_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    provider_status: Mapped[str] = mapped_column(String(30), default="NOT_CONFIGURED")
    provider_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("check_interval_seconds >= 300"),
        CheckConstraint("max_check_age_seconds >= 300"),
    )


Index(
    "one_active_session_per_account",
    Session.account_id,
    unique=True,
    postgresql_where=text(
        "account_id IS NOT NULL AND status IN ('STARTING','RUNNING','STOPPING','UNKNOWN')"
    ),
    sqlite_where=text(
        "account_id IS NOT NULL AND status IN ('STARTING','RUNNING','STOPPING','UNKNOWN')"
    ),
)


class SafetyPolicy(Base):
    __tablename__ = "safety_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cpu_temperature_limit: Mapped[float] = mapped_column(Float, default=95)
    gpu_temperature_limit: Mapped[float] = mapped_column(Float, default=90)
    ram_percent_limit: Mapped[float] = mapped_column(Float, default=97)
    min_ram_available_gb: Mapped[float] = mapped_column(Float, default=0.5)
    min_disk_free_gb: Mapped[float] = mapped_column(Float, default=0.5)
    cpu_percent_limit: Mapped[float] = mapped_column(Float, default=98)
    sustained_cpu_seconds: Mapped[int] = mapped_column(Integer, default=60)
    require_integrity_for_games: Mapped[bool] = mapped_column(Boolean, default=False)
    max_integrity_age_seconds: Mapped[int] = mapped_column(Integer, default=900)
    minimum_agent_version: Mapped[str] = mapped_column(String(30), default="")
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint("sustained_cpu_seconds BETWEEN 10 AND 3600"),
    )


class NodeSafety(Base):
    __tablename__ = "node_safety"
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(30), default="UNKNOWN", index=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cpu_high_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_integrity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    release_command_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        CheckConstraint(
            "state IN ('UNKNOWN','READY','WARNING','REVIEW_REQUIRED','RELEASING')"
        ),
    )


class IntegrityCheck(Identified, Base):
    __tablename__ = "integrity_checks"
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(100))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    report: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    __table_args__ = (
        CheckConstraint("status IN ('PASS','WARNING','FAILED','UNKNOWN')"),
    )


class SafetyDecision(Identified, Created, Base):
    __tablename__ = "safety_decisions"
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("risk_incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    node_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("steam_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100), default="safety-engine")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    deduplication_key: Mapped[str] = mapped_column(String(64), unique=True)


class ProtectionEngineState(Base):
    __tablename__ = "protection_engine_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_manager_check_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_cycle_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    nodes_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    accounts_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (CheckConstraint("id = 1"),)
