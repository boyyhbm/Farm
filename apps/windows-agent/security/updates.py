"""Signed update staging. Updates are local-admin initiated, never arbitrary WS commands."""
from __future__ import annotations
import base64
import hashlib
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_RELEASE_BYTES = 100 * 1024 * 1024


def verify_manifest(manifest: dict, public_key_b64: str):
    required = {"version", "url", "sha256", "signature"}
    if set(manifest) != required:
        raise ValueError("Invalid release manifest")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", manifest["version"]):
        raise ValueError("Invalid release version")
    if not re.fullmatch("[a-f0-9]{64}", manifest["sha256"]):
        raise ValueError("Invalid release SHA-256")
    signed = json.dumps({k: manifest[k] for k in ("version", "url", "sha256")}, sort_keys=True, separators=(",", ":")).encode()
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
    key.verify(base64.b64decode(manifest["signature"], validate=True), signed)
    return signed


def safe_extract(archive: Path, destination: Path):
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as release:
            if sum(info.file_size for info in release.infolist()) > MAX_RELEASE_BYTES:
                raise ValueError("Release expands beyond size limit")
            for info in release.infolist():
                path = PurePosixPath(info.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in info.filename or ":" in info.filename or
                        stat.S_ISLNK(info.external_attr >> 16)):
                    raise ValueError("Unsafe release member")
                target = (destination / Path(*path.parts)).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError("Release member escapes destination")
            release.extractall(destination)
        if not (destination / "agent" / "main.py").is_file():
            raise ValueError("Release must contain agent/main.py")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


async def stage_update(manifest_url: str, config):
    if not config.update_public_key or not config.allowed_update_host:
        raise ValueError("Pinned update key and host are required")
    def check_url(url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != config.allowed_update_host or parsed.username or parsed.password:
            raise ValueError("Update URL must use HTTPS on the locally pinned update host")
    check_url(manifest_url)
    from transport.client import tls_context
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, verify=tls_context(config)) as client:
        content = bytearray()
        async with client.stream("GET", manifest_url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 16384:
                    raise ValueError("Manifest too large")
        manifest = json.loads(content)
        verify_manifest(manifest, config.update_public_key)
        from agent import __version__
        version = lambda value: tuple(int(part) for part in value.split("-")[0].split("."))
        if version(manifest["version"]) <= version(__version__):
            return None  # No downgrade or same-version reinstallation.
        check_url(manifest["url"])
        release_dir = config.data_dir / "updates"
        release_dir.mkdir(exist_ok=True)
        archive = release_dir / "download.zip"
        digest = hashlib.sha256()
        total = 0
        try:
            async with client.stream("GET", manifest["url"]) as download:
                download.raise_for_status()
                with archive.open("wb") as output:
                    async for chunk in download.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_RELEASE_BYTES:
                            raise ValueError("Release exceeds size limit")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != manifest["sha256"]:
                raise ValueError("Release digest mismatch")
            destination = release_dir / manifest["version"]
            if destination.exists():
                shutil.rmtree(destination)
            safe_extract(archive, destination)
            # A fixed installation script performs atomic activation while service is stopped.
            return destination
        finally:
            archive.unlink(missing_ok=True)
