"""Immutable schema snapshot for migration 0004."""

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String,
    Integer,
    Float,
    Boolean,
    JSON,
    DateTime,
    ForeignKey,
    CheckConstraint,
    Table,
    Column,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def uid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)


class Created:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


for name in ("nodes", "jobs", "steam_accounts", "risk_incidents"):
    Table(name, Base.metadata, Column("id", String(36), primary_key=True))


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
    last_cycle_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    nodes_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    accounts_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    __table_args__ = (CheckConstraint("id = 1"),)
