"""Verify real NSIS PE containers without claiming Windows execution."""

import hashlib, json, struct, zlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def verify(path):
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("Not a PE executable: " + str(path))
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\x00\x00":
        raise ValueError("Missing PE header")
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    directory = optional + (96 if magic == 0x10B else 112)
    certificate_size = struct.unpack_from("<I", data, directory + 4 * 8 + 4)[0]
    marker = data.find(b"\xef\xbe\xad\xdeNullsoftInst")
    if marker < 4:
        raise ValueError("NSIS installer header missing")
    header = struct.unpack_from("<7I", data, marker - 4)
    if marker - 4 + header[6] != len(data):
        raise ValueError("Installer truncated or unexpected trailing data")
    stored = struct.unpack_from("<I", data, len(data) - 4)[0]
    if zlib.crc32(data[512:-4]) & 0xFFFFFFFF != stored:
        raise ValueError("NSIS full-container CRC mismatch")
    return {
        "filename": path.name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pe_machine": hex(machine),
        "nsis_container": True,
        "crc32_valid": True,
        "length_valid": True,
        "authenticode": "unsigned" if certificate_size == 0 else "present-unverified",
        "windows_executed": False,
    }


if __name__ == "__main__":
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "PE structure, complete NSIS container CRC and checksum; no Windows installation or launch executed",
        "artifacts": [],
    }
    for product, name in (
        ("manager", "CS2-Farm-Manager-Setup.exe"),
        ("agent", "CS2-Farm-Agent-Setup.exe"),
    ):
        report["artifacts"].append(verify(ROOT / "artifacts/windows" / product / name))
    (ROOT / "docs/windows-artifact-verification.json").write_text(
        json.dumps(report, indent=2)
    )
    print(json.dumps(report, indent=2))
