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
from sqlalchemy.orm import DeclarativeBase
from datetime import timezone


class Base(DeclarativeBase):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid.uuid4())


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)


class Created:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


from sqlalchemy import Table, Column

for name in ("steam_accounts", "nodes", "jobs"):
    Table(name, Base.metadata, Column("id", String(36), primary_key=True))


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
