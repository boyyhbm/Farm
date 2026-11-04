"""Bounded, finite telemetry and integrity evidence; no arbitrary executable paths."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Finding(Strict):
    code: str = Field(max_length=80, pattern=r"^[A-Z0-9_]+$")
    status: Literal["PASS", "WARNING", "FAILED", "UNKNOWN"]
    component: str = Field(max_length=100, pattern=r"^[a-zA-Z0-9_. /-]+$")
    explanation: str = Field(max_length=300)


class IntegrityEvidence(Strict):
    status: Literal["PASS", "WARNING", "FAILED", "UNKNOWN"]
    checked_at: datetime
    source: Literal["agent_self_check", "manager_self_check"]
    baseline_trust: Literal[
        "unsigned_local_manifest", "publisher_signature", "not_available"
    ]
    files_checked: int = Field(ge=0, le=20000)
    findings: list[Finding] = Field(default_factory=list, max_length=30)
    game_files_status: Literal[
        "UNCHANGED_FROM_OPERATOR_BASELINE", "CHANGED", "UNKNOWN"
    ] = "UNKNOWN"


class LocalSafetyEvidence(Strict):
    held: bool
    reasons: list[str] = Field(default_factory=list, max_length=12)
    observed_at: datetime
    source: Literal["local_safety_guard"]


class SafetyPolicyInput(Strict):
    cpu_temperature_limit: float = Field(ge=60, le=110)
    gpu_temperature_limit: float = Field(ge=60, le=110)
    ram_percent_limit: float = Field(ge=80, le=99.9)
    min_ram_available_gb: float = Field(ge=0.1, le=128)
    min_disk_free_gb: float = Field(ge=0.1, le=1024)
    cpu_percent_limit: float = Field(ge=50, le=100)
    sustained_cpu_seconds: int = Field(ge=10, le=3600)
    require_integrity_for_games: bool
    max_integrity_age_seconds: int = Field(ge=300, le=86400)
    minimum_agent_version: str = Field(
        default="", pattern=r"^(?:[0-9]+\.[0-9]+\.[0-9]+)?$", max_length=30
    )
    confirmed: bool
    note: str = Field(min_length=5, max_length=1000)


class Confirm(Strict):
    confirmed: bool
    note: str = Field(min_length=5, max_length=1000)
