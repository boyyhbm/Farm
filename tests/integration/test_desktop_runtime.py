"""Real packaged-entry source tests in separate processes; no Redis/PostgreSQL needed."""

import json
from contextlib import closing
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import zipfile
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "apps/api/desktop_entry.py"


def invoke(data, command, payload=None, *args):
    process = subprocess.run(
        [sys.executable, str(ENTRY), command, "--data-dir", str(data), *args],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_desktop_first_launch_api_persistence_backup_restore_without_redis(tmp_path):
    data = tmp_path / "Farm"
    api_port = port()
    origin = "http://127.0.0.1:17830"
    assert (
        invoke(
            data,
            "configure",
            {"username": "localadmin", "password": "desktop-password-1234"},
            "--origin",
            origin,
        )["database"]
        == "sqlite"
    )
    assert (data / "farm.sqlite3").exists()
    assert b"desktop-password-1234" not in (data / "farm.sqlite3").read_bytes()
    env = {
        **os.environ,
        "DESKTOP_INSTANCE_ID": "desktop-test-instance",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }

    def launch():
        log = (tmp_path / "runtime.log").open("ab")
        p = subprocess.Popen(
            [
                sys.executable,
                str(ENTRY),
                "serve",
                "--data-dir",
                str(data),
                "--origin",
                origin,
                "--port",
                str(api_port),
            ],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
            env=env,
            cwd=ROOT,
        )
        for _ in range(100):
            if p.poll() is not None:
                raise AssertionError((tmp_path / "runtime.log").read_text())
            try:
                result = httpx.get(
                    f"http://127.0.0.1:{api_port}/health/ready", timeout=0.3
                )
                if result.status_code == 200:
                    return p, log, result.json()
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        p.terminate()
        raise AssertionError("Runtime failed health check")

    def stop(p, log):
        p.stdin.write(b"shutdown\n")
        p.stdin.flush()
        try:
            p.wait(timeout=12)
        except subprocess.TimeoutExpired:
            p.terminate()
            p.wait(timeout=5)
        log.close()

    p, log, ready = launch()
    try:
        assert ready["desktop_instance_id"] == "desktop-test-instance"
        with httpx.Client(
            base_url=f"http://127.0.0.1:{api_port}", headers={"Origin": origin}
        ) as client:
            auth = client.post(
                "/api/v1/auth/login",
                json={"username": "localadmin", "password": "desktop-password-1234"},
            )
            assert auth.status_code == 200, auth.text
            client.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
            response = client.post(
                "/api/v1/accounts",
                json={
                    "steam_id": "76561198011112222",
                    "notes": "explicit desktop integration test",
                },
            )
            assert response.status_code == 201, response.text
            status = client.get("/api/v1/system/status").json()
            assert (
                status["queue"]["engine"] == "sqlite"
                and status["queue"]["status"] == "ready"
            )
            assert (
                status["database"]["engine"] == "sqlite"
                and status["worker"]["status"] == "running"
            )
            assert client.get("/api/v1/downloads/agent").status_code == 503
            from concurrent.futures import ThreadPoolExecutor

            token = client.post("/api/v1/nodes/enrollment-tokens").json()["token"]

            def enroll_once(index):
                return httpx.post(
                    f"http://127.0.0.1:{api_port}/api/v1/nodes/enroll",
                    json={
                        "token": token,
                        "hostname": f"concurrent-{index}",
                        "hardware": {},
                    },
                    timeout=10,
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(enroll_once, [1, 2])) == [200, 403]
    finally:
        stop(p, log)
    snapshot = tmp_path / "backup.farmbackup"
    assert (
        invoke(data, "backup", None, "--output", str(snapshot))["status"] == "backed_up"
    )
    with zipfile.ZipFile(snapshot) as archive:
        assert set(archive.namelist()) <= {
            "manifest.json",
            "farm.sqlite3",
            "coordination.sqlite3",
        }
    assert (
        invoke(data, "restore", None, "--input", str(snapshot))["status"] == "restored"
    )
    p, log, _ = launch()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{api_port}", headers={"Origin": origin}
        ) as client:
            auth = client.post(
                "/api/v1/auth/login",
                json={"username": "localadmin", "password": "desktop-password-1234"},
            )
            assert auth.status_code == 200
            assert client.get("/api/v1/accounts").json()["total"] == 1
    finally:
        stop(p, log)


