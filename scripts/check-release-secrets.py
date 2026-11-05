"""Scan release payloads without recording secret values or extracting to disk.

NSIS payloads are decompressed through 7-Zip; checking only compressed EXE bytes
would miss bundled configuration. This gate detects known secret formats and
exact sensitive values from the build environment, not every possible secret.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


RULES = {
    "github_token": rb"(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{40,255})",
    "aws_access_key": rb"(?<![A-Za-z0-9_+/])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9_+/])",
    "telegram_bot_token": rb"(?<![0-9])[0-9]{6,12}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])",
    "slack_token": rb"xox[baprs]-[A-Za-z0-9-]{25,255}",
    "stripe_live_secret": rb"(?:sk|rk)_live_[A-Za-z0-9]{20,255}",
    "private_key_material": (
        rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----\r?\n"
        rb"(?:[A-Za-z0-9+/=]+\r?\n){2,120}"
        rb"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
}
PATTERNS = {name: re.compile(pattern) for name, pattern in RULES.items()}
OVERLAP = 16384
CHUNK = 2 * 1024 * 1024


def sensitive_values(environment: dict[str, str]) -> list[bytes]:
    return list({
        value.encode()
        for name, value in environment.items()
        if re.search(r"(?:TOKEN|SECRET|PASSWORD|PRIVATE_KEY|API_KEY)", name, re.I)
        and 12 <= len(value) <= OVERLAP
        and not value.startswith(("/", "http://", "https://", "${", "***"))
    })


def scan_stream(stream, known: list[bytes], chunk_size: int = CHUNK):
    findings = set()
    tail = b""
    size = 0
    while chunk := stream.read(chunk_size):
        data = tail + chunk
        for name, pattern in PATTERNS.items():
            if pattern.search(data):
                findings.add(name)
        if any(value in data for value in known):
            findings.add("sensitive_environment_value")
        tail = data[-OVERLAP:]
        size += len(chunk)
    return size, sorted(findings)


def sensitive_filename(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (
        (name.startswith(".env") and name not in (".env.example", ".env.template"))
        or name.endswith((".sqlite", ".sqlite3", ".db", ".pfx", ".p12"))
        or name in (
            "id_rsa", "id_ed25519", "credentials.json", "tokens.json",
            "desktop-config.json", "agent-config.json", "safety-state.json",
        )
    )


def seven_zip():
    candidates = [os.environ.get("FARM_SEVEN_ZIP"), shutil.which("7z"),
                  shutil.which("7zz"), shutil.which("7zzs"),
                  r"C:\Program Files\7-Zip\7z.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError("7-Zip is required to inspect the decompressed NSIS payload")


def scan_installer(path: Path, known: list[bytes]):
    tool = seven_zip()
    listing = subprocess.run(
        [tool, "l", "-slt", "-sccUTF-8", str(path)], check=True,
        capture_output=True, encoding="utf-8", errors="replace",
    ).stdout
    paths = [line[7:] for line in listing.splitlines() if line.startswith("Path = ")][1:]
    names = [name for name in paths if sensitive_filename(name)]
    with path.open("rb") as source:
        raw_size, raw_findings = scan_stream(source, known)
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [tool, "x", "-so", str(path)], stdout=subprocess.PIPE, stderr=errors,
        )
        try:
            size, findings = scan_stream(process.stdout, known)
        finally:
            process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError("NSIS payload could not be fully decompressed")
    if not paths or size <= raw_size:
        raise RuntimeError("Incomplete NSIS payload inspection")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "filename": path.name, "size_bytes": raw_size, "sha256": digest,
        "payload_entries": len(paths), "decompressed_bytes_scanned": size,
        "findings": sorted(set(raw_findings + findings)),
        "sensitive_filenames": names,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, action="append", default=[])
    parser.add_argument("--files", type=Path, action="append", default=[])
    parser.add_argument("--source-commit")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    known = sensitive_values(dict(os.environ))
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "NSIS raw bytes and decompressed payloads, supplied release files, optional tracked source tar",
        "limitations": "Format patterns and exact sensitive environment values cannot prove absence of every possible secret; public CA certificates are allowed.",
        "rules": list(RULES) + ["sensitive_environment_value", "sensitive_configuration_filename"],
        "sensitive_environment_values_checked": len(known),
        "installers": [], "files": [], "passed": True,
    }
    for path in args.installer:
        item = scan_installer(path, known)
        report["installers"].append(item)
        report["passed"] &= not item["findings"] and not item["sensitive_filenames"]
    for folder in args.files:
        paths = sorted(folder.rglob("*")) if folder.is_dir() else [folder]
        for path in paths:
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                size, findings = scan_stream(stream, known)
            item = {"filename": path.name, "bytes_scanned": size, "findings": findings,
                    "sensitive_filename": sensitive_filename(path.name)}
            report["files"].append(item)
            report["passed"] &= not findings and not item["sensitive_filename"]
    if args.source_commit:
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
            raise ValueError("Source commit must be a full SHA")
        process = subprocess.Popen(
            ["git", "archive", "--format=tar", args.source_commit], stdout=subprocess.PIPE,
        )
        size, findings = scan_stream(process.stdout, known)
        process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError("Could not read exact release source tree")
        report["source"] = {"commit": args.source_commit, "bytes_scanned": size, "findings": findings}
        report["passed"] &= not findings
    if not report["installers"] and not report["files"]:
        raise ValueError("No release files supplied")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
