# ruff: noqa: B008
"""Local-only owner vault API. Responses are deliberately projected, never serialized secrets."""

import asyncio
import csv
import io
import uuid
from functools import wraps
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select
from starlette.requests import ClientDisconnect

from app.core.config import settings
from app.core.db import aware, get_db, utcnow
from app.core.security import admin, audit, hasher
from app.models.models import (
    AccountGroup,
    AccountRisk,
    Node,
    Session,
    SteamAccount,
    VaultImport,
    VaultRecord,
)
from app.services import vault as service
from app.services import vault_import as parser
from app.services.vault_crypto import VaultProblem

router = APIRouter(prefix="/api/v1/vault", tags=["Account Vault"])


def serialized(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with service.get_store().mutex:
            return function(*args, **kwargs)

    return guarded


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Unlock(Strict):
    password: SecretStr = Field(min_length=1, max_length=1024)


class Confirm(Strict):
    confirmed: bool


class Mapping(Strict):
    row_id: str = Field(max_length=10)
    login: str | None = Field(default=None, max_length=100)
    steam_id: str | None = Field(default=None, max_length=17)
    skip: bool = False


class Commit(Confirm):
    skip_invalid: bool = False
    mappings: list[Mapping] = Field(default_factory=list, max_length=5000)
    replace_existing: bool = False
    group_id: str | None = None
    node_id: str | None = None


class Bulk(Confirm):
    ids: list[str] = Field(min_length=1, max_length=1000)
    operation: Literal[
        "validate", "archive", "remove", "assign_node", "group", "inventory_sync"
    ]
    value: str | None = None


class Link(Confirm):
    steam_id: str = Field(max_length=17)


class RemovePart(Confirm):
    part: Literal["password", "guard"]


class Authentication(Confirm):
    node_id: str
    status: Literal["AUTHORIZED", "FAILED", "UNKNOWN"]
    error_code: Literal[
        "OWNER_CONFIRMATION_FAILED", "CLIENT_SIGNED_OUT", "STEAM_GUARD_PENDING", "NONE"
    ] = "NONE"


def owner(request: Request, user=Depends(admin)):
    if settings().deployment_mode != "desktop":
        raise HTTPException(409, "LOCAL_WINDOWS_DESKTOP_REQUIRED")
    if request.client and request.client.host not in (
        "127.0.0.1",
        "::1",
        "localhost",
        "testclient",
    ):
        raise HTTPException(403, "VAULT_LOCAL_ACCESS_ONLY")
    return user


def session(request):
    return request.state.auth_session.id


def confirmed(data):
    if not data.confirmed:
        raise HTTPException(400, "EXPLICIT_CONFIRMATION_REQUIRED")


def record(db, id, user):
    row = db.get(VaultRecord, id)
    if not row or row.owner_id != user.id:
        raise HTTPException(404, "VAULT_RECORD_NOT_FOUND")
    return row


def unlocked(request, user):
    store = service.get_store()
    with store.key(user.id, session(request)):
        pass
    return store


def job_view(j):
    return {
        key: getattr(j, key)
        for key in (
            "id",
            "status",
            "total",
            "processed",
            "imported",
            "skipped",
            "errors",
            "created_at",
            "updated_at",
        )
    }


@router.get("/status")
def status(request: Request, user=Depends(admin)):
    try:
        store = service.get_store()
        return {"available": True, **store.status(user.id, session(request))}
    except VaultProblem as exc:
        return {"available": False, "unlocked": False, "reason": exc.code}


@router.post("/unlock")
@serialized
def unlock(data: Unlock, request: Request, user=Depends(owner), db=Depends(get_db)):
    from app.core.local_store import LocalStore

    if LocalStore().counter("vault-unlock:" + user.id, 300) > 5:
        raise HTTPException(429, "VAULT_UNLOCK_RATE_LIMIT")
    try:
        valid = hasher.verify(user.password_hash, data.password.get_secret_value())
    except Exception:  # noqa: BLE001 - authentication fails closed without credential diagnostics
        valid = False
    if not valid:
        audit(db, user.username, "vault.unlock.denied")
        db.commit()
        raise HTTPException(401, "INVALID_PANEL_PASSWORD")
    store = service.get_store()
    store.unlock(user.id, session(request))
    audit(db, user.username, "vault.unlock")
    db.commit()
    return store.status(user.id, session(request))


@router.post("/lock")
@serialized
def lock(request: Request, user=Depends(owner), db=Depends(get_db)):
    service.get_store().lock(session(request))
    audit(db, user.username, "vault.lock")
    db.commit()
    return {"unlocked": False}


@router.post("/imports", status_code=201)
@serialized
def new_import(
    data: Confirm, request: Request, user=Depends(owner), db=Depends(get_db)
):
    confirmed(data)
    store = unlocked(request, user)
    id = str(uuid.uuid4())
    store.put_stage(
        id, user.id, session(request), {"rows": [], "file_count": 0, "formats": []}
    )
    audit(db, user.username, "vault.import.preview", id)
    db.commit()
    return {"id": id}


@router.post("/imports/{id}/files")
async def upload(
    id: str,
    request: Request,
    kind: Literal["csv", "txt", "json", "mafile", "zip"],
    user=Depends(owner),
    db=Depends(get_db),
):
    store = unlocked(request, user)
    # Raw request streaming avoids Starlette UploadFile's plaintext disk spool.
    body = bytearray()
    limit = parser.MAX_ZIP if kind == "zip" else parser.MAX_FILE
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(413, "FILE_SIZE_LIMIT")
            body.extend(chunk)
    except ClientDisconnect:
        raise HTTPException(400, "IMPORT_UPLOAD_INTERRUPTED") from None
    return await asyncio.to_thread(
        service.append_file, db, store, id, user.id, session(request), bytes(body), kind
    )


@router.get("/imports/{id}/preview")
def preview(
    id: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    user=Depends(owner),
    db=Depends(get_db),
):
    return service.preview(
        db, unlocked(request, user), id, user.id, session(request), page, page_size
    )


@router.delete("/imports/{id}")
@serialized
def discard(id: str, request: Request, user=Depends(owner), db=Depends(get_db)):
    store = unlocked(request, user)
    job = db.get(VaultImport, id)
    if job and job.owner_id != user.id:
        raise HTTPException(404, "IMPORT_NOT_FOUND")
    if job and job.status not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        job.status = "CANCELLED"
    store.remove_stage(id, user.id)
    audit(db, user.username, "vault.import.cancel", id)
    db.commit()
    return {"status": "discarded"}


@router.post("/imports/{id}/commit")
@serialized
def commit(
    id: str, data: Commit, request: Request, user=Depends(owner), db=Depends(get_db)
):
    confirmed(data)
    store = unlocked(request, user)
    previous = db.get(VaultImport, id)
    if previous:
        if previous.owner_id != user.id:
            raise HTTPException(404, "IMPORT_NOT_FOUND")
        return job_view(previous)
    for target, model in ((data.node_id, Node), (data.group_id, AccountGroup)):
        if target and (
            not db.get(model, target) or model is Node and db.get(Node, target).revoked
        ):
            raise HTTPException(422, "INVALID_ASSIGNMENT")
    value = store.stage(id, user.id, session(request), expected_kind="preview")
    if data.skip_invalid:
        for row in value["rows"]:
            if row["issues"] and not any(
                m.row_id == row["row_id"] for m in data.mappings
            ):
                data.mappings.append(Mapping(row_id=row["row_id"], skip=True))
    value["rows"] = service.configure_rows(
        value, [m.model_dump() for m in data.mappings]
    )
    value["replace_existing"] = data.replace_existing
    store.put_stage(id, user.id, session(request), value, kind="import")
    job = VaultImport(
        id=id,
        owner_id=user.id,
        auth_session_id=session(request),
        status="QUEUED",
        total=len(value["rows"]),
        group_id=data.group_id,
        node_id=data.node_id,
    )
    db.add(job)
    audit(
        db,
        user.username,
        "vault.import.queued",
        id,
        {"count": job.total, "replace_existing": data.replace_existing},
    )
    db.commit()
    return job_view(job)


@router.get("/imports")
def imports(user=Depends(owner), db=Depends(get_db)):
    return {
        "items": [
            job_view(j)
            for j in db.scalars(
                select(VaultImport)
                .where(VaultImport.owner_id == user.id)
                .order_by(VaultImport.created_at.desc())
                .limit(20)
            )
        ]
    }


@router.post("/imports/{id}/resume")
@serialized
def resume(
    id: str, data: Confirm, request: Request, user=Depends(owner), db=Depends(get_db)
):
    confirmed(data)
    store = unlocked(request, user)
    job = db.get(VaultImport, id)
    if not job or job.owner_id != user.id:
        raise HTTPException(404, "IMPORT_NOT_FOUND")
    if job.status not in ("WAITING_FOR_UNLOCK", "FAILED"):
        raise HTTPException(409, "IMPORT_NOT_PAUSED")
    store.stage(id, user.id, session(request), expected_kind="import")
    job.auth_session_id = session(request)
    job.status = "QUEUED"
    audit(db, user.username, "vault.import.resume", id)
    db.commit()
    return job_view(job)


def views(db, records, presence):
    ids = [r.account_id for r in records if r.account_id]
    accounts = {
        a.id: a
        for a in db.scalars(select(SteamAccount).where(SteamAccount.id.in_(ids)))
    }
    risks = {
        a.account_id: a
        for a in db.scalars(select(AccountRisk).where(AccountRisk.account_id.in_(ids)))
    }
    sessions = {}
    for s in db.scalars(
        select(Session)
        .where(
            Session.account_id.in_(ids),
            Session.status.in_(("STARTING", "RUNNING", "STOPPING", "UNKNOWN")),
        )
        .order_by(Session.started_at)
    ):
        sessions[s.account_id] = s.status
    result = []
    for row in records:
        account = accounts.get(row.account_id)
        risk = risks.get(row.account_id)
        state = (
            "MISSING_ON_THIS_DEVICE"
            if row.id not in presence
            else "READY"
            if presence[row.id] == row.revision
            else "NEEDS_REVIEW"
        )
        result.append(
            {
                "id": row.id,
                "account_id": row.account_id,
                "login": row.login,
                "steam_id": account.steam_id if account else None,
                "credential_status": state,
                "password_present": row.has_password,
                "guard_status": "LINKED" if row.has_guard else "MISSING",
                "assigned_node_id": account.assigned_node_id if account else None,
                "session_status": sessions.get(row.account_id, "NONE"),
                "authentication_status": "FAILED"
                if risk and risk.auth_status == "FAILED"
                else (
                    risk.auth_status
                    if risk
                    and risk.auth_expires_at
                    and aware(risk.auth_expires_at) > utcnow()
                    else "UNKNOWN"
                ),
                "authentication_source": risk.auth_source if risk else None,
                "authentication_checked_at": risk.auth_checked_at if risk else None,
                "last_login_attempt": row.last_login_attempt,
                "last_authentication_error": row.last_auth_error,
                "archived": row.archived,
                "updated_at": row.updated_at,
            }
        )
    return result


@router.get("/records")
def records(
    request: Request,
    q: str = Query("", max_length=100),
    archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    user=Depends(owner),
    db=Depends(get_db),
):
    stmt = select(VaultRecord).where(
        VaultRecord.owner_id == user.id, VaultRecord.archived == archived
    )
    if q:
        stmt = stmt.where(VaultRecord.login.contains(q, autoescape=True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(
        db.scalars(
            stmt.order_by(VaultRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    store = service.get_store()
    return {
        "items": views(db, rows, store.presence(user.id)),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/export")
def export(user=Depends(owner), db=Depends(get_db)):
    rows = list(db.scalars(select(VaultRecord).where(VaultRecord.owner_id == user.id)))
    result = views(db, rows, service.get_store().presence(user.id))
    output = io.StringIO()
    writer = csv.writer(output)
    columns = [
        "id",
        "account_id",
        "login",
        "steam_id",
        "credential_status",
        "guard_status",
        "assigned_node_id",
        "archived",
    ]
    writer.writerow(columns)
    for row in result:
        writer.writerow(
            [
                ("'" + str(row[c]))
                if str(row[c] or "")[:1] in ("=", "+", "-", "@", "\t", "\r")
                else row[c]
                for c in columns
            ]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="vault-metadata.csv"'},
    )


@router.post("/records/bulk")
@serialized
def bulk(data: Bulk, request: Request, user=Depends(owner), db=Depends(get_db)):
    confirmed(data)
    store = unlocked(request, user)
    rows = [record(db, id, user) for id in set(data.ids)]
    if data.operation in ("assign_node", "group") and data.value:
        model = Node if data.operation == "assign_node" else AccountGroup
        target = db.get(model, data.value)
        if not target or model is Node and target.revoked:
            raise HTTPException(422, "INVALID_ASSIGNMENT")
    results = []
    with store.mutex:
        if data.operation == "remove":
            store.remove_secrets([row.id for row in rows], user.id)
        for row in rows:
            code = "OK"
            if data.operation == "remove":
                db.delete(row)
            elif data.operation == "archive":
                row.archived = True
                if row.account_id and db.get(SteamAccount, row.account_id):
                    db.get(SteamAccount, row.account_id).archived = True
            elif data.operation == "validate":
                try:
                    value, revision = store.secret(row.id, user.id, session(request))
                    if value["login"].lower() != row.login_key:
                        raise VaultProblem("IDENTIFIER_MISMATCH")
                    account = (
                        db.get(SteamAccount, row.account_id) if row.account_id else None
                    )
                    if (
                        account
                        and value.get("steam_id")
                        and account.steam_id != value["steam_id"]
                    ):
                        raise VaultProblem("IDENTIFIER_MISMATCH")
                    if value.get("guard"):
                        parser.guard(value["guard"])
                    row.revision = revision
                    row.has_password = bool(value.get("password"))
                    row.has_guard = bool(value.get("guard"))
                except (VaultProblem, parser.ImportProblem) as exc:
                    code = exc.code
            else:
                account = (
                    db.get(SteamAccount, row.account_id) if row.account_id else None
                )
                if not account:
                    code = "STEAM_ID_LINK_REQUIRED"
                elif data.operation == "inventory_sync":
                    from app.core.local_store import LocalStore

                    LocalStore().enqueue("inventory", {"account_id": account.id})
                    code = "QUEUED"
                elif db.scalar(
                    select(Session.id).where(
                        Session.account_id == account.id,
                        Session.status.in_(
                            ("STARTING", "RUNNING", "STOPPING", "UNKNOWN")
                        ),
                    )
                ):
                    code = "ACCOUNT_BUSY"
                elif data.operation == "assign_node":
                    account.assigned_node_id = data.value
                else:
                    account.group_id = data.value
            results.append({"id": row.id, "code": code})
    audit(
        db,
        user.username,
        "vault.bulk." + data.operation,
        details={"count": len(rows), "results": results},
    )
    db.commit()
    return {"results": results}


@router.post("/records/{id}/link")
@serialized
def link(
    id: str, data: Link, request: Request, user=Depends(owner), db=Depends(get_db)
):
    confirmed(data)
    store = unlocked(request, user)
    row = record(db, id, user)
    identifier = parser.steam_id(data.steam_id)
    if not identifier:
        raise HTTPException(422, "STEAM_ID_REQUIRED")
    value, _ = store.secret(id, user.id, session(request))
    if value.get("steam_id") and identifier != value["steam_id"]:
        raise HTTPException(409, "IDENTIFIER_MISMATCH")
    if value.get("guard") and parser.guard(value["guard"]).get("steam_id") not in (
        None,
        identifier,
    ):
        raise HTTPException(409, "IDENTIFIER_MISMATCH")
    if row.account_id and db.get(SteamAccount, row.account_id).steam_id != identifier:
        raise HTTPException(409, "ACCOUNT_ALREADY_LINKED")
    account = db.scalar(select(SteamAccount).where(SteamAccount.steam_id == identifier))
    if not account:
        account = SteamAccount(
            steam_id=identifier,
            profile_url="https://steamcommunity.com/profiles/" + identifier,
        )
        db.add(account)
        db.flush()
    if db.scalar(
        select(VaultRecord.id).where(
            VaultRecord.owner_id == user.id,
            VaultRecord.account_id == account.id,
            VaultRecord.id != id,
        )
    ):
        raise HTTPException(409, "ACCOUNT_ALREADY_LINKED")
    value["steam_id"] = identifier
    row.revision = store.put_secret(
        id,
        user.id,
        session(request),
        value,
        service.guard_digest(store, user.id, session(request), value.get("guard")),
    )
    row.account_id = account.id
    row.updated_at = utcnow()
    audit(db, user.username, "vault.account.link", id, {"account_id": account.id})
    db.commit()
    return {"account_id": account.id}


@router.post("/records/{id}/remove-part")
@serialized
def remove_part(
    id: str, data: RemovePart, request: Request, user=Depends(owner), db=Depends(get_db)
):
    confirmed(data)
    store = unlocked(request, user)
    row = record(db, id, user)
    value, _ = store.secret(id, user.id, session(request))
    value[data.part] = None
    row.revision = store.put_secret(
        id,
        user.id,
        session(request),
        value,
        service.guard_digest(store, user.id, session(request), value.get("guard")),
    )
    row.has_password = bool(value.get("password"))
    row.has_guard = bool(value.get("guard"))
    row.updated_at = utcnow()
    audit(db, user.username, "vault.remove." + data.part, id)
    db.commit()
    return {"status": "removed"}


@router.post("/records/{id}/authentication/start")
@serialized
def auth_start(
    id: str, data: Confirm, request: Request, user=Depends(owner), db=Depends(get_db)
):
    confirmed(data)
    unlocked(request, user)
    row = record(db, id, user)
    row.last_login_attempt = utcnow()
    row.last_auth_error = None
    audit(db, user.username, "vault.authentication.manual_start", id)
    db.commit()
    return {
        "status": "OWNER_ACTION_REQUIRED",
        "instruction": "Sign in using the official Steam client. Passwords and Steam Guard codes are never filled or sent by Farm Manager.",
    }


@router.post("/records/{id}/authentication")
@serialized
def authentication(
    id: str,
    data: Authentication,
    request: Request,
    user=Depends(owner),
    db=Depends(get_db),
):
    confirmed(data)
    unlocked(request, user)
    row = record(db, id, user)
    if not row.account_id:
        raise HTTPException(409, "STEAM_ID_LINK_REQUIRED")
    account = db.get(SteamAccount, row.account_id)
    if account.assigned_node_id and account.assigned_node_id != data.node_id:
        raise HTTPException(409, "ASSIGNED_NODE_MISMATCH")
    from app.api_protection import AuthInput, authorization

    row.last_auth_error = data.error_code if data.status == "FAILED" else None
    row.last_login_attempt = utcnow()
    result = authorization(
        row.account_id,
        AuthInput(
            status=data.status,
            node_id=data.node_id,
            confirmed=True,
            note="Owner verified official Steam client through Authentication Manager",
        ),
        user,
        db,
    )
    return {
        "status": result.get("auth_status", data.status),
        "source": "owner_confirmation",
    }
