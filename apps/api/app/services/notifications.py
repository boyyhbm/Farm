"""Persistent Telegram notification outbox, bounded retries and safe text rendering."""

from __future__ import annotations

import os
import re
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models.models import Notification

SEVERITIES = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}


def _safe_message(message: str) -> str:
    value = str(message)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "ENCRYPTION_KEY",
        "ADMIN_PASSWORD",
        "STEAM_API_KEY",
    ):
        secret = os.getenv(key)
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?i)(bearer\s+)[a-z0-9._~-]+", r"\1[redacted]", value)
    value = re.sub(
        r"(?i)(password|token|secret|credential|api_key)\s*[=:]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    )
    return value[:3500]


def notify(
    db: Session, event: str, message: str, severity: str = "info"
) -> Notification:
    if severity not in SEVERITIES:
        raise ValueError("Invalid notification severity")
    minimum = os.getenv("TELEGRAM_MIN_SEVERITY", "info").lower()
    status = (
        "pending" if SEVERITIES[severity] >= SEVERITIES.get(minimum, 1) else "skipped"
    )
    safe_message = _safe_message(message)
    duplicate = db.scalar(
        select(Notification)
        .where(
            Notification.event == event[:80],
            Notification.message == safe_message,
            Notification.created_at >= utcnow() - timedelta(minutes=10),
        )
        .order_by(Notification.created_at.desc())
        .limit(1)
    )
    if duplicate:
        return duplicate
    row = Notification(
        event=event[:80],
        severity=severity,
        message=safe_message,
        status=status,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def deliver_notifications(db: Session) -> dict[str, Any]:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"configured": False, "sent": 0, "failed": 0}
    query = select(Notification).where(Notification.status == "pending")
    if hasattr(Notification, "next_attempt_at"):
        query = query.where(
            (Notification.next_attempt_at.is_(None))
            | (Notification.next_attempt_at <= utcnow())
        )
    rows = list(
        db.scalars(
            query.order_by(Notification.created_at)
            .with_for_update(skip_locked=True)
            .limit(50)
        )
    )
    sent = failed = 0
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        for row in rows:
            try:
                response = client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat,
                        "text": f"[{row.severity.upper()}] {row.event}\n{_safe_message(row.message)}",
                        "disable_web_page_preview": True,
                    },
                )
                payload = response.json() if response.status_code == 200 else {}
                if response.status_code != 200 or payload.get("ok") is not True:
                    # Do not include response body or request URL: either can contain secrets.
                    raise RuntimeError(
                        f"Telegram delivery rejected (HTTP {response.status_code})"
                    )
                row.status = "sent"
                row.sent_at = utcnow()
                row.error = None
                sent += 1
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                row.error = (
                    str(exc)
                    if isinstance(exc, RuntimeError)
                    else f"Telegram delivery failed: {type(exc).__name__}"
                )
                failed += 1
                if hasattr(row, "attempts") and hasattr(row, "next_attempt_at"):
                    row.attempts = (row.attempts or 0) + 1
                    row.status = "failed" if row.attempts >= 5 else "pending"
                    row.next_attempt_at = utcnow() + timedelta(
                        seconds=min(3600, 30 * 2**row.attempts)
                    )
                else:
                    row.status = "failed"
    db.commit()
    return {"configured": True, "sent": sent, "failed": failed}


def daily_report(db: Session) -> Notification:
    from app.services.finance import analytics_overview

    data = analytics_overview(db)
    value = data["inventory_value"]
    message = (
        f"Recorded expenses: ${data['expenses']:.2f}\n"
        f"Cash revenue: ${data['revenue']:.2f}\n"
        f"Realized result: ${data['realized_profit']:.2f}\n"
        f"Inventory listing estimate: {'unavailable' if value is None else '$' + format(value, '.2f')}\n"
        "Inventory and Steam Wallet values are not withdrawable cash."
    )
    return notify(db, "daily_report", message)
