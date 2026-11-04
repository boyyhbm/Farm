"""Fixed Windows SCM operations for the graphical Agent. No remote shell interface.

Service code lives in an administrator-owned Program Files directory; SYSTEM
credentials are enrolled by a temporary SYSTEM service and never reused from the
interactive user's DPAPI store.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

NAMES = {"CS2FarmAgent", "CS2FarmAgentTestA", "CS2FarmAgentTestB"}


def _name(value):
    if value not in NAMES:
        raise ValueError("Unsupported service identity")
    return value


def paths(name="CS2FarmAgent"):
    _name(name)
    return Path(
        os.environ.get("ProgramFiles", "C:/Program Files")
    ) / "CS2FarmAgent" / name / "runtime", Path(
        os.environ.get("PROGRAMDATA", "C:/ProgramData")
    ) / name


def _windows(admin=False):
    if os.name != "nt":
        raise RuntimeError(
            "Windows Service Control Manager is available only on Windows"
        )
    if admin and not ctypes.windll.shell32.IsUserAnAdmin():
        raise PermissionError("Administrator confirmation is required")


def _protect(path, readable=False):
    flags = ["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
    if readable:
        flags.append("*S-1-5-32-545:(OI)(CI)RX")
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", *flags, "/T", "/C"],
        check=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    subprocess.run(
        ["icacls", str(path), "/setowner", "*S-1-5-32-544", "/T", "/C"],
        check=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _reject_reparse(root):
    if not root.is_absolute() or str(root).startswith("\\\\"):
        raise ValueError("Service runtime must use a local absolute path")
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Service runtime cannot contain links or reparse points")


def _command(root, enroll=False, name="CS2FarmAgent"):
    frozen = root / "farm-runtime.exe"
    python = root / "python.exe"
    entry = root / "desktop_entry.py"
    if frozen.is_file():
        args = [str(frozen), "service-host"]
    elif python.is_file() and entry.is_file():
        args = [str(python), str(entry), "service-host"]
    else:
        raise ValueError("Bundled runtime is missing; reinstall the Agent")
    args += ["--service-name", _name(name)]
    if enroll:
        args += ["--enroll"]
    return subprocess.list2cmdline(args)


def status(name="CS2FarmAgent"):
    _windows()
    _name(name)
    import pywintypes
    import win32service

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        try:
            service = win32service.OpenService(
                scm,
                name,
                win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG,
            )
        except pywintypes.error as exc:
            if exc.winerror == 1060:
                return {"installed": False, "status": "not_installed", "name": name}
            raise
        code = win32service.QueryServiceStatus(service)[1]
        labels = {1: "stopped", 2: "starting", 3: "stopping", 4: "running", 7: "paused"}
        config = win32service.QueryServiceConfig(service)
        return {
            "installed": True,
            "status": labels.get(code, "unknown"),
            "name": name,
            "automatic_start": config[1] == win32service.SERVICE_AUTO_START,
            "account": config[7],
            "image_path": config[3],
        }
    finally:
        if service:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(scm)


def _wait(service, expected, timeout=40):
    import win32service

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = win32service.QueryServiceStatus(service)
        if result[1] == expected:
            return
        time.sleep(0.25)
    raise TimeoutError(
        "Windows service did not reach the requested state; open diagnostics"
    )


def control(action, name="CS2FarmAgent"):
    _windows(admin=True)
    _name(name)
    if action not in ("start", "stop", "remove"):
        raise ValueError("Unsupported service action")
    import pywintypes
    import win32service

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        try:
            service = win32service.OpenService(
                scm, name, win32service.SERVICE_ALL_ACCESS
            )
        except pywintypes.error as exc:
            if exc.winerror == 1060:
                return {"installed": False, "status": "not_installed", "name": name}
            raise
        state = win32service.QueryServiceStatus(service)[1]
        if action == "start":
            if state != win32service.SERVICE_RUNNING:
                win32service.StartService(service, None)
            _wait(service, win32service.SERVICE_RUNNING)
        else:
            if state != win32service.SERVICE_STOPPED:
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                _wait(service, win32service.SERVICE_STOPPED)
            if action == "remove":
                win32service.DeleteService(service)
    finally:
        if service:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(scm)
    return (
        {"installed": False, "status": "removed", "name": name}
        if action == "remove"
        else status(name)
    )


def install(payload, source_root, name="CS2FarmAgent"):
    _windows(admin=True)
    _name(name)
    if not isinstance(payload, dict) or set(payload) - {
        "server_url",
        "token",
        "ca_certificate",
        "steam_path",
        "cs2_path",
        "heartbeat_seconds",
    }:
        raise ValueError("Unsupported service setup field")
    from urllib.parse import urlparse

    url = urlparse(payload.get("server_url", ""))
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("A verified HTTPS server is required")
    if (
        not isinstance(payload.get("token"), str)
        or not 20 <= len(payload["token"]) <= 200
    ):
        raise ValueError("Fresh one-time enrollment code required")
    source_root = Path(source_root).resolve()
    _reject_reparse(source_root)
    target, data = paths(name)
    existing = status(name)
    reuse = (data / "credentials.dat").exists()
    if reuse:
        saved = json.loads((data / "config.json").read_text(encoding="utf-8"))
        if saved["server_url"].rstrip("/") != payload["server_url"].rstrip("/"):
            raise ValueError(
                "Protected credentials belong to another server. Revoke that node before reconnecting."
            )
        trusted = data / "trusted-server-ca.pem"
        if payload.get("ca_certificate") and (
            not trusted.exists()
            or trusted.read_text().strip() != payload["ca_certificate"].strip()
        ):
            raise ValueError(
                "Protected server CA differs. Existing credentials were preserved."
            )
        enrolled = json.loads((data / "enroll.result.json").read_text())
    if existing["installed"]:
        if not reuse:
            raise ValueError(
                "Service exists without valid enrollment data; remove it through System Management"
            )
        if existing["status"] != "running":
            control("start", name)
        return {**status(name), "node_id": enrolled["node_id"], "reused_identity": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    _protect(target.parent, readable=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source_root, target)
    _protect(target, readable=True)
    data.mkdir(parents=True, exist_ok=True)
    _protect(data)
    import win32service

    if not reuse:
        request = data / "enroll.request.json"
        result = data / "enroll.result.json"
        request.write_text(json.dumps(payload), encoding="utf-8")
        result.unlink(missing_ok=True)
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        temporary = None
        try:
            temporary = win32service.CreateService(
                scm,
                name + "Enroll",
                name + " enrollment",
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_DEMAND_START,
                win32service.SERVICE_ERROR_NORMAL,
                _command(target, True, name),
                None,
                0,
                None,
                None,
                None,
            )
            win32service.StartService(temporary, None)
            _wait(temporary, win32service.SERVICE_STOPPED, 90)
            if not result.exists():
                raise RuntimeError("SYSTEM enrollment did not return a result")
            enrolled = json.loads(result.read_text())
            if enrolled.get("status") != "enrolled":
                raise RuntimeError(enrolled.get("error", "SYSTEM enrollment failed"))
        finally:
            request.unlink(missing_ok=True)
            if temporary:
                try:
                    win32service.DeleteService(temporary)
                finally:
                    win32service.CloseServiceHandle(temporary)
            win32service.CloseServiceHandle(scm)
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    service = None
    try:
        service = win32service.CreateService(
            scm,
            name,
            "CS2 Farm Node Agent" if name == "CS2FarmAgent" else name,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_AUTO_START,
            win32service.SERVICE_ERROR_NORMAL,
            _command(target, False, name),
            None,
            0,
            None,
            None,
            None,
        )
        win32service.ChangeServiceConfig2(
            service,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": 86400,
                "RebootMsg": "",
                "Command": "",
                "Actions": [
                    (win32service.SC_ACTION_RESTART, 5000),
                    (win32service.SC_ACTION_RESTART, 15000),
                    (win32service.SC_ACTION_NONE, 0),
                ],
            },
        )
        win32service.StartService(service, None)
        _wait(service, win32service.SERVICE_RUNNING)
    finally:
        if service:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(scm)
    return {**status(name), "node_id": enrolled["node_id"]}


def host(name="CS2FarmAgent", enroll=False):
    _windows()
    _name(name)
    import servicemanager
    import win32service
    import win32serviceutil

    _, data = paths(name)
    if enroll:

        class Hosted(win32serviceutil.ServiceFramework):
            _svc_name_ = name + "Enroll"

            def SvcStop(self):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)

            def SvcDoRun(self):
                request = data / "enroll.request.json"
                result = data / "enroll.result.json"
                try:
                    from agent.desktop import enroll_desktop

                    response = asyncio.run(
                        enroll_desktop(data, json.loads(request.read_text()))
                    )
                except Exception as exc:
                    response = {
                        "status": "failed",
                        "error": type(exc).__name__ + ": " + str(exc)[:1000],
                    }
                finally:
                    request.unlink(missing_ok=True)
                result.write_text(json.dumps(response), encoding="utf-8")
    else:
        from agent.service import FarmAgentService

        class Hosted(FarmAgentService):
            _svc_name_ = name

            async def serve(self, ignored):
                return await super().serve(data / "config.json")

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(Hosted)
    servicemanager.StartServiceCtrlDispatcher()


def applications(payload, name="CS2FarmAgent"):
    _windows(admin=True)
    from agent.desktop import configure_applications

    _, data = paths(name)
    running = status(name).get("status") == "running"
    if running:
        control("stop", name)
    try:
        return configure_applications(data, payload)
    finally:
        if running:
            control("start", name)
