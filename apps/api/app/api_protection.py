# Depends defaults are FastAPI dependency declarations, never evaluated business calls.
# ruff: noqa: B008
"""Versioned, authenticated Protection API; no Steam secrets in responses or database."""

from collections import Counter
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, or_, select, update

from app.core.config import settings
from app.core.db import aware, get_db, utcnow
from app.core.security import admin, audit, authenticate, operator
from app.models.models import (
    AccountRisk,
    AuditLog,
    Node,
    RiskCheck,
    RiskIncident,
    SteamAccount,
)
from app.services import protection as service
from app.services.compliance import LEVELS, policy, risk_summary

router = APIRouter(prefix="/api/v1/protection", tags=["Protection"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyInput(Strict):
    api_key: SecretStr = Field(max_length=32)


class CheckInput(Strict):
    account_ids: list[str] = Field(default_factory=list, max_length=100)


class AuthInput(Strict):
    status: Literal["AUTHORIZED", "FAILED", "UNKNOWN"]
    node_id: str
    confirmed: bool
    note: str = Field(min_length=5, max_length=1000)
    expires_minutes: int = Field(default=720, ge=5, le=720)


class HoldInput(Strict):
    hold: bool
    confirmed: bool
    note: str = Field(min_length=5, max_length=1000)


class PolicyInput(Strict):
    require_fresh_checks: bool
    check_interval_seconds: int = Field(default=21600, ge=300, le=604800)
    max_check_age_seconds: int = Field(default=86400, ge=300, le=604800)


class ResponseInput(Strict):
    action: Literal["acknowledge", "resolve"]
    confirmed: bool
    note: str = Field(min_length=5, max_length=1000)


def serial(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def account(db, id):
    row = db.get(SteamAccount, id)
    if not row:
        raise HTTPException(404, "Account not found")
    return row


def account_view(a, r, rules):
    result = {
        "id": a.id,
        "steam_id": a.steam_id,
        "nickname": a.nickname,
        "assigned_node_id": a.assigned_node_id,
        "trade_restrictions": None,
        "trade_source": "Not exposed by GetPlayerBans; item tradability is not an account restriction",
        "game_ban_scope": "account-wide; CS2 attribution unavailable",
        "risk": risk_summary(a, r, rules),
    }
    if r:
        result.update(serial(r))
    else:
        result.update(
            {
                "check_status": "UNKNOWN",
                "checked_at": None,
                "attempted_at": None,
                "vac_banned": None,
                "community_banned": None,
                "game_bans": None,
                "economy_ban": None,
                "auth_status": "UNKNOWN",
                "source": None,
                "manual_hold": False,
            }
        )
    return result


@router.get("/overview")
def overview(user=Depends(authenticate), db=Depends(get_db)):
    rules = policy(db)
    accounts = list(
        db.scalars(select(SteamAccount).where(SteamAccount.archived == False))
    )
    risks = {r.account_id: r for r in db.scalars(select(AccountRisk))}
    counts = Counter(risk_summary(a, risks.get(a.id), rules)["level"] for a in accounts)
    open_rows = list(
        db.scalars(select(RiskIncident).where(RiskIncident.status != "RESOLVED"))
    )
    levels = [r.severity for r in open_rows] + list(counts)
    overall = max(levels, key=LEVELS.get) if levels else "UNKNOWN"
    cutoff = utcnow() - timedelta(hours=24)
    nodes = list(db.scalars(select(Node)))
    infrastructure_reasons = []
    for node in nodes:
        if node.revoked:
            continue
        if node.last_heartbeat and aware(node.last_heartbeat) < utcnow() - timedelta(
            seconds=settings().node_offline_seconds
        ):
            levels.append("HIGH")
            infrastructure_reasons.append(
                {
                    "code": "STALE_NODE_HEARTBEAT",
                    "node_id": node.id,
                    "source": "heartbeat",
                    "observed_at": node.last_heartbeat,
                }
            )
        elif (
            node.status != "online"
            or node.transport_security != "tls_at_listener_or_trusted_proxy"
        ):
            levels.append("MEDIUM")
            infrastructure_reasons.append(
                {
                    "code": "NODE_OR_TRANSPORT_UNVERIFIED",
                    "node_id": node.id,
                    "source": "listener",
                    "observed_at": node.transport_checked_at,
                }
            )
    overall = max(levels, key=LEVELS.get) if levels else "UNKNOWN"
    return {
        "observed_at": utcnow(),
        "level": overall,
        "infrastructure_reasons": infrastructure_reasons,
        "accounts": len(accounts),
        "risk_levels": dict(counts),
        "restricted_accounts": sum(
            service.confirmed_restriction(risks.get(a.id)) for a in accounts
        ),
        "verified_accounts": sum(
            bool(risks.get(a.id) and risks[a.id].checked_at) for a in accounts
        ),
        "open_incidents": len(open_rows),
        "failed_panel_logins_24h": db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "auth.failed", AuditLog.created_at >= cutoff
            )
        ),
        "sync_failures_24h": db.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "inventory.failed", AuditLog.created_at >= cutoff
            )
        ),
        "nodes": [
            {
                "id": n.id,
                "hostname": n.hostname,
                "status": n.status,
                "revoked": n.revoked,
                "transport_security": n.transport_security,
                "transport_checked_at": n.transport_checked_at,
                "last_heartbeat": n.last_heartbeat,
            }
            for n in nodes
        ],
        "provider": {
            "configured": bool(service.api_key()),
            "status": "CONFIGURATION_ERROR"
            if service.key_error()
            else rules.provider_status,
            "checked_at": rules.provider_checked_at,
            "retry_at": rules.provider_retry_at,
            "error": service.key_error() or rules.provider_error,
            "source": service.SOURCE,
        },
        "policy": serial(rules),
        "desktop": settings().deployment_mode == "desktop",
        "prediction_available": False,
    }


