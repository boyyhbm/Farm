from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Config:
    server_url: str
    data_dir: Path
    heartbeat_seconds: float = 10
    steam_path: str | None = None
    cs2_path: str | None = None
    test_capacity: int = 4
    allow_insecure_localhost: bool = False
    update_public_key: str | None = None
    allowed_update_host: str | None = None
    ca_file: str | None = None
    log_level: str = "INFO"
    automatic_updates: bool = False
    update_manifest_url: str | None = None
    update_check_seconds: int = 3600

    @classmethod
    def load(cls, path: str):
        values = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        values["data_dir"] = Path(
            os.path.expandvars(values.get("data_dir", "%PROGRAMDATA%/CS2FarmAgent"))
        ).resolve()
        config = cls(**values)
        parsed = urlparse(config.server_url)
        if parsed.scheme != "https":
            if not (
                config.allow_insecure_localhost
                and parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError(
                    "HTTPS is required; insecure localhost requires explicit opt-in"
                )
        if (
            parsed.username
            or parsed.password
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid server URL")
        if config.ca_file:
            import ssl

            ca = Path(config.ca_file)
            if not ca.is_absolute() or not ca.is_file():
                raise ValueError("Trusted CA must be an existing absolute local path")
            ssl.create_default_context().load_verify_locations(cafile=str(ca))
        if not 5 <= config.heartbeat_seconds <= 15:
            raise ValueError("Heartbeat interval must be 5..15 seconds")
        if not 1 <= config.test_capacity <= 32:
            raise ValueError("test_capacity must be 1..32")
        if config.automatic_updates and (
            not config.update_public_key
            or not config.allowed_update_host
            or not config.update_manifest_url
        ):
            raise ValueError(
                "Automatic updates require pinned key, host and manifest URL"
            )
        if config.update_check_seconds < 60:
            raise ValueError("Update interval must be at least 60 seconds")
        config.server_url = config.server_url.rstrip("/")
        for executable in (config.steam_path, config.cs2_path):
            if executable and not Path(executable).is_absolute():
                raise ValueError("Allowed executable path must be absolute")
            # A removed game must be reported as an installation fault, not take telemetry offline.
        return config
