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
        if result.returncode != 0 or not answer.get("ok"):
            protected = (
                Path(os.environ["ProgramFiles"]) / "CS2FarmAgent" / name / "runtime"
            )
            for candidate in (
                protected.parent,
                protected,
                protected / "python.exe",
                protected / "desktop_entry.py",
            ):
                permissions = subprocess.run(
                    ["icacls", str(candidate)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                print(permissions.stdout)
            executable = protected / (
                "farm-runtime.exe" if frozen.exists() else "python.exe"
            )
            if executable.exists():
                try:
                    probe = subprocess.run(
                        [
                            str(executable),
                            *(
                                []
                                if frozen.exists()
                                else [str(protected / "desktop_entry.py")]
                            ),
                            "--help",
                        ],
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                    print(
                        "Protected runtime local probe:",
                        probe.returncode,
                        probe.stderr.decode(errors="replace")[:2000],
                    )
                except OSError as exc:
                    print(
                        "Protected runtime local probe:", type(exc).__name__, str(exc)
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

        def stop_server(process):
            if process.stdin and not process.stdin.closed:
                process.stdin.write(b"shutdown\n")
                process.stdin.flush()
                process.stdin.close()  # same private-pipe EOF as Electron supervisor
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                print(
                    "BUNDLED SERVER SHUTDOWN LOG:",
                    (tmp_path / "server.log").read_text(errors="replace")[-6000:],
                )
                raise

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
            print(
                "WINDOWS_CHECK two SYSTEM services installed with distinct enrollment"
            )

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

            print("WINDOWS_CHECK real WSS telemetry from both services")

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
            print("WINDOWS_CHECK parallel RUNNING on two nodes")
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
            print("WINDOWS_CHECK controlled stop and round-robin policy")
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
            print(
                "WINDOWS_CHECK four completed synthetic jobs rotated across both nodes"
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
            print("WINDOWS_CHECK UNKNOWN and reconciliation after heartbeat loss")
            # Both services reconnect after the actual bundled backend is stopped/restarted.
            stop_server(server)
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
            print("WINDOWS_CHECK both services reconnect after backend restart")
            # A local service stop must not replay stale RUNNING after restart.
            interrupted = create(node_ids[0], 60)
            wait_value(lambda: job(interrupted), lambda j: j["status"] == "RUNNING")
            service(names[0], "stop")
            service(names[0], "start")
            interrupted_result = wait_value(
                lambda: job(interrupted), lambda j: j["status"] == "FAILED"
            )
            assert "stopped locally" in interrupted_result["error"]
            print(
                "WINDOWS_CHECK local service restart reconciles interrupted job as FAILED"
            )
            # Real protected runtime replacement retains both SYSTEM identities.
            for name, node_id in zip(names, node_ids, strict=True):
                before_heartbeat = node(node_id)["last_heartbeat"]
                updated = service(name, "refresh")
                assert updated["status"] == "running" and updated["automatic_start"]
                wait_value(
                    lambda node_id=node_id: node(node_id),
                    lambda n, before=before_heartbeat: (
                        n["status"] == "online" and n["last_heartbeat"] != before
                    ),
                )
            print("WINDOWS_CHECK service runtime replacement preserves identities")
            # Protection Engine 2.0: a real acknowledged, persistent local hold.
            review = {
                "confirmed": True,
                "note": "Explicit installed protection review test",
            }
            held_id = node_ids[0]
            held = client.post(f"/api/v1/protection/nodes/{held_id}/hold", json=review)
            assert held.status_code == 202, held.text
            wait_value(
                lambda: node(held_id),
                lambda n: n["metrics"].get("local_safety", {}).get("held") is True,
            )
            service(names[0], "stop")
            service(names[0], "start")
            wait_value(
                lambda: node(held_id),
                lambda n: (
                    n["status"] == "online"
                    and n["metrics"].get("local_safety", {}).get("held") is True
                ),
            )
            healthy_work = create(duration=1)
            wait_value(lambda: job(healthy_work), lambda j: j["status"] == "COMPLETED")
            safe_sessions = client.get("/api/v1/sessions?limit=100").json()["items"]
            assert (
                next(s for s in safe_sessions if s["job_id"] == healthy_work)["node_id"]
                == node_ids[1]
            )
            previous_integrity = node(held_id)["safety"]["last_integrity_id"]
            checked = client.post(
                f"/api/v1/protection/nodes/{held_id}/integrity-check", json=review
            )
            assert checked.status_code == 202, checked.text
            wait_value(
                lambda: node(held_id),
                lambda n: n["safety"]["last_integrity_id"] != previous_integrity,
            )
            detail = client.get("/api/v1/protection/nodes/safety?page_size=100").json()[
                "items"
            ]
            report = next(n for n in detail if n["id"] == held_id)["integrity"]
            assert (
                report["status"] != "FAILED" and report["report"]["files_checked"] > 0
            )
            released = client.post(
                f"/api/v1/protection/nodes/{held_id}/release", json=review
            )
            assert released.status_code == 202, released.text
            wait_value(
                lambda: node(held_id),
                lambda n: (
                    n["safety"]["state"] in ("READY", "WARNING")
                    and n["metrics"].get("local_safety", {}).get("held") is False
                ),
            )
            verified_work = create(held_id, 1)
            wait_value(lambda: job(verified_work), lambda j: j["status"] == "COMPLETED")
            actions = client.get(
                f"/api/v1/protection/decisions?node_id={held_id}"
            ).json()["items"]
            assert {"QUARANTINE_NODE", "REQUEST_NODE_RELEASE", "RELEASE_NODE"} <= {
                a["action"] for a in actions
            }
            print(
                "WINDOWS_CHECK protection v2 persistent hold, other-node isolation, real file checks and explicit agent-confirmed release"
            )
            assert client.get("/api/v1/logs").json()["total"] > 0
    finally:
        import sys

        original_failure = sys.exc_info()[0] is not None
        cleanup_errors = []
        for name in reversed(installed):
            try:
                service(name, "remove")
                remaining = invoke("service-status", extra=("--service-name", name))
                assert not remaining["installed"]
                runtime_parent = (
                    Path(os.environ["ProgramFiles"]) / "CS2FarmAgent" / name
                )
                assert not runtime_parent.exists(), (
                    "Protected executable payload was not removed"
                )
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup preserves original failure
                # Preserve original failure while leaving cleanup evidence in CI output.
                cleanup_errors.append(name + ": " + str(exc))
                print("SCM cleanup failed for " + name + ": " + str(exc))
        if server and server.poll() is None:
            try:
                stop_server(server)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        logs.close()
        if cleanup_errors and not original_failure:
            pytest.fail("SCM cleanup failed: " + "; ".join(cleanup_errors))
