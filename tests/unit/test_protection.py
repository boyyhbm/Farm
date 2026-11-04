"""Isolated evidence adapters. No real Steam credentials, bans or nodes are fabricated in production."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from app.core.db import Base, utcnow
from app.models.models import (
    AccountRisk,
    Command,
    Control,
    Job,
    Node,
    Notification,
    RiskCheck,
    RiskIncident,
    Session,
    SteamAccount,
)
from app.schemas.api import JobCreate
from app.services import inventory, protection, scheduler
from app.services.compliance import ProtectionDenied, decision, policy, risk_summary
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.pool import StaticPool

ORIGINAL_LIMITER = inventory._rate_limit


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    config = SimpleNamespace(
        deployment_mode="desktop",
        desktop_data_dir=str(tmp_path),
        redis_url="",
        scheduler_max_parallel=64,
        node_offline_seconds=45,
    )
    monkeypatch.setattr(protection, "settings", lambda: config)
    monkeypatch.setattr(scheduler, "settings", lambda: config)
    monkeypatch.setattr(inventory, "settings", lambda: config)
    monkeypatch.setattr("app.core.local_store.settings", lambda: config)
    monkeypatch.setattr(protection, "api_key", lambda: "A" * 32)
    monkeypatch.setattr(inventory, "_rate_limit", lambda *a, **kw: None)
    with DBSession(engine, expire_on_commit=False) as session:
        session.add(Control(id=1, paused=False))
        policy(session)
        session.commit()
        yield session
    engine.dispose()


def account(db, i=1):
    a = SteamAccount(
        steam_id=f"765611980{i:08d}",
        profile_url=f"https://steamcommunity.com/profiles/765611980{i:08d}",
    )
    db.add(a)
    db.flush()
    return a


def node(db, i=1):
    n = Node(
        hostname=f"explicit-test-node-{i}",
        credential_hash=f"credential-{i}",
        status="online",
        last_heartbeat=utcnow(),
        metrics={"cpu_percent": 0, "ram_available_gb": 4},
        hardware={"installed": {"test": True, "steam": True, "cs2": True}},
    )
    db.add(n)
    db.flush()
    return n


def authorized(db, a, n):
    r = protection.get_risk(db, a.id)
    r.auth_status = "AUTHORIZED"
    r.auth_node_id = n.id
    r.auth_checked_at = utcnow()
    r.auth_expires_at = utcnow() + timedelta(hours=1)
    r.auth_source = "operator:explicit-test"
    db.flush()
    return r


def response(a, **changes):
    return {
        "SteamId": a.steam_id,
        "VACBanned": False,
        "CommunityBanned": False,
        "NumberOfGameBans": 0,
        "EconomyBan": "none",
        **changes,
    }


def adapter(rows, status=200, headers=None, seen=None):
    def handle(request):
        assert (
            request.url.scheme == "https" and request.url.host == "api.steampowered.com"
        )
        assert request.headers["x-webapi-key"] == "A" * 32 and "key=" not in str(
            request.url
        )
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"players": rows}, headers=headers)

    return httpx.MockTransport(handle)


def running(db, a, n):
    j = Job(
        kind="steam",
        account_id=a.id,
        node_id=n.id,
        idempotency_key="test-" + n.id,
        status="RUNNING",
        payload={},
        max_attempts=1,
    )
    db.add(j)
    db.flush()
    s = Session(job_id=j.id, account_id=a.id, node_id=n.id, status="RUNNING", pid=123)
    db.add(s)
    db.commit()
    return j, s


def test_confirmed_ban_stops_jobs_and_preserves_proof_outbox_and_history(db):
    a, n = account(db), node(db)
    r = authorized(db, a, n)
    j, s = running(db, a, n)
    result = protection.check_accounts(
        db, [a.id], transport=adapter([response(a, VACBanned=True)])
    )
    assert result["status"] == "SUCCESS" and r.vac_banned is True
    assert j.status == "CANCELLED" and s.status == "STOPPING"
    assert (
        db.scalar(select(func.count(Command.id)).where(Command.operation == "stop"))
        == 1
    )
    incident = db.scalar(
        select(RiskIncident).where(RiskIncident.code == "CONFIRMED_RESTRICTION")
    )
    assert incident.severity == "CRITICAL" and incident.source == protection.SOURCE
    assert db.scalar(select(Notification.id)) and db.scalar(select(RiskCheck.id))
    protection.check_accounts(
        db, [a.id], transport=adapter([response(a, VACBanned=True)])
    )
    assert db.scalar(select(func.count(Command.id))) == 1
    assert (
        db.scalar(
            select(func.count(RiskIncident.id)).where(
                RiskIncident.code == "CONFIRMED_RESTRICTION",
                RiskIncident.job_id.is_(None),
            )
        )
        == 1
    )
    assert risk_summary(a, r, policy(db))["prediction"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"VACBanned": "false"},
        {"NumberOfGameBans": True},
        {"CommunityBanned": None},
        {"SteamId": "76561198099999999"},
    ],
)
def test_malformed_or_wrong_account_data_cannot_clear_previous_ban(db, bad):
    a = account(db)
    r = protection.get_risk(db, a.id)
    r.vac_banned = True
    r.checked_at = utcnow()
    r.source = protection.SOURCE
    db.commit()
    result = protection.check_accounts(
        db, [a.id], transport=adapter([response(a, **bad)])
    )
    assert result["status"] == "UNAVAILABLE" and r.vac_banned is True
    assert risk_summary(a, r, policy(db))["level"] == "CRITICAL"


def test_network_outage_does_not_invent_clear_status(db):
    a = account(db)
    db.commit()

    def fail(request):
        raise httpx.ConnectError("Explicit offline test adapter")

    result = protection.check_accounts(db, [a.id], transport=httpx.MockTransport(fail))
    r = db.get(AccountRisk, a.id)
    assert (
        result["status"] == "UNAVAILABLE"
        and r.vac_banned is None
        and r.checked_at is None
    )
    assert risk_summary(a, r, policy(db))["level"] == "MEDIUM"


def test_429_honors_shared_cooldown_and_no_repeated_requests(db):
    a = account(db)
    db.commit()
    seen = []
    protection.check_accounts(
        db, [a.id], transport=adapter([], 429, {"Retry-After": "120"}, seen)
    )
    result = protection.check_accounts(
        db, [a.id], transport=adapter([response(a)], seen=seen)
    )
    assert result["status"] == "RATE_LIMITED" and len(seen) == 1
    assert db.get(AccountRisk, a.id).vac_banned is None
    with pytest.raises(inventory.InventoryUnavailable, match="cooldown"):
        ORIGINAL_LIMITER("inventory")


def test_invalid_api_key_blocks_provider_not_steam_authorization(db):
    a, n = account(db), node(db)
    r = authorized(db, a, n)
    db.commit()
    seen = []
    protection.check_accounts(db, [a.id], transport=adapter([], 403, seen=seen))
    protection.check_accounts(db, [a.id], transport=adapter([], 403, seen=seen))
    assert len(seen) == 1 and policy(db).provider_status == "AUTH_FAILED"
    assert r.auth_status == "AUTHORIZED" and r.vac_banned is None


def test_manual_authorization_bound_to_node_and_policy_checked_before_dispatch(db):
    a, n, other = account(db), node(db), node(db, 2)
    r = authorized(db, a, n)
    db.commit()
    data = JobCreate(kind="steam", account_id=a.id, node_id=other.id)
    with pytest.raises(ProtectionDenied, match="AUTHORIZATION_NODE_MISMATCH"):
        scheduler.create_job(db, data, "denied-job-key", "operator")
    allowed = scheduler.create_job(
        db, JobCreate(kind="steam", account_id=a.id), "allowed-job-key", "operator"
    )
    db.commit()
    scheduler.tick(db)
    db.commit()
    s = db.scalar(select(Session).where(Session.job_id == allowed.id))
    assert s.node_id == n.id
    r.auth_status = "FAILED"
    protection.enforce(db)
    db.commit()
    assert s.status == "STOPPING" and allowed.status == "CANCELLED"


def test_offline_node_keeps_unknown_reservation_and_incident(db):
    a, n = account(db), node(db)
    authorized(db, a, n)
    j, s = running(db, a, n)
    n.last_heartbeat = utcnow() - timedelta(minutes=2)
    db.commit()
    scheduler.tick(db)
    db.commit()
    assert s.status == "UNKNOWN" and j.status == "UNKNOWN" and n.status == "offline"
    assert db.scalar(select(RiskIncident).where(RiskIncident.code == "NODE_OFFLINE"))


def test_account_session_conflict_has_database_constraint(db):
    a, n = account(db), node(db)
    authorized(db, a, n)
    j, _s = running(db, a, n)
    other = node(db, 2)
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(
            Session(job_id=j.id, account_id=a.id, node_id=other.id, status="STARTING")
        )
        db.flush()
    db.rollback()


def test_many_nodes_dispatched_once_and_synthetic_engine_independent(db):
    [node(db, i) for i in range(24)]
    jobs = [
        scheduler.create_job(db, JobCreate(kind="test"), f"bulk-key-{i}", "operator")
        for i in range(48)
    ]
    db.commit()
    scheduler.tick(db)
    db.commit()
    scheduler.tick(db)
    db.commit()
    sessions = list(db.scalars(select(Session)))
    assert len(sessions) == 24 and len({s.node_id for s in sessions}) == 24
    assert sum(j.status == "QUEUED" for j in jobs) == 24
    assert decision("test", None, None, policy(db)) is None


@pytest.fixture
def client(db):
    from app.api_protection import router
    from app.core.db import get_db
    from app.core.security import authenticate
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    user = SimpleNamespace(username="testoperator", role="admin")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate] = lambda: user
    with TestClient(app) as client:
        yield client, user


def test_auth_errors_stop_work_and_require_explicit_confirmation(db, client):
    api, _user = client
    a, n = account(db), node(db)
    authorized(db, a, n)
    j, s = running(db, a, n)
    data = {
        "status": "FAILED",
        "node_id": n.id,
        "confirmed": False,
        "note": "Observed login error",
    }
    assert (
        api.post(
            f"/api/v1/protection/accounts/{a.id}/authorization", json=data
        ).status_code
        == 400
    )
    data["confirmed"] = True
    result = api.post(f"/api/v1/protection/accounts/{a.id}/authorization", json=data)
    assert (
        result.status_code == 200 and s.status == "STOPPING" and j.status == "CANCELLED"
    )
    body = result.json()
    assert (
        body["auth_status"] == "FAILED"
        and body["risk"]["level"] == "HIGH"
        and body["vac_banned"] is None
    )
    assert body["trade_restrictions"] is None and "operator:" in body["auth_source"]


def test_rbac_and_incident_resolution_does_not_clear_bans(db, client):
    api, user = client
    a = account(db)
    r = protection.get_risk(db, a.id)
    r.vac_banned = True
    r.source = protection.SOURCE
    r.checked_at = utcnow()
    inc = protection.incident(db, "CONFIRMED_RESTRICTION", "CRITICAL", account_id=a.id)
    db.commit()
    user.role = "viewer"
    assert (
        api.post(
            f"/api/v1/protection/accounts/{a.id}/hold",
            json={"hold": True, "confirmed": True, "note": "Review hold"},
        ).status_code
        == 403
    )
    user.role = "admin"
    result = api.post(
        f"/api/v1/protection/incidents/{inc.id}/respond",
        json={"action": "resolve", "confirmed": True, "note": "Case reviewed"},
    )
    assert result.status_code == 200
    assert (
        api.get(f"/api/v1/protection/accounts/{a.id}").json()["risk"]["level"]
        == "CRITICAL"
    )


def test_registry_1200_accounts_unknown_is_not_low_and_paginated(db, client):
    api, _user = client
    for i in range(1200):
        account(db, i)
    db.commit()
    overview = api.get("/api/v1/protection/overview").json()
    assert (
        overview["accounts"] == 1200
        and overview["risk_levels"] == {"MEDIUM": 1200}
        and overview["restricted_accounts"] == 0
    )
    result = api.get("/api/v1/protection/accounts?page=24&page_size=50").json()
    assert result["total"] == 1200 and len(result["items"]) == 50


def test_switching_steam_account_on_same_node_revokes_prior_attestation(db, client):
    api, _ = client
    a, b, n = account(db), account(db, 2), node(db)
    r = authorized(db, a, n)
    j, s = running(db, a, n)
    result = api.post(
        f"/api/v1/protection/accounts/{b.id}/authorization",
        json={
            "status": "AUTHORIZED",
            "node_id": n.id,
            "confirmed": True,
            "note": "Verified second SteamID in official client",
        },
    )
    assert result.status_code == 200, result.text
    db.refresh(r)
    assert r.auth_status == "UNKNOWN" and r.auth_expires_at is None
    assert j.status == "CANCELLED" and s.status == "STOPPING"
    assert result.json()["auth_status"] == "AUTHORIZED"


def test_provider_lease_prevents_duplicate_parallel_requests(db, monkeypatch):
    from app.core.local_store import LocalStore

    a = account(db)
    db.commit()
    assert LocalStore().put("protection:provider_lease", "another-worker", 45, nx=True)

    def unexpected(request):
        pytest.fail("Provider lease must prevent a second Steam request")

    result = protection.check_accounts(
        db, [a.id], transport=httpx.MockTransport(unexpected)
    )
    assert result["status"] == "BUSY" and result["checked"] == 0
    assert db.get(AccountRisk, a.id) is None


def test_secret_redaction_excludes_api_key_from_diagnostic_notes(db, client):
    api, _ = client
    a, n = account(db), node(db)
    db.commit()
    result = api.post(
        f"/api/v1/protection/accounts/{a.id}/authorization",
        json={
            "status": "FAILED",
            "node_id": n.id,
            "confirmed": True,
            "note": "Observed failure with key " + "A" * 32,
        },
    )
    assert result.status_code == 200, result.text
    detail = api.get(f"/api/v1/protection/accounts/{a.id}")
    assert "A" * 32 not in detail.text and "[redacted]" in detail.text
