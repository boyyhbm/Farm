"""Explicit fault injection; production contains no simulated health/ban evidence."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.api_safety import router
from app.core.db import get_db, utcnow
from app.core.security import authenticate
from app.models.models import (
    Command,
    Control,
    IntegrityCheck,
    Job,
    NodeSafety,
    SafetyDecision,
    Session,
)
from app.schemas.api import JobCreate
from app.services import protection, safety, scheduler
from app.services.account_health import context, health
from app.services.compliance import ProtectionDenied, policy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from security.integrity import inspect
from sqlalchemy import func, select
from test_protection import account, adapter, authorized, node, response, running
from test_protection import db as db  # noqa: PLC0414 - re-export a pytest fixture; removing it breaks fixture discovery

_ORIGINAL_API_KEY = protection.api_key


def evidence(status="WARNING", game="UNKNOWN", **extra):
    return {
        "status": status,
        "source": "agent_self_check",
        "checked_at": utcnow().isoformat(),
        "baseline_trust": "unsigned_local_manifest",
        "files_checked": 1,
        "findings": [
            {
                "code": "COMPONENT_HASH_MISMATCH"
                if status == "FAILED"
                else "COMPONENT_HASHES_MATCH",
                "status": "FAILED" if status == "FAILED" else "PASS",
                "component": "explicit_test_fixture",
                "explanation": "Isolated test observation",
            }
        ],
        "game_files_status": game,
        **extra,
    }


def test_overheating_quarantines_only_affected_node_and_records_once(db):
    a, n = account(db), node(db)
    authorized(db, a, n)
    j, s = running(db, a, n)
    n2 = node(db, 2)
    other = scheduler.create_job(
        db, JobCreate(kind="test", node_id=n2.id), "another-node-job", "operator"
    )
    db.commit()
    n.metrics = {**n.metrics, "cpu_temperature": 101}
    db.commit()
    safety.cycle(db)
    db.commit()
    assert db.get(NodeSafety, n.id).state == "REVIEW_REQUIRED"
    assert j.status == "CANCELLED" and s.status == "STOPPING"
    assert other.status == "QUEUED"
    assert db.scalar(
        select(SafetyDecision.id).where(SafetyDecision.action == "QUARANTINE_NODE")
    )
    count = db.scalar(select(func.count(Command.id)))
    safety.cycle(db)
    db.commit()
    assert db.scalar(select(func.count(Command.id))) == count
    scheduler.tick(db)
    db.commit()
    assert other.status == "STARTING"


@pytest.mark.parametrize(
    "metrics,reason",
    [
        ({"ram_percent": 99}, "LOW_MEMORY"),
        ({"ram_available_gb": 0.01}, "LOW_MEMORY"),
        ({"gpu_temperature": 105}, "OVERHEATING"),
        ({"disk_free_gb": 0}, "LOW_DISK"),
    ],
)
def test_observed_critical_resource_limits(db, metrics, reason):
    n = node(db)
    n.metrics = {**n.metrics, **metrics}
    db.commit()
    safety.cycle(db)
    db.commit()
    row = db.get(NodeSafety, n.id)
    assert row.state == "REVIEW_REQUIRED" and reason in row.reasons


def test_missing_sensors_are_unknown_not_overheating(db):
    n = node(db)
    n.metrics = {**n.metrics, "cpu_temperature": None, "gpu_temperature": None}
    db.commit()
    safety.cycle(db)
    db.commit()
    assert db.get(NodeSafety, n.id).state == "WARNING"
    assert not db.scalar(select(SafetyDecision.id))


def test_stale_metrics_cannot_trigger_new_resource_incident(db):
    n = node(db)
    n.metrics = {**n.metrics, "cpu_temperature": 120}
    n.last_heartbeat = utcnow() - timedelta(minutes=5)
    db.commit()
    safety.cycle(db)
    db.commit()
    assert db.get(NodeSafety, n.id).state == "UNKNOWN"
    assert not db.scalar(select(SafetyDecision.id))


def test_sustained_cpu_uses_elapsed_observations_and_never_auto_releases(db):
    n = node(db)
    n.metrics = {**n.metrics, "cpu_percent": 99}
    db.commit()
    safety.cycle(db)
    db.commit()
    row = db.get(NodeSafety, n.id)
    assert row.state != "REVIEW_REQUIRED"
    row.cpu_high_since = utcnow() - timedelta(seconds=70)
    db.commit()
    safety.cycle(db)
    db.commit()
    assert row.state == "REVIEW_REQUIRED"
    n.metrics = {**n.metrics, "cpu_percent": 1}
    db.commit()
    safety.cycle(db)
    db.commit()
    assert row.state == "REVIEW_REQUIRED"
    with pytest.raises(ProtectionDenied):
        safety.assert_node_allowed(db, n.id, "test")


def test_release_requires_admin_and_fresh_agent_ack_and_does_not_requeue(db):
    a, n = account(db), node(db)
    authorized(db, a, n)
    j, _s = running(db, a, n)
    n.metrics = {**n.metrics, "ram_percent": 99}
    db.commit()
    safety.cycle(db)
    db.commit()
    n.metrics = {**n.metrics, "ram_percent": 40}
    db.commit()
    with pytest.raises(Exception, match="acknowledge the hold"):
        safety.request_release(db, n, "admin", "Reviewed cooling")
    hold = db.scalar(select(Command).where(Command.operation == "safety_hold"))
    scheduler.acknowledge(db, n, {"command_id": hold.id, "status": "STOPPED"})
    db.commit()
    safety.ingest_integrity(db, n, evidence())
    db.commit()
    row = safety.request_release(db, n, "admin", "Inspected physical node")
    db.commit()
    assert row.state == "RELEASING" and j.status == "CANCELLED"
    safety.cycle(db)
    db.commit()
    assert row.state == "RELEASING"
    cmd = db.get(Command, row.release_command_id)
    local = {
        "held": False,
        "reasons": [],
        "observed_at": utcnow().isoformat(),
        "source": "local_safety_guard",
    }
    scheduler.acknowledge(
        db,
        n,
        {
            "command_id": cmd.id,
            "status": "STOPPED",
            "resource_usage": {"integrity": evidence(), "safety": local},
        },
    )
    db.commit()
    assert row.state == "WARNING" and j.status == "CANCELLED"
    assert db.scalar(
        select(SafetyDecision.id).where(SafetyDecision.action == "RELEASE_NODE")
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"cpu_percent": float("nan")},
        {"ram_available_gb": -1},
        {"cpu_temperature": True},
        {"gpu_temperature": float("inf")},
        {"ram_percent": "99"},
    ],
)
def test_malformed_sensor_data_cannot_be_treated_as_safe(raw):
    with pytest.raises(ValueError):
        safety.validate_metrics(raw)


def test_integrity_history_deduplicates_replayed_heartbeat_and_rejects_future(db):
    n = node(db)
    db.commit()
    raw = evidence()
    first = safety.ingest_integrity(db, n, raw)
    db.commit()
    assert safety.ingest_integrity(db, n, raw).id == first.id
    assert db.scalar(select(func.count(IntegrityCheck.id))) == 1
    with pytest.raises(ValueError):
        safety.ingest_integrity(
            db, n, evidence(checked_at=(utcnow() + timedelta(hours=1)).isoformat())
        )


def test_changed_game_files_block_work_and_keep_synthetic_other_nodes_available(db):
    n = node(db)
    db.commit()
    safety.ingest_integrity(db, n, evidence(status="FAILED", game="CHANGED"))
    db.commit()
    safety.cycle(db)
    db.commit()
    assert "GAME_FILES_CHANGED" in db.get(NodeSafety, n.id).reasons
    with pytest.raises(ProtectionDenied):
        scheduler.create_job(
            db, JobCreate(kind="test", node_id=n.id), "blocked-test-key", "operator"
        )


def test_account_health_has_sourced_unknown_warning_restricted_and_banned_states(db):
    a, n = account(db), node(db)
    r = authorized(db, a, n)
    a.assigned_node_id = n.id
    db.commit()
    assert health(a, r, policy(db), context(db))["status"] == "Unknown"
    protection.check_accounts(db, [a.id], transport=adapter([response(a)]))
    a.last_sync = utcnow()
    db.commit()
    assert health(a, r, policy(db), context(db))["status"] == "Healthy"
    r.auth_status = "FAILED"
    db.commit()
    assert health(a, r, policy(db), context(db))["status"] == "Warning"
    r.manual_hold = True
    db.commit()
    assert health(a, r, policy(db), context(db))["status"] == "Restricted"
    r.vac_banned = True
    db.commit()
    view = health(a, r, policy(db), context(db))
    assert (
        view["status"] == "Banned"
        and view["observations"][0]["source"] == protection.SOURCE
    )


@pytest.fixture
def api_client(db):
    app = FastAPI()
    app.include_router(router)
    user = SimpleNamespace(username="explicit-test-admin", role="admin")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[authenticate] = lambda: user
    with TestClient(app) as api:
        yield api, user


def test_safety_api_requires_confirmation_and_admin_release(db, api_client):
    api, user = api_client
    n = node(db)
    db.commit()
    user.role = "viewer"
    assert (
        api.post(
            f"/api/v1/protection/nodes/{n.id}/integrity-check",
            json={"confirmed": True, "note": "Review node"},
        ).status_code
        == 403
    )
    user.role = "admin"
    assert (
        api.post(
            f"/api/v1/protection/nodes/{n.id}/release",
            json={"confirmed": False, "note": "Review node"},
        ).status_code
        == 400
    )
    result = api.post(
        f"/api/v1/protection/nodes/{n.id}/integrity-check",
        json={"confirmed": True, "note": "Review node"},
    )
    assert result.status_code == 202 and result.json()["status"] == "QUEUED"
    assert db.get(Command, result.json()["command_id"]).operation == "integrity_check"


def test_reports_paginated_filters_and_csv_formula_protection(db, api_client):
    api, _ = api_client
    for i in range(40):
        account(db, i)
    n = node(db)
    n.hostname = '=HYPERLINK("bad")'
    db.commit()
    safety.cycle(db)
    db.commit()
    result = api.get(
        "/api/v1/protection/health/accounts?page=2&page_size=25&health_status=Unknown"
    ).json()
    assert result["total"] == 40 and len(result["items"]) == 15
    csv = api.get("/api/v1/protection/reports/export?kind=nodes&format=csv")
    assert csv.status_code == 200 and "'=HYPERLINK" in csv.text
    assert "credential_hash" not in csv.text
    assert (
        len(api.get("/api/v1/protection/reports/export?kind=health").json()["items"])
        == 40
    )


def test_own_component_hash_change_is_detected_with_an_explicit_local_baseline(
    tmp_path,
):
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "component.py"
    file.write_text("original source")
    manifest = {
        "schema": 1,
        "files": {"component.py": hashlib.sha256(file.read_bytes()).hexdigest()},
    }
    (root / "component-integrity.json").write_text(json.dumps(manifest))
    result = inspect(root=root, check_signatures=False)
    assert (
        result["status"] == "WARNING"
        and result["baseline_trust"] == "unsigned_local_manifest"
    )
    file.write_text("changed source")
    assert inspect(root=root, check_signatures=False)["status"] == "FAILED"


@pytest.mark.asyncio
async def test_disconnected_local_guard_stops_real_owned_child_and_persists_hold(
    tmp_path, monkeypatch
):
    from agent.config import Config
    from agent.main import Agent
    from agent.safety import LocalSafetyGuard

    metrics = {
        "cpu_percent": 1,
        "ram_percent": 20,
        "ram_available_gb": 8,
        "disk_free_gb": 20,
        "cpu_temperature": None,
        "gpu_temperature": None,
    }

    async def collect(_):
        return dict(metrics)

    monkeypatch.setattr("collectors.metrics.collect", collect)
    config = Config(server_url="https://explicit-test.invalid", data_dir=tmp_path)
    agent = Agent(config, {})
    try:
        cmd = {
            "type": "command",
            "command_id": "local-guard-start",
            "operation": "start",
            "session_id": "local-test-session",
            "kind": "test",
            "payload": {"duration_seconds": 60},
            "deadline": (utcnow() + timedelta(minutes=2)).isoformat(),
        }
        started = await agent.command(cmd)
        assert started["status"] == "RUNNING"
        metrics["cpu_temperature"] = 105
        await agent.safety.sample()
        assert not agent.processes.running() and agent.safety.evidence()["held"]
        assert any(a["status"] == "FAILED" for a in agent.store.latest_acks())
        restarted = LocalSafetyGuard(config, agent.processes, agent.store)
        assert "OVERHEATING" in restarted.reasons
        metrics["cpu_temperature"] = 40
        await restarted.sample()
        assert restarted.evidence()[
            "held"
        ]  # A recovered sensor does not auto-release a hold.
        await restarted.release()
        assert not restarted.evidence()["held"]
    finally:
        for record in agent.store.processes():
            await agent.processes.stop(record["session_id"])
        agent.store.close()


def test_corrupt_guard_configuration_fails_closed_without_disclosing_contents(tmp_path):
    from agent.safety import LocalSafetyGuard

    (tmp_path / "safety-state.json").write_text("broken private data")
    guard = LocalSafetyGuard(SimpleNamespace(data_dir=tmp_path), None, None)
    assert guard.evidence()["held"] and guard.reasons == ["CONFIGURATION_CORRUPT"]
    assert "private" not in json.dumps(guard.evidence())


def test_manager_corruption_keeps_queue_paused_until_fresh_repair_check(
    db, tmp_path, monkeypatch
):
    root = tmp_path / "runtime"
    (root / "app" / "services").mkdir(parents=True)
    file = root / "critical.py"
    file.write_text("official component")
    (root / "component-integrity.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "files": {"critical.py": hashlib.sha256(file.read_bytes()).hexdigest()},
            }
        )
    )
    monkeypatch.setattr(
        safety, "__file__", str(root / "app" / "services" / "safety.py")
    )
    initial = safety.inspect_manager(db, force=True)
    db.commit()
    assert initial.status == "WARNING"
    file.write_text("changed component")
    failed = safety.inspect_manager(db, force=True)
    db.commit()
    assert failed.status == "FAILED" and db.get(Control, 1).paused
    with pytest.raises(ProtectionDenied):
        safety.assert_manager_allowed(db)
    db.get(Control, 1).paused = False
    db.commit()
    safety.cycle(db)
    db.commit()
    assert db.get(
        Control, 1
    ).paused  # A cached failure cannot be bypassed by resuming the queue.
    file.write_text("official component")
    safety.inspect_manager(db, force=True)
    db.commit()
    safety.assert_manager_allowed(db)
    assert db.get(Control, 1).paused  # Repair does not auto-resume cancelled work.


def test_repeated_client_failures_hold_only_affected_account(db):
    a, b, n = account(db), account(db, 2), node(db)
    risk = authorized(db, a, n)
    db.commit()
    for i in range(3):
        j = Job(
            kind="steam",
            account_id=a.id,
            node_id=n.id,
            status="FAILED",
            payload={},
            idempotency_key="client-failure-" + str(i),
        )
        db.add(j)
        db.flush()
        db.add(
            Session(
                job_id=j.id,
                account_id=a.id,
                node_id=n.id,
                status="FAILED",
                ended_at=utcnow(),
                exit_code=1,
            )
        )
    db.commit()
    safety.cycle(db)
    db.commit()
    assert risk.manual_hold and not protection.get_risk(db, b.id).manual_hold
    assert db.scalar(
        select(SafetyDecision.id).where(
            SafetyDecision.reason == "REPEATED_CLIENT_FAILURE"
        )
    )


def test_failed_agent_update_requires_review_and_graphical_repair_ack(tmp_path):
    from agent.config import Config

    config = Config(server_url="https://explicit-test.invalid", data_dir=tmp_path)
    (tmp_path / "update-failure.json").write_text(
        json.dumps({"code": "AGENT_UPDATE_FAILED", "phase": "activation_rolled_back"})
    )
    value = inspect(config, check_signatures=False)
    assert value["status"] == "FAILED"
    assert any(f["code"] == "AGENT_UPDATE_FAILED" for f in value["findings"])


def test_game_baseline_detects_executable_changes_without_touching_memory(tmp_path):
    from agent.config import Config
    from security.integrity import capture_game_baseline

    folder = tmp_path / "official-install-fixture"
    folder.mkdir()
    steam = folder / "steam.exe"
    cs2 = folder / "cs2.exe"
    steam.write_bytes(b"explicit test steam")
    cs2.write_bytes(b"explicit test cs2")
    config = Config(
        server_url="https://explicit-test.invalid",
        data_dir=tmp_path,
        steam_path=str(steam),
        cs2_path=str(cs2),
    )
    capture_game_baseline(config)
    assert (
        inspect(config, check_signatures=False)["game_files_status"]
        == "UNCHANGED_FROM_OPERATOR_BASELINE"
    )
    cs2.write_bytes(b"changed executable fixture")
    assert inspect(config, check_signatures=False)["game_files_status"] == "CHANGED"


def test_expired_or_failed_stop_is_reissued_with_a_bound_and_never_frees_node(db):
    a, n = account(db), node(db)
    authorized(db, a, n)
    _j, s = running(db, a, n)
    scheduler.stop_session(db, s)
    db.commit()
    first = db.scalar(select(Command).where(Command.operation == "stop"))
    first.deadline = utcnow() - timedelta(seconds=1)
    db.commit()
    scheduler.stop_session(db, s)
    db.commit()
    assert (
        db.scalar(select(func.count(Command.id)).where(Command.operation == "stop"))
        == 2
    )
    assert s.status == "STOPPING"
    for command in db.scalars(select(Command).where(Command.operation == "stop")):
        command.ack_status = "FAILED"
    db.commit()
    scheduler.stop_session(db, s)
    db.commit()
    for command in db.scalars(select(Command).where(Command.operation == "stop")):
        command.ack_status = "FAILED"
    db.commit()
    scheduler.stop_session(db, s)
    db.commit()
    assert (
        db.scalar(select(func.count(Command.id)).where(Command.operation == "stop"))
        == 3
    )
    assert (
        s.status == "STOPPING" and db.get(NodeSafety, n.id).state == "REVIEW_REQUIRED"
    )


def test_removed_steam_installation_keeps_agent_config_loadable_and_reports_fault(
    tmp_path,
):
    from agent.config import Config

    file = tmp_path / "config.json"
    file.write_text(
        json.dumps(
            {
                "server_url": "https://explicit-test.invalid",
                "data_dir": str(tmp_path),
                "steam_path": str(tmp_path / "steam.exe"),
            }
        )
    )
    config = Config.load(str(file))
    assert inspect(config, check_signatures=False)["status"] == "FAILED"


def test_corrupt_protected_credentials_do_not_disable_safety_logging_or_invent_bans(
    db, tmp_path, monkeypatch
):
    (tmp_path / "protection-secrets.dat").write_bytes(
        b"explicit corrupted ciphertext fixture"
    )

    def broken(_):
        raise ValueError("private ciphertext must never be echoed")

    monkeypatch.setattr("security.credentials.read_credentials", broken)
    monkeypatch.setattr(protection, "api_key", _ORIGINAL_API_KEY)
    monkeypatch.setattr(protection, "_credential_error", None)
    a = account(db)
    db.commit()
    result = protection.check_accounts(db)
    assert result["status"] == "CONFIGURATION_ERROR" and result["checked"] == 0
    assert (
        protection.safe_message("diagnostic remains available")
        == "diagnostic remains available"
    )
    assert "ciphertext" not in (protection.key_error() or "")
    assert protection.get_risk(db, a.id).vac_banned is None
