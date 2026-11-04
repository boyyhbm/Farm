"""Real PostgreSQL + Redis tests. Use scripts/test.sh to supply isolated farm_test DB."""

import uuid
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from app.core.db import SessionLocal, engine, Base, utcnow
from app.core.security import bootstrap
from app.main import app
from app.models.models import (
    Job,
    Session,
    Node,
    Command,
    SteamAccount,
    AuthSession,
    AuditLog,
)
from app.services.scheduler import tick


@pytest.fixture()
def client():
    # The test runner only accepts a distinct *_test database; never clear a real installation.
    assert engine.url.database.endswith("_test"), (
        "Use scripts/test.sh (isolated test database)"
    )
    from redis import Redis
    from app.core.config import settings

    isolated_redis = Redis.from_url(settings().redis_url)
    assert isolated_redis.connection_pool.connection_kwargs["db"] == 14
    isolated_redis.delete("farm:rate:login:testclient", "farm:rate:api:testclient")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        bootstrap(db)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/auth/login",
            json={"username": "testadmin", "password": "test-password-123456"},
            headers={"Origin": "http://testserver"},
        )
        assert result.status_code == 200, result.text
        client.headers.update(
            {"Origin": "http://testserver", "X-CSRF-Token": result.json()["csrf_token"]}
        )
        yield client


def account(client, i=1):
    r = client.post("/api/v1/accounts", json={"steam_id": f"765611980{i:08d}"})
    assert r.status_code == 201, r.text
    return r.json()


def node(client, hostname="node-a"):
    token = client.post("/api/v1/nodes/enrollment-tokens").json()["token"]
    data = {
        "token": token,
        "hostname": hostname,
        "hardware": {"installed": {"test": True}},
        "os_version": "Linux test adapter",
        "agent_version": "test",
    }
    r = client.post("/api/v1/nodes/enroll", json=data)
    assert r.status_code == 200, r.text
    assert client.post("/api/v1/nodes/enroll", json=data).status_code == 403
    return r.json()