def test_backup_rejects_traversal_and_notifications_are_not_returned(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    attack = tmp_path / "attack.zip"
    with zipfile.ZipFile(attack, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1, "kind": "database-only"}))
        z.writestr("../escaped", "bad")
    p = subprocess.run(
        [
            sys.executable,
            str(ENTRY),
            "restore",
            "--data-dir",
            str(data),
            "--input",
            str(attack),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert p.returncode != 0 and not (tmp_path / "escaped").exists()
    token = "123456789:" + ("A" * 35)
    result = invoke(
        data,
        "notifications-configure",
        {"token": token, "chat_id": "-12345", "min_severity": "warning"},
    )
    assert result == {"configured": True, "min_severity": "warning"}
    assert token not in json.dumps(invoke(data, "notifications-info"))


def test_graphical_runtime_secure_listener_and_real_agent(tmp_path, monkeypatch):
    """A real TLS listener, private CA trust, enrollment, WSS telemetry and engine process."""
    import ssl
    import uuid

    bind = socket.gethostbyname(socket.gethostname())
    if bind.startswith("127."):
        pytest.skip("Host has no separately bindable network interface for TLS test")
    # This listener is a process on our own host. Keep local test traffic local;
    # external HTTP/HTTPS requests retain the managed environment proxy unchanged.
    excluded = os.environ.get("NO_PROXY", os.environ.get("no_proxy", "")) + "," + bind
    monkeypatch.setenv("NO_PROXY", excluded)
    monkeypatch.setenv("no_proxy", excluded)
    data = tmp_path / "manager"
    agent_data = tmp_path / "agent"
    api_port = port()
    tls_port = port()
    origin = "http://127.0.0.1:17830"
    invoke(
        data,
        "configure",
        {"username": "tlsadmin", "password": "tls-desktop-password-123"},
    )
    network = invoke(
        data,
        "network-configure",
        {"enabled": True, "bind_host": bind, "advertised_host": bind, "port": tls_port},
    )
    assert network["url"] == f"https://{bind}:{tls_port}"
    assert (
        (data / "tls/server.key")
        .read_bytes()
        .startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")
    )
    context = ssl.create_default_context(cadata=network["ca_certificate"])
    log = (tmp_path / "tls-runtime.log").open("wb")
    runtime = subprocess.Popen(
        [
            sys.executable,
            str(ENTRY),
            "serve",
            "--data-dir",
            str(data),
            "--origin",
            origin,
            "--port",
            str(api_port),
        ],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=log,
        cwd=ROOT,
    )
    agent = None
    agent_log = None
    try:
        with httpx.Client(
            base_url=network["url"],
            verify=context,
            trust_env=False,
            headers={"Origin": network["url"]},
            timeout=3,
        ) as client:
            for _ in range(100):
                if runtime.poll() is not None:
                    raise AssertionError((tmp_path / "tls-runtime.log").read_text())
                try:
                    if client.get("/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("TLS listener did not become ready")
            with pytest.raises(httpx.TransportError):
                httpx.get(network["url"] + "/health", trust_env=False, timeout=3)
            auth = client.post(
                "/api/v1/auth/login",
                json={"username": "tlsadmin", "password": "tls-desktop-password-123"},
            )
            assert auth.status_code == 200, auth.text
            assert "secure" in auth.headers["set-cookie"].lower()
            client.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
            token = client.post("/api/v1/nodes/enrollment-tokens").json()["token"]
            enrolled = invoke(
                agent_data,
                "agent-enroll",
                {
                    "server_url": network["url"],
                    "token": token,
                    "ca_certificate": network["ca_certificate"],
                    "heartbeat_seconds": 5,
                },
            )
            assert token not in (agent_data / "config.json").read_text()
            agent_log = (tmp_path / "agent.log").open("wb")
            agent = subprocess.Popen(
                [sys.executable, str(ENTRY), "agent", "--data-dir", str(agent_data)],
                stdin=subprocess.PIPE,
                stdout=agent_log,
                stderr=agent_log,
                cwd=ROOT,
            )
            for _ in range(100):
                node = client.get("/api/v1/nodes/" + enrolled["node_id"]).json()
                if node["status"] == "online":
                    break
                if agent.poll() is not None:
                    raise AssertionError((tmp_path / "agent.log").read_text())
                time.sleep(0.1)
            assert (
                node["status"] == "online" and node["metrics"]["ram_available_gb"] > 0
            )
            job = client.post(
                "/api/v1/jobs",
                json={
                    "kind": "test",
                    "node_id": enrolled["node_id"],
                    "payload": {"duration_seconds": 1, "seed": 73},
                    "max_attempts": 1,
                },
                headers={"Idempotency-Key": str(uuid.uuid4())},
            )
            assert job.status_code == 201, job.text
            for _ in range(100):
                jobs = client.get("/api/v1/jobs").json()["items"]
                if jobs and jobs[0]["status"] == "COMPLETED":
                    break
                time.sleep(0.3)
            assert jobs[0]["status"] == "COMPLETED", jobs
            client.post("/api/v1/nodes/" + enrolled["node_id"] + "/revoke")
    finally:
        for process, output in ((agent, agent_log), (runtime, log)):
            if process and process.poll() is None:
                process.stdin.write(b"shutdown\n")
                process.stdin.flush()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)
            if output:
                output.close()


def test_failed_restore_rolls_back_both_databases(tmp_path, monkeypatch):
    import sqlite3
    from desktop_entry import backup, restore

    data = tmp_path / "state"
    data.mkdir()
    for name in ("farm.sqlite3", "coordination.sqlite3"):
        with closing(sqlite3.connect(data / name)) as db:
            db.executescript(
                "CREATE TABLE users(id INTEGER); CREATE TABLE steam_accounts(id INTEGER); CREATE TABLE alembic_version(version_num TEXT); CREATE TABLE auth_sessions(id INTEGER); INSERT INTO users VALUES(1);"
            )
    archive = tmp_path / "snapshot.farmbackup"
    backup(data, archive)
    with closing(sqlite3.connect(data / "farm.sqlite3")) as db:
        db.execute("INSERT INTO users VALUES(2)")
        db.commit()
    real_replace = os.replace

    def fail_second(source, target):
        if Path(target).name == "coordination.sqlite3":
            raise OSError("Simulated second-database replacement failure")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(OSError):
        restore(data, archive)
    with closing(sqlite3.connect(data / "farm.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    with closing(sqlite3.connect(data / "coordination.sqlite3")) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_desktop_private_stdio_uses_utf8_for_non_ascii_password(tmp_path):
    from argon2 import PasswordHasher
    import sqlite3

    password = "Надёжный-пароль-12345"
    payload = json.dumps(
        {"username": "unicode-admin", "password": password}, ensure_ascii=False
    ).encode("utf-8")
    process = subprocess.run(
        [
            sys.executable,
            str(ENTRY),
            "configure",
            "--data-dir",
            str(tmp_path / "unicode-data"),
        ],
        input=payload,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1251"},
        cwd=ROOT,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    with sqlite3.connect(tmp_path / "unicode-data/farm.sqlite3") as db:
        hashed = db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert PasswordHasher().verify(hashed, password)


def test_backup_restore_closes_sqlite_handles_for_windows_file_operations(
    tmp_path, monkeypatch
):
    import sqlite3
    from desktop_entry import backup, restore

    data = tmp_path / "handles"
    data.mkdir()
    with closing(sqlite3.connect(data / "farm.sqlite3")) as db:
        db.executescript(
            "CREATE TABLE users(id INTEGER); CREATE TABLE steam_accounts(id INTEGER); CREATE TABLE alembic_version(version_num TEXT); CREATE TABLE auth_sessions(id INTEGER);"
        )
    db.close()
    original = sqlite3.connect
    opened = []

    def tracking_connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    archive = tmp_path / "handles.farmbackup"
    backup(data, archive)
    restore(data, archive)
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_readonly_database_digests_detect_changes_without_exposing_values(tmp_path):
    import sqlite3
    from desktop_entry import data_check

    file = tmp_path / "farm.sqlite3"
    with closing(sqlite3.connect(file)) as db:
        db.executescript(
            "CREATE TABLE accounts(id TEXT PRIMARY KEY, notes TEXT); INSERT INTO accounts VALUES('a', 'private-value');"
        )
    first = data_check(tmp_path)
    assert first["ok"]
    assert first == data_check(tmp_path)
    assert "private-value" not in json.dumps(first)
    assert first["databases"]["farm.sqlite3"]["tables"]["accounts"]["rows"] == 1
    with closing(sqlite3.connect(file)) as db:
        db.execute("UPDATE accounts SET notes='changed'")
        db.commit()
    assert data_check(tmp_path) != first
    file.rename(tmp_path / "moved.sqlite3")  # Windows: no leaked SQLite handle
