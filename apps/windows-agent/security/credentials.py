from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path


def restrict_path(path: Path):
    if os.name == "nt":
        # LocalSystem and current owner only; remove inherited permissions.
        owner = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True, creationflags=subprocess.CREATE_NO_WINDOW).strip().split(",")[-1].strip('"')
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{owner}:(OI)(CI)F" if path.is_dir() else f"*{owner}:F",
                        "*S-1-5-18:(OI)(CI)F" if path.is_dir() else "*S-1-5-18:F",
                        "*S-1-5-32-544:(OI)(CI)F" if path.is_dir() else "*S-1-5-32-544:F"], check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def write_credentials(path: Path, value: dict):
    # DPAPI protects the token at rest on Windows; service must enroll as same identity.
    raw = json.dumps(value).encode()
    if os.name == "nt":
        import win32crypt
        raw = win32crypt.CryptProtectData(raw, "CS2 Farm Agent", None, None, None, 0)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(raw)
    restrict_path(temporary)
    temporary.replace(path)
    restrict_path(path)


def read_credentials(path: Path):
    raw = path.read_bytes()
    if os.name == "nt":
        import win32crypt
        raw = win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1]
    return json.loads(raw)
