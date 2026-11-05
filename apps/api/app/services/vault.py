"""Local owner vault; no credential delivery, Steam Guard codes or automatic sign-in."""

import hashlib
import hmac
import json
import os
import threading
import uuid

from app.core.config import settings
from app.core.db import aware, utcnow
from app.core.security import audit
from app.models.models import (
    AuthSession,
    Session,
    SteamAccount,
    User,
    VaultImport,
    VaultRecord,
)
from app.services import vault_import as parser
from app.services.vault_crypto import VaultProblem, VaultStore
from sqlalchemy import select

_store = None
_factory_lock = threading.Lock()


def get_store():
    global _store
    config = settings()
    if config.deployment_mode != "desktop":
        raise VaultProblem("LOCAL_WINDOWS_DESKTOP_REQUIRED")
    if os.name != "nt":
        raise VaultProblem("WINDOWS_DPAPI_REQUIRED")
    with _factory_lock:
        if _store is None:
            _store = VaultStore(config.desktop_data_dir)
        return _store


def row_view(row, existing):
    return {
        "row_id": row["row_id"],
        "login": row.get("login"),
        "steam_id": row.get("steam_id"),
        "password": "••••••" if row.get("password") else None,
        "guard_status": "PRESENT" if row.get("guard") else "MISSING",
        "issues": row.get("issues", []),
        "sources": row.get("sources", 1),
        "duplicate": bool(existing),
        "existing_record_id": existing.id if existing else None,
        "link_required": not row.get("steam_id"),
        "login_required": not row.get("login"),
    }


def preview(db, store, id, owner, session, page=1, page_size=50):
    data = store.stage(id, owner, session, expected_kind="preview")
    rows = data["rows"]
    records = list(db.scalars(select(VaultRecord).where(VaultRecord.owner_id == owner)))
    names = {r.login_key: r for r in records}
    linked = {
        a.id: a.steam_id
        for a in db.scalars(
            select(SteamAccount).where(
                SteamAccount.id.in_([r.account_id for r in records if r.account_id])
            )
        )
    }
    by_id = {linked[r.account_id]: r for r in records if r.account_id in linked}
    views = []
    for row in rows[(page - 1) * page_size : page * page_size]:
        existing = names.get((row.get("login") or "").lower()) or by_id.get(
            row.get("steam_id")
        )
        views.append(row_view(row, existing))
    return {
        "id": id,
        "items": views,
        "total": len(rows),
        "page": page,
        "page_size": page_size,
        "formats": data.get("formats", []),
        "file_count": data.get("file_count", 0),
        "invalid": sum(bool(r["issues"]) for r in rows),
        "missing_guards": sum(not r.get("guard") for r in rows),
    }


def append_file(db, store, id, owner, session, raw, kind):
    with store.mutex:
        return append_file_locked(db, store, id, owner, session, raw, kind)


def append_file_locked(db, store, id, owner, session, raw, kind):
    value = store.stage(id, owner, session, expected_kind="preview")
    new = parser.parse_file(raw, kind)
    known = {
        a.steam_id: r.login
        for r, a in db.execute(
            select(VaultRecord, SteamAccount)
            .join(SteamAccount, VaultRecord.account_id == SteamAccount.id)
            .where(VaultRecord.owner_id == owner)
        )
    }
    for row in new:
        if not row.get("login") and row.get("steam_id") in known:
            row["login"] = known[row["steam_id"]]

    value["rows"] = parser.merge_rows(value["rows"] + new)
    if (
        value.get("file_count", 0) >= 100
        or value.get("uploaded_bytes", 0) + len(raw) > parser.MAX_EXPANDED
    ):
        raise parser.ImportProblem("IMPORT_SIZE_LIMIT")
    value["file_count"] = value.get("file_count", 0) + 1
    value["uploaded_bytes"] = value.get("uploaded_bytes", 0) + len(raw)
    value["formats"] = sorted(set(value.get("formats", []) + [kind]))
    if len(json.dumps(value).encode()) > parser.MAX_EXPANDED:
        raise parser.ImportProblem("IMPORT_EXPANSION_LIMIT")
    store.put_stage(id, owner, session, value)
    return preview(db, store, id, owner, session)