def job(client, **kwargs):
    r = client.post(
        "/api/v1/jobs",
        json={"kind": "test", **kwargs},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    return r.json()


def heartbeat(ws, processes=None):
    ws.send_json(
        {
            "type": "heartbeat",
            "metrics": {
                "cpu_percent": 12,
                "ram_percent": 40,
                "ram_available_gb": 16,
                "gpu_percent": None,
            },
            "processes": processes or [],
        }
    )
    commands = []
    while True:
        msg = ws.receive_json()
        if msg["type"] == "heartbeat_ack":
            return commands
        commands.append(msg)


def ack(ws, command, status, **extra):
    ws.send_json(
        {"type": "ack", "command_id": command["command_id"], "status": status, **extra}
    )
    while True:
        msg = ws.receive_json()
        if msg["type"] == "heartbeat_ack":
            return


def dispatch():
    with SessionLocal() as db:
        tick(db)
        db.commit()


def test_protection_rejects_unsupported_operations_and_audits_without_secrets(client):
    from app.models.models import RiskIncident

    result = client.post(
        "/api/v1/jobs",
        json={"kind": "auto_xp", "password": "DO_NOT_LOG_ME"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert result.status_code == 422 and "DO_NOT_LOG_ME" not in result.text
    result = client.post("/api/v1/accounts", json={"steam_id": "invalid-account"})
    assert result.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(
            select(RiskIncident).where(RiskIncident.code == "POLICY_VIOLATION")
        )
        assert db.scalar(
            select(RiskIncident).where(RiskIncident.code == "INVALID_ACCOUNT_DATA")
        )
    assert (
        client.get("/api/v1/protection/overview").json()["prediction_available"]
        is False
    )


def test_protection_emergency_stop_records_incident_and_real_stop_commands(client):
    from app.models.models import RiskIncident, Control

    n = node(client)
    j = job(client)
    with client.websocket_connect(
        "/ws/agent", headers={"Authorization": "Bearer " + n["credential"]}
    ) as ws:
        heartbeat(ws)
        dispatch()
        commands = heartbeat(ws)
        assert len(commands) == 1
        ack(ws, commands[0], "RUNNING", pid=100)
        result = client.post("/api/v1/control/emergency-stop")
        assert result.status_code == 200, result.text
        commands = heartbeat(ws)
        assert any(c.get("operation") == "stop" for c in commands)
        with SessionLocal() as db:
            assert db.get(Control, 1).paused
            assert db.get(Job, j["id"]).status == "CANCELLED"
            assert db.scalar(
                select(RiskIncident).where(RiskIncident.code == "EMERGENCY_STOP")
            )


def test_account_import_search_export_bulk_and_csrf(client):
    r = client.post(
        "/api/v1/accounts/import",
        content="steam_id,notes,tags\n"
        + "".join(f"765611980{i:08d},note,group\n" for i in range(10)),
        headers={"content-type": "text/csv"},
    )
    assert r.json()["imported"] == 10, r.text
    listing = client.get("/api/v1/accounts?page_size=3").json()
    assert listing["total"] == 10 and len(listing["items"]) == 3
    id = listing["items"][0]["id"]
    assert (
        client.post(
            "/api/v1/accounts/bulk",
            json={
                "ids": [id],
                "changes": {"notes": "changed", "prime_status": "confirmed"},
            },
        ).json()["updated"]
        == 1
    )
    assert client.get("/api/v1/accounts?q=changed").json()["total"] == 1
    assert "steam_id" in client.get("/api/v1/accounts/export").text
    assert (
        client.post(
            "/api/v1/accounts",
            json={"steam_id": "76561198099999999"},
            headers={"X-CSRF-Token": "bad"},
        ).status_code
        == 403
    )
    assert client.get("/api/v1/accounts?q=%27%20OR%201%3D1").json()["total"] == 0
    assert client.delete("/api/v1/accounts/" + id).json()["status"] == "deleted"


def test_parallel_dispatch_idempotency_completion_and_sequential_queue(client):
    n1 = node(client)
    n2 = node(client, "node-b")
    j1 = job(client)
    j2 = job(client)
    j3 = job(client)
    with (
        client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer " + n1["credential"]}
        ) as ws1,
        client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer " + n2["credential"]}
        ) as ws2,
    ):
        heartbeat(ws1)
        heartbeat(ws2)
        dispatch()
        c1 = heartbeat(ws1)
        c2 = heartbeat(ws2)
        assert len(c1) == len(c2) == 1
        assert c1[0]["session_id"] != c2[0]["session_id"]
        with SessionLocal() as db:
            assert (
                len(
                    list(
                        db.scalars(select(Session).where(Session.status == "STARTING"))
                    )
                )
                == 2
            )
        ack(ws1, c1[0], "RUNNING", pid=100)
        ack(ws1, c1[0], "RUNNING", pid=100)
        ack(ws1, c1[0], "STOPPED", exit_code=0)
        ack(ws1, c1[0], "STOPPED", exit_code=0)
        dispatch()
        c3 = heartbeat(ws1)
        assert len(c3) == 1 and c3[0]["session_id"] != c1[0]["session_id"]
        statuses = {j["status"] for j in client.get("/api/v1/jobs").json()["items"]}
        assert "COMPLETED" in statuses
    assert client.get("/api/v1/logs").json()["total"] >= 8


def test_offline_unknown_reserves_node_cancel_and_recovery(client):
    n = node(client)
    job(client)
    with client.websocket_connect(
        "/ws/agent", headers={"Authorization": "Bearer " + n["credential"]}
    ) as ws:
        heartbeat(ws)
        dispatch()
        command = heartbeat(ws)[0]
        ack(ws, command, "RUNNING", pid=222)
        with SessionLocal() as db:
            db.get(Node, n["node_id"]).last_heartbeat = utcnow() - timedelta(
                seconds=120
            )
            db.commit()
        dispatch()
        assert client.get("/api/v1/sessions").json()["items"][0]["status"] == "UNKNOWN"
        j2 = job(client)
        dispatch()
        assert client.get("/api/v1/jobs").json()["items"][0]["status"] == "QUEUED"
        client.post("/api/v1/control/emergency-stop")
        commands = heartbeat(ws, [{"session_id": command["session_id"], "pid": 222}])
        assert any(c["operation"] == "stop" for c in commands)
        stop = next(c for c in commands if c["operation"] == "stop")
        ack(ws, stop, "STOPPED", exit_code=0)
        assert client.get("/api/v1/sessions").json()["items"][0]["status"] == "STOPPED"
        assert all(
            j["status"] == "CANCELLED"
            for j in client.get("/api/v1/jobs").json()["items"]
        )


def test_roles_finances_drops_and_schedule(client):
    a = account(client)
    assert (
        client.post(
            "/api/v1/drops/confirm",
            json={
                "account_id": a["id"],
                "period_id": "bad",
                "source": "manual evidence",
            },
        ).status_code
        == 404
    )
    start = utcnow()
    end = start + timedelta(days=7)
    p = client.post(
        "/api/v1/drops/periods",
        json={
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "source": "Operator verified reset https://example.org/evidence",
        },
    )
    assert p.status_code == 200, p.text
    d = client.post(
        "/api/v1/drops/confirm",
        json={
            "account_id": a["id"],
            "period_id": p.json()["id"],
            "source": "manual screenshot reference",
            "item_name": "Case",
            "value": "2.50",
        },
    )
    assert d.json()["status"] == "CONFIRMED", d.text
    client.post(
        "/api/v1/expenses",
        json={
            "category": "hardware",
            "amount": "100",
            "occurred_at": start.isoformat(),
        },
    )
    client.post(
        "/api/v1/revenues",
        json={"amount": "150", "fees": "15", "occurred_at": start.isoformat()},
    )
    data = client.get("/api/v1/analytics/overview").json()
    assert data["realized_profit"] == 35 and data["roi"] == 35
    s = client.post(
        "/api/v1/schedules",
        json={
            "name": "test cadence",
            "interval_seconds": 60,
            "next_run": start.isoformat(),
            "job_template": {"kind": "test"},
        },
    )
    assert s.status_code == 200, s.text
    dispatch()
    dispatch()
    assert client.get("/api/v1/jobs").json()["total"] == 1
    client.post(
        "/api/v1/users",
        json={
            "username": "reader",
            "password": "reader-password-123456",
            "role": "viewer",
        },
    )
    client.post("/api/v1/auth/logout")
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "reader", "password": "reader-password-123456"},
    )
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.get("/api/v1/accounts").status_code == 200
    assert (
        client.post(
            "/api/v1/jobs",
            json={"kind": "test"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        ).status_code
        == 403
    )
    assert client.get("/api/v1/users").status_code == 403


def test_revoked_agent_and_mutation_idempotency(client):
    n = node(client)
    key = str(uuid.uuid4())
    body = {"kind": "test"}
    one = client.post(
        "/api/v1/jobs", json=body, headers={"Idempotency-Key": key}
    ).json()
    two = client.post(
        "/api/v1/jobs", json=body, headers={"Idempotency-Key": key}
    ).json()
    assert one["id"] == two["id"]
    client.post("/api/v1/nodes/" + n["node_id"] + "/revoke")
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer " + n["credential"]}
        ):
            pass


