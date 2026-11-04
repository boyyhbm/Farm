"""Protection 2.0 GUI/API: durable node review, health, history, and report export."""

# ruff: noqa: B008
import csv
import io
import json
from collections import Counter
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from app.api_protection import account_view, serial
from app.core.db import aware, get_db, utcnow
from app.core.security import admin, audit, authenticate, operator
from app.models.models import (
    AccountRisk,
    IntegrityCheck,
    Job,
    Node,
    NodeSafety,
    ProtectionEngineState,
    SafetyDecision,
    SteamAccount,
)
from app.schemas.safety import Confirm, SafetyPolicyInput
from app.services import safety
from app.services.account_health import context, health
from app.services.compliance import policy

router = APIRouter(prefix="/api/v1/protection", tags=["Protection Engine 2.0"])


def node(db, id):
    value = db.get(Node, id)
    if not value or value.revoked:
        raise HTTPException(404, "Active node not found")
    return value


def confirm(data):
    if not data.confirmed:
        raise HTTPException(
            400, "Explicit administrator/operator confirmation required"
        )


def page_result(db, query, page, page_size, mapper=serial):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {
        "items": [
            mapper(r)
            for r in db.scalars(query.offset((page - 1) * page_size).limit(page_size))
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/safety/status")
def status(user=Depends(authenticate), db=Depends(get_db)):
    engine = db.get(ProtectionEngineState, 1)
    live = bool(
        engine
        and engine.last_cycle_at
        and aware(engine.last_cycle_at) > utcnow() - timedelta(seconds=30)
        and not engine.last_error
    )
    held = db.scalar(
        select(func.count(NodeSafety.node_id)).where(
            NodeSafety.state.in_(("REVIEW_REQUIRED", "RELEASING"))
        )
    )
    latest = safety.latest_manager_check(db)
    return {
        "version": "2.0",
        "service": {
            "status": "RUNNING"
            if live
            else "UNKNOWN"
            if not engine or not engine.last_cycle_at
            else "STALE",
            "source": "persistent_engine_heartbeat",
            "checked_at": engine.last_cycle_at if engine else None,
            "error": engine.last_error if engine else None,
        },
        "engine": serial(engine) if engine else None,
        "nodes_requiring_review": held,
        "queue": dict(
            db.execute(
                select(Job.status, func.count(Job.id)).group_by(Job.status)
            ).all()
        ),
        "policy": serial(safety.get_policy(db)),
        "manager_integrity": serial(latest) if latest else None,
        "safe_auto_resume": False,
    }


@router.post("/safety/check-manager")
def check_manager(data: Confirm, user=Depends(admin), db=Depends(get_db)):
    confirm(data)
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text

        if not db.scalar(text("SELECT pg_try_advisory_xact_lock(73294021)")):
            raise HTTPException(
                409, "Safety cycle is in progress; retry the integrity check shortly"
            )
    result = safety.inspect_manager(db, force=True)
    audit(
        db,
        user.username,
        "safety.manager_integrity_check",
        result.id,
        {"note": safety.safe_message(data.note)},
    )
    db.commit()
    return serial(result)


@router.patch("/safety/policy")
def save_policy(data: SafetyPolicyInput, user=Depends(admin), db=Depends(get_db)):
    confirm(data)
    row = safety.get_policy(db)
    for key, value in data.model_dump(exclude={"confirmed", "note"}).items():
        setattr(row, key, value)
    audit(
        db,
        user.username,
        "safety.policy",
        details={
            "policy": data.model_dump(exclude={"note"}),
            "note": safety.safe_message(data.note),
        },
    )
    safety.cycle(db)
    db.commit()
    return serial(row)


@router.get("/nodes/safety")
def nodes(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=500),
    state: Literal["UNKNOWN", "READY", "WARNING", "REVIEW_REQUIRED", "RELEASING"]
    | None = None,
    user=Depends(authenticate),
    db=Depends(get_db),
):
    query = (
        select(Node)
        .where(Node.revoked == False, Node.hostname.contains(q, autoescape=True))
        .order_by(Node.hostname)
    )
    states = {s.node_id: s for s in db.scalars(select(NodeSafety))}
    checks = {
        c.id: c
        for c in db.scalars(
            select(IntegrityCheck).where(
                IntegrityCheck.id.in_(
                    [
                        s.last_integrity_id
                        for s in states.values()
                        if s.last_integrity_id
                    ]
                )
            )
        )
    }
    items = []
    for n in db.scalars(query):
        s = states.get(n.id)
        value = {
            "id": n.id,
            "hostname": n.hostname,
            "agent_version": n.agent_version,
            "connection": n.status,
            "last_heartbeat": n.last_heartbeat,
            "transport_security": n.transport_security,
            "metrics": n.metrics,
            "last_error": safety.safe_message(n.last_error) if n.last_error else None,
            "safety": serial(s) if s else {"state": "UNKNOWN", "reasons": []},
            "integrity": serial(checks[s.last_integrity_id])
            if s and s.last_integrity_id in checks
            else None,
        }
        if not state or value["safety"]["state"] == state:
            items.append(value)
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


@router.post("/nodes/{id}/integrity-check", status_code=202)
def inspect_node(id: str, data: Confirm, user=Depends(operator), db=Depends(get_db)):
    confirm(data)
    n = node(db, id)
    cmd = safety.command(db, n, "integrity_check")
    audit(
        db,
        user.username,
        "safety.integrity_requested",
        id,
        {"command_id": cmd.id, "note": safety.safe_message(data.note)},
    )
    db.commit()
    return {
        "command_id": cmd.id,
        "status": "QUEUED",
        "message": "The node must acknowledge the actual file check; offline requests remain pending until their deadline",
    }


@router.post("/nodes/{id}/game-baseline", status_code=202)
def capture_baseline(id: str, data: Confirm, user=Depends(admin), db=Depends(get_db)):
    confirm(data)
    n = node(db, id)
    cmd = safety.command(db, n, "game_baseline_capture", {"confirmed": True})
    safety.record_decision(
        db,
        "CAPTURE_GAME_BASELINE",
        "OPERATOR_CONFIRMED_STEAM_VERIFICATION",
        node_id=id,
        source="operator",
        actor=user.username,
        evidence={"note": safety.safe_message(data.note)},
        key="baseline:" + cmd.id,
    )
    db.commit()
    return {"command_id": cmd.id, "status": "QUEUED"}


@router.post("/nodes/{id}/repair-confirmation", status_code=202)
def confirm_repair(id: str, data: Confirm, user=Depends(admin), db=Depends(get_db)):
    confirm(data)
    n = node(db, id)
    cmd = safety.command(db, n, "repair_acknowledge", {"confirmed": True})
    safety.record_decision(
        db,
        "ACKNOWLEDGE_REPAIR",
        "ADMIN_REPAIR_REVIEW",
        node_id=id,
        source="operator",
        actor=user.username,
        evidence={"note": safety.safe_message(data.note)},
        key="repair:" + cmd.id,
    )
    db.commit()
    return {"command_id": cmd.id, "status": "QUEUED"}


@router.post("/nodes/{id}/hold", status_code=202)
def hold_node(id: str, data: Confirm, user=Depends(admin), db=Depends(get_db)):
    confirm(data)
    n = node(db, id)
    safety.quarantine(
        db,
        n,
        safety.state(db, id),
        ["LOCAL_SAFETY_HOLD"],
        source="operator",
        actor=user.username,
    )
    audit(
        db,
        user.username,
        "safety.node_hold",
        id,
        {"note": safety.safe_message(data.note)},
    )
    db.commit()
    return serial(safety.state(db, id))


@router.post("/nodes/{id}/release", status_code=202)
def release(id: str, data: Confirm, user=Depends(admin), db=Depends(get_db)):
    confirm(data)
    row = safety.request_release(db, node(db, id), user.username, data.note)
    db.commit()
    return serial(row)


@router.get("/health/accounts")
def accounts(
    q: str = "",
    health_status: Literal["Healthy", "Warning", "Restricted", "Banned", "Unknown"]
    | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(authenticate),
    db=Depends(get_db),
):
    rules = policy(db)
    ctx = context(db)
    query = (
        select(SteamAccount, AccountRisk)
        .outerjoin(AccountRisk, AccountRisk.account_id == SteamAccount.id)
        .where(
            SteamAccount.archived == False,
            SteamAccount.steam_id.contains(q, autoescape=True)
            | SteamAccount.nickname.contains(q, autoescape=True),
        )
        .order_by(SteamAccount.steam_id)
    )
    rows = []
    counts = Counter()
    for a, r in db.execute(query):
        value = account_view(a, r, rules)
        value["health"] = health(a, r, rules, ctx)
        counts[value["health"]["status"]] += 1
        if not health_status or value["health"]["status"] == health_status:
            rows.append(value)
    return {
        "items": rows[(page - 1) * page_size : page * page_size],
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "health_counts": dict(counts),
    }


@router.get("/decisions")
def decisions(
    q: str = "",
    node_id: str | None = None,
    action: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user=Depends(authenticate),
    db=Depends(get_db),
):
    query = (
        select(SafetyDecision)
        .where(
            SafetyDecision.reason.contains(q, autoescape=True)
            | SafetyDecision.action.contains(q, autoescape=True)
        )
        .order_by(SafetyDecision.created_at.desc())
    )
    if node_id:
        query = query.where(SafetyDecision.node_id == node_id)
    if action:
        query = query.where(SafetyDecision.action == action)
    return page_result(db, query, page, page_size)


@router.get("/integrity")
def integrity(
    node_id: str | None = None,
    result: Literal["PASS", "WARNING", "FAILED", "UNKNOWN"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=500),
    user=Depends(authenticate),
    db=Depends(get_db),
):
    query = select(IntegrityCheck).order_by(IntegrityCheck.received_at.desc())
    if node_id:
        query = query.where(IntegrityCheck.node_id == node_id)
    if result:
        query = query.where(IntegrityCheck.status == result)
    return page_result(db, query, page, page_size)


@router.get("/reports/export")
def export(
    kind: Literal["health", "decisions", "integrity", "nodes"] = "health",
    format: Literal["json", "csv"] = "json",
    q: str = "",
    node_id: str | None = None,
    user=Depends(authenticate),
    db=Depends(get_db),
):
    if kind == "health":
        # Export all registry pages with the same health definition as the GUI.
        rules = policy(db)
        ctx = context(db)
        query = (
            select(SteamAccount, AccountRisk)
            .outerjoin(AccountRisk, AccountRisk.account_id == SteamAccount.id)
            .where(
                SteamAccount.archived == False,
                SteamAccount.steam_id.contains(q, autoescape=True)
                | SteamAccount.nickname.contains(q, autoescape=True),
            )
        )
        rows = [
            {"account_id": a.id, "steam_id": a.steam_id, **health(a, r, rules, ctx)}
            for a, r in db.execute(query)
        ]
    elif kind == "nodes":
        rows = nodes(q=q, state=None, page=1, page_size=50000, user=user, db=db)[
            "items"
        ]
    else:
        model = SafetyDecision if kind == "decisions" else IntegrityCheck
        query = select(model).order_by(
            model.created_at.desc() if kind == "decisions" else model.checked_at.desc()
        )
        if node_id:
            query = query.where(model.node_id == node_id)
        if q and kind == "decisions":
            query = query.where(
                model.reason.contains(q, autoescape=True)
                | model.action.contains(q, autoescape=True)
            )
        rows = [serial(r) for r in db.scalars(query.limit(50000))]
    encoded = jsonable_encoder(rows)
    if format == "json":
        body = json.dumps(
            {
                "generated_at": utcnow().isoformat(),
                "source": "Protection Engine 2.0",
                "kind": kind,
                "items": encoded,
            },
            ensure_ascii=False,
        )
        media = "application/json"
    else:
        output = io.StringIO()
        keys = sorted({key for row in encoded for key in row})
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        for row in encoded:
            safe = {
                k: json.dumps(v, ensure_ascii=False)
                if isinstance(v, (dict, list))
                else v
                for k, v in row.items()
            }
            # Spreadsheet formula injection must not turn an exported hostname or note into code.
            writer.writerow(
                {
                    k: "'" + v
                    if isinstance(v, str)
                    and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                    else v
                    for k, v in safe.items()
                }
            )
        body = "\ufeff" + output.getvalue()
        media = "text/csv"
    audit(
        db, user.username, "safety.export", kind, {"format": format, "rows": len(rows)}
    )
    db.commit()
    return Response(
        body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="farm-protection-{kind}.{format}"',
            "Cache-Control": "no-store",
        },
    )
