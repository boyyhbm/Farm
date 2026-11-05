"""Verify GitHub release downloads with no GitHub credentials or cookies."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.request


def request(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Farm-Public-Download-Check/1.0"})
    # urllib does not consult gh credentials or .netrc; no cookie/auth handlers.
    assert not req.has_header("Authorization")
    return urllib.request.build_opener().open(req, timeout=90)


def verify_asset(asset, expected):
    digest = hashlib.sha256()
    total = 0
    prefix = b""
    with request(asset["browser_download_url"]) as response:
        status = response.status
        if status != 200:
            raise RuntimeError("Public download did not return HTTP 200")
        while chunk := response.read(4 * 1024 * 1024):
            if not prefix:
                prefix = chunk[:2]
            digest.update(chunk)
            total += len(chunk)
    if prefix != b"MZ" or total != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
        raise RuntimeError("Public executable differs from the verified build manifest")
    return {"filename": asset["name"], "url": asset["browser_download_url"],
            "http_status": status, "downloaded_bytes": total, "sha256": digest.hexdigest(),
            "github_authentication_sent": False, "cookies_sent": False, "passed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", args.tag):
        raise ValueError("Invalid version tag")
    with request(f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}") as response:
        release = json.load(response)
    if release["draft"] or not release["prerelease"]:
        raise RuntimeError("Expected a published pre-release")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))["artifacts"]
    names = ("CS2-Farm-Manager-Setup.exe", "CS2-Farm-Agent-Setup.exe")
    assets = {a["name"]: a for a in release["assets"]}
    if any(name not in assets or name not in manifest for name in names):
        raise RuntimeError("Both verified installers must be attached")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(verify_asset, assets[name], manifest[name]) for name in names]
        results = [future.result() for future in futures]
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "repository": args.repo,
              "release_id": release["id"], "tag": args.tag, "release_url": release["html_url"],
              "draft": release["draft"], "prerelease": release["prerelease"],
              "verification": "Full HTTP GET and SHA-256 with no GitHub Authorization header or cookies",
              "installers": results, "passed": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
