"""Installed Windows runtimes and two real SCM agents on one CI host.

This does not establish two physical PCs, a clean Windows 11 OS, or reboot behavior.
Only isolated synthetic jobs are dispatched. Requires elevated Windows CI runner.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

RUNTIME = os.getenv("FARM_INSTALLED_RUNTIME_DIR")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not RUNTIME,
    reason="Requires actual installed Windows runtime and SCM",
)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_two_installed_system_services_dispatch_stop_rotate_and_reconnect(tmp_path):
    import psutil
    import win32service

    runtime = Path(RUNTIME)
    frozen = runtime / "farm-runtime.exe"
    prefix = (
        [str(frozen)]
        if frozen.exists()
        else [str(runtime / "python.exe"), str(runtime / "desktop_entry.py")]
    )
    data = tmp_path / "manager"
    names = ["CS2FarmAgentTestA", "CS2FarmAgentTestB"]
    installed = []
    server = None
    logs = (tmp_path / "server.log").open("wb")

    def invoke(command, payload=None, extra=(), timeout=150):
        result = subprocess.run(
            prefix + [command, "--data-dir", str(data), *extra],
            input=json.dumps(payload).encode() if payload else None,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return json.loads(result.stdout)

    def service(name, action, payload=None):
        request = tmp_path / (name + ".request.json")
        response = tmp_path / (name + ".result.json")
        request.write_text(
            json.dumps({"action": action, "payload": payload or {}}), encoding="utf-8"
        )
        result = subprocess.run(
            prefix
            + [
                "service-admin",
                "--service-name",
                name,
                "--input",
                str(request),
                "--output",
                str(response),
            ],
            capture_output=True,
            check=False,
            timeout=180,
        )
        answer = (
            json.loads(response.read_text(encoding="utf-8"))
            if response.exists()
            else {}
        )
        assert result.returncode == 0 and answer.get("ok"), answer.get(
            "error", result.stderr.decode(errors="replace")
        )
        return answer["result"]

    def wait_value(read, matches, timeout=90):
        deadline = time.monotonic() + timeout
        value = None
        while time.monotonic() < deadline:
            value = read()
            if matches(value):
                return value
            time.sleep(0.5)
        raise AssertionError(
            "Timed out awaiting state: " + json.dumps(value, default=str)[:2000]
        )

    try:
        invoke(
            "configure",
            {"username": "windows-ci", "password": "Windows-CI-private-password-1234"},
        )
        bind = socket.gethostbyname(socket.gethostname())
        assert not bind.startswith("127."), (
            "Windows runner needs a separately bindable interface"
        )
        api_port, tls_port = free_port(), free_port()
        network = invoke(
            "network-configure",
            {
                "enabled": True,
                "bind_host": bind,
                "advertised_host": bind,
                "port": tls_port,
            },
        )
        env = {**os.environ, "NODE_OFFLINE_SECONDS": "12"}

        def launch_server():
            return subprocess.Popen(
                prefix + ["serve", "--data-dir", str(data), "--port", str(api_port)],
                stdin=subprocess.PIPE,
                stdout=logs,
                stderr=logs,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        server = launch_server()
        with httpx.Client(
            base_url=network["url"],
            verify=ssl.create_default_context(cadata=network["ca_certificate"]),
            trust_env=False,
            headers={"Origin": network["url"]},
            timeout=5,
        ) as client:

            def ready():
                try:
                    return client.get("/health/ready").status_code
                except httpx.HTTPError:
                    assert server.poll() is None, (
                        "Bundled server stopped: "
                        + (tmp_path / "server.log").read_text(errors="replace")[-2000:]
                    )
                    return 0

            wait_value(ready, lambda value: value == 200)
            auth = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "windows-ci",
                    "password": "Windows-CI-private-password-1234",
                },
            )
            assert auth.status_code == 200
            client.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
            node_ids = []
            for name in names:
                token = client.post("/api/v1/nodes/enrollment-tokens").json()["token"]
                installed.append(
                    name
                )  # cleanup even if CreateService or StartService fails
                answer = service(
                    name,
                    "install",
                    {
                        "server_url": network["url"],
                        "token": token,
                        "ca_certificate": network["ca_certificate"],
                        "heartbeat_seconds": 5,
                    },
                )
                assert answer["status"] == "running" and answer["automatic_start"]
                assert answer["account"].lower() == "localsystem"
                assert "program files" in answer["image_path"].lower()
                node_ids.append(answer["node_id"])
            assert len(set(node_ids)) == 2

            def node(node_id):
                response = client.get("/api/v1/nodes/" + node_id)
                assert response.status_code == 200, response.text
                return response.json()

            for node_id in node_ids:
                result = wait_value(
                    lambda node_id=node_id: node(node_id),
                    lambda n: (
                        n["status"] == "online"
                        and n.get("metrics", {}).get("ram_available_gb", 0) > 0
                    ),
                )
                assert result["agent_version"]

            def jobs():
                return client.get("/api/v1/jobs?limit=100").json()["items"]

            def job(job_id):
                return next(item for item in jobs() if item["id"] == job_id)

            def create(node_id=None, duration=30):
                response = client.post(
                    "/api/v1/jobs",
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    json={
                        "kind": "test",
                        "node_id": node_id,
                        "payload": {
                            "duration_seconds": duration,
                            "seed": 73,
                            "max_cpu_percent": 100,
                        },
                        "max_attempts": 1,
                    },
                )
                assert response.status_code == 201, response.text
                return response.json()["id"]

            parallel = [create(node_id, 60) for node_id in node_ids]
            wait_value(
                jobs,
                lambda all_jobs: all(
                    any(
                        j["id"] == wanted and j["status"] == "RUNNING" for j in all_jobs
                    )
                    for wanted in parallel
                ),
            )
            sessions = client.get("/api/v1/sessions").json()["items"]
            assert (
                len({s["node_id"] for s in sessions if s["status"] == "RUNNING"}) == 2
            )
            for session in sessions:
                if session["job_id"] in parallel:
                    response = client.post(
                        "/api/v1/sessions/" + session["id"] + "/stop"
                    )
                    assert response.status_code == 200, response.text
            wait_value(
                lambda: client.get("/api/v1/sessions").json()["items"],
                lambda rows: all(s["status"] == "STOPPED" for s in rows),
            )
            assert all(job(wanted)["status"] == "CANCELLED" for wanted in parallel)
            assert (
                client.patch(
                    "/api/v1/control", json={"policy": "round_robin"}
                ).status_code
                == 200
            )
            rotation = [create(duration=1) for _ in range(4)]
            wait_value(
                jobs,
                lambda rows: all(
                    any(j["id"] == wanted and j["status"] == "COMPLETED" for j in rows)
                    for wanted in rotation
                ),
                120,
            )
            sessions = client.get("/api/v1/sessions?limit=100").json()["items"]
            rotated = [s for s in sessions if s["job_id"] in rotation]
            assert len(rotated) == 4 and {s["node_id"] for s in rotated} == set(
                node_ids
            )
            assert all(
                s["resource_usage"].get("synthetic")
                and s["resource_usage"].get("steps", 0) > 0
                for s in rotated
            )
            # Suspend one SCM host to simulate network/heartbeat loss while engine lives.
            recovery_job = create(node_ids[0], 180)
            wait_value(lambda: job(recovery_job), lambda j: j["status"] == "RUNNING")
            scm = win32service.OpenSCManager(
                None, None, win32service.SC_MANAGER_CONNECT
            )
            handle = win32service.OpenService(
                scm, names[0], win32service.SERVICE_QUERY_STATUS
            )
            try:
                pid = win32service.QueryServiceStatusEx(handle)["ProcessId"]
            finally:
                win32service.CloseServiceHandle(handle)
                win32service.CloseServiceHandle(scm)
            process = psutil.Process(pid)
            process.suspend()
            try:
                wait_value(
                    lambda: job(recovery_job), lambda j: j["status"] == "UNKNOWN", 45
                )
                assert node(node_ids[0])["status"] == "offline"
                blocked = create(node_ids[0], 1)
                assert job(blocked)["status"] == "QUEUED"
            finally:
                process.resume()
            wait_value(lambda: node(node_ids[0]), lambda n: n["status"] == "online")
            wait_value(lambda: job(recovery_job), lambda j: j["status"] == "RUNNING")
            assert (
                client.post("/api/v1/jobs/" + recovery_job + "/cancel").status_code
                == 200
            )
            wait_value(lambda: job(blocked), lambda j: j["status"] == "COMPLETED")
            # Both services reconnect after the actual bundled backend is stopped/restarted.
            server.stdin.write(b"shutdown\n")
            server.stdin.flush()
            server.wait(timeout=25)
            server = launch_server()
            wait_value(ready, lambda code: code == 200)
            for node_id in node_ids:
                previous = node(node_id).get("last_heartbeat")
                wait_value(
                    lambda node_id=node_id: node(node_id),
                    lambda n, previous=previous: (
                        n["status"] == "online" and n["last_heartbeat"] != previous
                    ),
                )
            final_job = create(duration=1)
            wait_value(lambda: job(final_job), lambda j: j["status"] == "COMPLETED")
            assert client.get("/api/v1/logs").json()["total"] > 0
    finally:
        for name in reversed(installed):
            try:
                service(name, "remove")
            except Exception:  # noqa: BLE001 - best-effort cleanup preserves original failure
                # Preserve original failure while leaving cleanup evidence in CI output.
                print("SCM cleanup failed for " + name)
        if server and server.poll() is None:
            server.stdin.write(b"shutdown\n")
            server.stdin.flush()
            try:
                server.wait(timeout=25)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        logs.close()
