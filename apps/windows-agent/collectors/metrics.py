from __future__ import annotations
import asyncio
import json
import os
import platform
import shutil
import subprocess
import psutil
from pathlib import Path


def hardware(config):
    disk = psutil.disk_usage(str(config.data_dir.anchor))
    return {
        "cpu": platform.processor() or platform.machine(),
        "logical_cpus": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 2),
        "disk_gb": round(disk.total / 1024**3, 2),
        "gpu": None,
        "installed": {
            "steam": bool(config.steam_path and Path(config.steam_path).is_file()),
            "cs2": bool(config.cs2_path and Path(config.cs2_path).is_file()),
            "test": True,
        },
        "test_capacity": config.test_capacity,
    }


async def collect(config):
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(config.data_dir.anchor))
    temperature = None
    if hasattr(psutil, "sensors_temperatures"):
        sensors = psutil.sensors_temperatures()
        readings = [
            x.current
            for group in sensors.values()
            for x in group
            if x.current is not None
        ]
        temperature = max(readings) if readings else None
    result = {
        "cpu_percent": psutil.cpu_percent(),
        "ram_percent": memory.percent,
        "ram_available_gb": round(memory.available / 1024**3, 3),
        "gpu_percent": None,
        "gpu_name": None,
        "gpu_temperature": None,
        "cpu_temperature_source": "psutil_sensor"
        if temperature is not None
        else "unavailable",
        "gpu_temperature_source": "unavailable",
        "cpu_temperature": temperature,
        "disk_free_gb": round(disk.free / 1024**3, 3),
        "disk_percent": disk.percent,
    }
    if os.name == "nt" and temperature is None:
        result["cpu_temperature"] = await optional_windows_temperature()
        if result["cpu_temperature"] is not None:
            result["cpu_temperature_source"] = "hardware_monitor_wmi"
    # Optional vendor utility with constant args. Missing/unsupported metrics remain null.
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            proc = await asyncio.create_subprocess_exec(
                nvidia_smi,
                "--query-gpu=name,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), 2)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            first = output.decode(errors="replace").splitlines()[0]
            name, load, gpu_temp = first.rsplit(",", 2)
            result.update(
                gpu_name=name.strip(),
                gpu_percent=float(load),
                gpu_temperature=float(gpu_temp),
                gpu_temperature_source="nvidia_smi",
            )
        except (OSError, ValueError, IndexError, TimeoutError):
            pass
    return result


_temperature_probe_due = 0.0
_last_cpu_temperature = None
_last_cpu_temperature_at = 0.0


async def optional_windows_temperature():
    """Read an already installed Libre/OpenHardwareMonitor WMI provider; never load a driver."""
    import math
    import time

    global _temperature_probe_due, _last_cpu_temperature, _last_cpu_temperature_at
    now = time.monotonic()
    if now < _temperature_probe_due:
        return _last_cpu_temperature if now - _last_cpu_temperature_at < 30 else None
    _temperature_probe_due = now + 300
    script = """$ErrorActionPreference='Stop'; foreach($ns in @('root/LibreHardwareMonitor','root/OpenHardwareMonitor')) { try { $t=Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop | Where-Object { $_.SensorType -eq 'Temperature' -and $_.Identifier -like '*cpu*' } | Select-Object -ExpandProperty Value; if($t) { ($t | Measure-Object -Maximum).Maximum.ToString([Globalization.CultureInfo]::InvariantCulture); break } } catch {} }"""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 3)
        value = float(output.decode().strip())
        if not math.isfinite(value) or not -20 <= value <= 150:
            return None
        _last_cpu_temperature = value
        _last_cpu_temperature_at = now
        _temperature_probe_due = now + 15
        return value
    except (OSError, ValueError, TimeoutError):
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        return None
