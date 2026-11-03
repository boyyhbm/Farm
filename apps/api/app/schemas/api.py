from datetime import datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Strict):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class UserCreate(Login):
    password: str = Field(min_length=14, max_length=1024)
    role: Literal["admin", "operator", "viewer"] = "viewer"


class AccountCreate(Strict):
    steam_id: str = Field(pattern=r"^7656119[0-9]{10}$")
    nickname: str | None = Field(default=None, max_length=200)
    notes: str = Field(default="", max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    assigned_node_id: str | None = None
    group_id: str | None = None
    prime_status: Literal["unknown", "confirmed", "unconfirmed"] = "unknown"


class AccountPatch(Strict):
    nickname: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)
    tags: list[str] | None = Field(default=None, max_length=30)
    assigned_node_id: str | None = None
    group_id: str | None = None
    prime_status: Literal["unknown", "confirmed", "unconfirmed"] | None = None
    archived: bool | None = None


class AccountBulk(Strict):
    ids: list[str] = Field(min_length=1, max_length=5000)
    changes: AccountPatch


class Enrollment(Strict):
    token: str = Field(min_length=20, max_length=200)
    hostname: str = Field(min_length=1, max_length=255)
    os_version: str = Field(default="", max_length=255)
    hardware: dict = Field(default_factory=dict)
    agent_version: str = Field(default="", max_length=30)


class JobPayload(Strict):
    duration_seconds: int = Field(default=60, ge=1, le=86400)
    seed: int = Field(default=1, ge=0, le=2**32 - 1)
    min_ram_gb: float = Field(default=0, ge=0, le=1024)
    max_cpu_percent: float = Field(default=95, ge=1, le=100)


class JobCreate(Strict):
    account_id: str | None = None
    node_id: str | None = None
    kind: Literal["test", "steam", "cs2"] = "test"
    priority: int = Field(default=0, ge=-100, le=100)
    payload: JobPayload = Field(default_factory=JobPayload)
    timeout_seconds: int = Field(default=300, ge=10, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def tz(cls, v):
        if v and not v.tzinfo:
            raise ValueError("Timezone required")
        return v


class ScheduleCreate(Strict):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    interval_seconds: int = Field(ge=60, le=31536000)
    next_run: datetime
    job_template: JobCreate

    @field_validator("next_run")
    @classmethod
    def tz(cls, v):
        if not v.tzinfo:
            raise ValueError("Timezone required")
        return v


class PeriodCreate(Strict):
    starts_at: datetime
    ends_at: datetime
    source: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def check(self):
        if not self.starts_at.tzinfo or not self.ends_at.tzinfo:
            raise ValueError("Timezone required")
        if self.ends_at <= self.starts_at:
            raise ValueError("Invalid period")
        if (self.ends_at - self.starts_at).total_seconds() != 604800:
            raise ValueError(
                "Weekly period must be seven days with a source-backed UTC reset"
            )
        return self


class DropCreate(Strict):
    account_id: str
    period_id: str
    status: Literal["NOT_RECORDED", "PENDING_REVIEW", "CONFIRMED", "NOT_RECEIVED"] = (
        "PENDING_REVIEW"
    )
    item_name: str = Field(default="", max_length=500)
    value: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: Literal["USD"] = "USD"
    source: str = Field(default="", max_length=5000)


class ExpenseCreate(Strict):
    category: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    currency: Literal["USD"] = "USD"
    description: str = Field(default="", max_length=5000)
    occurred_at: datetime


class RevenueCreate(Strict):
    amount: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    fees: Decimal = Field(default=Decimal(0), ge=0, max_digits=16, decimal_places=2)
    currency: Literal["USD"] = "USD"
    description: str = Field(default="", max_length=5000)
    occurred_at: datetime

    @model_validator(mode="after")
    def check(self):
        if self.fees > self.amount:
            raise ValueError("Fees exceed revenue")
        return self


class ControlPatch(Strict):
    policy: Literal["fifo", "priority", "round_robin"]
