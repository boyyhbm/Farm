"""Publication gate regressions; test tokens are generated, never real credentials."""

import importlib.util
import io
from pathlib import Path


path = Path(__file__).resolve().parents[2] / "scripts/check-release-secrets.py"
spec = importlib.util.spec_from_file_location("release_security", path)
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)


def test_prefixed_token_split_across_stream_chunks_is_detected():
    token = b"ghp_" + b"x" * 36
    payload = b"plain" * 7 + token + b"suffix"
    size, findings = security.scan_stream(io.BytesIO(payload), [], chunk_size=37)
    assert size == len(payload)
    assert findings == ["github_token"]


def test_build_secret_is_detected_without_leaking_its_value():
    value = b"opaque-test-credential-123"
    _, findings = security.scan_stream(io.BytesIO(b"before" + value + b"after"), [value], 10)
    assert findings == ["sensitive_environment_value"]
    assert value.decode() not in repr(findings)


def test_public_certificates_and_key_format_templates_are_not_private_material():
    public = b"-----BEGIN CERTIFICATE-----\n" + b"a" * 80 + b"\n-----END CERTIFICATE-----"
    template = b"-----BEGIN PRIVATE KEY-----"
    assert security.scan_stream(io.BytesIO(public + template), [])[1] == []
    private = b"-----BEGIN PRIVATE KEY-----\n" + b"a" * 64 + b"\n" + b"b" * 64 + b"\n-----END PRIVATE KEY-----"
    assert security.scan_stream(io.BytesIO(private), [])[1] == ["private_key_material"]


def test_runtime_credentials_and_databases_are_forbidden_payload_names():
    for name in ("resources/.env", "farm.sqlite3", "vault.pfx", "credentials.json", "desktop-config.json"):
        assert security.sensitive_filename(name)
    for name in (".env.example", "certifi/cacert.pem", "app/config.py", "component-integrity.json"):
        assert not security.sensitive_filename(name)


def test_sensitive_environment_filter_excludes_paths_and_redactions():
    values = security.sensitive_values({"GH_TOKEN": "opaque-value-123456", "NOT_A_KEY": "anything-12345",
        "API_KEY_PATH": "/tmp/credentials.json", "TOKEN_MASK": "***************"})
    assert values == [b"opaque-value-123456"]


def test_credential_formats_do_not_match_embedded_base64_or_jpeg_tables():
    aws = b"AKIA" + b"A" * 16
    assert security.scan_stream(io.BytesIO(b'"' + aws + b'"'), [])[1] == ["aws_access_key"]
    assert security.scan_stream(io.BytesIO(b"base64" + aws + b"suffix"), [])[1] == []
    token = b"123456789:" + b"a" * 35
    assert security.scan_stream(io.BytesIO(token), [])[1] == ["telegram_bot_token"]
    jpeg_table_fragment = b"456789:" + b"CDEFGHIJSTUVWXYZcdefghijstuvwxyz" + b"\x82\x83"
    assert security.scan_stream(io.BytesIO(jpeg_table_fragment), [])[1] == []
