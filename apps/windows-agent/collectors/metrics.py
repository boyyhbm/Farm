from __future__ import annotations
import asyncio
import json
import os
import platform
import shutil
import psutil


def hardware(config):
    disk = psutil.disk_usage(str(config.data_dir.anchor))
    return {"cpu": platform.processor() or platform.machine(), "logical_cpus": psutil.cpu_count(),
            "ram_gb": round(psutil.virtual_memory().total / 1024**3, 2),
            "disk_gb": round(disk.total / 1024**3, 2), "gpu": None,
            "installed": {"steam": bool(config.steam_path), "cs2": bool(config.cs2_path), "test": True},
            "test_capacity": config.test_capacity}


async def collect(config):
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(config.data_dir.anchor))
    temperature = None
    if hasattr(psutil, "sensors_temperatures"):
        sensors = psutil.sensors_temperatures()
        readings = [x.current for group in sensors.values() for x in group if x.current is not None]
        temperature = max(readings) if readings else None
    result = {"cpu_percent": psutil.cpu_percent(), "ram_percent": memory.percent,
              "ram_available_gb": round(memory.available / 1024**3, 3),
              "gpu_percent": None, "gpu_name": None,
              "cpu_temperature": temperature,
              "disk_free_gb": round(disk.free / 1024**3, 3), "disk_percent": disk.percent}
    # Optional vendor utility with constant args. Missing/unsupported metrics remain null.
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            proc = await asyncio.create_subprocess_exec(nvidia_smi, "--query-gpu=name,utilization.gpu", "--format=csv,noheader,nounits",
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), 2)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            first = output.decode(errors="replace").splitlines()[0]
            name, load = first.rsplit(",", 1)
            result.update(gpu_name=name.strip(), gpu_percent=float(load))
        except (OSError, ValueError, IndexError, TimeoutError):
            pass
    return result