@router.get("/accounts")
def accounts(
    q: str = "",
    level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(authenticate),
    db=Depends(get_db),
):
    rules = policy(db)
    rows = db.execute(
        select(SteamAccount, AccountRisk)
        .outerjoin(AccountRisk, AccountRisk.account_id == SteamAccount.id)
        .where(
            SteamAccount.archived == False,
            or_(
                SteamAccount.steam_id.contains(q, autoescape=True),
                SteamAccount.nickname.contains(q, autoescape=True),
            ),
        )
        .order_by(SteamAccount.steam_id)
    ).all()
    views = [account_view(a, r, rules) for a, r in rows]
    if level:
        views = [r for r in views if r["risk"]["level"] == level]
    return {
        "items": views[(page - 1) * page_size : page * page_size],
        "total": len(views),
        "page": page,
        "page_size": page_size,
    }


@router.get("/accounts/{id}")
def detail(
    id: str, page: int = Query(1, ge=1), user=Depends(authenticate), db=Depends(get_db)
):
    a = account(db, id)
    return {
        **account_view(a, db.get(AccountRisk, id), policy(db)),
        "history": [
            serial(r)
            for r in db.scalars(
                select(RiskCheck)
                .where(RiskCheck.account_id == id)
                .order_by(RiskCheck.checked_at.desc())
                .offset((page - 1) * 50)
                .limit(50)
            )
        ],
    }


@router.post("/check", status_code=202)
def queue_checks(data: CheckInput, user=Depends(operator), db=Depends(get_db)):
    for id in data.account_ids:
        account(db, id)
    if settings().deployment_mode == "desktop":
        from app.core.local_store import LocalStore

        task = LocalStore().enqueue(
            "protection", {"account_ids": data.account_ids or None}
        )
    else:
        from app.workers.tasks import protection_check

        task = protection_check.apply_async(
            args=[data.account_ids or None], queue="protection"
        ).id
    audit(
        db,
        user.username,
        "protection.check_queued",
        str(task),
        {"accounts": data.account_ids},
    )
    db.commit()
    return {"status": "QUEUED", "task_id": task}


@router.post("/key")
def save_key(data: KeyInput, user=Depends(admin), db=Depends(get_db)):
    try:
        service.configure_key(db, data.api_key.get_secret_value())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    audit(
        db,
        user.username,
        "protection.key_configured",
        details={"configured": bool(data.api_key.get_secret_value())},
    )
    db.commit()
    return {"configured": bool(service.api_key())}


@router.patch("/policy")
def save_policy(data: PolicyInput, user=Depends(admin), db=Depends(get_db)):
    rules = policy(db)
    for k, v in data.model_dump().items():
        setattr(rules, k, v)
    service.enforce(db)
    audit(db, user.username, "protection.policy", details=data.model_dump())
    db.commit()
    return serial(rules)


