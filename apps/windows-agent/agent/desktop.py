"""Desktop enrollment entrypoints; credentials and enrollment tokens never leave IPC output.

Electron calls these functions through its bundled runtime. No network listener or
firewall change is created. A remote server must already be available over HTTPS.
"""
from __future__ import annotations

import json
import os
import ssl
import tempfile
from pathlib import Path

from agent.config import Config
from agent.main import run
from collectors.metrics import hardware
from security.credentials import restrict_path, write_credentials
from transport.client import enroll

_ALLOWED = {"server_url", "token", "ca_certificate", "steam_path", "cs2_path", "heartbeat_seconds"}


async def enroll_desktop(data_dir: Path, payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) - _ALLOWED:
        raise ValueError("Unknown desktop enrollment field")
    if not isinstance(payload.get("token"), str) or not 20 <= len(payload["token"]) <= 256:
        raise ValueError("A valid one-time enrollment code is required")
    if not isinstance(payload.get("server_url"), str):
        raise ValueError("HTTPS server address is required")
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    restrict_path(data_dir)
    if (data_dir / "credentials.dat").exists():
        raise ValueError("Agent is already connected; revoke it on the server before replacing its credentials")

    ca_file = None
    certificate = payload.get("ca_certificate")
    if certificate is not None:
        if not isinstance(certificate, str) or not 1 <= len(certificate.encode()) <= 32768:
            raise ValueError("Trusted certificate must be a PEM certificate smaller than 32 KB")
        # Parse before saving. Never suppress hostname verification or allow verify=False.
        ssl.create_default_context().load_verify_locations(cadata=certificate)
        ca_file = data_dir / "trusted-server-ca.pem"
        ca_file.write_text(certificate, encoding="ascii")
        restrict_path(ca_file)

    discovered = discover_applications()
    values = {
        "server_url": payload["server_url"].strip(),
        "data_dir": str(data_dir),
        "heartbeat_seconds": payload.get("heartbeat_seconds", 10),
        "steam_path": _validate_application_path(payload.get("steam_path", discovered["steam_path"]), "steam.exe"),
        "cs2_path": _validate_application_path(payload.get("cs2_path", discovered["cs2_path"]), "cs2.exe"),
        "ca_file": str(ca_file) if ca_file else None,
        "allow_insecure_localhost": False,
    }
    config_path = data_dir / "config.json"
    temporary = data_dir / "config.pending.json"
    try:
        temporary.write_text(json.dumps(values, indent=2), encoding="utf-8")
        restrict_path(temporary)
        config = Config.load(str(temporary))
        credential = await enroll(config, payload["token"], hardware(config))
        # DPAPI binds credentials to the interactive Windows account running Electron.
        write_credentials(data_dir / "credentials.dat", credential)
        temporary.replace(config_path)
        restrict_path(config_path)
        return {"node_id": credential["node_id"], "status": "enrolled"}
    finally:
        temporary.unlink(missing_ok=True)


async def run_desktop(data_dir: Path) -> int:
    config_path = Path(data_dir).resolve() / "config.json"
    if not config_path.is_file():
        raise ValueError("Finish the agent connection wizard first")
    return await run(str(config_path))



def _validate_application_path(value, basename: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Choose the installed {basename} file")
    path = Path(value)
    if not path.is_absolute() or path.name.casefold() != basename:
        raise ValueError(f"Application path must point to an absolute existing {basename} file")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Installed {basename} file was not found") from exc
    if not resolved.is_file() or resolved.name.casefold() != basename:
        raise ValueError(f"Application must be an existing {basename} file")
    return str(resolved)


def _read_desktop_config(data_dir: Path) -> tuple[Path, dict]:
    config_path = Path(data_dir).resolve() / "config.json"
    if not config_path.is_file():
        raise ValueError("Finish the agent connection wizard first")
    values = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise ValueError("Agent configuration is invalid")
    return config_path, values


def get_applications(data_dir: Path) -> dict:
    """Return local paths and observed availability, without exposing credentials."""
    _, values = _read_desktop_config(data_dir)
    result = {}
    for name, basename in (("steam", "steam.exe"), ("cs2", "cs2.exe")):
        value = values.get(name + "_path")
        result[name + "_path"] = value if isinstance(value, str) else None
        try:
            result[name + "_available"] = _validate_application_path(value, basename) is not None
        except ValueError:
            result[name + "_available"] = False
    return result


def configure_applications(data_dir: Path, payload: dict) -> dict:
    """A local GUI may select only Steam/CS2 binaries; no launch arguments are accepted."""
    if not isinstance(payload, dict) or set(payload) - {"steam_path", "cs2_path"}:
        raise ValueError("Only Steam and CS2 application paths may be changed")
    config_path, values = _read_desktop_config(data_dir)
    for key, basename in (("steam_path", "steam.exe"), ("cs2_path", "cs2.exe")):
        if key in payload:
            values[key] = _validate_application_path(payload[key], basename)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".applications-", suffix=".json", dir=config_path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(values, indent=2), encoding="utf-8")
        restrict_path(temporary)
        Config.load(str(temporary))
        temporary.replace(config_path)
        restrict_path(config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return get_applications(data_dir)


def discover_applications() -> dict:
    """Read installed Steam locations only. Other libraries remain a GUI selection."""
    found = {"steam_path": None, "cs2_path": None}
    if os.name != "nt":
        return found
    import winreg
    candidates = []
    for hive, subkey, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                location, _ = winreg.QueryValueEx(key, value)
            if isinstance(location, str):
                candidates.append(Path(location) if value == "SteamExe" else Path(location) / "steam.exe")
        except OSError:
            continue
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Steam" / "steam.exe")
    for candidate in candidates:
        try:
            steam = _validate_application_path(str(candidate), "steam.exe")
        except ValueError:
            continue
        found["steam_path"] = steam
        cs2 = Path(steam).parent / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "bin" / "win64" / "cs2.exe"
        try:
            found["cs2_path"] = _validate_application_path(str(cs2), "cs2.exe")
        except ValueError:
            pass
        break
    return found
