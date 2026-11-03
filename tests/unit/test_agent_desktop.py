"""Desktop enrollment tests: no live server and no production mock adapter."""
import json
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agent import desktop
from agent.config import Config
from agent.store import Store
from process_manager import manager
from transport.client import tls_context


def ca_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Explicit test CA")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.mark.asyncio
async def test_gui_enroll_saves_credential_not_token_and_returns_no_secret(tmp_path, monkeypatch):
    credential = "test-secret-credential-should-not-leave-enrollment"
    token = "one-time-code-0123456789"
    async def fake_enroll(config, code, hardware):
        assert code == token
        assert config.server_url == "https://farm.example.test"
        assert config.allow_insecure_localhost is False
        assert Path(config.ca_file).is_file()
        context = tls_context(config)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        return {"node_id": "node-test", "credential": credential}
    monkeypatch.setattr(desktop, "enroll", fake_enroll)
    result = await desktop.enroll_desktop(tmp_path, {"server_url": "https://farm.example.test",
        "token": token, "ca_certificate": ca_pem()})
    assert result == {"node_id": "node-test", "status": "enrolled"}
    assert credential not in json.dumps(result)
    assert token not in "".join(p.read_text() for p in tmp_path.iterdir() if p.suffix in {".json", ".pem"})
    assert (tmp_path / "credentials.dat").is_file()
    assert not (tmp_path / "config.pending.json").exists()
    config = Config.load(str(tmp_path / "config.json"))
    assert config.ca_file == str(tmp_path / "trusted-server-ca.pem")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://example.test", "https://user:pass@example.test", "https://example.test/?token=secret", "https://example.test/#token"])
async def test_gui_enroll_rejects_unsafe_urls_without_network(tmp_path, monkeypatch, url):
    async def unexpected(*args):
        raise AssertionError("Network must not be called for rejected configuration")
    monkeypatch.setattr(desktop, "enroll", unexpected)
    with pytest.raises(ValueError):
        await desktop.enroll_desktop(tmp_path, {"server_url": url, "token": "a" * 32})
    assert not (tmp_path / "credentials.dat").exists()


@pytest.mark.asyncio
async def test_existing_credential_cannot_be_silently_replaced(tmp_path):
    (tmp_path / "credentials.dat").write_bytes(b"existing")
    with pytest.raises(ValueError, match="already connected"):
        await desktop.enroll_desktop(tmp_path, {"server_url": "https://farm.example.test", "token": "a" * 32})
    assert (tmp_path / "credentials.dat").read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_enrollment_failure_does_not_persist_token_or_credential(tmp_path, monkeypatch):
    token = "never-persist-this-code-123456"
    async def failed(*args):
        raise RuntimeError("Enrollment failed: HTTP 403")
    monkeypatch.setattr(desktop, "enroll", failed)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        await desktop.enroll_desktop(tmp_path, {"server_url": "https://farm.example.test", "token": token})
    assert not (tmp_path / "config.json").exists()
    assert not (tmp_path / "credentials.dat").exists()
    assert not (tmp_path / "config.pending.json").exists()


@pytest.mark.asyncio
async def test_runtime_reuses_enrolled_desktop_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    async def fake_run(path):
        assert path == str(tmp_path / "config.json")
        return 75
    monkeypatch.setattr(desktop, "run", fake_run)
    assert await desktop.run_desktop(tmp_path) == 75


