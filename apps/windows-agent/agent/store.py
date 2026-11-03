"""Durable idempotency and process ownership journal, independent from transport."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, ack TEXT)")
        if "created" not in {column[1] for column in self.db.execute("PRAGMA table_info(commands)")}:
            self.db.execute("ALTER TABLE commands ADD COLUMN created REAL")
            self.db.execute("UPDATE commands SET created=? WHERE created IS NULL", (time.time(),))
        self.db.execute("CREATE TABLE IF NOT EXISTS processes (session_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, kind TEXT NOT NULL, pid INTEGER NOT NULL, created REAL NOT NULL)")
        self.db.commit()

    def reserve(self, command_id: str, fingerprint: str):
        row = self.db.execute("SELECT fingerprint, ack FROM commands WHERE id=?", (command_id,)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("Command ID was reused with different parameters")
            return True, json.loads(row[1]) if row[1] else None
        self.db.execute("INSERT INTO commands(id, fingerprint, created) VALUES (?,?,?)", (command_id, fingerprint, time.time()))
        self.db.commit()
        return False, None

    def ack(self, command_id, value):
        self.db.execute("UPDATE commands SET ack=? WHERE id=?", (json.dumps(value), command_id))
        self.db.commit()

    def add_process(self, session_id, command_id, kind, pid, created):
        self.db.execute("INSERT INTO processes VALUES (?,?,?,?,?)", (session_id, command_id, kind, pid, created))
        self.db.commit()

    def processes(self):
        keys = ("session_id", "command_id", "kind", "pid", "created")
        return [dict(zip(keys, row)) for row in self.db.execute("SELECT * FROM processes").fetchall()]

    def remove_process(self, session_id):
        self.db.execute("DELETE FROM processes WHERE session_id=?", (session_id,))
        self.db.commit()

    def latest_acks(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT ack FROM commands WHERE ack IS NOT NULL").fetchall()]

    def prune(self, days=30):
        # Expired commands cannot relaunch even after deletion: deadline validation is mandatory.
        self.db.execute("DELETE FROM commands WHERE created<? AND json_extract(ack,'$.status') IN ('STOPPED','FAILED')",
                        (time.time() - days * 86400,))
        self.db.commit()

    def close(self):
        self.db.close()