@router.post("/accounts/{id}/authorization")
def authorization(id: str, data: AuthInput, user=Depends(operator), db=Depends(get_db)):
    account(db, id)
    if not data.confirmed:
        raise HTTPException(400, "Explicit operator confirmation required")
    node = db.get(Node, data.node_id)
    if not node or node.revoked:
        raise HTTPException(400, "Active registered node required")
    r = service.get_risk(db, id)
    now = utcnow()
    if data.status == "AUTHORIZED":
        db.execute(
            update(AccountRisk)
            .where(
                AccountRisk.auth_node_id == node.id,
                AccountRisk.account_id != id,
                AccountRisk.auth_status == "AUTHORIZED",
            )
            .values(
                auth_status="UNKNOWN",
                auth_expires_at=None,
                auth_checked_at=now,
                auth_source="operator:" + user.username,
            )
        )
    r.auth_status = data.status
    r.auth_node_id = node.id
    r.auth_checked_at = now
    r.auth_source = "operator:" + user.username
    r.auth_expires_at = (
        now + timedelta(minutes=data.expires_minutes)
        if data.status == "AUTHORIZED"
        else None
    )
    note = service.safe_message(data.note)
    db.add(
        RiskCheck(
            account_id=id,
            status="AUTH_" + data.status,
            source=r.auth_source,
            evidence={
                "node_id": node.id,
                "note": note,
                "expires_at": r.auth_expires_at.isoformat()
                if r.auth_expires_at
                else None,
            },
        )
    )
    if data.status == "FAILED":
        service.incident(
            db,
            "AUTHORIZATION_FAILED",
            "HIGH",
            account_id=id,
            node_id=node.id,
            source=r.auth_source,
            evidence={"note": note},
            occurrence=True,
        )
    elif data.status == "AUTHORIZED":
        db.execute(
            update(RiskIncident)
            .where(
                RiskIncident.account_id == id,
                RiskIncident.code.in_(
                    (
                        "AUTHORIZATION_FAILED",
                        "MANUAL_AUTHORIZATION_REQUIRED",
                        "AUTHORIZATION_NODE_MISMATCH",
                    )
                ),
                RiskIncident.status != "RESOLVED",
            )
            .values(
                status="RESOLVED",
                active_key=None,
                resolved_at=now,
                resolution="New operator authorization",
            )
        )
    service.enforce(db)
    audit(
        db,
        user.username,
        "protection.authorization",
        id,
        {"status": data.status, "node_id": node.id, "note": note},
    )
    db.commit()
    return account_view(account(db, id), r, policy(db))


@router.post("/accounts/{id}/hold")
def hold(id: str, data: HoldInput, user=Depends(admin), db=Depends(get_db)):
    account(db, id)
    if not data.confirmed:
        raise HTTPException(400, "Explicit confirmation required")
    r = service.get_risk(db, id)
    r.manual_hold = data.hold
    if data.hold:
        service.incident(
            db,
            "MANUAL_HOLD",
            "HIGH",
            account_id=id,
            source="operator:" + user.username,
            evidence={"note": service.safe_message(data.note)},
        )
    else:
        service.resolve_condition(db, "MANUAL_HOLD", account_id=id)
    service.enforce(db)
    audit(
        db,
        user.username,
        "protection.hold",
        id,
        {"hold": data.hold, "note": service.safe_message(data.note)},
    )
    db.commit()
    return account_view(account(db, id), r, policy(db))


@router.get("/incidents")
def incidents(
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(authenticate),
    db=Depends(get_db),
):
    query = select(RiskIncident)
    if status:
        query = query.where(RiskIncident.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {
        "items": [
            serial(r)
            for r in db.scalars(
                query.order_by(RiskIncident.last_seen.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/incidents/{id}/respond")
def respond(id: str, data: ResponseInput, user=Depends(operator), db=Depends(get_db)):
    row = db.get(RiskIncident, id)
    if not row:
        raise HTTPException(404, "Incident not found")
    if not data.confirmed:
        raise HTTPException(400, "Explicit confirmation required")
    if data.action == "resolve" and user.role != "admin":
        raise HTTPException(403, "Administrator role required to resolve incidents")
    if row.status == "RESOLVED":
        return serial(row)
    if data.action == "resolve":
        # Closing a case does not clear source evidence or lift enforcement.
        row.status = "RESOLVED"
        row.active_key = None
        row.resolved_at = utcnow()
        row.resolution = service.safe_message(data.note)
    else:
        row.status = "ACKNOWLEDGED"
        row.acknowledged_by = user.username
        row.acknowledged_at = utcnow()
    audit(
        db,
        user.username,
        "protection.incident_" + data.action,
        id,
        {"note": service.safe_message(data.note)},
    )
    db.commit()
    return serial(row)


@router.post("/provider/retry")
def provider_retry(data: HoldInput, user=Depends(admin), db=Depends(get_db)):
    if not data.confirmed:
        raise HTTPException(400, "Explicit confirmation required")
    rules = policy(db)
    rules.provider_retry_at = None
    rules.provider_status = "READY" if service.api_key() else "NOT_CONFIGURED"
    audit(
        db,
        user.username,
        "protection.provider_retry",
        details={"note": service.safe_message(data.note)},
    )
    db.commit()
    return {"status": rules.provider_status}
