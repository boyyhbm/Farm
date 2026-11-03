"""Persistent coordination for a single Windows desktop installation, not a Redis mock."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from app.core.config import settings


class LocalStore:
    def __init__(self, path=None):
        self.path = Path(
            path or (Path(settings().desktop_data_dir) / "coordination.sqlite3")
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,
                  available REAL NOT NULL,lease_until REAL,error TEXT,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status,available);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def ping(self):
        with self.connection() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def put(self, key, value, ttl, nx=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM kv WHERE key=? AND expires<=?", (key, time.time()))
            if nx:
                return bool(
                    db.execute(
                        "INSERT OR IGNORE INTO kv VALUES(?,?,?)",
                        (key, json.dumps(value), time.time() + ttl),
                    ).rowcount
                )
            db.execute(
                "INSERT OR REPLACE INTO kv VALUES(?,?,?)",
                (key, json.dumps(value), time.time() + ttl),
            )
            return True

    def get(self, key):
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM kv WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
            return json.loads(row[0]) if row else None

    def remove(self, key):
        with self.connection() as db:
            db.execute("DELETE FROM kv WHERE key=?", (key,))

    def ttl(self, key):
        with self.connection() as db:
            row = db.execute("SELECT expires FROM kv WHERE key=?", (key,)).fetchone()
            return max(0, int(row[0] - time.time() + 0.999)) if row else 0

    def counter(self, key, seconds=60):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value,expires FROM kv WHERE key=?", (key,)
            ).fetchone()
            now = time.time()
            value = int(json.loads(row[0])) + 1 if row and row[1] > now else 1
            expires = row[1] if row and row[1] > now else now + seconds
            db.execute(
                "INSERT OR REPLACE INTO kv VALUES(?,?,?)",
                (key, json.dumps(value), expires),
            )
            return value

    def reserve_slot(self, key, seconds):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute("SELECT expires FROM kv WHERE key=?", (key,)).fetchone()
            if row and row[0] > now:
                return row[0] - now
            db.execute(
                "INSERT OR REPLACE INTO kv VALUES(?,?,?)", (key, "true", now + seconds)
            )
            return 0

    def enqueue(self, kind, payload):
        id = str(uuid.uuid4())
        now = time.time()
        with self.connection() as db:
            db.execute(
                "INSERT INTO tasks(id,kind,payload,available,created) VALUES(?,?,?,?,?)",
                (id, kind, json.dumps(payload), now, now),
            )
        return id

    def claim(self, lease_seconds=900):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute(
                "UPDATE tasks SET status='queued' WHERE status='running' AND lease_until<?",
                (now,),
            )
            row = db.execute(
                "SELECT * FROM tasks WHERE status='queued' AND available<=? ORDER BY created LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE tasks SET status='running',attempts=attempts+1,lease_until=? WHERE id=?",
                (now + lease_seconds, row["id"]),
            )
            result = dict(row)
            result["attempts"] += 1
            result["payload"] = json.loads(result["payload"])
            return result

    def finish(self, id, error=None, retry_after=None):
        with self.connection() as db:
            db.execute(
                "UPDATE tasks SET status=?,error=?,available=?,lease_until=NULL WHERE id=?",
                (
                    "queued"
                    if retry_after is not None
                    else ("failed" if error else "completed"),
                    error,
                    time.time() + (retry_after or 0),
                    id,
                ),
            )

    def prune(self, days=30):
        with self.connection() as db:
            db.execute("DELETE FROM kv WHERE expires<?", (time.time(),))
            db.execute(
                "DELETE FROM tasks WHERE status IN ('completed','failed') AND created<?",
                (time.time() - days * 86400,),
            )