def configure_rows(value, mappings):
    by_id = {str(m["row_id"]): m for m in mappings}
    if set(by_id) - {r["row_id"] for r in value["rows"]}:
        raise VaultProblem("INVALID_ROW_MAPPING")
    selected = []
    for original in value["rows"]:
        row = dict(original)
        mapping = by_id.get(row["row_id"], {})
        if mapping.get("skip"):
            continue
        if row["issues"]:
            raise VaultProblem("INVALID_ROWS_MUST_BE_SKIPPED")
        name = parser.login(mapping.get("login")) or row.get("login")
        identifier = parser.steam_id(mapping.get("steam_id")) or row.get("steam_id")
        if row.get("login") and name.lower() != row["login"].lower():
            raise VaultProblem("IDENTIFIER_MISMATCH")
        if row.get("steam_id") and identifier != row["steam_id"]:
            raise VaultProblem("IDENTIFIER_MISMATCH")
        if not name:
            raise VaultProblem("LOGIN_REQUIRED")
        row.update(login=name, steam_id=identifier)
        selected.append(row)
    if not selected:
        raise VaultProblem("NO_VALID_ROWS")
    # Re-check after manual mapping; do not let two selected identities collide.
    names, ids = set(), set()
    for row in selected:
        if row["login"].lower() in names or (
            row.get("steam_id") and row["steam_id"] in ids
        ):
            raise VaultProblem("MAPPING_DUPLICATE")
        names.add(row["login"].lower())
        if row.get("steam_id"):
            ids.add(row["steam_id"])
    return selected


def guard_digest(store, owner, session, guard):
    if not guard:
        return None
    with store.key(owner, session) as key:
        content = json.dumps(
            [guard["shared_secret"], guard["identity_secret"]]
        ).encode()
        return hmac.new(key, content, hashlib.sha256).hexdigest()


def import_one(db, store, job, row, replace):
    existing = db.scalar(
        select(VaultRecord).where(
            VaultRecord.owner_id == job.owner_id,
            VaultRecord.login_key == row["login"].lower(),
        )
    )
    account = (
        db.scalar(select(SteamAccount).where(SteamAccount.steam_id == row["steam_id"]))
        if row.get("steam_id")
        else None
    )
    if account:
        other = db.scalar(
            select(VaultRecord).where(
                VaultRecord.owner_id == job.owner_id,
                VaultRecord.account_id == account.id,
            )
        )
        if other and other != existing:
            raise VaultProblem("IDENTIFIER_MISMATCH")
    if existing and existing.account_id:
        old_account = db.get(SteamAccount, existing.account_id)
        if (
            account
            and existing.account_id != account.id
            or (
                row.get("steam_id")
                and old_account
                and old_account.steam_id != row["steam_id"]
            )
        ):
            raise VaultProblem("IDENTIFIER_MISMATCH")
        account = old_account
    deterministic_id = str(uuid.uuid5(uuid.UUID(job.id), row["login"].lower()))
    if existing and not replace:
        return existing.id == deterministic_id
    id = (
        existing.id
        if existing
        else str(uuid.uuid5(uuid.UUID(job.id), row["login"].lower()))
    )
    if existing:
        previous, _ = store.secret(existing.id, job.owner_id, job.auth_session_id)
    else:
        previous = {}
    if (
        previous.get("steam_id")
        and row.get("steam_id")
        and previous["steam_id"] != row["steam_id"]
    ):
        raise VaultProblem("IDENTIFIER_MISMATCH")
    secret = {
        "login": row["login"],
        "steam_id": row.get("steam_id") or previous.get("steam_id"),
        "password": row.get("password") or previous.get("password"),
        "guard": row.get("guard") or previous.get("guard"),
    }
    if secret.get("guard"):
        g = parser.guard(secret["guard"])
        if (
            g["login"]
            and g["login"].lower() != row["login"].lower()
            or g["steam_id"]
            and secret["steam_id"]
            and g["steam_id"] != secret["steam_id"]
        ):
            raise VaultProblem("IDENTIFIER_MISMATCH")
    if row.get("steam_id") and not account:
        account = SteamAccount(
            steam_id=row["steam_id"],
            profile_url="https://steamcommunity.com/profiles/" + row["steam_id"],
        )
        db.add(account)
        db.flush()
    if account and (job.node_id or job.group_id):
        if db.scalar(
            select(Session.id).where(
                Session.account_id == account.id,
                Session.status.in_(("STARTING", "RUNNING", "STOPPING", "UNKNOWN")),
            )
        ):
            raise VaultProblem("ACCOUNT_BUSY")
        if job.node_id:
            account.assigned_node_id = job.node_id
        if job.group_id:
            account.group_id = job.group_id
    revision = store.put_secret(
        id,
        job.owner_id,
        job.auth_session_id,
        secret,
        guard_digest(store, job.owner_id, job.auth_session_id, secret.get("guard")),
    )
    record = existing or VaultRecord(
        id=id, owner_id=job.owner_id, login=row["login"], login_key=row["login"].lower()
    )
    record.account_id = account.id if account else None
    record.has_password = bool(secret.get("password"))
    record.has_guard = bool(secret.get("guard"))
    record.revision = revision
    record.updated_at = utcnow()
    db.add(record)
    return True


