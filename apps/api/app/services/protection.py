"""Steam evidence, incident outbox and bounded safe responses for the existing farm."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from pathlib import Path

import httpx
from app.core.config import settings
from app.core.db import aware, utcnow
from app.core.security import audit
from app.models.models import (
    AccountRisk,
    Job,
    RiskCheck,
    RiskIncident,
    Session,
    SteamAccount,
    uid,
)
from app.services.compliance import (
    ACTIVE,
    confirmed_restriction,
    decision,
    policy,
)
from app.services.notifications import _safe_message, notify
from sqlalchemy import or_, select, update

SOURCE = "Steam Web API ISteamUser/GetPlayerBans/v1"
ENDPOINT = "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"
REMEDIES = {
    "CONFIRMED_RESTRICTION": "Review the official Steam account notices. Do not attempt to bypass restrictions. Game ban counts are account-wide, not proof of a CS2-specific ban.",
    "AUTHORIZATION_FAILED": "Sign in manually using the official Steam client on the assigned node, verify SteamID, then record a new operator confirmation.",
    "MANUAL_AUTHORIZATION_REQUIRED": "Verify the account manually in official Steam on the selected node and confirm it in Protection.",
    "MANUAL_HOLD": "Investigate the incident; only an administrator can remove this hold.",
    "NODE_OFFLINE": "Restore the node connection. UNKNOWN sessions remain reserved until process reconciliation.",
    "STEAM_RATE_LIMIT": "Wait until Retry-After expires. All Steam request workers share the cooldown; do not rotate proxies.",
    "STEAM_API_AUTH_FAILED": "Replace the Steam Web API key in Protection. This is an integration credential error, not a Steam-account ban or sign-out.",
    "STEAM_API_UNAVAILABLE": "Retry after the indicated interval. Previously confirmed evidence is preserved.",
    "STEAM_SYNC_FAILED": "Review inventory visibility and Steam availability. A private inventory is not an account trade ban.",
    "STEAM_PROCESS_ERROR": "Review managed process diagnostics and verify the official client manually; process errors do not confirm a ban.",
    "POLICY_VIOLATION": "Use only synthetic tests or explicitly permitted manually authorized Steam/CS2 sessions. Automated XP/reward collection and protection bypass are unsupported.",
    "PANEL_AUTH_FAILURE": "Review panel access and change the panel password if these attempts are unexpected.",
    "AGENT_AUTH_FAILURE": "Check whether the node credential was revoked. Re-enroll through the GUI if necessary.",
}


def safe_message(value):
    result = _safe_message(value)
    key = api_key()
    return result.replace(key, "[redacted]") if key else result


def incident(
    db,
    code,
    severity="MEDIUM",
    *,
    account_id=None,
    node_id=None,
    job_id=None,
    source="controller",
    evidence=None,
    occurrence=False,
):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    key = hashlib.sha256(f"{code}:{account_id}:{node_id}:{job_id}".encode()).hexdigest()
    existing = db.scalar(select(RiskIncident).where(RiskIncident.active_key == key))
    if existing and not occurrence:
        return existing
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    now = utcnow()
    statement = insert(RiskIncident).values(
        id=uid(),
        active_key=key,
        code=code,
        severity=severity,
        status="OPEN",
        account_id=account_id,
        node_id=node_id,
        job_id=job_id,
        summary=code.replace("_", " "),
        recommendation=REMEDIES.get(
            code, "Review the sourced evidence and restore permitted operation."
        ),
        source=source,
        evidence=evidence or {},
        occurrences=1,
        created_at=now,
        last_seen=now,
        resolution="",
    )
    statement = statement.on_conflict_do_update(
        index_elements=["active_key"],
        set_={
            "last_seen": now,
            "occurrences": RiskIncident.occurrences + 1,
            "evidence": evidence or {},
        },
    ).returning(RiskIncident.id)
    id = db.scalar(statement)
    db.flush()
    if not existing:
        audit(
            db,
            "protection",
            "protection.incident",
            id,
            {"code": code, "account_id": account_id, "node_id": node_id},
        )
        notify(
            db,
            "protection_incident",
            f"{severity}: {code}; account={account_id or '—'}; node={node_id or '—'}. {REMEDIES.get(code, 'Review Protection.')}",
            {
                "MEDIUM": "warning",
                "HIGH": "error",
                "CRITICAL": "critical",
                "LOW": "info",
            }[severity],
        )
    return db.get(RiskIncident, id, populate_existing=True)


def resolve_condition(db, code, account_id=None, node_id=None):
    db.execute(
        update(RiskIncident)
        .where(
            RiskIncident.code == code,
            RiskIncident.account_id == account_id,
            RiskIncident.node_id == node_id,
            RiskIncident.status != "RESOLVED",
        )
        .values(
            status="RESOLVED",
            active_key=None,
            resolved_at=utcnow(),
            resolution="Underlying condition cleared by a new sourced observation",
        )
    )


def get_risk(db, id):
    risk = db.get(AccountRisk, id)
    if not risk:
        risk = AccountRisk(account_id=id)
        db.add(risk)
        db.flush()
    return risk


_credential_error = None


def key_error():
    return _credential_error


def api_key():
    global _credential_error
    _credential_error = None
    if settings().deployment_mode == "desktop":
        file = Path(settings().desktop_data_dir) / "protection-secrets.dat"
        if file.exists():
            from security.credentials import read_credentials

            try:
                value = read_credentials(file).get("steam_api_key", "")
                if not isinstance(value, str):
                    raise TypeError("Invalid protected credential format")
                return value
            except Exception:  # noqa: BLE001 - DPAPI raises native errors; never expose encrypted bytes or disable safety logging
                _credential_error = "Protected Steam API credentials cannot be read. Re-enter the key using Protection; other Windows identities cannot decrypt this DPAPI store."
                return ""
    return settings().steam_api_key.get_secret_value()


def configure_key(db, value):
    if settings().deployment_mode != "desktop":
        raise ValueError(
            "Server deployments configure STEAM_API_KEY through the deployment secret manager"
        )
    if value and not re.fullmatch("[A-Fa-f0-9]{32}", value):
        raise ValueError("Steam Web API key must contain 32 hexadecimal characters")
    from security.credentials import write_credentials

    write_credentials(
        Path(settings().desktop_data_dir) / "protection-secrets.dat",
        {"steam_api_key": value},
    )
    rules = policy(db)
    rules.provider_status = "READY" if value else "NOT_CONFIGURED"
    rules.provider_retry_at = None
    rules.provider_error = None
    db.execute(update(AccountRisk).values(next_check_at=utcnow()))


def parse_players(data, requested):
    if not isinstance(data, dict) or not isinstance(data.get("players"), list):
        raise ValueError("Invalid Steam response")  # noqa: TRY004 - external protocol validation
    result = {}
    for row in data["players"]:
        if (
            not isinstance(row, dict)
            or row.get("SteamId") not in requested
            or row["SteamId"] in result
        ):
            raise ValueError("Unexpected or duplicate SteamID in response")
        if (
            type(row.get("VACBanned")) is not bool
            or type(row.get("CommunityBanned")) is not bool
            or type(row.get("NumberOfGameBans")) is not int
            or not 0 <= row["NumberOfGameBans"] <= 1000000
        ):
            raise ValueError("Invalid ban evidence types")
        result[row["SteamId"]] = {
            "vac_banned": row["VACBanned"],
            "community_banned": row["CommunityBanned"],
            "game_bans": row["NumberOfGameBans"],
            "economy_ban": row.get("EconomyBan")
            if row.get("EconomyBan") in {"none", "probation", "banned"}
            else None,
        }
    if set(result) != set(requested):
        raise ValueError("Incomplete Steam response; previous evidence preserved")
    return result


def _check_accounts(db, ids=None, *, transport=None):
    """At most 100 accounts per request, no production mock adapter or inferred statuses."""
    from app.services.inventory import InventoryUnavailable, _cooldown, _rate_limit

    rules = policy(db)
    now = utcnow()
    if rules.provider_status == "AUTH_FAILED" or (
        rules.provider_retry_at and aware(rules.provider_retry_at) > now
    ):
        return {"status": rules.provider_status, "checked": 0}
    query = (
        select(SteamAccount)
        .outerjoin(AccountRisk, AccountRisk.account_id == SteamAccount.id)
        .where(SteamAccount.archived == False)
    )
    if ids is not None:
        query = query.where(SteamAccount.id.in_(ids[:100]))
    else:
        query = query.where(
            or_(AccountRisk.next_check_at.is_(None), AccountRisk.next_check_at <= now)
        )
    accounts = list(
        db.scalars(
            query.order_by(
                AccountRisk.attempted_at.asc().nullsfirst(), SteamAccount.id
            ).limit(100)
        )
    )
    if not accounts:
        return {"status": "idle", "checked": 0}
    key = api_key()
    if not key and (ids is None or key_error()):
        rules.provider_status = (
            "CONFIGURATION_ERROR" if key_error() else "NOT_CONFIGURED"
        )
        rules.provider_error = key_error()
        if key_error():
            incident(
                db,
                "STEAM_API_CONFIGURATION_ERROR",
                "HIGH",
                source="protected_credential_store",
                evidence={
                    "code": "DPAPI_STORE_UNREADABLE",
                    "remedy": "Re-enter the API key using the graphical Protection settings",
                },
            )
        return {"status": rules.provider_status, "checked": 0}
    # Shared atomic request slot across replicas and inventory workers.
    try:
        _rate_limit("protection", 2)
    except InventoryUnavailable as exc:
        return {"status": "COOLDOWN", "checked": 0, "retry_after": exc.retry_after}
    code = None
    error = None
    status = "SUCCESS"
    retry = 300
    parsed = None
    try:
        if not key:
            status = "NOT_CONFIGURED"
            error = "Steam Web API key is not configured"
        else:
            with httpx.Client(
                timeout=15, follow_redirects=False, transport=transport
            ) as client:
                response = client.get(
                    ENDPOINT,
                    params={"steamids": ",".join(a.steam_id for a in accounts)},
                    headers={"x-webapi-key": key},
                )
            if response.status_code == 429:
                raw = response.headers.get("retry-after", "60")
                retry = min(86400, max(1, int(raw))) if raw.isdigit() else 60
                _cooldown(retry)
                status = "RATE_LIMITED"
                code = "STEAM_RATE_LIMIT"
                error = "Steam HTTP 429"
            elif response.status_code in (401, 403):
                status = "AUTH_FAILED"
                code = "STEAM_API_AUTH_FAILED"
                error = f"Steam Web API HTTP {response.status_code}"
            elif response.status_code != 200:
                status = "UNAVAILABLE"
                code = "STEAM_API_UNAVAILABLE"
                error = f"Steam Web API HTTP {response.status_code}"
            elif len(response.content) > 2 * 1024 * 1024:
                raise ValueError("Oversized Steam response")
            else:
                parsed = parse_players(response.json(), {a.steam_id for a in accounts})
    except (httpx.HTTPError, ValueError) as exc:
        status = "UNAVAILABLE"
        code = "STEAM_API_UNAVAILABLE"
        error = f"Steam response unavailable: {type(exc).__name__}"
    # Do not hold SQLite's writer lock during the external HTTP request.
    rows = [get_risk(db, a.id) for a in accounts]
    rules.provider_status = status
    rules.provider_checked_at = now
    rules.provider_error = error
    rules.provider_retry_at = (
        now + timedelta(seconds=retry)
        if status not in {"SUCCESS", "AUTH_FAILED"}
        else None
    )
    if code:
        incident(
            db,
            code,
            "HIGH" if status == "AUTH_FAILED" else "MEDIUM",
            source=SOURCE,
            evidence={
                "http_status": response.status_code if "response" in locals() else None
            },
            occurrence=True,
        )
    if status == "SUCCESS":
        for event in (
            "STEAM_RATE_LIMIT",
            "STEAM_API_AUTH_FAILED",
            "STEAM_API_UNAVAILABLE",
        ):
            resolve_condition(db, event)
    for account, risk in zip(accounts, rows, strict=True):
        risk.attempted_at = now
        risk.check_status = status
        risk.last_error = error
        risk.next_check_at = now + timedelta(
            seconds=rules.check_interval_seconds if parsed else retry
        )
        evidence = parsed[account.steam_id] if parsed else {}
        if parsed:
            for field, value in evidence.items():
                setattr(risk, field, value)
            risk.checked_at = now
            risk.source = SOURCE
            if confirmed_restriction(risk):
                incident(
                    db,
                    "CONFIRMED_RESTRICTION",
                    "CRITICAL",
                    account_id=account.id,
                    source=SOURCE,
                    evidence=evidence,
                )
            else:
                resolve_condition(db, "CONFIRMED_RESTRICTION", account_id=account.id)
        db.add(
            RiskCheck(
                account_id=account.id,
                status=status,
                source=SOURCE,
                evidence=evidence,
                error=error,
            )
        )
    enforce(db)
    return {
        "status": status,
        "checked": len(rows),
        "retry_after": retry if error else None,
    }


def enforce(db):
    """Persist stop commands in the same transaction as evidence; offline nodes stay reserved."""
    from app.services.scheduler import cancel_job

    rules = policy(db)
    jobs = list(
        db.scalars(
            select(Job).where(
                Job.kind.in_(("steam", "cs2")),
                Job.status.in_(("QUEUED", "STARTING", "RUNNING", "UNKNOWN")),
            )
        )
    )
    if not jobs:
        return
    account_ids = {j.account_id for j in jobs}
    accounts = {
        a.id: a
        for a in db.scalars(
            select(SteamAccount).where(SteamAccount.id.in_(account_ids))
        )
    }
    risks = {r.account_id: r for r in db.scalars(select(AccountRisk))}
    sessions = {
        s.job_id: s
        for s in db.scalars(select(Session).where(Session.status.in_(ACTIVE)))
    }
    for job in jobs:
        session = sessions.get(job.id)
        reason = decision(
            job.kind,
            accounts.get(job.account_id),
            risks.get(job.account_id),
            rules,
            session.node_id if session else job.node_id,
        )
        if reason:
            cancel_job(db, job, "protection")
            job.error = "Protection: " + reason
            event = incident(
                db,
                reason,
                "CRITICAL" if reason == "CONFIRMED_RESTRICTION" else "HIGH",
                account_id=job.account_id,
                node_id=session.node_id if session else job.node_id,
                job_id=job.id,
                source="compliance",
                evidence={"operation": job.kind},
            )
            from app.services.safety import record_decision

            record_decision(
                db,
                "STOP_AFFECTED_JOB",
                reason,
                account_id=job.account_id,
                node_id=session.node_id if session else job.node_id,
                job_id=job.id,
                event=event,
                source="compliance",
            )
            audit(
                db, "protection", "protection.job_stopped", job.id, {"reason": reason}
            )


def check_accounts(db, ids=None, *, transport=None):
    """Provider lease also covers commit; workers never overlap API batches."""
    import secrets

    owner = secrets.token_hex(16)
    name = "protection:provider_lease"
    if settings().deployment_mode == "desktop":
        from app.core.local_store import LocalStore

        store = LocalStore()
        acquired = store.put(name, owner, 45, nx=True)

        def release():
            import json

            with store.connection() as connection:
                connection.execute(
                    "DELETE FROM kv WHERE key=? AND value=?", (name, json.dumps(owner))
                )
    else:
        from redis import Redis

        store = Redis.from_url(
            settings().redis_url, decode_responses=True, socket_timeout=3
        )
        acquired = store.set(name, owner, nx=True, ex=45)

        def release():
            store.eval(
                "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                1,
                name,
                owner,
            )

    if not acquired:
        return {"status": "BUSY", "checked": 0, "retry_after": 5}
    try:
        result = _check_accounts(db, ids, transport=transport)
        if store.get(name) != owner:
            db.rollback()
            return {"status": "LEASE_EXPIRED", "checked": 0, "retry_after": 5}
        db.commit()
        return result
    finally:
        release()
