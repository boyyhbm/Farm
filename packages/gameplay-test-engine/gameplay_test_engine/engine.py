from __future__ import annotations

import asyncio
import hashlib
import json
import random
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any


class State(StrEnum):
    NEW = "NEW"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GameplayEngine(ABC):
    @abstractmethod
    async def initialize(self, config: dict): ...
    @abstractmethod
    async def start(self, session_id: str): ...
    @abstractmethod
    async def get_state(self): ...
    @abstractmethod
    async def pause(self): ...
    @abstractmethod
    async def resume(self): ...
    @abstractmethod
    async def stop(self): ...
    @abstractmethod
    async def get_metrics(self): ...


class SyntheticEngine(GameplayEngine):
    """Seeded grid navigation used only to test orchestration and FSM behavior.

    Determinism depends on seed and completed steps, not wall-clock scheduling.
    Each instance holds isolated state; no mutable module-level session registry.
    """
    def __init__(self):
        self.state = State.NEW
        self.session_id: str | None = None
        self.config: dict[str, Any] = {}
        self.task: asyncio.Task | None = None
        self._gate = asyncio.Event()
        self._gate.set()
        self._stopping = asyncio.Event()
        self._rng = random.Random()
        self.steps = 0
        self.position = [0, 0]
        self.collisions = 0
        self.trace: list[tuple[int, int]] = []
        self.error: str | None = None

    async def initialize(self, config):
        if self.state != State.NEW:
            raise ValueError("initialize is allowed only in NEW state")
        allowed = {"seed", "duration_seconds", "tick_seconds", "max_steps", "grid_size"}
        if set(config) - allowed:
            raise ValueError("Unknown synthetic engine option")
        self.config = {"seed": 1, "duration_seconds": 60, "tick_seconds": 0.1,
                       "max_steps": 100000, "grid_size": 20, **config}
        if not 0.01 <= float(self.config["tick_seconds"]) <= 10:
            raise ValueError("tick_seconds must be 0.01..10")
        if not 0.1 <= float(self.config["duration_seconds"]) <= 86400:
            raise ValueError("duration_seconds must be 0.1..86400")
        if not 1 <= int(self.config["max_steps"]) <= 1000000:
            raise ValueError("max_steps must be 1..1000000")
        if not 2 <= int(self.config["grid_size"]) <= 1000:
            raise ValueError("grid_size must be 2..1000")
        self._rng.seed(int(self.config["seed"]))
        self.state = State.READY

    async def start(self, session_id):
        if self.state == State.RUNNING and self.session_id == session_id:
            return
        if self.state != State.READY:
            raise ValueError(f"Cannot start from {self.state}")
        self.session_id = str(session_id)
        self.state = State.RUNNING
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        active_seconds = 0.0
        tick = float(self.config["tick_seconds"])
        try:
            while not self._stopping.is_set():
                await self._gate.wait()
                if self._stopping.is_set():
                    break
                dx, dy = self._rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
                x, y = self.position[0] + dx, self.position[1] + dy
                size = int(self.config["grid_size"])
                if 0 <= x < size and 0 <= y < size:
                    self.position[:] = [x, y]
                else:
                    self.collisions += 1
                self.steps += 1
                # Bounded trace prevents runaway telemetry memory growth.
                self.trace.append(tuple(self.position))
                if len(self.trace) > 2048:
                    del self.trace[0]
                active_seconds += tick
                if (self.steps >= int(self.config["max_steps"]) or
                        active_seconds >= float(self.config["duration_seconds"])):
                    self.state = State.COMPLETED
                    return
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=tick)
                except TimeoutError:
                    pass
            self.state = State.STOPPED
        except asyncio.CancelledError:
            self.state = State.STOPPED
            raise
        except Exception as exc:
            self.error = str(exc)
            self.state = State.FAILED

    async def get_state(self):
        return {"state": self.state.value, "session_id": self.session_id, "error": self.error}

    async def pause(self):
        if self.state == State.PAUSED:
            return
        if self.state != State.RUNNING:
            raise ValueError(f"Cannot pause from {self.state}")
        self.state = State.PAUSED
        self._gate.clear()

    async def resume(self):
        if self.state == State.RUNNING:
            return
        if self.state != State.PAUSED:
            raise ValueError(f"Cannot resume from {self.state}")
        self.state = State.RUNNING
        self._gate.set()

    async def stop(self):
        if self.state in {State.STOPPED, State.COMPLETED, State.FAILED}:
            return
        self._stopping.set()
        self._gate.set()
        if self.task:
            await self.task
        self.state = State.STOPPED

    async def get_metrics(self):
        return {**await self.get_state(), "synthetic": True, "seed": self.config.get("seed"),
                "steps": self.steps, "position": list(self.position), "collisions": self.collisions,
                "trace_digest": hashlib.sha256(json.dumps(self.trace).encode()).hexdigest()}
