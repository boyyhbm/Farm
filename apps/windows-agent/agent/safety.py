"""Independent local resource guard; persists holds while disconnected and across restart."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from security.credentials import restrict_path

log = logging.getLogger("farm-agent.safety")

ALLOWED_REASONS = {
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

LIMITS = SimpleNamespace(
    cpu_temperature_limit=95,
    gpu_temperature_limit=90,
    ram_percent_limit=97,
    min_ram_available_gb=0.5,
    min_disk_free_gb=0.5,
)


def critical(metrics):
    def value(key):
        v = metrics.get(key)
        return v if type(v) in (int, float) else None

    result = []
    if any(
        value(k) is not None and value(k) >= limit
        for k, limit in [
            ("cpu_temperature", LIMITS.cpu_temperature_limit),
            ("gpu_temperature", LIMITS.gpu_temperature_limit),
        ]
    ):
        result.append("OVERHEATING")
    if (
        value("ram_percent") is not None
        and value("ram_percent") >= LIMITS.ram_percent_limit
    ) or (
        value("ram_available_gb") is not None
        and value("ram_available_gb") < LIMITS.min_ram_available_gb
    ):
        result.append("LOW_MEMORY")
    if (
        value("disk_free_gb") is not None
        and value("disk_free_gb") < LIMITS.min_disk_free_gb
    ):
        result.append("LOW_DISK")
    return result


class LocalSafetyGuard:
    def __init__(self, config, processes, store):
        self.config, self.processes, self.store = config, processes, store
        self.path = config.data_dir / "safety-state.json"
        self.reasons = []
        self.metrics = {}
        self.integrity = None
        self.observed_at = datetime.now(UTC).isoformat()
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text())
                reasons = value["reasons"]
                if not isinstance(reasons, list) or any(
                    not isinstance(r, str) or r not in ALLOWED_REASONS for r in reasons
                ):
                    raise ValueError("Invalid guard state")
                self.reasons = reasons
            except (OSError, ValueError, KeyError, TypeError):
                self.reasons = ["CONFIGURATION_CORRUPT"]

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"reasons": self.reasons}))
        restrict_path(temporary)
        temporary.replace(self.path)
        restrict_path(self.path)

    def evidence(self):
        return {
            "held": bool(self.reasons),
            "reasons": self.reasons,
            "observed_at": self.observed_at,
            "source": "local_safety_guard",
        }

    async def check_integrity(self, capture=False):
        from security.integrity import capture_game_baseline, inspect

        if capture:
            if self.processes.running():
                raise ValueError(
                    "Stop managed processes before recording a game baseline"
                )
            await asyncio.to_thread(capture_game_baseline, self.config)
        self.integrity = await asyncio.to_thread(inspect, self.config)
        return self.integrity

    async def sample(self):
        from collectors.metrics import collect

        self.metrics = await collect(self.config)
        self.observed_at = datetime.now(UTC).isoformat()
        reasons = critical(self.metrics)
        if self.integrity and self.integrity["status"] == "FAILED":
            codes = {f["code"] for f in self.integrity["findings"]}
            if "AGENT_UPDATE_FAILED" in codes:
                reasons.append("AGENT_UPDATE_FAILED")
            if self.integrity["game_files_status"] == "CHANGED":
                reasons.append("GAME_FILES_CHANGED")
            elif "AGENT_UPDATE_FAILED" not in codes or any(
                f["status"] == "FAILED" and f["code"] != "AGENT_UPDATE_FAILED"
                for f in self.integrity["findings"]
            ):
                reasons.append("COMPONENT_INTEGRITY_FAILED")
        if reasons:
            changed = sorted(set(self.reasons) | set(reasons))
            if changed != self.reasons:
                self.reasons = changed
                self.save()
        if self.reasons:
            await self.stop_owned()
        return self.metrics

    async def stop_owned(self):
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
                    "error": "Local Safety: " + ",".join(self.reasons),
                },
            )

    async def hold(self, reasons):
        self.reasons = sorted(set(self.reasons) | set(reasons))
        self.save()
        await self.stop_owned()

    async def release(self):
        await self.check_integrity()
        await self.sample()
        if critical(self.metrics) or self.integrity["status"] == "FAILED":
            raise ValueError(
                "Local checks still fail; repair the issue before review release"
            )
        self.reasons = []
        self.save()
        return {"integrity": self.integrity, "safety": self.evidence()}

    async def run(self, stopping):
        next_integrity = 0
        while not stopping.is_set():
            try:
                now = asyncio.get_running_loop().time()
                if now >= next_integrity:
                    await self.check_integrity()
                    next_integrity = now + 300
                await self.sample()
            except Exception as exc:  # noqa: BLE001 - a monitor failure must fail closed and preserve the hold
                log.error("Local safety monitor failed (%s)", type(exc).__name__)
                # A failed guard cannot silently permit new managed work.
                self.reasons = sorted(set(self.reasons) | {"LOCAL_MONITOR_FAILED"})
                self.observed_at = datetime.now(UTC).isoformat()
                self.metrics = {}
                try:
                    self.save()
                    await self.stop_owned()
                except Exception as stop_error:  # noqa: BLE001 - keep the guard alive and held when a local stop or storage operation fails
                    log.error(
                        "Local safety stop or persistence failed (%s)",
                        type(stop_error).__name__,
                    )
            try:
                await asyncio.wait_for(stopping.wait(), 5)
            except TimeoutError:
                pass
