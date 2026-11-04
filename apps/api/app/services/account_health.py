"""Derived account health from sourced observations, never a ban prediction."""

from datetime import timedelta

from app.core.config import settings
from app.core.db import aware, utcnow
from app.models.models import Node, NodeSafety, RiskIncident
from sqlalchemy import select

HEALTH_STATES = ("Healthy", "Warning", "Restricted", "Banned", "Unknown")


def context(db):
    from collections import defaultdict

    by_account = defaultdict(list)
    by_node = defaultdict(list)
    for event in db.scalars(
        select(RiskIncident)
        .where(RiskIncident.status != "RESOLVED")
        .order_by(RiskIncident.last_seen.desc())
    ):
        if event.account_id:
            by_account[event.account_id].append(event)
        if event.node_id:
            by_node[event.node_id].append(event)
    return {
        "nodes": {n.id: n for n in db.scalars(select(Node))},
        "safety": {s.node_id: s for s in db.scalars(select(NodeSafety))},
        "by_account": by_account,
        "by_node": by_node,
    }


def health(account, risk, rules, ctx):
    observations = []

    def add(code, status, source, time, explanation):
        observations.append(
            {
                "code": code,
                "status": status,
                "source": source,
                "checked_at": time,
                "explanation": explanation,
            }
        )

    if risk and (
        risk.vac_banned is True
        or risk.community_banned is True
        or (risk.game_bans or 0) > 0
    ):
        add(
            "CONFIRMED_BAN",
            "Banned",
            risk.source,
            risk.checked_at,
            "Official account-level ban evidence; a game ban count does not identify CS2",
        )
    if risk and risk.economy_ban in ("probation", "banned"):
        add(
            "ECONOMY_RESTRICTION",
            "Restricted",
            risk.source,
            risk.checked_at,
            "Economy restriction reported by GetPlayerBans",
        )
    if risk and risk.manual_hold:
        add(
            "MANUAL_HOLD",
            "Restricted",
            "operator_policy",
            risk.auth_checked_at,
            "Administrative hold; not an inferred Steam ban",
        )
    verified = bool(
        risk
        and risk.checked_at
        and risk.check_status == "SUCCESS"
        and aware(risk.checked_at)
        > utcnow() - timedelta(seconds=rules.max_check_age_seconds)
    )
    if not verified:
        add(
            "BAN_DATA_UNKNOWN_OR_STALE",
            "Unknown",
            risk.source if risk else "Steam Web API",
            risk.checked_at if risk else None,
            "No fresh successful ban observation is available; earlier evidence is retained",
        )
    else:
        add(
            "BAN_DATA_OBSERVED",
            "Healthy",
            risk.source,
            risk.checked_at,
            "Fresh official response; no promise about future restrictions",
        )
    authorized = bool(
        risk
        and risk.auth_status == "AUTHORIZED"
        and risk.auth_expires_at
        and aware(risk.auth_expires_at) > utcnow()
    )
    if not authorized:
        add(
            "AUTHORIZATION_" + (risk.auth_status if risk else "UNKNOWN"),
            "Warning" if risk and risk.auth_status == "FAILED" else "Unknown",
            risk.auth_source if risk else "operator",
            risk.auth_checked_at if risk else None,
            "Manual Steam sign-in attestation is missing, expired or failed",
        )
    node_id = account.assigned_node_id or (risk.auth_node_id if risk else None)
    node = ctx["nodes"].get(node_id)
    if not node:
        add(
            "NODE_UNASSIGNED",
            "Unknown",
            "account_assignment",
            None,
            "No assigned or manually attested node",
        )
    else:
        safety = ctx["safety"].get(node.id)
        if (
            node.revoked
            or node.status != "online"
            or not node.last_heartbeat
            or aware(node.last_heartbeat)
            < utcnow() - timedelta(seconds=settings().node_offline_seconds)
        ):
            add(
                "NODE_UNAVAILABLE",
                "Warning",
                "heartbeat",
                node.last_heartbeat,
                "Assigned node is unavailable; active sessions remain reserved until reconciliation",
            )
        if safety and safety.state in ("REVIEW_REQUIRED", "RELEASING"):
            add(
                "NODE_REVIEW_REQUIRED",
                "Warning",
                "safety_engine",
                safety.evaluated_at,
                "Node requires explicit review; automatic resumption is disabled",
            )
        if risk and authorized and risk.auth_node_id != node.id:
            add(
                "AUTHORIZATION_NODE_MISMATCH",
                "Warning",
                "operator_and_assignment",
                risk.auth_checked_at,
                "Attested sign-in is on a different node",
            )
    if account.last_sync:
        add(
            "INVENTORY_LAST_SYNC",
            "Healthy"
            if aware(account.last_sync) > utcnow() - timedelta(days=1)
            else "Warning",
            "steam_inventory",
            account.last_sync,
            "Last successful inventory snapshot",
        )
    else:
        add(
            "INVENTORY_UNKNOWN",
            "Unknown",
            "steam_inventory",
            None,
            "No successful inventory sync; privacy is not a trade ban",
        )
    relevant = {
        e.id: e
        for e in ctx["by_account"].get(account.id, []) + ctx["by_node"].get(node_id, [])
    }
    incidents = sorted(
        relevant.values(), key=lambda e: aware(e.last_seen), reverse=True
    )
    for event in incidents[:3]:
        add(event.code, "Warning", event.source, event.last_seen, event.recommendation)
    states = {o["status"] for o in observations}
    status = next(
        (s for s in ("Banned", "Restricted", "Warning", "Unknown") if s in states),
        "Healthy",
    )
    return {
        "status": status,
        "source": "protection_engine_v2_derived",
        "checked_at": utcnow(),
        "explanation": "Derived from the observations below; no hidden Valve checks or prediction",
        "observations": observations,
        "node_id": node_id,
        "latest_incident_id": incidents[0].id if incidents else None,
    }
