import hashlib, secrets
from datetime import timedelta
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.db import get_db, utcnow, aware
from app.core.config import settings, allowed_origin
from app.models.models import User, AuthSession, AuditLog, Role, Control

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def audit(db, actor, action, target="", details=None):
    db.add(
        AuditLog(actor=actor, action=action, target=str(target), details=details or {})
    )


def bootstrap(db):
    for role in ("admin", "operator", "viewer"):
        if not db.get(Role, role):
            db.add(Role(name=role))
    db.flush()
    if not db.get(Control, 1):
        db.add(Control(id=1))
    if not db.scalar(select(User.id)):
        password = settings().admin_password
        if len(password) < 14:
            raise RuntimeError("ADMIN_PASSWORD must contain at least 14 characters")
        db.add(
            User(
                username=settings().admin_username,
                password_hash=hasher.hash(password),
                role="admin",
            )
        )
    from app.services.compliance import policy
    policy(db)
    from app.services.safety import get_policy
    get_policy(db)
    db.commit()


def authenticate(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("farm_session", "")
    session = (
        db.scalar(select(AuthSession).where(AuthSession.token_hash == digest(token)))
        if token
        else None
    )
    if not session or aware(session.expires_at) <= utcnow():
        raise HTTPException(401, "Authentication required")
    user = db.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(401, "Access revoked")
    request.state.user = user
    request.state.auth_session = session
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not allowed_origin(request.headers.get("origin")):
            raise HTTPException(403, "Invalid Origin")
        if not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token
        ):
            raise HTTPException(403, "Invalid CSRF token")
    return user


def operator(user=Depends(authenticate)):
    if user.role not in ("admin", "operator"):
        raise HTTPException(403, "Operator role required")
    return user


def admin(user=Depends(authenticate)):
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return user