def test_timeout_does_not_free_node_before_ack_and_retry_is_bounded(client):
    n = node(client)
    j = job(client, max_attempts=1, timeout_seconds=10)
    with client.websocket_connect(
        "/ws/agent", headers={"Authorization": "Bearer " + n["credential"]}
    ) as ws:
        heartbeat(ws)
        dispatch()
        command = heartbeat(ws)[0]
        ack(ws, command, "RUNNING", pid=101)
        with SessionLocal() as db:
            s = db.get(Session, command["session_id"])
            s.started_at = utcnow() - timedelta(seconds=30)
            db.commit()
        dispatch()
        session = client.get("/api/v1/sessions").json()["items"][0]
        assert session["status"] == "STOPPING"
        stop = next(c for c in heartbeat(ws) if c["operation"] == "stop")
        ack(ws, stop, "STOPPED", exit_code=0)
        assert client.get("/api/v1/jobs").json()["items"][0]["status"] == "TIMED_OUT"
        dispatch()
        assert client.get("/api/v1/sessions").json()["total"] == 1
        assert (
            client.post("/api/v1/jobs/" + j["id"] + "/retry").json()["status"]
            == "QUEUED"
        )


def test_two_dispatchers_cannot_claim_same_jobs(client):
    from concurrent.futures import ThreadPoolExecutor

    for name in ("a", "b"):
        n = node(client, name)
        with SessionLocal() as db:
            record = db.get(Node, n["node_id"])
            record.status = "online"
            record.last_heartbeat = utcnow()
            record.metrics = {"cpu_percent": 0, "ram_available_gb": 16}
            db.commit()
    for _ in range(6):
        job(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: dispatch(), range(4)))
    with SessionLocal() as db:
        active = list(
            db.scalars(
                select(Session).where(Session.status.in_(("STARTING", "RUNNING")))
            )
        )
        assert (
            len(active) == 2
            and len({s.node_id for s in active}) == 2
            and len({s.job_id for s in active}) == 2
        )
    assert client.get("/api/v1/jobs?status=QUEUED").json()["total"] == 4


def test_restart_and_mismatched_idempotency(client):
    n = node(client)
    j = job(client)
    with client.websocket_connect(
        "/ws/agent", headers={"Authorization": "Bearer " + n["credential"]}
    ) as ws:
        heartbeat(ws)
        dispatch()
        command = heartbeat(ws)[0]
        ack(ws, command, "RUNNING", pid=22)
        response = client.post(
            "/api/v1/sessions/" + command["session_id"] + "/restart",
            headers={"Idempotency-Key": "restart-key-123"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "QUEUED"
        dispatch()
        assert client.get("/api/v1/jobs?status=STARTING").json()["total"] == 0
    key = "idempotency-mismatch-123"
    assert (
        client.post(
            "/api/v1/jobs", json={"kind": "test"}, headers={"Idempotency-Key": key}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/jobs",
            json={"kind": "test", "priority": 9},
            headers={"Idempotency-Key": key},
        ).status_code
        == 409
    )


def test_agent_reconnect_stale_retained_ack_is_harmless(client):
    n = node(client)
    with client.websocket_connect(
        "/ws/agent", headers={"Authorization": "Bearer " + n["credential"]}
    ) as ws:
        # A terminal ack retained locally longer than the server must not disconnect the node.
        ack(ws, {"command_id": str(uuid.uuid4())}, "STOPPED", exit_code=0)
        assert heartbeat(ws) == []
        assert client.get("/api/v1/nodes/" + n["node_id"]).json()["status"] == "online"
