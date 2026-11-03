"""Create update manifest; keep private signing key offline, never on server/agent."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

parser = argparse.ArgumentParser()
parser.add_argument("--archive", type=Path, required=True)
parser.add_argument("--private-key", type=Path, required=True)
parser.add_argument("--version", required=True)
parser.add_argument("--url", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
key = load_pem_private_key(args.private_key.read_bytes(), password=None)
if not isinstance(key, Ed25519PrivateKey):
    raise ValueError("Ed25519 signing key required")
manifest = {"version": args.version, "url": args.url, "sha256": hashlib.file_digest(args.archive.open("rb"), "sha256").hexdigest()}
canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
manifest["signature"] = base64.b64encode(key.sign(canonical)).decode()
args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
