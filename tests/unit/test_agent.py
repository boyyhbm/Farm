from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from agent.config import Config
from agent.main import Agent
from agent.store import Store
from process_manager.manager import ProcessManager
from security.credentials import write_credentials, read_credentials
from security.updates import safe_extract, verify_manifest


def config(tmp_path):
    return Config(server_url="https://example.invalid", data_dir=tmp_path)


def command(operation="start", **extra):
    return {
        "type": "command",
        "command_id": "command-1",
        "operation": operation,
        "session_id": "session-1",
        "kind": "test",
        "payload": {"duration_seconds": 0.1},
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        **extra,
    }


@pytest.mark.asyncio
async def test_real_child_launch_replay_and_completion(tmp_path):
    agent = Agent(config(tmp_path), {"node_id": "n", "credential": "secret"})
    request = command(
        payload={"duration_seconds": 0.1, "min_ram_gb": 0, "max_cpu_percent": 95}
    )
    first = await agent.command(request)
    second = await agent.command(request)
    assert first["status"] == "RUNNING"
    assert first == second
    assert len(agent.store.processes()) == 1
    results = []
    for _ in range(80):
        await asyncio.sleep(0.1)
        results = agent.processes.poll()
        if results:
            break
    assert len(results) == 1
    assert results[0]["status"] == "STOPPED", json.dumps(results, ensure_ascii=False)
    assert results[0]["resource_usage"]["synthetic"] is True
    assert results[0]["resource_usage"]["steps"] > 0
    replay = await agent.command(request)
    assert replay["status"] == "STOPPED"
    assert agent.store.processes() == []
    agent.store.close()


@pytest.mark.asyncio
async def test_real_child_stop_is_idempotent(tmp_path):
    agent = Agent(config(tmp_path), {})
    launch = await agent.command(command(payload={"duration_seconds": 60}))
    assert launch["status"] == "RUNNING"
    stop_command = command("stop", command_id="stop-1", payload={})
    assert (await agent.command(stop_command))["status"] == "STOPPED"
    assert (await agent.command(stop_command))["status"] == "STOPPED"
    assert not agent.processes.running()
    agent.store.close()


@pytest.mark.asyncio
async def test_command_expiry_allowlist_and_mutated_replay(tmp_path):
    agent = Agent(config(tmp_path), {})
    expired = command(
        deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    )
    assert (await agent.command(expired))["status"] == "FAILED"
    with pytest.raises(ValueError, match="reused"):
        await agent.command(command())
    bad = command(command_id="bad-kind", kind="shell", payload={"command": "whoami"})
    assert (await agent.command(bad))["status"] == "FAILED"
    injected = command(
        command_id="injected", payload={"duration_seconds": 1, "executable": "cmd.exe"}
    )
    assert (await agent.command(injected))["status"] == "FAILED"
    assert not agent.processes.running()
    agent.store.close()


def test_durable_journal_survives_reopen(tmp_path):
    path = tmp_path / "journal.db"
    store = Store(path)
    assert store.reserve("a", "fingerprint") == (False, None)
    store.ack("a", {"status": "STOPPED"})
    store.close()
    reopened = Store(path)
    assert reopened.reserve("a", "fingerprint") == (True, {"status": "STOPPED"})
    reopened.close()


def test_pid_reuse_does_not_own_unrelated_process(tmp_path):
    import psutil

    proc = psutil.Process()
    assert (
        ProcessManager.owned({"pid": proc.pid, "created": proc.create_time() - 5})
        is None
    )
    assert (
        ProcessManager.owned({"pid": proc.pid, "created": proc.create_time()}).pid
        == proc.pid
    )


def test_signed_update_verifies_integrity():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    manifest = {
        "version": "1.0.0",
        "url": "https://updates.example.com/1.zip",
        "sha256": "a" * 64,
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["signature"] = base64.b64encode(private.sign(canonical)).decode()
    assert verify_manifest(manifest, public) == canonical
    manifest["sha256"] = "b" * 64
    with pytest.raises(InvalidSignature):
        verify_manifest(manifest, public)


def test_update_zip_rejects_traversal_and_allows_valid_release(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escaped.txt", "not allowed")
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract(archive, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    assert not (tmp_path / "escaped.txt").exists()
    archive = tmp_path / "good.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("agent/main.py", "# signed release")
    safe_extract(archive, tmp_path / "good")
    assert (tmp_path / "good/agent/main.py").exists()


def test_credentials_roundtrip_and_local_permissions(tmp_path):
    path = tmp_path / "credential"
    value = {"node_id": "node", "credential": "secret"}
    write_credentials(path, value)
    assert read_credentials(path) == value
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_configuration_requires_tls(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"server_url": "http://example.com", "data_dir": str(tmp_path)})
    )
    with pytest.raises(ValueError, match="HTTPS"):
        Config.load(str(path))
    path.write_text(
        json.dumps(
            {
                "server_url": "http://127.0.0.1:8000",
                "data_dir": str(tmp_path),
                "allow_insecure_localhost": True,
            }
        )
    )
    assert Config.load(str(path)).server_url == "http://127.0.0.1:8000"


@pytest.mark.asyncio
async def test_stop_does_not_race_completion_telemetry(tmp_path):
    agent = Agent(config(tmp_path), {})
    launch = await agent.command(command(payload={"duration_seconds": 60}))
    assert launch["status"] == "RUNNING"
    task = asyncio.create_task(agent.processes.stop("session-1"))
    reports = []
    while not task.done():
        await asyncio.sleep(0.001)
        reports.extend(agent.processes.poll())
    await task
    assert reports == []
    agent.store.close()


@pytest.mark.asyncio
async def test_pruned_expired_command_cannot_relaunch(tmp_path):
    agent = Agent(config(tmp_path), {})
    request = command(
        deadline=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )
    assert (await agent.command(request))["status"] == "FAILED"
    agent.store.db.execute("UPDATE commands SET created=0")
    agent.store.db.commit()
    agent.store.prune()
    assert agent.store.latest_acks() == []
    assert (await agent.command(request))["status"] == "FAILED"
    assert not agent.processes.running()
    agent.store.close()


@pytest.mark.asyncio
async def test_local_shutdown_persists_interrupted_result_instead_of_replaying_running(
    tmp_path, monkeypatch
):
    agent = Agent(config(tmp_path), {})
    started = await agent.command(command(payload={"duration_seconds": 60}))
    assert started["status"] == "RUNNING"

    async def stopped_transport(*args):
        return None

    monkeypatch.setattr("agent.main.reconnecting", stopped_transport)
    assert await agent.run() == 0
    reopened = Store(tmp_path / "journal.sqlite3")
    try:
        ack = next(
            item for item in reopened.latest_acks() if item["command_id"] == "command-1"
        )
        assert ack["status"] == "FAILED"
        assert "stopped locally" in ack["error"]
        assert reopened.processes() == []
    finally:
        reopened.close()


def test_live_handle_is_authoritative_when_pid_disappears_between_polls(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "race-journal.sqlite3")
    store.reserve("race-command", "explicit-fingerprint")
    store.add_process("race-session", "race-command", "test", 777, 0)
    manager = ProcessManager(config(tmp_path), store)

    class SavedHandle:
        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 0

    manager.handles["race-session"] = SavedHandle()
    monkeypatch.setattr(manager, "owned", lambda _: None)
    assert manager.poll() == [] and len(store.processes()) == 1
    result = manager.poll()
    assert result[0]["status"] == "STOPPED" and result[0]["exit_code"] == 0
    store.close()
