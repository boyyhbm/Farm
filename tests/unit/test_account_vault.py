"""Synthetic credentials only. Linux key protection is an explicit test adapter."""

import base64
import hashlib
import io
import json
import os
import stat
import zipfile
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.core.db import Base, get_db, utcnow
from app.core.security import digest, hasher
from app.models.models import (
    AuditLog,
    AuthSession,
    Role,
    SteamAccount,
    User,
    VaultImport,
    VaultRecord,
)
from app.services import vault
from app.services import vault_import as parser
from app.services.vault_crypto import VaultProblem, VaultStore
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


class ExplicitTestProtector:
    """Not available to production code. Simulates key wrapping for non-Windows tests."""

    key = b"\x27" * 32

    def wrap(self, raw):
        nonce = os.urandom(12)
        return nonce + AESGCM(self.key).encrypt(nonce, raw, b"test-only")

    def unwrap(self, raw):
        return AESGCM(self.key).decrypt(raw[:12], raw[12:], b"test-only")


def store_at(path, **kwargs):
    return VaultStore(
        path, protector=None if os.name == "nt" else ExplicitTestProtector(), **kwargs
    )


def mafile(index=1, **changes):
    value = {
        "account_name": f"vault_synthetic_{index}",
        "shared_secret": base64.b64encode(
            hashlib.sha256(f"shared-{index}".encode()).digest()[:20]
        ).decode(),
        "identity_secret": base64.b64encode(
            hashlib.sha256(f"identity-{index}".encode()).digest()[:20]
        ).decode(),
        "Session": {"SteamID": f"76561198{index:09d}"},
    }
    value.update(changes)
    return value


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from app.main import app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.add(Role(name="admin"))
    db.add(Role(name="viewer"))
    db.flush()
    user = User(
        username="vault_owner",
        role="admin",
        password_hash=hasher.hash("panel-synthetic-pass-1234"),
    )
    db.add(user)
    db.flush()
    auth = AuthSession(
        user_id=user.id,
        token_hash=digest("vault-synthetic-cookie"),
        csrf_token="test-csrf",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(auth)
    db.commit()
    config = SimpleNamespace(
        deployment_mode="desktop",
        desktop_data_dir=str(tmp_path),
        public_origin="http://testserver",
        desktop_network_origin="",
        redis_url="redis://localhost:6379/15",
    )
    for target in (
        "app.core.config.settings",
        "app.core.local_store.settings",
        "app.main.settings",
        "app.api_vault.settings",
        "app.services.vault.settings",
    ):
        monkeypatch.setattr(target, lambda: config)
    store = store_at(tmp_path)
    monkeypatch.setattr(vault, "get_store", lambda: store)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set("farm_session", "vault-synthetic-cookie")
    client.headers.update({"Origin": "http://testserver", "X-CSRF-Token": "test-csrf"})
    yield client, db, store, user, auth
    app.dependency_overrides.clear()
    db.close()
    engine.dispose()


def unlock(client):
    r = client.post(
        "/api/v1/vault/unlock", json={"password": "panel-synthetic-pass-1234"}
    )
    assert r.status_code == 200, r.text


def stage(client, content, kind="csv"):
    r = client.post("/api/v1/vault/imports", json={"confirmed": True})
    assert r.status_code == 201, r.text
    id = r.json()["id"]
    r = client.post(
        f"/api/v1/vault/imports/{id}/files?kind={kind}",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200, r.text
    return id, r.json()


def finish(client, db, store, id, **changes):
    r = client.post(
        f"/api/v1/vault/imports/{id}/commit", json={"confirmed": True, **changes}
    )
    assert r.status_code == 200, r.text
    job = db.get(VaultImport, id)
    for _ in range(100):
        if job.status in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
            break
        vault.process_import(db, store, job)
    return job


@pytest.mark.parametrize("count", [10, 100, 1000])
def test_mass_import_masking_encryption_and_pagination(setup, count, caplog):
    client, db, store, _user, _auth = setup
    unlock(client)
    password = "synthetic-private-password-z9!"
    content = (
        "login,password,steam_id\n"
        + "".join(f"owner_{i},{password},76561198{i:09d}\n" for i in range(count))
    ).encode()
    id, preview = stage(client, content)
    assert preview["total"] == count and preview["page_size"] == 50
    assert password not in json.dumps(preview) and "••••••" in json.dumps(
        preview, ensure_ascii=False
    )
    job = finish(client, db, store, id)
    assert job.status == "COMPLETED" and job.imported == count and not job.errors
    response = client.get("/api/v1/vault/records?page_size=10")
    assert response.json()["total"] == count and len(response.json()["items"]) == 10
    assert password not in response.text and password not in caplog.text
    assert password.encode() not in store.path.read_bytes()
    assert password not in str(list(db.scalars(select(AuditLog.details))))
    assert db.scalar(select(func.count(SteamAccount.id))) == count


def test_zip_mafiles_merge_by_identity_and_detect_duplicates(setup):
    client, db, store, _user, _auth = setup
    unlock(client)
    id, _ = stage(
        client, b"login,password\nvault_synthetic_1,synthetic-owned-password\n"
    )
    raw = json.dumps(mafile()).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("nested/one.maFile", raw)
        z.writestr("two.maFile", raw)
    r = client.post(
        f"/api/v1/vault/imports/{id}/files?kind=zip", content=archive.getvalue()
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1 and r.json()["items"][0]["guard_status"] == "PRESENT"
    secret = mafile()["shared_secret"]
    assert secret not in r.text and "identity_secret" not in r.text
    job = finish(client, db, store, id)
    assert job.imported == 1
    record = db.scalar(select(VaultRecord))
    assert record.has_guard and record.has_password
    assert secret.encode() not in store.path.read_bytes()


def test_login_only_records_are_not_fabricated_steam_accounts_and_can_be_linked(setup):
    client, db, store, *_ = setup
    unlock(client)
    id, _ = stage(client, b"login:synthetic-password:with-colon", "txt")
    job = finish(client, db, store, id)
    assert job.imported == 1 and db.scalar(select(func.count(SteamAccount.id))) == 0
    row = db.scalar(select(VaultRecord))
    r = client.post(
        f"/api/v1/vault/records/{row.id}/link",
        json={"confirmed": True, "steam_id": "76561198000000001"},
    )
    assert r.status_code == 200, r.text
    assert db.get(VaultRecord, row.id).account_id


def test_mismatch_invalid_encrypted_and_corrupt_inputs_have_no_raw_values(setup):
    client, _db, _store, *_ = setup
    unlock(client)
    g = mafile()
    g["Session"]["SteamID"] = "76561198000000002"
    g["steamid"] = "76561198000000003"
    id, result = stage(client, json.dumps(g).encode(), "mafile")
    assert result["items"][0]["issues"] == ["IDENTIFIER_MISMATCH"]
    r = client.post(f"/api/v1/vault/imports/{id}/commit", json={"confirmed": True})
    assert r.status_code == 422
    _, result = stage(
        client, b'{"encrypted":true,"data":"secret-never-echo"}', "mafile"
    )
    assert result["items"][0]["issues"] == ["ENCRYPTED_MAFILE_UNSUPPORTED"]
    assert "secret-never-echo" not in json.dumps(result)
    r = client.post(
        "/api/v1/vault/unlock", json={"password": {"private": "never-return-this"}}
    )
    assert r.status_code == 422 and "never-return-this" not in r.text
    with pytest.raises(parser.ImportProblem, match="CORRUPT_FILE"):
        parser.parse_file(b"{invalid-private-value", "json")


@pytest.mark.parametrize(
    "name",
    ["../outside.maFile", "/absolute.maFile", "C:/path.maFile", "x\\escape.maFile"],
)
def test_zip_slip_is_rejected_without_extraction(name, tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr(name, json.dumps(mafile()))
    with pytest.raises(parser.ImportProblem, match="ZIP_UNSAFE_PATH"):
        parser.parse_file(output.getvalue(), "zip")
    assert not list(tmp_path.iterdir())


def test_zip_bomb_nested_archives_symlinks_and_large_count():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("huge.maFile", "x" * 600000)
    with pytest.raises(parser.ImportProblem, match="ZIP_EXPANSION_LIMIT"):
        parser.parse_file(output.getvalue(), "zip")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("nested.zip", b"PK")
    with pytest.raises(parser.ImportProblem, match="ZIP_ONLY_MAFILES"):
        parser.parse_file(output.getvalue(), "zip")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        info = zipfile.ZipInfo("link.maFile")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "target")
    with pytest.raises(parser.ImportProblem, match="ZIP_UNSAFE_PATH"):
        parser.parse_file(output.getvalue(), "zip")


def test_large_zip_1000_mafiles_is_valid():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for i in range(1000):
            z.writestr(f"{i}.maFile", json.dumps(mafile(i)))
    rows = parser.parse_file(output.getvalue(), "zip")
    assert len(rows) == 1000


def test_restart_auto_lock_key_corruption_and_active_removal(setup):
    client, db, store, user, auth = setup
    unlock(client)
    id, _ = stage(client, b"login,password\nrestart_owner,synthetic-persist-private\n")
    finish(client, db, store, id)
    record = db.scalar(select(VaultRecord))
    store.lock(auth.id)
    restarted = store_at(store.root.parent)
    with pytest.raises(VaultProblem, match="VAULT_LOCKED"):
        restarted.secret(record.id, user.id, auth.id)
    restarted.unlock(user.id, auth.id)
    value, _ = restarted.secret(record.id, user.id, auth.id)
    assert value["password"] == "synthetic-persist-private"
    restarted.remove_secret(record.id, user.id)
    assert record.id not in restarted.presence(user.id)
    with restarted.connection() as s:
        assert s.execute("SELECT count(*) FROM secrets").fetchone()[0] == 0
    restarted.lock(auth.id)
    restarted.key_path.write_bytes(b"corrupt-dpapi")
    with pytest.raises(VaultProblem, match="VAULT_KEY_UNAVAILABLE"):
        restarted.unlock(user.id, auth.id)


def test_idle_lock_does_not_refresh_on_status_polling(tmp_path):
    now = [10.0]
    store = store_at(tmp_path, clock=lambda: now[0])
    store.unlock("owner", "session")
    for i in range(1, 302):
        now[0] = 10 + i
        state = store.status("owner", "session")
    assert state["unlocked"] is False and not store.grants


def test_duplicate_guard_cannot_replace_another_record(tmp_path):
    store = store_at(tmp_path)
    store.unlock("owner", "session")
    store.put_secret(
        "first",
        "owner",
        "session",
        {"password": "synthetic-first"},
        "same-guard-digest",
    )
    with pytest.raises(VaultProblem, match="AUTHENTICATOR_ALREADY_BOUND"):
        store.put_secret(
            "second",
            "owner",
            "session",
            {"password": "synthetic-second"},
            "same-guard-digest",
        )
    assert store.secret("first", "owner", "session")[0]["password"] == "synthetic-first"
    assert "second" not in store.presence("owner")


def test_rbac_owner_boundary_lock_and_no_secret_export(setup):
    client, db, store, user, _auth = setup
    r = client.post("/api/v1/vault/imports", json={"confirmed": True})
    assert r.status_code == 423
    unlock(client)
    id, _ = stage(
        client, b"login,password\nowned_login,synthetic-confidential-password\n"
    )
    finish(client, db, store, id)
    row = db.scalar(select(VaultRecord))
    assert (
        "synthetic-confidential-password" not in client.get("/api/v1/vault/export").text
    )
    other = User(
        username="another_admin", role="admin", password_hash=user.password_hash
    )
    db.add(other)
    db.flush()
    other_auth = AuthSession(
        user_id=other.id,
        token_hash=digest("other-cookie"),
        csrf_token="other-csrf",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(other_auth)
    db.commit()
    client.cookies.set("farm_session", "other-cookie")
    client.headers["X-CSRF-Token"] = "other-csrf"
    assert client.get("/api/v1/vault/records").json()["total"] == 0
    assert (
        client.post(
            f"/api/v1/vault/records/{row.id}/link",
            json={"confirmed": True, "steam_id": "76561198000000001"},
        ).status_code
        == 423
    )
    other.role = "viewer"
    db.commit()
    assert client.get("/api/v1/vault/status").status_code == 403
    client.cookies.clear()
    assert client.get("/api/v1/vault/status").status_code == 401


def test_background_import_waits_for_unlock_and_resumes_without_duplicate_records(
    setup,
):
    client, db, store, _user, auth = setup
    unlock(client)
    id, _ = stage(
        client, b"login,password\none,synthetic-password-1\ntwo,synthetic-password-2\n"
    )
    r = client.post(f"/api/v1/vault/imports/{id}/commit", json={"confirmed": True})
    assert r.status_code == 200
    job = db.get(VaultImport, id)
    vault.process_import(db, store, job, batch_size=1)
    store.lock(auth.id)
    vault.process_import(db, store, job)
    assert job.status == "WAITING_FOR_UNLOCK" and job.processed == 1
    unlock(client)
    r = client.post(f"/api/v1/vault/imports/{id}/resume", json={"confirmed": True})
    assert r.status_code == 200
    vault.process_import(db, store, job)
    assert (
        job.status == "COMPLETED" and db.scalar(select(func.count(VaultRecord.id))) == 2
    )


def test_supported_authenticator_wrapper_and_camel_case_fields():
    source = mafile()
    value = {
        "MobileAuthenticator": {
            "AccountName": source["account_name"],
            "SteamID": source["Session"]["SteamID"],
            "SharedSecret": source["shared_secret"],
            "IdentitySecret": source["identity_secret"],
        }
    }
    row = parser.parse_file(json.dumps(value).encode(), "mafile")[0]
    assert row["login"] == source["account_name"]
    assert row["guard"]["shared_secret"] == source["shared_secret"]


def test_ciphertext_cannot_be_substituted_between_owner_or_record_ids(tmp_path):
    store = store_at(tmp_path)
    store.unlock("first-owner", "session")
    store.put_secret(
        "first", "first-owner", "session", {"password": "synthetic-owned-value"}
    )
    with store.connection() as db:
        row = db.execute("SELECT nonce,cipher FROM secrets WHERE id='first'").fetchone()
        db.execute(
            "INSERT INTO secrets VALUES(?,?,?,?,?,?)",
            ("other", "first-owner", "revision", None, row["nonce"], row["cipher"]),
        )
    with pytest.raises(VaultProblem, match="VAULT_DATA_CORRUPT"):
        store.secret("other", "first-owner", "session")


def test_zip_1000_accounts_is_imported_into_the_encrypted_store(setup):
    client, db, store, *_ = setup
    unlock(client)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for index in range(1000):
            z.writestr(f"{index}.maFile", json.dumps(mafile(index)))
    id, preview = stage(client, stream.getvalue(), "zip")
    assert preview["total"] == 1000
    job = finish(client, db, store, id)
    assert job.status == "COMPLETED" and job.imported == 1000 and not job.errors
    assert db.scalar(select(func.count(VaultRecord.id))) == 1000


def test_guard_file_without_login_matches_only_owned_existing_steam_id(setup):
    client, db, store, *_ = setup
    unlock(client)
    identifier = mafile()["Session"]["SteamID"]
    id, _ = stage(
        client,
        f"login,password,steam_id\nvault_synthetic_1,synthetic-password,{identifier}\n".encode(),
    )
    finish(client, db, store, id)
    value = mafile()
    value.pop("account_name")
    id, preview = stage(client, json.dumps(value).encode(), "mafile")
    assert (
        preview["items"][0]["login"] == "vault_synthetic_1"
        and preview["items"][0]["duplicate"]
    )
    job = finish(client, db, store, id, replace_existing=True)
    assert job.imported == 1 and db.scalar(select(VaultRecord)).has_guard


def test_confirmed_import_payload_is_sealed_against_later_uploads(setup):
    client, db, store, *_ = setup
    unlock(client)
    id, _ = stage(client, b"login,password\nsealed_owner,synthetic-sealed-password\n")
    assert (
        client.post(
            f"/api/v1/vault/imports/{id}/commit", json={"confirmed": True}
        ).status_code
        == 200
    )
    r = client.post(
        f"/api/v1/vault/imports/{id}/files?kind=txt",
        content=b"other:must-not-enter-sealed-job",
    )
    assert r.status_code == 422 and r.json()["code"] == "IMPORT_ALREADY_CONFIRMED"
    job = db.get(VaultImport, id)
    vault.process_import(db, store, job)
    assert job.imported == 1 and db.scalar(select(func.count(VaultRecord.id))) == 1