def test_custom_ca_keeps_hostname_and_certificate_verification(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text(ca_pem())
    config = Config(server_url="https://farm.example.test", data_dir=tmp_path, ca_file=str(ca))
    context = tls_context(config)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_frozen_child_uses_runtime_engine_entrypoint(tmp_path, monkeypatch):
    import os
    captured = {}
    class FakePopen:
        pid = os.getpid()
        def __init__(self, argv, **kwargs): captured.update(argv=argv, kwargs=kwargs)
    config = Config(server_url="https://farm.example.test", data_dir=tmp_path)
    store = Store(tmp_path / "journal.sqlite3")
    pm = manager.ProcessManager(config, store)
    monkeypatch.setattr(manager.sys, "frozen", True, raising=False)
    monkeypatch.setattr(manager.subprocess, "Popen", FakePopen)
    pm.start("session-test", "command-test", "test", {"duration_seconds": 1})
    assert captured["argv"][:2] == [manager.sys.executable, "engine"]
    assert "-m" not in captured["argv"]
    assert captured["kwargs"]["shell"] is False
    pm.logs["session-test"].close()
    store.close()


@pytest.mark.asyncio
async def test_real_tls_enrollment_with_imported_ca_and_no_verification_bypass(tmp_path):
    """An actual TLS handshake, with a test-only HTTP enrollment endpoint."""
    import ipaddress
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test-only local enrollment")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(key, hashes.SHA256()))
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_file = tmp_path / 'test-only-server.pem'
    key_file = tmp_path / 'test-only-server.key'
    cert_file.write_bytes(pem)
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            response = json.dumps({'node_id':'actual-tls-node', 'credential':'test-only-credential'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        def log_message(self, *args):
            return
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = await desktop.enroll_desktop(tmp_path / 'agent', {
            'server_url':f'https://127.0.0.1:{server.server_port}',
            'token':'test-only-one-time-code-123', 'ca_certificate':pem.decode()})
        assert result == {'node_id':'actual-tls-node', 'status':'enrolled'}
        assert received[0]['token'] == 'test-only-one-time-code-123'
        # The same untrusted self-signed server is rejected without the explicit CA.
        import httpx
        with pytest.raises(httpx.ConnectError):
            await desktop.enroll_desktop(tmp_path / 'untrusted-agent', {
                'server_url':f'https://127.0.0.1:{server.server_port}', 'token':'test-only-one-time-code-123'})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_application_settings_saved_atomically_with_fixed_binaries(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'server_url':'https://farm.example.test', 'data_dir':str(tmp_path)}))
    steam = tmp_path / 'steam.exe'
    cs2 = tmp_path / 'cs2.exe'
    steam.write_bytes(b'explicit-test-executable')
    cs2.write_bytes(b'explicit-test-executable')
    result = desktop.configure_applications(tmp_path, {'steam_path':str(steam), 'cs2_path':str(cs2)})
    assert result == {'steam_path':str(steam), 'cs2_path':str(cs2), 'steam_available':True, 'cs2_available':True}
    assert Config.load(str(config)).steam_path == str(steam)
    assert not list(tmp_path.glob('.applications-*'))
    desktop.configure_applications(tmp_path, {'steam_path':None})
    assert desktop.get_applications(tmp_path)['steam_available'] is False
    assert desktop.get_applications(tmp_path)['cs2_available'] is True


@pytest.mark.parametrize('bad', ['relative/steam.exe', '/missing/steam.exe', '/bin/sh', 42])
def test_application_settings_reject_unapproved_paths_without_mutation(tmp_path, bad):
    config = tmp_path / 'config.json'
    original = json.dumps({'server_url':'https://farm.example.test', 'data_dir':str(tmp_path)})
    config.write_text(original)
    with pytest.raises(ValueError):
        desktop.configure_applications(tmp_path, {'steam_path':bad})
    assert config.read_text() == original
    with pytest.raises(ValueError):
        desktop.configure_applications(tmp_path, {'command':'whoami'})


def test_application_info_reports_removed_binary_unavailable(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'server_url':'https://farm.example.test', 'data_dir':str(tmp_path), 'steam_path':str(tmp_path / 'steam.exe')}))
    result = desktop.get_applications(tmp_path)
    assert result['steam_path'] == str(tmp_path / 'steam.exe')
    assert result['steam_available'] is False
    assert result['cs2_available'] is False


@pytest.mark.asyncio
async def test_enrollment_uses_discovery_without_launching_applications(tmp_path, monkeypatch):
    steam = tmp_path / 'steam.exe'
    steam.write_bytes(b'explicit-test-executable')
    monkeypatch.setattr(desktop, 'discover_applications', lambda:{'steam_path':str(steam), 'cs2_path':None})
    async def fake_enroll(config, token, hardware):
        assert config.steam_path == str(steam)
        assert hardware['installed']['steam'] is True
        assert hardware['installed']['cs2'] is False
        return {'node_id':'discovered-node', 'credential':'explicit-test-secret'}
    monkeypatch.setattr(desktop, 'enroll', fake_enroll)
    result = await desktop.enroll_desktop(tmp_path, {'server_url':'https://farm.example.test', 'token':'test-only-one-time-code-123'})
    assert result['node_id'] == 'discovered-node'
    assert desktop.get_applications(tmp_path)['steam_available'] is True
