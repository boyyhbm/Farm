"""AES-256-GCM local store. The production key provider is Windows user DPAPI only."""

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultProblem(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class WindowsKey:
    def wrap(self, raw):
        if os.name != "nt":
            raise VaultProblem("WINDOWS_DPAPI_REQUIRED")
        import win32crypt

        return win32crypt.CryptProtectData(
            raw, "CS2 Farm Account Vault AES-256", None, None, None, 0
        )

    def unwrap(self, raw):
        if os.name != "nt":
            raise VaultProblem("WINDOWS_DPAPI_REQUIRED")
        import win32crypt

        return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1]


class VaultStore:
    def __init__(self, root, protector=None, clock=time.monotonic):
        from security.credentials import restrict_path

        self.root = Path(root) / "account-vault"
        self.root.mkdir(parents=True, exist_ok=True)
        restrict_path(self.root)
        self.path = self.root / "ciphertexts.sqlite3"
        self.key_path = self.root / "master-key.dpapi"
        self.protector = protector or WindowsKey()
        self.clock = clock
        self.mutex = threading.RLock()
        self.grants = {}
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise VaultProblem("VAULT_SCHEMA_UNSUPPORTED")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS secrets(id TEXT PRIMARY KEY,owner TEXT NOT NULL,revision TEXT NOT NULL,guard_digest TEXT,nonce BLOB NOT NULL,cipher BLOB NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS guard_owner_unique ON secrets(owner,guard_digest) WHERE guard_digest IS NOT NULL;
            CREATE TABLE IF NOT EXISTS stages(id TEXT PRIMARY KEY,owner TEXT NOT NULL,session TEXT NOT NULL,kind TEXT NOT NULL,expires REAL NOT NULL,nonce BLOB NOT NULL,cipher BLOB NOT NULL);
            PRAGMA user_version=1;
            """)
            db.execute(
                "DELETE FROM stages WHERE kind='preview' OR expires<?", (time.time(),)
            )
        restrict_path(self.path)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def unlock(self, owner, session):
        with self.mutex:
            self.lock(session)
            try:
                if not self.key_path.exists():
                    with self.connection() as db:
                        if db.execute("SELECT 1 FROM secrets LIMIT 1").fetchone():
                            raise VaultProblem("VAULT_KEY_MISSING")
                    raw = secrets.token_bytes(32)
                    wrapped = self.protector.wrap(raw)
                    from security.credentials import restrict_path

                    temporary = self.key_path.with_suffix(".tmp")
                    temporary.write_bytes(wrapped)
                    restrict_path(temporary)
                    temporary.replace(self.key_path)
                raw = self.protector.unwrap(self.key_path.read_bytes())
                if len(raw) != 32:
                    raise VaultProblem("VAULT_KEY_INVALID")
            except VaultProblem:
                raise
            except Exception:  # noqa: BLE001 - DPAPI/AEAD failures must not expose native data
                raise VaultProblem("VAULT_KEY_UNAVAILABLE") from None
            now = self.clock()
            self.grants[session] = {
                "owner": owner,
                "key": bytearray(raw),
                "idle": now + 300,
                "hard": now + 900,
            }

    def lock(self, session):
        with self.mutex:
            grant = self.grants.pop(session, None)
            if grant:
                grant["key"][:] = b"\0" * len(grant["key"])
            with self.connection() as db:
                db.execute(
                    "DELETE FROM stages WHERE session=? AND kind='preview'", (session,)
                )

    def expire(self):
        with self.mutex:
            now = self.clock()
            for session, grant in list(self.grants.items()):
                if now >= min(grant["idle"], grant["hard"]):
                    self.lock(session)
            with self.connection() as db:
                db.execute("DELETE FROM stages WHERE expires<?", (time.time(),))

    def status(self, owner, session):
        self.expire()
        grant = self.grants.get(session)
        unlocked = bool(grant and grant["owner"] == owner)
        return {
            "unlocked": unlocked,
            "idle_seconds_remaining": max(
                0, int(min(grant["idle"], grant["hard"]) - self.clock())
            )
            if unlocked
            else 0,
            "idle_timeout_seconds": 300,
            "maximum_unlock_seconds": 900,
            "key_protection": "Windows DPAPI CurrentUser",
            "cipher": "AES-256-GCM",
            "backup_includes_credentials": False,
            "secrets_export_supported": False,
        }

    @contextmanager
    def key(self, owner, session, touch=True):
        with self.mutex:
            self.expire()
            grant = self.grants.get(session)
            if not grant or grant["owner"] != owner:
                raise VaultProblem("VAULT_LOCKED")
            if touch:
                grant["idle"] = min(self.clock() + 300, grant["hard"])
            yield bytes(grant["key"])

    @staticmethod
    def encrypt(key, aad, value):
        nonce = secrets.token_bytes(12)
        return nonce, AESGCM(key).encrypt(
            nonce, json.dumps(value, ensure_ascii=False).encode(), aad.encode()
        )

    @staticmethod
    def decrypt(key, aad, row):
        try:
            return json.loads(
                AESGCM(key).decrypt(row["nonce"], row["cipher"], aad.encode())
            )
        except Exception:  # noqa: BLE001 - DPAPI/AEAD failures must not expose native data
            raise VaultProblem("VAULT_DATA_CORRUPT") from None

    def put_stage(self, id, owner, session, value, kind="preview"):
        with self.key(owner, session) as key:
            nonce, cipher = self.encrypt(key, f"stage:{id}:{owner}", value)
            with self.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO stages VALUES(?,?,?,?,?,?,?)",
                    (
                        id,
                        owner,
                        session,
                        kind,
                        time.time() + (86400 if kind == "import" else 900),
                        nonce,
                        cipher,
                    ),
                )

    def stage(self, id, owner, session, expected_kind=None):
        with self.key(owner, session, touch=False) as key:
            with self.connection() as db:
                row = db.execute(
                    "SELECT * FROM stages WHERE id=? AND owner=? AND expires>?",
                    (id, owner, time.time()),
                ).fetchone()
            if not row or (row["kind"] == "preview" and row["session"] != session):
                raise VaultProblem("IMPORT_NOT_AVAILABLE")
            if expected_kind and row["kind"] != expected_kind:
                raise VaultProblem("IMPORT_ALREADY_CONFIRMED")
            return self.decrypt(key, f"stage:{id}:{owner}", row)

    def remove_stage(self, id, owner):
        with self.connection() as db:
            db.execute("DELETE FROM stages WHERE id=? AND owner=?", (id, owner))

    def put_secret(self, id, owner, session, value, guard_digest=None):
        revision = secrets.token_hex(16)
        with self.key(owner, session) as key:
            nonce, cipher = self.encrypt(key, f"entry:{id}:{owner}", value)
            try:
                with self.connection() as db:
                    db.execute(
                        "INSERT INTO secrets VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,guard_digest=excluded.guard_digest,nonce=excluded.nonce,cipher=excluded.cipher WHERE owner=excluded.owner",
                        (id, owner, revision, guard_digest, nonce, cipher),
                    )
            except sqlite3.IntegrityError:
                raise VaultProblem("AUTHENTICATOR_ALREADY_BOUND") from None
        return revision

    def secret(self, id, owner, session):
        with self.key(owner, session) as key:
            with self.connection() as db:
                row = db.execute(
                    "SELECT * FROM secrets WHERE id=? AND owner=?", (id, owner)
                ).fetchone()
            if not row:
                raise VaultProblem("CREDENTIALS_MISSING_ON_THIS_DEVICE")
            return self.decrypt(key, f"entry:{id}:{owner}", row), row["revision"]

    def presence(self, owner):
        with self.connection() as db:
            return {
                r["id"]: r["revision"]
                for r in db.execute(
                    "SELECT id,revision FROM secrets WHERE owner=?", (owner,)
                )
            }

    def remove_secret(self, id, owner):
        self.remove_secrets([id], owner)

    def remove_secrets(self, ids, owner):
        with self.connection() as db:
            db.executemany(
                "DELETE FROM secrets WHERE id=? AND owner=?",
                [(id, owner) for id in ids],
            )
        # Compact once per bounded batch, not once per record.
        with self.connection() as db:
            db.execute("VACUUM")
