import logging
from datetime import timedelta
from celery import Celery
from celery.schedules import crontab
from sqlalchemy import delete, select
from app.core.config import settings
from app.core.db import SessionLocal, utcnow
from app.core.security import audit
from app.models.models import NodeMetric, AuditLog, AuthSession, Command, Notification
from app.services.scheduler import tick

log = logging.getLogger(__name__)
celery_app = Celery("farm", broker=settings().redis_url, backend=settings().redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_routes={"farm.inventory_sync": {"queue": "inventory"}},
    beat_schedule={
        "dispatch": {"task": "farm.dispatch", "schedule": 5.0},
        "notifications": {"task": "farm.notifications", "schedule": 30.0},
        "inventory-scan": {"task": "farm.inventory_scan", "schedule": 60.0},
        "retention": {"task": "farm.retention", "schedule": 3600.0},
        "daily-report": {
            "task": "farm.daily_report",
            "schedule": crontab(hour=0, minute=5),
        },
    },
)


@celery_app.task(name="farm.dispatch")
def dispatch():
    with SessionLocal() as db:
        tick(db)
        db.commit()
    from app.main import publish

    publish()


@celery_app.task(name="farm.inventory_sync", bind=True, max_retries=3)
def inventory_sync(self, account_id):
    from app.services.inventory import sync_inventory, InventoryUnavailable

    with SessionLocal() as db:
        try:
            result = sync_inventory(db, account_id)
            audit(db, "inventory", "inventory.synced", account_id)
            db.commit()
            return result
        except InventoryUnavailable as exc:
            db.rollback()
            audit(db, "inventory", "inventory.failed", account_id, {"error": str(exc)})
            from app.services.notifications import notify

            notify(
                db,
                "inventory_sync_failed",
                f"Inventory sync failed for {account_id}: {str(exc)[:200]}",
                "warning",
            )
            db.commit()
            if getattr(exc, "visibility", None) == "private":
                return {"status": "private", "error": str(exc)}
            raise self.retry(
                exc=exc,
                countdown=min(
                    1800,
                    max(
                        int(getattr(exc, "retry_after", None) or 0),
                        30 * 2**self.request.retries,
                    ),
                ),
            )
        except Exception:
            db.rollback()
            log.exception("Inventory task failed account=%s", account_id)
            raise


@celery_app.task(name="farm.notifications")
def notifications():
    from app.services.notifications import deliver_notifications

    with SessionLocal() as db:
        return deliver_notifications(db)


@celery_app.task(name="farm.daily_report")
def daily_report():
    from app.services.notifications import daily_report as report

    with SessionLocal() as db:
        obj = report(db)
        db.commit()
        return obj.id


@celery_app.task(name="farm.retention")
def retention():
    cutoff = utcnow() - timedelta(days=settings().retention_days)
    with SessionLocal() as db:
        db.execute(delete(NodeMetric).where(NodeMetric.created_at < cutoff))
        db.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
        db.execute(
            delete(Command).where(
                Command.created_at < cutoff,
                Command.ack_status.in_(("STOPPED", "FAILED")),
            )
        )
        # Administrative audit retained 365 days, metrics retention separately configurable.
        db.execute(
            delete(AuditLog).where(AuditLog.created_at < utcnow() - timedelta(days=365))
        )
        db.execute(
            delete(Notification).where(
                Notification.created_at < cutoff,
                Notification.status.in_(("sent", "skipped", "failed")),
            )
        )
        db.commit()


@celery_app.task(name="farm.inventory_scan")
def inventory_scan():
    """Bounded, independently routed inventory rotation; cache keys prevent queue floods."""
    import os
    from redis import Redis
    from app.models.models import SteamAccount

    interval = max(300, int(os.getenv("INVENTORY_SYNC_INTERVAL_SECONDS", "21600")))
    client = Redis.from_url(settings().redis_url, socket_timeout=3)
    queued = 0
    with SessionLocal() as db:
        accounts = db.scalars(
            select(SteamAccount)
            .where(SteamAccount.archived == False)
            .order_by(SteamAccount.last_sync.asc().nullsfirst())
            .limit(5000)
        )
        for account in accounts:
            key = "farm:inventory-scheduled:" + account.id
            if client.set(key, "queued", nx=True, ex=interval):
                try:
                    inventory_sync.apply_async(args=[account.id], queue="inventory")
                except Exception:
                    client.delete(key)
                    raise
                queued += 1
                if queued >= 20:
                    break
    return {"queued": queued}
