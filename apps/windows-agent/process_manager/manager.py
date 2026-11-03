from __future__ import annotations
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import psutil


class ProcessManager:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.handles: dict[str, subprocess.Popen] = {}
        self.logs: dict[str, object] = {}
        self.jobs: dict[str, object] = {}
        self.stopping: set[str] = set()

    @staticmethod
    def owned(record):
        try:
            proc = psutil.Process(record["pid"])
            return proc if abs(proc.create_time() - record["created"]) < 0.01 and proc.is_running() else None
        except psutil.Error:
            return None

    def validate(self, kind, payload):
        if not isinstance(payload, dict):
            raise ValueError("Payload must be an object")
        if kind == "test":
            if set(payload) - {"duration_seconds", "seed", "tick_seconds", "max_steps", "grid_size", "min_ram_gb", "max_cpu_percent"}:
                raise ValueError("Unknown synthetic test parameter")
            # Constructor validation is mirrored synchronously before process creation.
            if not 0.1 <= float(payload.get("duration_seconds", 60)) <= 86400:
                raise ValueError("duration_seconds must be 0.1..86400")
            if not 0.01 <= float(payload.get("tick_seconds", 0.1)) <= 10:
                raise ValueError("tick_seconds must be 0.01..10")
            if not 1 <= int(payload.get("max_steps", 100000)) <= 1000000:
                raise ValueError("max_steps must be 1..1000000")
            if not 2 <= int(payload.get("grid_size", 20)) <= 1000:
                raise ValueError("grid_size must be 2..1000")
            int(payload.get("seed", 1))
            return
        if kind not in {"steam", "cs2"}:
            raise ValueError("Only test, steam and cs2 applications are allowed")
        if set(payload) - {"duration_seconds", "seed", "min_ram_gb", "max_cpu_percent"}:
            raise ValueError("Steam/CS2 do not accept remote launch arguments; authorize manually in official Steam client")
        path = self.config.steam_path if kind == "steam" else self.config.cs2_path
        if not path or not Path(path).is_file():
            raise ValueError(f"{kind} is not configured or installed")
        if os.name != "nt":
            raise ValueError("Steam/CS2 operations require a Windows interactive user session")
        # Steam launchers may hand off to a pre-existing instance: never claim ownership of it.
        if kind == "steam":
            for proc in psutil.process_iter(["name"]):
                if (proc.info["name"] or "").lower() == "steam.exe":
                    raise ValueError("Steam is already running; finish manual authorization, then schedule CS2")

    def start(self, session_id, command_id, kind, payload):
        self.validate(kind, payload)
        if not session_id or len(session_id) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in session_id):
            raise ValueError("Invalid session ID")
        records = self.store.processes()
        if any(r["session_id"] == session_id for r in records):
            raise ValueError("Session is already tracked")
        active = [r for r in records if self.owned(r)]
        if kind == "test" and sum(r["kind"] == "test" for r in active) >= self.config.test_capacity:
            raise ValueError("Synthetic test capacity exhausted")
        if kind != "test" and any(r["kind"] != "test" for r in active):
            raise ValueError("One interactive Steam/CS2 process session per node")
        if kind == "test":
            source_root = Path(__file__).resolve().parents[1]
            installed_python = source_root.parent / "venv" / "Scripts" / "python.exe"
            interpreter = str(installed_python) if os.name == "nt" and installed_python.is_file() else sys.executable
            if Path(interpreter).name.lower() == "pythonservice.exe":
                raise RuntimeError("Installed Python interpreter not found for synthetic test subprocess")
            argv = ([sys.executable, "engine"] if getattr(sys, "frozen", False) else [interpreter, "-m", "gameplay_test_engine.run"]) + ["--session", session_id,
                    "--config", json.dumps({k: v for k, v in payload.items() if k not in {"min_ram_gb", "max_cpu_percent"}}, separators=(",", ":"), allow_nan=False)]
        else:
            argv = [self.config.steam_path if kind == "steam" else self.config.cs2_path]
        log_path = self.config.data_dir / "process-logs" / f"{session_id}.log"
        log_path.parent.mkdir(exist_ok=True)
        # Fixed binary argv only; shell=False on every platform.
        log = log_path.open("ab", buffering=0)
        try:
            if os.name == "nt" and os.environ.get("CS2_FARM_SERVICE") == "1" and kind != "test":
                from process_manager.windows import launch_interactive
                proc = launch_interactive(argv)
            else:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        shell=False, cwd=str(Path(__file__).resolve().parents[1]) if kind == "test" else None, creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0)
            if os.name == "nt":
                from process_manager.windows import attach_job
                self.jobs[session_id] = attach_job(proc.pid)
                if hasattr(proc, "resume"):
                    proc.resume()
            created = psutil.Process(proc.pid).create_time()
            self.store.add_process(session_id, command_id, kind, proc.pid, created)
        except Exception:
            log.close()
            job = self.jobs.pop(session_id, None)
            if job:
                job.Close()
            if "proc" in locals():
                proc.terminate()
                proc.wait(timeout=5)
                if hasattr(proc, "close"):
                    proc.close()
            raise
        self.handles[session_id] = proc
        self.logs[session_id] = log
        return proc.pid

    async def stop(self, session_id):
        self.stopping.add(session_id)
        try:
            await self._stop(session_id)
        finally:
            self.stopping.discard(session_id)

    async def _stop(self, session_id):
        record = next((r for r in self.store.processes() if r["session_id"] == session_id), None)
        if not record:
            return
        job = self.jobs.pop(session_id, None)
        if job:
            import win32job
            win32job.TerminateJobObject(job, 1)
            job.Close()
        root = self.owned(record)
        if root:
            try:
                descendants = root.children(recursive=True)
            except psutil.Error:
                descendants = []
            # Creation-time guard prevents PID reuse from targeting unrelated processes.
            targets = descendants + [root]
            for proc in targets:
                try:
                    proc.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = await asyncio.to_thread(psutil.wait_procs, targets, timeout=5)
            for proc in alive:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
            await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
        handle = self.handles.pop(session_id, None)
        if handle:
            await asyncio.to_thread(handle.wait, timeout=5)
            if hasattr(handle, "close"):
                handle.close()
        log = self.logs.pop(session_id, None)
        if log:
            log.close()
        self.store.remove_process(session_id)

    def poll(self):
        events = []
        for record in self.store.processes():
            session_id = record["session_id"]
            if session_id in self.stopping:
                continue
            handle = self.handles.get(session_id)
            exit_code = handle.poll() if handle else None
            if self.owned(record) and exit_code is None:
                continue
            event = {"type": "ack", "command_id": record["command_id"], "pid": record["pid"],
                     "status": "STOPPED" if exit_code == 0 else "FAILED", "exit_code": exit_code,
                     "error": None if exit_code == 0 else (f"Process exited with code {exit_code}" if exit_code is not None else
                                                          "Owned process disappeared after agent restart; exit code unavailable")}
            log_path = self.config.data_dir / "process-logs" / f"{session_id}.log"
            if record["kind"] == "test" and log_path.exists():
                try:
                    with log_path.open("rb") as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 8192))
                        line = stream.read().decode(errors="replace").strip().splitlines()[-1]
                    event["resource_usage"] = json.loads(line)
                except (OSError, ValueError, IndexError):
                    pass
            events.append(event)
            self.store.ack(record["command_id"], event)
            self.store.remove_process(session_id)
            self.handles.pop(session_id, None)
            if handle and hasattr(handle, "close"):
                handle.close()
            job = self.jobs.pop(session_id, None)
            if job:
                job.Close()
            log = self.logs.pop(session_id, None)
            if log:
                log.close()
        return events

    def running(self):
        output = []
        for record in self.store.processes():
            proc = self.owned(record)
            if proc:
                try:
                    output.append({"session_id": record["session_id"], "pid": proc.pid, "kind": record["kind"],
                                   "create_time": record["created"], "ram_mb": round(proc.memory_info().rss / 1024**2, 2)})
                except psutil.Error:
                    pass
        return output
