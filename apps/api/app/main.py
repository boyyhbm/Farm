import asyncio, csv, io, json, logging, secrets, uuid, os
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    Query,
    Header,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, FileResponse
from pathlib import Path
from app.core.local_store import LocalStore
from sqlalchemy import select, func, or_, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session as DBSession
from redis import Redis
from app.core.config import settings, allowed_origin
from app.core.db import SessionLocal, get_db, utcnow, aware
from app.core.security import (
    authenticate,
    operator,
    admin,
    audit,
    bootstrap,
    digest,
    hasher,
    DUMMY_HASH,
)
from app.models.models import *
from app.schemas.api import *
from app.services.scheduler import (
    create_job,
    stop_session,
    cancel_job,
    retry_job,
    acknowledge,
    reconcile,
    ACTIVE,
)

log = logging.getLogger("farm")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


def redis_client():
    return Redis.from_url(
        settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def publish():
    if settings().deployment_mode == "desktop":
        return
    try:
        redis_client().publish("farm:live", '{"type":"refresh"}')
    except Exception:
        log.exception(
            "Realtime invalidation unavailable; dashboard polling remains active"
        )


def serial(obj, exclude=()):
    if obj is None:
        return None
    return jsonable_encoder(
        {
            c.key: getattr(obj, c.key)
            for c in inspect(obj).mapper.column_attrs
            if c.key not in exclude
        },
        custom_encoder={Decimal: float},
    )


def get_or_404(db, model, id):
    obj = db.get(model, id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj


def paginate(db, stmt, page=1, page_size=50, exclude=()):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    return {
        "items": [
            serial(o, exclude)
            for o in db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def pages(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
    return page, page_size


def commit(db):
    db.commit()
    publish()


def csv_response(rows, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        # Protect spreadsheet consumers against formula injection.
        writer.writerow(
            [
                "'" + v
                if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r")
                else v
                for v in row
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@asynccontextmanager
async def lifespan(app):
    if (
        settings().database_url.startswith("sqlite")
        and not settings().allow_test_sqlite
        and settings().deployment_mode != "desktop"
    ):
        raise RuntimeError(
            "PostgreSQL required. SQLite is for explicit automated tests only."
        )
    with SessionLocal() as db:
        bootstrap(db)
    worker = None
    if settings().deployment_mode == "desktop":
        from app.workers.local_worker import DesktopWorker

        worker = DesktopWorker()
        app.state.desktop_worker = worker
        worker.start()
    try:
        yield
    finally:
        if worker:
            await asyncio.to_thread(worker.stop)
        from app.services import vault
        if vault._store:
            for vault_session in list(vault._store.grants):
                vault._store.lock(vault_session)


app = FastAPI(title="CS2 Farm Manager", version="0.5.0", lifespan=lifespan)


from app.api_protection import router as protection_router
from app.services.compliance import ProtectionDenied

app.include_router(protection_router)
from app.api_safety import router as safety_router

app.include_router(safety_router)
from app.api_vault import router as vault_router
from app.services.vault_crypto import VaultProblem
from app.services.vault_import import ImportProblem
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
app.include_router(vault_router)


@app.exception_handler(VaultProblem)
@app.exception_handler(ImportProblem)
async def vault_error(request, exc):
    code = exc.code
    status = 423 if code == 'VAULT_LOCKED' else 503 if code in ('WINDOWS_DPAPI_REQUIRED','LOCAL_WINDOWS_DESKTOP_REQUIRED','VAULT_KEY_UNAVAILABLE') else 422
    return JSONResponse(status_code=status, content={'detail':code,'code':code})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith('/api/v1/vault'):
        return JSONResponse(status_code=422,content={'detail':'INVALID_VAULT_REQUEST'})
    return await request_validation_exception_handler(request, exc)



@app.exception_handler(ProtectionDenied)
async def protection_denied(request, exc):
    from app.services.protection import incident

    with SessionLocal() as db:
        incident(
            db,
            exc.code,
            "CRITICAL" if exc.code == "CONFIRMED_RESTRICTION" else "HIGH",
            account_id=exc.account_id,
            node_id=exc.node_id,
            source="compliance",
        )
        audit(
            db,
            getattr(getattr(request.state, "user", None), "username", "operator"),
            "protection.operation_denied",
            exc.account_id or "",
            {"code": exc.code},
        )
        db.commit()
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail, "code": exc.code}
    )


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(
        status_code=409, content={"detail": "Conflicting record or invalid reference"}
    )


@app.middleware("http")
async def headers_and_limits(request, call_next):
    # Redis is mandatory for auth/commands; fail closed rather than per-worker limits.
    if request.url.path.startswith("/api/"):
        try:
            client = redis_client()
            identity = request.client.host if request.client else "unknown"
            bucket = (
                "farm:rate:"
                + ("login:" if request.url.path.endswith("/auth/login") else "api:")
                + identity
            )
            if settings().deployment_mode == "desktop":
                count = LocalStore().counter(bucket, 60)
            else:
                with client.pipeline() as pipe:
                    pipe.incr(bucket)
                    pipe.expire(bucket, 60, nx=True)
                    count, _ = pipe.execute()
            if count > (10 if request.url.path.endswith("/auth/login") else 600):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded"},
                    headers={"Retry-After": "60"},
                )
        except Exception:
            log.exception("Rate limit storage unavailable")
            return JSONResponse(
                status_code=503, content={"detail": "Rate limit service unavailable"}
            )
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0") or 0) > (32 * 1024 * 1024 if request.url.path.startswith("/api/v1/vault/imports/") and request.url.path.endswith("/files") else 5_000_000)
        ):
            return JSONResponse(status_code=413, content={"detail": "Body too large"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"status": "alive"}


@app.get("/health/ready")
def ready():
    try:
        with SessionLocal() as db:
            db.execute(select(1))
        (
            LocalStore().ping()
            if settings().deployment_mode == "desktop"
            else redis_client().ping()
        )
        result = {"status": "ready"}
        if settings().deployment_mode == "desktop":
            result["desktop_instance_id"] = os.getenv("DESKTOP_INSTANCE_ID", "")
            worker = getattr(app.state, "desktop_worker", None)
            result["local_worker_running"] = bool(
                worker and all(t.is_alive() for t in worker.threads)
            )
            if not result["local_worker_running"]:
                raise RuntimeError("Local worker unavailable")
        return result
    except Exception:
        raise HTTPException(503, "Database or Redis unavailable")


P = "/api/v1"


@app.post(P + "/auth/login")
def login(
    data: Login, request: Request, response: Response, db: DBSession = Depends(get_db)
):
    if not allowed_origin(request.headers.get("origin")):
        raise HTTPException(403, "Invalid Origin")
    user = db.scalar(
        select(User).where(User.username == data.username, User.active == True)
    )
    valid = False
    try:
        valid = hasher.verify(
            user.password_hash if user else DUMMY_HASH,
            data.password,
        )
    except Exception:
        pass
    if not valid:
        from app.services.protection import incident

        incident(
            db, "PANEL_AUTH_FAILURE", "MEDIUM", source="panel_auth", occurrence=True
        )
        audit(db, user.username if user else "anonymous", "auth.failed")
        db.commit()
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=digest(token),
            csrf_token=csrf,
            expires_at=utcnow() + timedelta(hours=settings().session_hours),
        )
    )
    audit(db, user.username, "auth.login")
    db.commit()
    response.set_cookie(
        "farm_session",
        token,
        httponly=True,
        secure=settings().session_secure or request.url.scheme == "https",
        samesite="strict",
        max_age=settings().session_hours * 3600,
        path="/",
    )
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "csrf_token": csrf,
    }


@app.get(P + "/auth/me")
def me(request: Request, user=Depends(authenticate)):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "csrf_token": request.state.auth_session.csrf_token,
    }


@app.post(P + "/auth/logout")
def logout(
    request: Request,
    response: Response,
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    from app.services import vault
    try:
        vault.get_store().lock(request.state.auth_session.id)
    except VaultProblem:
        pass
    db.delete(request.state.auth_session)
    audit(db, user.username, "auth.logout")
    db.commit()
    response.delete_cookie("farm_session", path="/")
    return {"status": "logged_out"}


@app.get(P + "/accounts")
def accounts(
    q: str = "",
    archived: bool = False,
    node_id: str | None = None,
    prime_status: str | None = None,
    tag: str | None = None,
    pg=Depends(pages),
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    stmt = select(SteamAccount).where(SteamAccount.archived == archived)
    if q:
        stmt = stmt.where(
            or_(
                SteamAccount.steam_id.contains(q, autoescape=True),
                SteamAccount.notes.contains(q, autoescape=True),
                SteamAccount.nickname.contains(q, autoescape=True),
            )
        )
    if node_id:
        stmt = stmt.where(SteamAccount.assigned_node_id == node_id)
    if prime_status:
        stmt = stmt.where(SteamAccount.prime_status == prime_status)
    if tag:
        stmt = stmt.where(
            SteamAccount.tags.cast(String).contains(json.dumps(tag), autoescape=True)
        )
    result = paginate(db, stmt.order_by(SteamAccount.created_at.desc()), *pg)
    from app.services.account_health import context, health
    from app.services.compliance import policy
    from app.models.models import AccountRisk

    ids = [row["id"] for row in result["items"]]
    accounts_by_id = {
        a.id: a
        for a in db.scalars(select(SteamAccount).where(SteamAccount.id.in_(ids)))
    }
    risks = {
        r.account_id: r
        for r in db.scalars(select(AccountRisk).where(AccountRisk.account_id.in_(ids)))
    }
    ctx = context(db)
    rules = policy(db)
    for row in result["items"]:
        row["health"] = health(
            accounts_by_id[row["id"]], risks.get(row["id"]), rules, ctx
        )
    return result


@app.post(P + "/accounts", status_code=201)
def add_account(
    data: AccountCreate, user=Depends(operator), db: DBSession = Depends(get_db)
):
    account = SteamAccount(
        **data.model_dump(),
        profile_url=f"https://steamcommunity.com/profiles/{data.steam_id}",
    )
    db.add(account)
    db.flush()
    audit(db, user.username, "account.create", account.id)
    commit(db)
    return serial(account)


@app.post(P + "/accounts/import")
async def import_accounts(
    request: Request, user=Depends(operator), db: DBSession = Depends(get_db)
):
    try:
        body = await request.body()
        if len(body) > 5_000_000:
            raise ValueError("CSV exceeds 5MB")
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        if not reader.fieldnames or "steam_id" not in reader.fieldnames:
            raise ValueError("CSV header steam_id required")
        imported = []
        for line, row in enumerate(reader, 2):
            if line > 5001:
                raise ValueError("Maximum 5000 rows per import")
            data = AccountCreate(
                steam_id=row["steam_id"].strip(),
                notes=row.get("notes", ""),
                tags=[t.strip() for t in row.get("tags", "").split(";") if t.strip()],
            )
            if db.scalar(
                select(SteamAccount.id).where(SteamAccount.steam_id == data.steam_id)
            ):
                continue
            account = SteamAccount(
                **data.model_dump(),
                profile_url=f"https://steamcommunity.com/profiles/{data.steam_id}",
            )
            db.add(account)
            db.flush()
            imported.append(account.id)
        audit(db, user.username, "accounts.import", details={"count": len(imported)})
        commit(db)
        return {"imported": len(imported), "ids": imported}
    except (ValueError, UnicodeError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc))


@app.get(P + "/accounts/export")
def export_accounts(user=Depends(authenticate), db: DBSession = Depends(get_db)):
    rows = [["steam_id", "nickname", "notes", "tags", "prime_status"]]
    rows.extend(
        [a.steam_id, a.nickname, a.notes, ";".join(a.tags), a.prime_status]
        for a in db.scalars(select(SteamAccount))
    )
    return csv_response(rows, "accounts.csv")


@app.post(P + "/accounts/bulk")
def bulk_accounts(
    data: AccountBulk, user=Depends(operator), db: DBSession = Depends(get_db)
):
    changes = data.changes.model_dump(exclude_unset=True)
    if any(
        v is None and k not in ("nickname", "assigned_node_id", "group_id")
        for k, v in changes.items()
    ):
        raise HTTPException(422, "Non-null account field cannot be null")
    for id in set(data.ids):
        account = get_or_404(db, SteamAccount, id)
        for k, v in changes.items():
            setattr(account, k, v)
    audit(
        db,
        user.username,
        "accounts.bulk",
        details={"ids": data.ids, "fields": list(changes)},
    )
    commit(db)
    return {"updated": len(set(data.ids))}


@app.get(P + "/accounts/{id}")
def account_detail(
    id: str, user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return serial(get_or_404(db, SteamAccount, id))


@app.patch(P + "/accounts/{id}")
def patch_account(
    id: str, data: AccountPatch, user=Depends(operator), db: DBSession = Depends(get_db)
):
    account = get_or_404(db, SteamAccount, id)
    changes = data.model_dump(exclude_unset=True)
    if any(
        v is None and k not in ("nickname", "assigned_node_id", "group_id")
        for k, v in changes.items()
    ):
        raise HTTPException(422, "Non-null account field cannot be null")
    for k, v in changes.items():
        setattr(account, k, v)
    audit(
        db, user.username, "account.update", id, {"fields": list(data.model_fields_set)}
    )
    commit(db)
    return serial(account)


@app.delete(P + "/accounts/{id}")
def delete_account(id: str, user=Depends(operator), db: DBSession = Depends(get_db)):
    account = get_or_404(db, SteamAccount, id)
    if (
        db.scalar(select(Job.id).where(Job.account_id == id))
        or db.scalar(
            select(InventorySnapshot.id).where(InventorySnapshot.account_id == id)
        )
        or db.scalar(select(DropRecord.id).where(DropRecord.account_id == id))
        or db.scalar(select(RiskIncident.id).where(RiskIncident.account_id == id))
    ):
        account.archived = True
        status = "archived"
    else:
        db.delete(account)
        status = "deleted"
    audit(db, user.username, "account." + status, id)
    commit(db)
    return {"status": status}


@app.get(P + "/groups")
def groups(user=Depends(authenticate), db: DBSession = Depends(get_db)):
    return {"items": [serial(x) for x in db.scalars(select(AccountGroup))]}


@app.post(P + "/groups")
def add_group(data: dict, user=Depends(operator), db: DBSession = Depends(get_db)):
    name = data.get("name", "")
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise HTTPException(422, "Group name required, maximum 100 characters")
    group = AccountGroup(name=name)
    db.add(group)
    db.flush()
    audit(db, user.username, "group.create", group.id)
    commit(db)
    return serial(group)


@app.get(P + "/nodes")
def nodes(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    result = paginate(
        db,
        select(Node).order_by(Node.created_at.desc()),
        *pg,
        exclude=("credential_hash",),
    )
    from app.models.models import NodeSafety

    ids = [row["id"] for row in result["items"]]
    states = {
        s.node_id: s
        for s in db.scalars(select(NodeSafety).where(NodeSafety.node_id.in_(ids)))
    }
    for row in result["items"]:
        row["safety"] = (
            serial(states[row["id"]])
            if row["id"] in states
            else {"state": "UNKNOWN", "reasons": []}
        )
    return result


@app.post(P + "/nodes/enrollment-tokens")
def enrollment_token(user=Depends(admin), db: DBSession = Depends(get_db)):
    token = secrets.token_urlsafe(40)
    expiry = utcnow() + timedelta(minutes=15)
    db.add(EnrollmentToken(token_hash=digest(token), expires_at=expiry))
    audit(db, user.username, "node.enrollment_token")
    commit(db)
    return {"token": token, "expires_at": expiry}


@app.post(P + "/nodes/enroll")
def enroll(data: Enrollment, request: Request, db: DBSession = Depends(get_db)):
    # Conditional UPDATE is atomic on SQLite as well as PostgreSQL; row-lock hints
    # alone do not protect one-time enrollment in the desktop adapter.
    now = utcnow()
    consumed = db.scalar(
        update(EnrollmentToken)
        .where(
            EnrollmentToken.token_hash == digest(data.token),
            EnrollmentToken.used_at.is_(None),
            EnrollmentToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(EnrollmentToken.id)
    )
    if not consumed:
        raise HTTPException(403, "Invalid, used or expired enrollment token")
    credential = secrets.token_urlsafe(48)
    node = Node(
        **data.model_dump(exclude={"token"}),
        credential_hash=digest(credential),
        ip=request.client.host if request.client else None,
    )
    db.add(node)
    db.flush()
    audit(db, "enrollment", "node.enroll", node.id)
    commit(db)
    return {"node_id": node.id, "credential": credential}


@app.get(P + "/nodes/{id}")
def node_detail(id: str, user=Depends(authenticate), db: DBSession = Depends(get_db)):
    result = serial(get_or_404(db, Node, id), ("credential_hash",))
    from app.models.models import NodeSafety

    current = db.get(NodeSafety, id)
    result["safety"] = (
        serial(current) if current else {"state": "UNKNOWN", "reasons": []}
    )
    return result


@app.get(P + "/nodes/{id}/metrics")
def node_metrics(
    id: str,
    pg=Depends(pages),
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    get_or_404(db, Node, id)
    return paginate(
        db,
        select(NodeMetric)
        .where(NodeMetric.node_id == id)
        .order_by(NodeMetric.created_at.desc()),
        *pg,
    )


@app.post(P + "/nodes/{id}/revoke")
def revoke_node(id: str, user=Depends(admin), db: DBSession = Depends(get_db)):
    node = get_or_404(db, Node, id)
    node.revoked = True
    node.status = "offline"
    audit(db, user.username, "node.revoke", id)
    commit(db)
    return {"status": "revoked"}


@app.post(P + "/nodes/{id}/restart")
def restart_node(id: str, user=Depends(admin), db: DBSession = Depends(get_db)):
    node = get_or_404(db, Node, id)
    if node.revoked:
        raise HTTPException(409, "Node revoked")
    if db.scalar(
        select(Session.id).where(Session.node_id == id, Session.status.in_(ACTIVE))
    ):
        raise HTTPException(409, "Stop active sessions before restarting agent")
    command = db.scalar(
        select(Command).where(
            Command.node_id == id,
            Command.operation == "restart_agent",
            Command.ack_status.is_(None),
        )
    )
    if not command:
        command = Command(
            node_id=id,
            operation="restart_agent",
            deadline=utcnow() + timedelta(minutes=5),
        )
        db.add(command)
        db.flush()
    audit(db, user.username, "node.restart", id)
    commit(db)
    return {"command_id": command.id, "status": "queued"}


@app.get(P + "/jobs")
def jobs(
    status: str | None = None,
    pg=Depends(pages),
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    stmt = select(Job)
    if status:
        stmt = stmt.where(Job.status == status)
    return paginate(db, stmt.order_by(Job.created_at.desc()), *pg)


@app.post(P + "/jobs", status_code=201)
def add_job(
    data: JobCreate,
    idempotency_key: str = Header(..., min_length=8, max_length=100),
    user=Depends(operator),
    db: DBSession = Depends(get_db),
):
    job = create_job(db, data, idempotency_key, user.username)
    commit(db)
    return serial(job)


@app.post(P + "/jobs/{id}/cancel")
def cancel(id: str, user=Depends(operator), db: DBSession = Depends(get_db)):
    job = get_or_404(db, Job, id)
    cancel_job(db, job, user.username)
    commit(db)
    return serial(job)


@app.post(P + "/jobs/{id}/retry")
def retry(id: str, user=Depends(operator), db: DBSession = Depends(get_db)):
    job = get_or_404(db, Job, id)
    retry_job(db, job, user.username)
    commit(db)
    return serial(job)


@app.get(P + "/sessions")
def sessions(
    status: str | None = None,
    pg=Depends(pages),
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    stmt = select(Session)
    if status:
        stmt = stmt.where(Session.status == status)
    return paginate(db, stmt.order_by(Session.started_at.desc()), *pg)


@app.post(P + "/sessions/{id}/stop")
def stop(id: str, user=Depends(operator), db: DBSession = Depends(get_db)):
    session = get_or_404(db, Session, id)
    # User stop is cancellation, not a successfully completed workload.
    cancel_job(db, db.get(Job, session.job_id), user.username)
    commit(db)
    return serial(session)


@app.post(P + "/sessions/{id}/restart")
def restart_session(
    id: str,
    idempotency_key: str = Header(..., min_length=8, max_length=100),
    user=Depends(operator),
    db: DBSession = Depends(get_db),
):
    session = get_or_404(db, Session, id)
    old = db.get(Job, session.job_id)
    existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing:
        return serial(existing)
    data = JobCreate(
        kind=old.kind,
        account_id=old.account_id,
        node_id=session.node_id,
        payload=old.payload,
        priority=old.priority,
        timeout_seconds=old.timeout_seconds,
        max_attempts=old.max_attempts,
    )
    replacement = create_job(db, data, idempotency_key, user.username)
    cancel_job(db, old, user.username)
    audit(
        db, user.username, "session.restart", id, {"replacement_job_id": replacement.id}
    )
    commit(db)
    return serial(replacement)


@app.get(P + "/control")
def control(user=Depends(authenticate), db: DBSession = Depends(get_db)):
    return serial(db.get(Control, 1))


@app.patch(P + "/control")
def policy(data: ControlPatch, user=Depends(operator), db: DBSession = Depends(get_db)):
    control = db.get(Control, 1)
    control.policy = data.policy
    audit(db, user.username, "queue.policy", details={"policy": data.policy})
    commit(db)
    return serial(control)


@app.post(P + "/control/{action}")
def control_action(
    action: str, user=Depends(operator), db: DBSession = Depends(get_db)
):
    control = db.get(Control, 1)
    if action == "pause":
        control.paused = True
    elif action == "resume":
        from app.services.safety import assert_manager_allowed

        assert_manager_allowed(db)
        control.paused = False
    elif action == "emergency-stop":
        control.paused = True
        for job in db.scalars(
            select(Job).where(
                Job.status.in_(("QUEUED", "STARTING", "RUNNING", "UNKNOWN"))
            )
        ):
            cancel_job(db, job, user.username)
        for s in db.scalars(select(Session).where(Session.status.in_(ACTIVE))):
            stop_session(db, s, user.username)
    else:
        raise HTTPException(404, "Unknown action")
    if action == "emergency-stop":
        from app.services.protection import incident

        event = incident(
            db, "EMERGENCY_STOP", "CRITICAL", source="operator:" + user.username
        )
        from app.services.safety import record_decision

        record_decision(
            db,
            "EMERGENCY_STOP",
            "OPERATOR_REQUEST",
            event=event,
            source="operator",
            actor=user.username,
            key="emergency:" + utcnow().isoformat(),
        )
    audit(db, user.username, "control." + action)
    commit(db)
    return serial(control)


@app.get(P + "/schedules")
def schedules(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return paginate(db, select(Schedule).order_by(Schedule.next_run), *pg)


@app.post(P + "/schedules")
def add_schedule(
    data: ScheduleCreate, user=Depends(operator), db: DBSession = Depends(get_db)
):
    schedule = Schedule(
        **data.model_dump(exclude={"job_template"}),
        job_template=data.job_template.model_dump(mode="json"),
    )
    db.add(schedule)
    db.flush()
    audit(db, user.username, "schedule.create", schedule.id)
    commit(db)
    return serial(schedule)


@app.patch(P + "/schedules/{id}")
def patch_schedule(
    id: str,
    data: ScheduleCreate,
    user=Depends(operator),
    db: DBSession = Depends(get_db),
):
    schedule = get_or_404(db, Schedule, id)
    for k, v in data.model_dump(mode="json").items():
        setattr(schedule, k, data.next_run if k == "next_run" else v)
    audit(db, user.username, "schedule.update", id)
    commit(db)
    return serial(schedule)


@app.delete(P + "/schedules/{id}")
def delete_schedule(id: str, user=Depends(operator), db: DBSession = Depends(get_db)):
    db.delete(get_or_404(db, Schedule, id))
    audit(db, user.username, "schedule.delete", id)
    commit(db)
    return {"status": "deleted"}


@app.get(P + "/inventory/export")
def export_inventory(
    account_id: str, user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    snapshot = db.scalar(
        select(InventorySnapshot)
        .where(
            InventorySnapshot.account_id == account_id,
            InventorySnapshot.status == "success",
        )
        .order_by(InventorySnapshot.fetched_at.desc())
    )
    rows = [["asset_id", "name", "amount", "price_usd"]]
    if snapshot:
        rows.extend(
            [i.asset_id, i.name, i.amount, i.price]
            for i in db.scalars(
                select(InventoryItem).where(InventoryItem.snapshot_id == snapshot.id)
            )
        )
    return csv_response(rows, "inventory.csv")


@app.get(P + "/inventory/{account_id}")
def inventory(
    account_id: str, user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    get_or_404(db, SteamAccount, account_id)
    snapshots = list(
        db.scalars(
            select(InventorySnapshot)
            .where(InventorySnapshot.account_id == account_id)
            .order_by(InventorySnapshot.fetched_at.desc())
            .limit(30)
        )
    )
    latest = next((s for s in snapshots if s.status == "success"), None)
    return {
        "items": [
            serial(i)
            for i in db.scalars(
                select(InventoryItem).where(InventoryItem.snapshot_id == latest.id)
            )
        ]
        if latest
        else [],
        "snapshots": [serial(s) for s in snapshots],
        "changes": [
            serial(c)
            for c in db.scalars(
                select(InventoryChange)
                .where(InventoryChange.account_id == account_id)
                .order_by(InventoryChange.detected_at.desc())
                .limit(500)
            )
        ],
    }


@app.post(P + "/inventory/{account_id}/sync")
def sync(account_id: str, user=Depends(operator), db: DBSession = Depends(get_db)):
    get_or_404(db, SteamAccount, account_id)
    from app.workers.tasks import inventory_sync

    try:
        if settings().deployment_mode == "desktop":
            from types import SimpleNamespace

            task = SimpleNamespace(
                id=LocalStore().enqueue("inventory", {"account_id": account_id})
            )
        else:
            task = inventory_sync.apply_async(args=[account_id], queue="inventory")
    except Exception:
        raise HTTPException(503, "Inventory queue unavailable")
    audit(db, user.username, "inventory.sync", account_id, {"task_id": task.id})
    commit(db)
    return {"task_id": task.id, "status": "queued"}


@app.get(P + "/market")
def market(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return paginate(
        db, select(MarketPrice).order_by(MarketPrice.fetched_at.desc()), *pg
    )


@app.get(P + "/drops/periods")
def periods(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return paginate(
        db, select(WeeklyPeriod).order_by(WeeklyPeriod.starts_at.desc()), *pg
    )


@app.post(P + "/drops/periods")
def add_period(
    data: PeriodCreate, user=Depends(operator), db: DBSession = Depends(get_db)
):
    overlap = db.scalar(
        select(WeeklyPeriod.id).where(
            WeeklyPeriod.starts_at < data.ends_at, WeeklyPeriod.ends_at > data.starts_at
        )
    )
    if overlap:
        raise HTTPException(409, "Overlapping period")
    period = WeeklyPeriod(**data.model_dump())
    db.add(period)
    db.flush()
    audit(db, user.username, "period.create", period.id, {"source": data.source})
    commit(db)
    return serial(period)


@app.get(P + "/drops")
def drops(
    account_id: str | None = None,
    period_id: str | None = None,
    pg=Depends(pages),
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    stmt = select(DropRecord)
    if account_id:
        stmt = stmt.where(DropRecord.account_id == account_id)
    if period_id:
        stmt = stmt.where(DropRecord.period_id == period_id)
    return paginate(db, stmt.order_by(DropRecord.confirmed_at.desc()), *pg)


def save_drop(data, db, user, confirmed=False):
    get_or_404(db, SteamAccount, data.account_id)
    get_or_404(db, WeeklyPeriod, data.period_id)
    status = "CONFIRMED" if confirmed else data.status
    if status == "CONFIRMED" and len(data.source.strip()) < 5:
        raise HTTPException(422, "Confirmation evidence/source required")
    drop = db.scalar(
        select(DropRecord)
        .where(
            DropRecord.account_id == data.account_id,
            DropRecord.period_id == data.period_id,
        )
        .with_for_update()
    )
    if not drop:
        drop = DropRecord(account_id=data.account_id, period_id=data.period_id)
        db.add(drop)
    for k, v in data.model_dump().items():
        setattr(drop, k, v)
    drop.status = status
    drop.confirmed_at = utcnow() if status == "CONFIRMED" else None
    if status == "CONFIRMED":
        db.get(SteamAccount, data.account_id).last_drop = utcnow()
        from app.services.notifications import notify

        notify(
            db,
            "drop_confirmed",
            f"Drop manually confirmed for account {data.account_id}",
        )
    db.flush()
    audit(db, user.username, "drop." + status.lower(), drop.id, {"source": data.source})
    commit(db)
    return serial(drop)


@app.post(P + "/drops")
def add_drop(data: DropCreate, user=Depends(operator), db: DBSession = Depends(get_db)):
    return save_drop(data, db, user)


@app.post(P + "/drops/confirm")
def confirm_drop(
    data: DropCreate, user=Depends(operator), db: DBSession = Depends(get_db)
):
    return save_drop(data, db, user, True)


@app.get(P + "/analytics/overview")
def analytics(user=Depends(authenticate), db: DBSession = Depends(get_db)):
    from app.services.finance import analytics_overview

    return analytics_overview(db)


@app.get(P + "/expenses")
def expenses(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return paginate(db, select(Expense).order_by(Expense.occurred_at.desc()), *pg)


@app.post(P + "/expenses")
def add_expense(
    data: ExpenseCreate, user=Depends(operator), db: DBSession = Depends(get_db)
):
    obj = Expense(**data.model_dump())
    db.add(obj)
    db.flush()
    audit(db, user.username, "expense.create", obj.id)
    commit(db)
    return serial(obj)


@app.get(P + "/revenues")
def revenues(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return paginate(db, select(Revenue).order_by(Revenue.occurred_at.desc()), *pg)


@app.post(P + "/revenues")
def add_revenue(
    data: RevenueCreate, user=Depends(operator), db: DBSession = Depends(get_db)
):
    obj = Revenue(**data.model_dump())
    db.add(obj)
    db.flush()
    audit(db, user.username, "revenue.create", obj.id)
    commit(db)
    return serial(obj)


@app.get(P + "/analytics/export")
def export_analytics(user=Depends(authenticate), db: DBSession = Depends(get_db)):
    from app.services.finance import analytics_overview

    data = analytics_overview(db)
    return csv_response(
        [["metric", "value"]]
        + [[k, v] for k, v in data.items() if not isinstance(v, (dict, list))],
        "analytics.csv",
    )


@app.get(P + "/logs")
def logs(
    q: str = "",
    pg=Depends(pages),
    user=Depends(authenticate),
    db: DBSession = Depends(get_db),
):
    stmt = select(AuditLog)
    if q:
        stmt = stmt.where(AuditLog.action.contains(q, autoescape=True))
    return paginate(db, stmt.order_by(AuditLog.created_at.desc()), *pg)


@app.get(P + "/notifications")
def notifications(
    pg=Depends(pages), user=Depends(authenticate), db: DBSession = Depends(get_db)
):
    return paginate(
        db, select(Notification).order_by(Notification.created_at.desc()), *pg
    )


@app.get(P + "/settings")
def get_settings(user=Depends(authenticate)):
    return {
        "telegram_configured": bool(
            os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
        ),
        "base_currency": "USD",
        "weekly_period_policy": "Manually register source-verified CS2 reset boundaries; no calendar-week inference",
        "max_parallel": settings().scheduler_max_parallel,
        "node_offline_seconds": settings().node_offline_seconds,
        "retention_days": settings().retention_days,
        "manual_steam_login_required": True,
        "version": "0.5.0",
    }


@app.get(P + "/users")
def users(pg=Depends(pages), user=Depends(admin), db: DBSession = Depends(get_db)):
    return paginate(db, select(User), *pg, exclude=("password_hash",))


@app.post(P + "/users")
def add_user(data: UserCreate, user=Depends(admin), db: DBSession = Depends(get_db)):
    obj = User(
        username=data.username, password_hash=hasher.hash(data.password), role=data.role
    )
    db.add(obj)
    db.flush()
    audit(db, user.username, "user.create", obj.id, {"role": obj.role})
    commit(db)
    return serial(obj, ("password_hash",))


@app.get(P + "/dashboard/overview")
def overview(user=Depends(authenticate), db: DBSession = Depends(get_db)):
    count = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w))
    nodes = list(
        db.scalars(select(Node).where(Node.status == "online", Node.revoked == False))
    )
    avg = lambda k: (
        sum(float(n.metrics[k]) for n in nodes if n.metrics.get(k) is not None)
        / len([n for n in nodes if n.metrics.get(k) is not None])
        if any(n.metrics.get(k) is not None for n in nodes)
        else None
    )
    from app.services.finance import analytics_overview, portfolio_value_history

    finance = analytics_overview(db)
    value = finance["inventory_value"]
    active = count(Session, Session.status == "RUNNING")
    values = portfolio_value_history(db)
    return {
        "accounts": count(SteamAccount, SteamAccount.archived == False),
        "active_accounts": db.scalar(
            select(func.count(func.distinct(Session.account_id))).where(
                Session.status == "RUNNING"
            )
        ),
        "nodes_online": len(nodes),
        "nodes_offline": count(Node) - len(nodes),
        "running_sessions": active,
        "running_cs2_processes": db.scalar(
            select(func.count())
            .select_from(Session)
            .join(Job, Session.job_id == Job.id)
            .where(Session.status == "RUNNING", Job.kind == "cs2")
        ),
        "confirmed_drops": count(DropRecord, DropRecord.status == "CONFIRMED"),
        "inventory_value": float(value) if value is not None else None,
        "errors_24h": count(
            AuditLog,
            AuditLog.created_at >= utcnow() - timedelta(days=1),
            or_(
                AuditLog.action.in_(("inventory.failed", "auth.failed")),
                (AuditLog.action == "session.finish")
                & AuditLog.details["status"].as_string().in_(("FAILED", "TIMED_OUT")),
            ),
        ),
        "cpu_percent": avg("cpu_percent"),
        "ram_percent": avg("ram_percent"),
        "gpu_percent": avg("gpu_percent"),
        "job_counts": dict(
            db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        ),
        "value_history": values,
        "prime_accounts": finance["prime_accounts"],
        "average_drop_value": finance["average_drop_value"],
    }


@app.websocket("/ws/live")
async def live(ws: WebSocket):
    token = ws.cookies.get("farm_session", "")
    with SessionLocal() as db:
        auth = (
            db.scalar(
                select(AuthSession).where(AuthSession.token_hash == digest(token))
            )
            if token
            else None
        )
        user = db.get(User, auth.user_id) if auth else None
        valid = bool(
            auth and aware(auth.expires_at) > utcnow() and user and user.active
        )
    if not valid or not allowed_origin(ws.headers.get("origin")):
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        if settings().deployment_mode == "desktop":
            while True:
                with SessionLocal() as db:
                    auth = db.scalar(
                        select(AuthSession).where(
                            AuthSession.token_hash == digest(token)
                        )
                    )
                    if not auth or aware(auth.expires_at) <= utcnow():
                        await ws.close(code=4401)
                        return
                await ws.send_json({"type": "refresh"})
                await asyncio.sleep(3)
        # Redis invalidation shared across replicas; periodic refresh is reconnect-safe.
        from redis.asyncio import Redis as AsyncRedis

        client = AsyncRedis.from_url(settings().redis_url)
        async with client.pubsub() as pubsub:
            await pubsub.subscribe("farm:live")
            while True:
                with SessionLocal() as db:
                    auth = db.scalar(
                        select(AuthSession).where(
                            AuthSession.token_hash == digest(token)
                        )
                    )
                    if not auth or aware(auth.expires_at) <= utcnow():
                        await ws.close(code=4401)
                        break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=10
                )
                await ws.send_json({"type": "refresh"})
                # Coalesce invalidations so hundreds of nodes cannot trigger a refetch storm.
                while await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0
                ):
                    pass
                await asyncio.sleep(2)
        await client.aclose()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("Live socket ended")


@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    credential = ws.headers.get("authorization", "").removeprefix("Bearer ")
    with SessionLocal() as db:
        node = (
            db.scalar(
                select(Node).where(
                    Node.credential_hash == digest(credential), Node.revoked == False
                )
            )
            if credential
            else None
        )
        node_id = node.id if node else None
    if not node_id:
        from app.services.protection import incident

        with SessionLocal() as db:
            incident(
                db, "AGENT_AUTH_FAILURE", "HIGH", source="agent_auth", occurrence=True
            )
            db.commit()
        await ws.close(code=4401)
        return
    await ws.accept()
    with SessionLocal() as db:
        connected_node = db.get(Node, node_id)
        connected_node.transport_security = (
            "tls_at_listener_or_trusted_proxy"
            if ws.url.scheme == "wss"
            else "not_verified_at_listener"
        )
        connected_node.transport_checked_at = utcnow()
        db.commit()
    try:
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=40)
            if len(raw) > 131072:
                await ws.close(code=1009)
                return
            data = json.loads(raw)
            with SessionLocal() as db:
                node = db.scalar(
                    select(Node).where(Node.id == node_id).with_for_update()
                )
                if node.revoked:
                    await ws.close(code=4403)
                    return
                if data.get("type") == "heartbeat":
                    if not isinstance(data.get("metrics"), dict) or not isinstance(
                        data.get("processes", []), list
                    ):
                        raise ValueError("Invalid heartbeat")
                    from app.services.safety import (
                        validate_metrics,
                        ingest_integrity,
                        ingest_local_safety,
                    )

                    metrics = validate_metrics(data["metrics"])
                    if data.get("integrity") is not None:
                        ingest_integrity(db, node, data["integrity"])
                    if data.get("safety") is not None:
                        metrics["local_safety"] = ingest_local_safety(data["safety"])
                    if "hardware" in data:
                        hardware = data["hardware"]
                        if not isinstance(hardware, dict) or not isinstance(
                            hardware.get("installed", {}), dict
                        ):
                            raise ValueError("Invalid hardware capabilities")
                        if any(
                            not isinstance(v, bool)
                            for v in hardware.get("installed", {}).values()
                        ):
                            raise ValueError("Installed capabilities must be booleans")
                        node.hardware = hardware
                    if (
                        isinstance(data.get("agent_version"), str)
                        and len(data["agent_version"]) <= 30
                    ):
                        node.agent_version = data["agent_version"]
                    for key in ("cpu_percent", "ram_percent", "gpu_percent"):
                        if metrics.get(key) is not None and (
                            not isinstance(metrics[key], (float, int))
                            or not 0 <= metrics[key] <= 100
                        ):
                            raise ValueError("Invalid metric")
                    from app.services.notifications import notify

                    if node.status != "online":
                        notify(db, "node_online", f"Node {node.hostname} online")
                    from app.services.protection import resolve_condition

                    resolve_condition(db, "NODE_OFFLINE", node_id=node.id)
                    node.status = "online"
                    node.last_heartbeat = utcnow()
                    node.metrics = {**metrics, "processes": data.get("processes", [])}
                    db.add(NodeMetric(node_id=node.id, metrics=metrics))
                    reconcile(db, node, data.get("processes", []))
                    # Threshold notifications are deduplicated by the service.
                    if metrics.get("ram_percent", 0) > 95:
                        notify(
                            db, "high_ram", f"High RAM on {node.hostname}", "warning"
                        )
                    if (
                        metrics.get("disk_free_gb", 100) > 0
                        and metrics.get("disk_free_gb", 100) < 5
                    ):
                        notify(
                            db,
                            "low_disk",
                            f"Low disk space on {node.hostname}",
                            "warning",
                        )
                    if (
                        metrics.get("cpu_temperature")
                        and metrics["cpu_temperature"] > 90
                    ):
                        notify(
                            db,
                            "high_cpu_temperature",
                            f"High CPU temperature on {node.hostname}",
                            "warning",
                        )
                elif data.get("type") == "ack":
                    acknowledge(db, node, data)
                elif data.get("type") == "log":
                    from app.services.notifications import _safe_message

                    audit(
                        db,
                        f"node:{node_id}",
                        "agent.log",
                        node_id,
                        {"message": _safe_message(data.get("message", ""))[:2000]},
                    )
                else:
                    raise ValueError("Unknown message type")
                db.commit()
                commands = list(
                    db.scalars(
                        select(Command)
                        .where(Command.node_id == node_id, Command.ack_status.is_(None))
                        .order_by(Command.created_at)
                        .limit(20)
                    )
                )
                outgoing = [
                    {
                        "type": "command",
                        "command_id": c.id,
                        "operation": c.operation,
                        "session_id": c.session_id,
                        **c.payload,
                        "deadline": aware(c.deadline).isoformat(),
                    }
                    for c in commands
                ]
            for command in outgoing:
                await ws.send_json(command)
            await ws.send_json({"type": "heartbeat_ack"})
            await asyncio.to_thread(publish)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except (ValueError, TypeError) as exc:
        log.warning("Invalid agent message node=%s error=%s", node_id, str(exc))
        await ws.close(code=1008)
    except Exception:
        log.exception("Agent socket failed node=%s", node_id)
    finally:
        # Scheduler applies disconnect grace; do not release occupied nodes here.
        with SessionLocal() as db:
            audit(db, f"node:{node_id}", "agent.disconnected", node_id)
            db.commit()


@app.get(P + "/system/status")
def system_status(user=Depends(authenticate)):
    desktop = settings().deployment_mode == "desktop"
    checks = {}
    try:
        with SessionLocal() as db:
            db.execute(select(1))
        checks["database"] = "ready"
    except Exception:
        checks["database"] = "unavailable"
    try:
        (LocalStore().ping() if desktop else redis_client().ping())
        checks["queue"] = "ready"
    except Exception:
        checks["queue"] = "unavailable"
    heartbeat = (
        LocalStore().get("worker:heartbeat")
        if desktop and checks["queue"] == "ready"
        else None
    )
    return {
        "mode": "desktop" if desktop else "server",
        "version": "0.5.0",
        "backend": {"status": "ready", "version": "0.5.0"},
        "database": {
            "engine": "sqlite" if desktop else "postgresql",
            "status": checks["database"],
        },
        "queue": {
            "engine": "sqlite" if desktop else "redis",
            "status": checks["queue"],
        },
        "worker": {
            "status": ("running" if heartbeat else "stopped")
            if desktop
            else "external",
            "last_heartbeat": heartbeat.get("timestamp") if heartbeat else None,
        },
        "agent_installer_available": bool(
            settings().agent_installer_path
            and Path(settings().agent_installer_path).is_file()
        ),
    }


@app.get(P + "/system/diagnostics")
def system_diagnostics(user=Depends(admin)):
    state = system_status(user)
    return {
        "checks": [
            {
                "name": name,
                "status": state[name]["status"],
                "detail": state[name].get("engine", "API runtime"),
            }
            for name in ("backend", "database", "queue", "worker")
        ]
    }


@app.get(P + "/downloads/agent")
def download_agent(user=Depends(authenticate)):
    path = (
        Path(settings().agent_installer_path)
        if settings().agent_installer_path
        else None
    )
    if not path or not path.is_file():
        raise HTTPException(503, "Agent installer is not bundled with this deployment")
    return FileResponse(
        path,
        media_type="application/vnd.microsoft.portable-executable",
        filename="CS2-Farm-Agent-Setup.exe",
    )


if settings().deployment_mode == "desktop":

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def desktop_dashboard_proxy(path: str, request: Request):
        # Same-origin HTTPS dashboard for explicitly enabled LAN access.
        if path.split("/")[0] in ("api", "ws", "health"):
            raise HTTPException(404, "Not found")
        import httpx

        target = settings().public_origin.rstrip("/") + "/" + path
        if request.url.query:
            target += "?" + request.url.query
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                result = await client.get(
                    target, headers={"accept": request.headers.get("accept", "*/*")}
                )
            headers = {
                k: v
                for k, v in result.headers.items()
                if k.lower()
                in (
                    "content-type",
                    "cache-control",
                    "etag",
                    "x-content-type-options",
                    "x-frame-options",
                )
            }
            return Response(
                result.content, status_code=result.status_code, headers=headers
            )
        except httpx.HTTPError:
            raise HTTPException(503, "Dashboard component unavailable")


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def protected_validation_error(request, exc):
    errors = [
        {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
    ]
    actor = getattr(getattr(request.state, "user", None), "username", None)
    if (
        actor
        and request.method == "POST"
        and request.url.path in {P + "/jobs", P + "/accounts", P + "/schedules"}
    ):
        from app.services.protection import incident

        code = (
            "INVALID_ACCOUNT_DATA"
            if request.url.path == P + "/accounts"
            else "POLICY_VIOLATION"
        )
        with SessionLocal() as db:
            incident(
                db,
                code,
                "MEDIUM",
                source="request_validation",
                evidence={"error_types": list({e["type"] for e in exc.errors()})},
                occurrence=True,
            )
            db.commit()
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errors}))