def process_import(db, store, job, batch_size=100):
    user = db.get(User, job.owner_id)
    auth = db.get(AuthSession, job.auth_session_id)
    if (
        not user
        or not user.active
        or user.role != "admin"
        or not auth
        or aware(auth.expires_at) <= utcnow()
    ):
        job.status = "WAITING_FOR_UNLOCK"
        db.commit()
        return
    if not store.status(job.owner_id, job.auth_session_id)["unlocked"]:
        job.status = "WAITING_FOR_UNLOCK"
        db.commit()
        return
    with store.mutex:
        db.refresh(job)
        if job.status not in ("QUEUED", "RUNNING"):
            return
        try:
            data = store.stage(
                job.id, job.owner_id, job.auth_session_id, expected_kind="import"
            )
        except VaultProblem as exc:
            job.status = "FAILED"
            job.errors = [{"code": exc.code}]
            db.commit()
            return
        job.status = "RUNNING"
        for row in data["rows"][job.processed : job.processed + batch_size]:
            try:
                with db.begin_nested():
                    added = import_one(db, store, job, row, data["replace_existing"])
                if added:
                    job.imported += 1
                else:
                    job.skipped += 1
            except (VaultProblem, parser.ImportProblem) as exc:
                job.errors = (
                    job.errors + [{"row_id": row["row_id"], "code": exc.code}]
                )[:5000]
            job.processed += 1
            job.updated_at = utcnow()
            db.commit()  # bounded durable progress; retry never recreates completed rows
        if job.processed >= job.total:
            job.status = "COMPLETED_WITH_ERRORS" if job.errors else "COMPLETED"
            store.remove_stage(job.id, job.owner_id)
            audit(
                db,
                user.username,
                "vault.import.completed",
                job.id,
                {
                    "imported": job.imported,
                    "skipped": job.skipped,
                    "errors": len(job.errors),
                },
            )
            db.commit()


def tick():
    from app.core.db import SessionLocal

    try:
        store = get_store()
    except VaultProblem:
        return
    store.expire()
    with SessionLocal() as db:
        for job in db.scalars(
            select(VaultImport)
            .where(VaultImport.status.in_(("QUEUED", "RUNNING")))
            .order_by(VaultImport.created_at)
            .limit(3)
        ):
            try:
                process_import(db, store, job)
            except Exception:  # noqa: BLE001 - bounded worker failure, diagnostics contain only a fixed code
                # Never log exceptions, SQL parameters, uploaded records or provider text.
                db.rollback()
                job.status = "FAILED"
                job.errors = [{"code": "IMPORT_STORAGE_FAILURE"}]
                audit(
                    db,
                    "vault",
                    "vault.import.failed",
                    job.id,
                    {"code": "IMPORT_STORAGE_FAILURE"},
                )
                db.commit()
