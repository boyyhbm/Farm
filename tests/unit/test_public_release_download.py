import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / "scripts/verify-public-release.py"
spec = importlib.util.spec_from_file_location("public_release", path)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class Response(io.BytesIO):
    status = 200


def test_public_request_ignores_github_token_and_sends_no_cookies(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-value-never-forwarded")

    class Opener:
        def open(self, request, timeout):
            assert not request.has_header("Authorization")
            assert not request.has_header("Cookie")
            assert timeout == 90
            return Response(b"MZpayload")

    monkeypatch.setattr(release.urllib.request, "build_opener", lambda: Opener())
    with release.request("https://github.com/example/project/releases/download/v1.0.0/file.exe") as response:
        assert response.read() == b"MZpayload"


def test_public_verification_reads_complete_exe_and_validates_digest(monkeypatch):
    content = b"MZ" + b"payload" * 1000
    monkeypatch.setattr(release, "request", lambda url: Response(content))
    expected = {"size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    asset = {"name": "installer.exe", "browser_download_url": "https://github.com/example/file.exe"}
    result = release.verify_asset(asset, expected)
    assert result["downloaded_bytes"] == len(content)
    assert result["passed"] and not result["github_authentication_sent"]


def test_public_verification_rejects_truncated_or_replaced_download(monkeypatch):
    monkeypatch.setattr(release, "request", lambda url: Response(b"MZtruncated"))
    asset = {"name": "installer.exe", "browser_download_url": "https://github.com/example/file.exe"}
    with pytest.raises(RuntimeError, match="differs"):
        release.verify_asset(asset, {"size_bytes": 100, "sha256": "0" * 64})
