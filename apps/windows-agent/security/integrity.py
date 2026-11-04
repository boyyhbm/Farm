"""Read-only file integrity and Authenticode observations. No process memory or VAC access."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def stamp():
    return datetime.now(UTC).isoformat()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def signature(path):
    if os.name != "nt":
        return "UNKNOWN"
    try:
        # The path is data in an environment variable, never interpolated into PowerShell code.
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; (Get-AuthenticodeSignature -LiteralPath $env:FARM_SIGNATURE_FILE).Status.ToString()",
            ],
            env={**os.environ, "FARM_SIGNATURE_FILE": str(path)},
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        value = result.stdout.strip()
        return {
            "Valid": "VALID",
            "NotSigned": "NOT_SIGNED",
            "HashMismatch": "INVALID",
            "NotTrusted": "INVALID",
            "UnknownError": "UNKNOWN",
            "NotSupportedFileFormat": "UNKNOWN",
        }.get(value, "UNKNOWN")
    except (OSError, subprocess.TimeoutExpired):
        return "UNKNOWN"


def finding(code, status, component, explanation):
    return {
        "code": code,
        "status": status,
        "component": component,
        "explanation": explanation,
    }


def own_components(root=None):
    root = Path(
        root
        or (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else RUNTIME_ROOT
        )
    ).resolve()
    manifest = root / "component-integrity.json"
    if not manifest.is_file():
        return (
            [
                finding(
                    "BASELINE_UNAVAILABLE",
                    "UNKNOWN",
                    "farm_runtime",
                    "No packaged integrity baseline is available",
                )
            ],
            0,
            "not_available",
        )
    try:
        data = json.loads(manifest.read_text())
        files = data["files"]
        if (
            data.get("schema") != 1
            or not isinstance(files, dict)
            or not 1 <= len(files) <= 20000
        ):
            raise ValueError("Invalid manifest")
        results = []
        checked = 0
        for name, expected in files.items():
            target = (root / name).resolve()
            if (
                not target.is_relative_to(root)
                or not isinstance(expected, str)
                or len(expected) != 64
            ):
                raise ValueError("Invalid baseline path or digest")
            checked += 1
            if not target.is_file() or digest(target) != expected:
                results.append(
                    finding(
                        "COMPONENT_HASH_MISMATCH",
                        "FAILED",
                        "farm_runtime",
                        "A packaged component is missing or changed",
                    )
                )
                if len(results) >= 20:
                    break
        if not results:
            results.append(
                finding(
                    "COMPONENT_HASHES_MATCH",
                    "PASS",
                    "farm_runtime",
                    f"{checked} files match the packaged baseline",
                )
            )
        # A hash file in an unsigned installer is a corruption check, not publisher authentication.
        results.append(
            finding(
                "BASELINE_NOT_AUTHENTICATED",
                "WARNING",
                "farm_runtime",
                "Unsigned local baseline; verify installer provenance and Authenticode separately",
            )
        )
        return results, checked, "unsigned_local_manifest"
    except (ValueError, KeyError, OSError, TypeError):
        return (
            [
                finding(
                    "COMPONENT_MANIFEST_INVALID",
                    "FAILED",
                    "farm_runtime",
                    "The packaged baseline cannot be validated",
                )
            ],
            0,
            "not_available",
        )


def game_files(config):
    files = {}
    for kind in ("steam", "cs2"):
        value = getattr(config, kind + "_path", None)
        if value:
            files[kind + ".exe"] = Path(value).resolve()
    steam = files.get("steam.exe")
    cs2 = files.get("cs2.exe")
    if steam:
        for name in ("steamclient.dll", "steamclient64.dll"):
            if (steam.parent / name).is_file():
                files[name] = steam.parent / name
    if cs2:
        for name in ("engine2.dll",):
            if (cs2.parent / name).is_file():
                files[name] = cs2.parent / name
        # Standard game/bin/win64/cs2.exe -> game/csgo/bin/win64/client.dll.
        client = cs2.parent.parent.parent / "csgo" / "bin" / "win64" / "client.dll"
        if client.is_file():
            files["client.dll"] = client
    if cs2:
        for directory, label in (
            (cs2.parent, "cs2_bin"),
            (cs2.parent.parent.parent / "csgo" / "bin" / "win64", "csgo_bin"),
        ):
            for path in directory.glob("*"):
                if path.is_file() and path.suffix.lower() in (".dll", ".exe"):
                    files[label + "/" + path.name] = path.resolve()
                    if len(files) > 512:
                        raise ValueError("Too many executable game components")
    return files


def capture_game_baseline(config):
    files = game_files(config)
    if not files or not files.get("steam.exe") or not files.get("cs2.exe"):
        raise ValueError(
            "Configure the real Steam and CS2 executables in the graphical application settings first"
        )
    if any(
        not p.is_file() or p.name.lower() != Path(name).name.lower()
        for name, p in files.items()
    ):
        raise ValueError("Unsupported application path or missing file")
    if sum(p.stat().st_size for p in files.values()) > 2 * 1024**3:
        raise ValueError("Selected baseline exceeds size limit")
    record = {
        "schema": 1,
        "checked_at": stamp(),
        "source": "operator_after_official_steam_verification",
        "files": {
            name: {"sha256": digest(p), "path": str(p)} for name, p in files.items()
        },
    }
    from security.credentials import restrict_path

    path = config.data_dir / "game-integrity-baseline.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    restrict_path(temporary)
    temporary.replace(path)
    restrict_path(path)


def inspect(
    config=None, *, root=None, source="agent_self_check", check_signatures=True
):
    findings, count, trust = own_components(root)
    game_status = "UNKNOWN"
    if config:
        files = game_files(config)
        for kind in ("steam", "cs2"):
            path = files.get(kind + ".exe")
            if not path:
                findings.append(
                    finding(
                        "APPLICATION_NOT_CONFIGURED",
                        "UNKNOWN",
                        kind,
                        "Select the installed executable using the local graphical application settings",
                    )
                )
                continue
            if not path.is_file() or path.name.lower() != kind + ".exe":
                findings.append(
                    finding(
                        "APPLICATION_INSTALLATION_INVALID",
                        "FAILED",
                        kind,
                        "Configured executable is missing or has an unexpected name",
                    )
                )
                continue
            if check_signatures:
                signed = signature(path)
                findings.append(
                    finding(
                        "EXECUTABLE_SIGNATURE_" + signed,
                        "FAILED"
                        if signed == "INVALID"
                        else "PASS"
                        if signed == "VALID"
                        else "WARNING",
                        kind,
                        "Windows Authenticode status: " + signed,
                    )
                )
        baseline = config.data_dir / "game-integrity-baseline.json"
        if baseline.exists():
            try:
                data = json.loads(baseline.read_text())
                expected = data["files"]
                if (
                    data.get("schema") != 1
                    or not isinstance(expected, dict)
                    or set(expected) != set(files)
                ):
                    raise ValueError("Application set changed")
                for name, record in expected.items():
                    if (
                        str(files[name]) != record["path"]
                        or not files[name].is_file()
                        or digest(files[name]) != record["sha256"]
                    ):
                        raise ValueError("Game file changed")
                game_status = "UNCHANGED_FROM_OPERATOR_BASELINE"
                findings.append(
                    finding(
                        "GAME_BASELINE_MATCH",
                        "PASS",
                        "steam_cs2",
                        "Selected files match the operator baseline; this does not prove every game file is unmodified",
                    )
                )
            except (ValueError, KeyError, TypeError, OSError):
                game_status = "CHANGED"
                findings.append(
                    finding(
                        "GAME_FILES_CHANGED",
                        "FAILED",
                        "steam_cs2",
                        "Selected files differ from the operator-approved baseline. Steam updates also require a new reviewed baseline",
                    )
                )
        else:
            findings.append(
                finding(
                    "GAME_BASELINE_UNAVAILABLE",
                    "UNKNOWN",
                    "steam_cs2",
                    "Official Steam file verification and an operator baseline are required; unsupported modifications cannot be inferred",
                )
            )
        update_failure = config.data_dir / "update-failure.json"
        if update_failure.exists():
            findings.append(
                finding(
                    "AGENT_UPDATE_FAILED",
                    "FAILED",
                    "agent_update",
                    "A previous update failed; inspect local repair diagnostics",
                )
            )
    if check_signatures and os.name == "nt":
        signed = signature(Path(sys.executable))
        findings.append(
            finding(
                "RUNTIME_SIGNATURE_" + signed,
                "FAILED"
                if signed == "INVALID"
                else "PASS"
                if signed == "VALID"
                else "WARNING",
                "python_runtime",
                "Windows Authenticode status: " + signed,
            )
        )
    statuses = {x["status"] for x in findings}
    status = (
        "FAILED"
        if "FAILED" in statuses
        else "WARNING"
        if "WARNING" in statuses
        else "UNKNOWN"
        if "UNKNOWN" in statuses
        else "PASS"
    )
    return {
        "status": status,
        "source": source,
        "checked_at": stamp(),
        "baseline_trust": trust,
        "files_checked": count,
        "findings": findings[:30],
        "game_files_status": game_status,
    }
