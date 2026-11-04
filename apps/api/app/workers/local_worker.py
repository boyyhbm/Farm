"""Embedded single-installation scheduler and durable inventory worker.

PostgreSQL installations continue using Celery/Redis. Desktop has two threads so
external Steam latency cannot block process scheduling or heartbeat requests.
"""

import logging
import threading
import time
from datetime import timedelta
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal, utcnow
from app.core.local_store import LocalStore
from app.core.security import audit
from app.models.models import SteamAccount

log = logging.getLogger("farm.desktop.worker")


class DesktopWorker:
    def __init__(self):
        self.store = LocalStore()
        self.stop_event = threading.Event()
        self.threads = []

    def start(self):
        for target, name in (
            (self.control_loop, "desktop-scheduler"),
            (self.inventory_loop, "desktop-inventory"),
            (self.protection_loop, "desktop-protection"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)

    def control_loop(self):
        from app.services.scheduler import tick
        from app.services.notifications import deliver_notifications, daily_report
        from app.workers.tasks import retention

        due = {}
        while not self.stop_event.is_set():
            now = time.monotonic()
            try:
                self.store.put(
                    "worker:heartbeat", {"timestamp": utcnow().isoformat()}, 15
                )
                if now >= due.get("scheduler", 0):
                    with SessionLocal() as db:
                        tick(db)
                        db.commit()
                    due["scheduler"] = now + 5
                if now >= due.get("notifications", 0):
                    with SessionLocal() as db:
                        deliver_notifications(db)
                    due["notifications"] = now + 30
                if now >= due.get("scan", 0):
                    with SessionLocal() as db:
                        accounts = list(
                            db.scalars(
                                select(SteamAccount)
                                .where(SteamAccount.archived == False)
                                .order_by(SteamAccount.last_sync.asc().nullsfirst())
                                .limit(5000)
                            )
                        )
                        queued = 0
                        for a in accounts:
                            if self.store.put(
                                "inventory:scheduled:" + a.id, True, 21600, nx=True
                            ):
                                self.store.enqueue("inventory", {"account_id": a.id})
                                queued += 1
                                if queued >= 20:
                                    break
                    due["scan"] = now + 60
                today = utcnow().date().isoformat()
                if self.store.put("daily-report:" + today, True, 172800, nx=True):
                    with SessionLocal() as db:
                        daily_report(db)
                        db.commit()
                if now >= due.get("retention", 0):
                    retention.run()
                    self.store.prune(settings().retention_days)
                    due["retention"] = now + 3600
            except Exception:
                log.exception("Local scheduler cycle failed")
            self.stop_event.wait(1)

    def inventory_loop(self):
        from app.services.inventory import sync_inventory, InventoryUnavailable
        from app.services.notifications import notify

        while not self.stop_event.is_set():
            task = None
            try:
                task = self.store.claim(kind="inventory")
                if task:
                    with SessionLocal() as db:
                        sync_inventory(db, task["payload"]["account_id"])
                        audit(
                            db,
                            "desktop-worker",
                            "inventory.synced",
                            task["payload"]["account_id"],
                        )
                        db.commit()
                    self.store.finish(task["id"])
                else:
                    self.stop_event.wait(1)
            except Exception as exc:
                log.exception("Local inventory task failed")
                if task:
                    error = str(exc)[:2000]
                    retry = (
                        isinstance(exc, InventoryUnavailable)
                        and exc.visibility != "private"
                        and task["attempts"] < 4
                    )
                    self.store.finish(
                        task["id"],
                        error,
                        max(exc.retry_after, 30 * 2 ** task["attempts"])
                        if retry
                        else None,
                    )
                    with SessionLocal() as db:
                        audit(
                            db,
                            "desktop-worker",
                            "inventory.failed",
                            task["payload"]["account_id"],
                            {"error": error},
                        )
                        from app.services.protection import incident, safe_message

                        incident(
                            db,
                            "STEAM_RATE_LIMIT"
                            if isinstance(exc, InventoryUnavailable)
                            and "429" in str(exc)
                            else "STEAM_SYNC_FAILED",
                            account_id=task["payload"]["account_id"],
                            source="steam_inventory",
                            evidence={
                                "error": safe_message(error)
                            },
                            occurrence=True,
                        )
                        notify(
                            db,
                            "inventory_sync_failed",
                            f"Inventory sync failed for {task['payload']['account_id']}",
                            "warning",
                        )
                        db.commit()
                self.stop_event.wait(1)

    def protection_loop(self):
        from app.services.protection import check_accounts

        while not self.stop_event.is_set():
            task = None
            try:
                task = self.store.claim(kind="protection")
                with SessionLocal() as db:
                    result = check_accounts(
                        db, task["payload"].get("account_ids") if task else None
                    )
                    if task:
                        deferred = result["status"] in {
                            "BUSY",
                            "COOLDOWN",
                            "LEASE_EXPIRED",
                        }
                        self.store.finish(
                            task["id"],
                            "Protection request deferred" if deferred else None,
                            result.get("retry_after", 60)
                            if deferred and task["attempts"] < 4
                            else None,
                        )
            except Exception:
                log.exception("Protection worker failed")
                if task:
                    self.store.finish(
                        task["id"],
                        "Protection worker unavailable",
                        60 if task["attempts"] < 4 else None,
                    )
            self.stop_event.wait(10)
