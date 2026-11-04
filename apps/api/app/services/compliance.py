"""Evidence-based operation policy. This never predicts VAC or drives official matches."""

from datetime import timedelta

from app.core.db import aware, utcnow
from app.models.models import AccountRisk, ProtectionPolicy
from fastapi import HTTPException

ACTIVE = ("STARTING", "RUNNING", "STOPPING", "UNKNOWN")
LEVELS = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


class ProtectionDenied(HTTPException):
    def __init__(self, code, account_id=None, node_id=None):
        self.code, self.account_id, self.node_id = code, account_id, node_id
        super().__init__(
            409,
            f"Protection: {code}. Review the account in Protection before starting Steam/CS2.",
        )


def policy(db):
    row = db.get(ProtectionPolicy, 1)
    if row is None:
        row = ProtectionPolicy(id=1)
        db.add(row)
        db.flush()
    return row


def confirmed_restriction(risk):
    return bool(
        risk
        and (
            risk.vac_banned is True
            or risk.community_banned is True
            or (risk.game_bans or 0) > 0
            or risk.economy_ban in {"banned", "probation"}
        )
    )


def decision(kind, account, risk, rules, node_id=None):
    if kind == "test":  # Independent synthetic engine, not an automated CS2 client.
        return None
    if kind not in {"steam", "cs2"}:
        return "OPERATION_NOT_ALLOWED"
    if not account or account.archived:
        return "ACTIVE_ACCOUNT_REQUIRED"
    if confirmed_restriction(risk):
        return "CONFIRMED_RESTRICTION"
    if risk and risk.manual_hold:
        return "MANUAL_HOLD"
    if risk and risk.auth_status == "FAILED":
        return "AUTHORIZATION_FAILED"
    if (
        not risk
        or risk.auth_status != "AUTHORIZED"
        or not risk.auth_expires_at
        or aware(risk.auth_expires_at) <= utcnow()
    ):
        return "MANUAL_AUTHORIZATION_REQUIRED"
    if node_id and node_id != risk.auth_node_id:
        return "AUTHORIZATION_NODE_MISMATCH"
    if rules.require_fresh_checks and (
        not risk.checked_at
        or aware(risk.checked_at)
        < utcnow() - timedelta(seconds=rules.max_check_age_seconds)
        or risk.check_status != "SUCCESS"
    ):
        return "FRESH_CHECK_REQUIRED"
    return None


def assert_operation(db, data, account):
    risk = db.get(AccountRisk, account.id) if account else None
    node_id = data.node_id or (account.assigned_node_id if account else None)
    reason = decision(data.kind, account, risk, policy(db), node_id)
    if reason:
        raise ProtectionDenied(reason, account.id if account else None, node_id)


def risk_summary(account, risk, rules):
    reasons = []

    def add(level, code, source, time=None):
        reasons.append(
            {"level": level, "code": code, "source": source, "observed_at": time}
        )

    if confirmed_restriction(risk):
        add("CRITICAL", "CONFIRMED_RESTRICTION", risk.source, risk.checked_at)
    if risk and risk.auth_status == "FAILED":
        add("HIGH", "AUTHORIZATION_FAILED", risk.auth_source, risk.auth_checked_at)
    if risk and risk.manual_hold:
        add("HIGH", "MANUAL_HOLD", "operator")
    if not risk or not risk.checked_at:
        add("MEDIUM", "NO_VERIFIED_BAN_DATA", "verification_gap")
    elif risk.check_status != "SUCCESS" or aware(
        risk.checked_at
    ) < utcnow() - timedelta(seconds=rules.max_check_age_seconds):
        add("MEDIUM", "STALE_OR_UNAVAILABLE_CHECK", risk.source, risk.checked_at)
    if (
        not risk
        or risk.auth_status != "AUTHORIZED"
        or not risk.auth_expires_at
        or aware(risk.auth_expires_at) <= utcnow()
    ):
        add("MEDIUM", "AUTHORIZATION_UNVERIFIED", "operator_confirmation_required")
    level = max((r["level"] for r in reasons), key=LEVELS.get, default="LOW")
    return {"level": level, "reasons": reasons, "prediction": False}
