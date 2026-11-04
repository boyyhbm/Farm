from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import psutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from agent import __version__
from agent.config import Config
from agent.store import Store
from collectors.metrics import collect, hardware
from process_manager.manager import ProcessManager
from security.credentials import read_credentials, write_credentials, restrict_path
from transport.client import enroll, reconnecting

log = logging.getLogger("farm-agent")


class Agent:
    def __init__(self, config, credentials):
        self.config = config
        self.credentials = credentials
        self.store = Store(config.data_dir / "journal.sqlite3")
        restrict_path(config.data_dir / "journal.sqlite3")
        self.processes = ProcessManager(config, self.store)
        self.stopping = asyncio.Event()
        self.restart_requested = False
        self._last_maintenance = 0
        self._command_lock = asyncio.Lock()
        from agent.safety import LocalSafetyGuard

        self.safety = LocalSafetyGuard(config, self.processes, self.store)

    async def command(self, command):
        """Strict command vocabulary and durable replay protection before mutation."""
        async with self._command_lock:
            allowed_keys = {
                "type",
                "command_id",
                "operation",
                "session_id",
                "kind",
                "payload",
                "deadline",
            }
            command_id = command.get("command_id")
            if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
                raise ValueError("Missing or invalid command ID")
            ack = {"type": "ack", "command_id": command_id, "status": "FAILED"}
            canonical = json.dumps(
                command, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if len(canonical) > 16384:
                raise ValueError("Command exceeds maximum size")
            fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
            repeated, previous = self.store.reserve(command_id, fingerprint)
            if repeated:
                return previous or {
                    **ack,
                    "error": "Previous command interrupted before commit; refusing duplicate launch",
                }
            try:
                if set(command) - allowed_keys:
                    raise ValueError("Unknown command property")
                deadline = command.get("deadline")
                if not deadline:
                    raise ValueError("Command requires an expiry deadline")
                expires = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
                if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                    raise ValueError("Command expired or deadline lacks timezone")
                operation = command.get("operation")
                if operation == "start":
                    if self.safety.integrity is None:
                        await self.safety.check_integrity()
                    await self.safety.sample()
                    if self.safety.reasons:
                        raise ValueError(
                            "Local safety review required: "
                            + ",".join(self.safety.reasons)
                        )
                    if self.processes.running():
                        raise ValueError(
                            "Node concurrency limit reached; one managed session is allowed"
                        )
                    session_id = command.get("session_id")
                    pid = self.processes.start(
                        session_id,
                        command_id,
                        command.get("kind"),
                        command.get("payload") or {},
                    )
                    ack.update(status="RUNNING", pid=pid)
                elif operation == "stop":
                    session_id = command.get("session_id")
                    if not isinstance(session_id, str):
                        raise ValueError("Stop command requires a session ID")
                    # Mark originating launch STOPPED too, so reconnect cannot replay stale RUNNING.
                    record = next(
                        (
                            r
                            for r in self.store.processes()
                            if r["session_id"] == session_id
                        ),
                        None,
                    )
                    await self.processes.stop(session_id)
                    if record:
                        self.store.ack(
                            record["command_id"],
                            {
                                "type": "ack",
                                "command_id": record["command_id"],
                                "status": "STOPPED",
                                "exit_code": None,
                            },
                        )
                    ack.update(status="STOPPED")
                elif operation == "safety_hold":
                    reasons = (command.get("payload") or {}).get("reasons", [])
                    permitted = {
                        "OVERHEATING",
                        "LOW_MEMORY",
                        "LOW_DISK",
                        "SUSTAINED_CPU",
                        "COMPONENT_INTEGRITY_FAILED",
                        "GAME_FILES_CHANGED",
                        "LOCAL_SAFETY_HOLD",
                        "AGENT_VERSION_UNSUPPORTED",
                        "AGENT_UPDATE_FAILED",
                        "CONFIGURATION_CORRUPT",
                        "AGENT_CRASH",
                        "LOCAL_MONITOR_FAILED",
                        "STOP_RETRY_EXHAUSTED",
                    }
                    if (
                        not isinstance(reasons, list)
                        or not reasons
                        or len(reasons) > 12
                        or any(r not in permitted for r in reasons)
                    ):
                        raise ValueError("Invalid safety hold reasons")
                    await self.safety.hold(reasons)
                    ack.update(status="STOPPED")
                elif operation in {
                    "integrity_check",
                    "game_baseline_capture",
                    "safety_release",
                    "repair_acknowledge",
                }:
                    payload = command.get("payload") or {}
                    if set(payload) - {"confirmed"}:
                        raise ValueError("Unsupported integrity parameter")
                    if operation == "repair_acknowledge":
                        if payload.get("confirmed") is not True:
                            raise ValueError(
                                "Explicit administrator repair confirmation is required"
                            )
                        (self.config.data_dir / "update-failure.json").unlink(
                            missing_ok=True
                        )
                        evidence = {"integrity": await self.safety.check_integrity()}
                    elif operation == "safety_release":
                        evidence = await self.safety.release()
                    else:
                        capture = operation == "game_baseline_capture"
                        if capture and payload.get("confirmed") is not True:
                            raise ValueError(
                                "Explicit official Steam verification confirmation is required"
                            )
                        evidence = {
                            "integrity": await self.safety.check_integrity(
                                capture=capture
                            )
                        }
                        await self.safety.sample()
                    ack.update(status="STOPPED", resource_usage=evidence)
                elif operation == "restart_agent":
                    if self.processes.running():
                        raise ValueError(
                            "Stop managed sessions before restarting the agent"
                        )
                    self.restart_requested = True
                    ack.update(status="STOPPED")
                else:
                    raise ValueError("Operation is not on the agent allowlist")
            except (ValueError, TypeError, OSError, RuntimeError, psutil.Error) as exc:
                log.warning("Command %s failed: %s", command_id, str(exc))
                ack["error"] = str(exc)[:1000]
            self.store.ack(command_id, ack)
            return ack

    async def connected(self, websocket):
        send_lock = asyncio.Lock()

        async def send(message):
            async with send_lock:
                await websocket.send(json.dumps(message, allow_nan=False))

        await send(
            {
                "type": "log",
                "message": f"Agent {__version__} connected; managed process count {len(self.processes.running())}",
            }
        )
        # Reconcile completion while disconnected before replaying durable results.
        self.processes.poll()
        for ack in self.store.latest_acks():
            await send(ack)

        async def receiver():
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                    if (
                        isinstance(message, dict)
                        and message.get("type") == "heartbeat_ack"
                    ):
                        continue
                    if (
                        not isinstance(message, dict)
                        or message.get("type") != "command"
                    ):
                        raise ValueError("Unsupported agent message")
                    result = await self.command(message)
                    await send(result)
                    if result.get("error"):
                        await send(
                            {
                                "type": "log",
                                "message": f"Command {result['command_id']}: {result['error']}",
                            }
                        )
                    if self.restart_requested:
                        self.stopping.set()
                        return
                except (ValueError, TypeError) as exc:
                    log.warning("Rejected malformed command: %s", str(exc))

        async def heartbeat():
            while not self.stopping.is_set():
                if time.monotonic() - self._last_maintenance > 3600:
                    self.store.prune()
                    active = {p["session_id"] for p in self.store.processes()}
                    for path in (self.config.data_dir / "process-logs").glob("*.log"):
                        if (
                            path.stem not in active
                            and path.stat().st_mtime < time.time() - 30 * 86400
                        ):
                            path.unlink(missing_ok=True)
                    self._last_maintenance = time.monotonic()
                for ack in self.processes.poll():
                    await send(ack)
                await send(
                    {
                        "type": "heartbeat",
                        "agent_version": __version__,
                        "metrics": self.safety.metrics or await collect(self.config),
                        "safety": self.safety.evidence(),
                        "integrity": self.safety.integrity,
                        "processes": self.processes.running(),
                        "hardware": hardware(self.config),
                    }
                )
                try:
                    await asyncio.wait_for(
                        self.stopping.wait(), self.config.heartbeat_seconds
                    )
                except TimeoutError:
                    pass

        tasks = [asyncio.create_task(receiver()), asyncio.create_task(heartbeat())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def update_watch(self):
        from security.updates import stage_update

        while not self.stopping.is_set():
            try:
                if not self.processes.running():
                    destination = await stage_update(
                        self.config.update_manifest_url, self.config
                    )
                    if destination:
                        if self.processes.running():
                            log.info(
                                "Signed release staged; activation deferred until managed sessions finish"
                            )
                            try:
                                await asyncio.wait_for(
                                    self.stopping.wait(),
                                    self.config.update_check_seconds,
                                )
                            except TimeoutError:
                                pass
                            continue
                        script = (
                            Path(__file__).resolve().parents[1]
                            / "installer"
                            / "Apply-Update.ps1"
                        )
                        if (
                            os.name != "nt"
                            or os.environ.get("CS2_FARM_SERVICE") != "1"
                            or not script.is_file()
                        ):
                            raise RuntimeError(
                                "Automatic activation requires installed Windows service"
                            )
                        install_dir = script.parents[2]
                        subprocess.Popen(
                            [
                                "powershell.exe",
                                "-NoProfile",
                                "-NonInteractive",
                                "-File",
                                str(script),
                                "-Release",
                                str(destination),
                                "-InstallDir",
                                str(install_dir),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                            | subprocess.DETACHED_PROCESS,
                        )
                        log.info(
                            "Verified signed agent update handed to local installer"
                        )
                        self.stopping.set()
                        return
            except Exception as exc:
                log.error("Signed update check failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self.stopping.wait(), self.config.update_check_seconds
                )
            except TimeoutError:
                pass

    async def run(self):
        guard = asyncio.create_task(self.safety.run(self.stopping))
        updater = (
            asyncio.create_task(self.update_watch())
            if self.config.automatic_updates
            else None
        )
        try:
            await reconnecting(
                self.config, self.credentials, self.connected, self.stopping
            )
        finally:
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
            if updater:
                updater.cancel()
                await asyncio.gather(updater, return_exceptions=True)
            # Disconnect alone preserves sessions; stopping service safely terminates owned trees.
            self.processes.poll()
            for record in self.store.processes():
                await self.processes.stop(record["session_id"])
                self.store.ack(
                    record["command_id"],
                    {
                        "type": "ack",
                        "command_id": record["command_id"],
                        "status": "FAILED",
                        "pid": record["pid"],
                        "exit_code": None,
                        "error": "Agent stopped locally before session completion",
                    },
                )
            self.store.close()
        return 75 if self.restart_requested else 0


def configure_logging(config):
    handler = RotatingFileHandler(
        config.data_dir / "agent.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )
    # HTTP and websocket debug output can expose Authorization headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def run(config_path, enroll_token=None):
    config = Config.load(config_path)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    restrict_path(config.data_dir)
    configure_logging(config)
    credentials_path = config.data_dir / "credentials.dat"
    if enroll_token:
        if credentials_path.exists():
            raise ValueError(
                "Already enrolled; revoke the old node before replacing its local credential"
            )
        write_credentials(
            credentials_path, await enroll(config, enroll_token, hardware(config))
        )
        log.info("Enrollment completed; credentials stored with local OS protection")
        return 0
    if not credentials_path.exists():
        raise RuntimeError(
            "Agent is not enrolled; run enrollment as the service identity"
        )
    agent = Agent(config, read_credentials(credentials_path))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, agent.stopping.set)
        except (NotImplementedError, ValueError):
            pass
    return await agent.run()


def cli():
    parser = argparse.ArgumentParser(
        description="CS2 Farm Agent — restricted process orchestration"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--enroll",
        action="store_true",
        help="Read one-time token interactively; token never appears in command line",
    )
    parser.add_argument("--stage-update", metavar="HTTPS_MANIFEST_URL")
    args = parser.parse_args()
    if args.stage_update:
        from security.updates import stage_update

        config = Config.load(args.config)
        destination = asyncio.run(stage_update(args.stage_update, config))
        print(
            f"Verified update staged at {destination}; apply with installer/Apply-Update.ps1"
            if destination
            else "Agent is current; no newer signed release"
        )
        return
    import getpass

    token = getpass.getpass("One-time enrollment token: ") if args.enroll else None
    try:
        raise SystemExit(asyncio.run(run(args.config, token)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
