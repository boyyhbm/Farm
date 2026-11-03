"""Live API + PostgreSQL/Redis + two actual Linux agent processes integration.

Requires local API :8000 and scheduler worker; .env admin credentials are loaded
without printing them. Linux agents run only explicitly synthetic test sessions.
"""
from __future__ import annotations
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from agent.config import Config
from agent.main import run

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "tests/integration/agent-report.json"
BASE = os.environ.get("FARM_TEST_URL", "http://127.0.0.1:8000")


def main():
    started = time.monotonic()
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    report = {"executed_at": datetime.now(timezone.utc).isoformat(), "environment": "Linux, two real Python agents on one host; synthetic gameplay only",
              "checks": [], "node_ids": [], "job_ids": []}
    processes = []
    nodes = []
    jobs = []
    client = httpx.Client(base_url=BASE + "/api/v1", headers={"Origin": "http://localhost:3000"}, timeout=20)
    def check(name, assertion, **details):
        if not assertion:
            raise AssertionError(f"{name}: {details}")
        report["checks"].append({"name": name, "passed": True, **details})
        print(f"PASS {name}", flush=True)
    def post(path, data=None):
        response = client.post(path, json=data)
        response.raise_for_status()
        return response.json()
    def get(path):
        response = client.get(path)
        response.raise_for_status()
        return response.json()
    def wait_for(callback, timeout=40):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            latest = callback()
            if latest:
                return latest
            time.sleep(0.5)
        raise TimeoutError(f"Condition not met after {timeout}s; latest={latest}")
    def states():
        return {job["id"]: job["status"] for job in get("/jobs?page_size=200")["items"] if job["id"] in jobs}
    def parallel_state():
        observed = states()
        return observed if list(observed.values()).count("RUNNING") == 2 and list(observed.values()).count("QUEUED") == 1 else None
    def online_nodes():
        return [n for n in get("/nodes?page_size=200")["items"] if n["id"] in nodes and n["status"] == "online"]
    def one_job(job_id):
        return next(job for job in get("/jobs?page_size=200")["items"] if job["id"] == job_id)
    def create(node_id=None, duration=2):
        import uuid
        response = client.post("/jobs", headers={"Idempotency-Key": str(uuid.uuid4())}, json={
            "kind": "test", "node_id": node_id, "payload": {"duration_seconds": duration, "seed": 37}, "timeout_seconds": max(120, duration + 60), "max_attempts": 1})
        response.raise_for_status()
        job_id = response.json()["id"]
        jobs.append(job_id)
        report["job_ids"].append(job_id)
        return job_id
    success = False
    try:
        auth = post("/auth/login", {"username": env["ADMIN_USERNAME"], "password": env["ADMIN_PASSWORD"]})
        client.headers["X-CSRF-Token"] = auth["csrf_token"]
        with tempfile.TemporaryDirectory(prefix="farm-agent-integration-") as temporary:
            root = Path(temporary)
            for index in (1, 2):
                directory = root / f"node-{index}"
                directory.mkdir()
                config_path = directory / "config.json"
                config_path.write_text(json.dumps({"server_url": BASE, "data_dir": str(directory),
                                                   "allow_insecure_localhost": True, "heartbeat_seconds": 5}))
                token = post("/nodes/enrollment-tokens")["token"]
                asyncio.run(run(str(config_path), token))
                from security.credentials import read_credentials
                node_id = read_credentials(directory / "credentials.dat")["node_id"]
                nodes.append(node_id)
                report["node_ids"].append(node_id)
                output = (directory / "console.log").open("wb")
                process = subprocess.Popen([sys.executable, "-m", "agent.main", "--config", str(config_path)],
                                           stdout=output, stderr=output, cwd=ROOT)
                processes.append((process, output))
            online = wait_for(lambda: online_nodes() if len(online_nodes()) == 2 else None)
            check("two_real_agents_online_with_telemetry", len(online) == 2 and all(n["metrics"].get("ram_available_gb", 0) > 0 for n in online))
            post("/control/pause")
            initial = [create(duration=2) for _ in range(3)]
            check("queue_paused_retains_jobs", all(states()[j] == "QUEUED" for j in initial))
            post("/control/resume")
            overlap = wait_for(parallel_state, timeout=40)
            check("two_nodes_parallel_third_queued", True, observed=overlap)
            wait_for(lambda: all(states()[j] == "COMPLETED" for j in initial), timeout=50)
            check("parallel_then_sequential_queue_completed", True)
            stop_job = create(node_id=nodes[0], duration=60)
            wait_for(lambda: one_job(stop_job)["status"] == "RUNNING", timeout=30)
            post(f"/jobs/{stop_job}/cancel")
            wait_for(lambda: any(s["job_id"] == stop_job and s["status"] == "STOPPED" for s in get("/sessions?page_size=200")["items"]), timeout=30)
            check("cancel_stops_owned_real_process", one_job(stop_job)["status"] == "CANCELLED")
            disconnect_job = create(node_id=nodes[0], duration=90)
            wait_for(lambda: one_job(disconnect_job)["status"] == "RUNNING", timeout=30)
            processes[0][0].send_signal(signal.SIGSTOP)
            offline_started = time.monotonic()
            wait_for(lambda: one_job(disconnect_job)["status"] == "UNKNOWN", timeout=65)
            check("disconnected_agent_job_becomes_unknown", True, elapsed_seconds=round(time.monotonic() - offline_started, 2))
            processes[0][0].send_signal(signal.SIGCONT)
            wait_for(lambda: one_job(disconnect_job)["status"] == "RUNNING", timeout=35)
            check("reconnect_reconciles_existing_owned_process", True)
            post(f"/jobs/{disconnect_job}/cancel")
            wait_for(lambda: any(s["job_id"] == disconnect_job and s["status"] == "STOPPED" for s in get("/sessions?page_size=200")["items"]), timeout=30)
            check("stop_after_reconnect", True)
            success = True
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for job_id in jobs:
            try:
                if one_job(job_id)["status"] not in {"COMPLETED", "CANCELLED", "FAILED", "TIMED_OUT"}:
                    post(f"/jobs/{job_id}/cancel")
            except Exception:
                pass
        for process, output in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            output.close()
        for node_id in nodes:
            try:
                post(f"/nodes/{node_id}/revoke")
            except Exception:
                pass
        try:
            post("/control/resume")
        except Exception:
            pass
        report["passed"] = success
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        client.close()


if __name__ == "__main__":
    main()
