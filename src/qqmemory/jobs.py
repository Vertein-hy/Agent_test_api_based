"""Durable generation-based revocation and consolidation jobs."""

import json
import sqlite3
import time


class Jobs:
    """Persist actor generations and retryable jobs; old generations cannot regain access."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS actors (actor TEXT PRIMARY KEY,generation INTEGER NOT NULL,enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,actor TEXT NOT NULL,scope TEXT NOT NULL,
                generation INTEGER NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status,next_at);
        """)
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='processing'")
        self.db.commit()

    def state(self, actor):
        row = self.db.execute(
            "SELECT generation,enabled FROM actors WHERE actor=?", (actor,)
        ).fetchone()
        return (row[0], bool(row[1])) if row else (0, True)

    def allowed(self, actor, generation):
        return self.state(actor) == (generation, True)

    def control(self, actor, generation, enabled, forget):
        """Apply monotonic revocation and delete queued work for the affected actor."""
        current, _ = self.state(actor)
        if generation < current:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO actors VALUES (?,?,?)", (actor, generation, int(enabled))
        )
        self.db.execute(
            "DELETE FROM jobs WHERE actor=? AND (generation<>? OR ?)",
            (actor, generation, int(forget or not enabled)),
        )
        self.db.commit()
        if forget:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True

    def put(self, actor, scope, generation, event):
        if not self.allowed(actor, generation):
            return False
        count = self.db.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('pending','processing')"
        ).fetchone()[0]
        if count >= 500:
            raise RuntimeError("Memory queue is full")
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,actor,scope,generation,payload) VALUES (?,?,?,?,?)",
            (event["id"], actor, scope, generation, json.dumps(event, ensure_ascii=False)),
        )
        self.db.commit()
        return True

    def next(self):
        row = self.db.execute(
            "SELECT id,actor,scope,generation,payload,attempts FROM jobs WHERE status='pending' AND next_at<=? ORDER BY rowid LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row:
            self.db.execute("UPDATE jobs SET status='processing' WHERE id=?", (row[0],))
            self.db.commit()
            return {
                "id": row[0],
                "actor": row[1],
                "scope": row[2],
                "generation": row[3],
                "event": json.loads(row[4]),
                "attempts": row[5],
            }

    def finish(self, job, error=False):
        if error and job["attempts"] < 2:
            self.db.execute(
                "UPDATE jobs SET status='pending',attempts=attempts+1,next_at=? WHERE id=?",
                (time.time() + 30 * (job["attempts"] + 1), job["id"]),
            )
        else:
            self.db.execute(
                "UPDATE jobs SET status=?,payload='' WHERE id=?",
                ("failed" if error else "done", job["id"]),
            )
        self.db.commit()

    def counts(self):
        return dict(self.db.execute("SELECT status,count(*) FROM jobs GROUP BY status"))
