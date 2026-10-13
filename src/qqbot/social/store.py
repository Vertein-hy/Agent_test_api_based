"""Durable social jobs and public journal, independent of personal memory.

Persist sending BEFORE network I/O. A crash or uncertain write never requeues
the write; read-only reconciliation may later prove delivery. Pause increments
the generation so slow model work cannot publish after control changes.
"""

import json
import os
from pathlib import Path
import sqlite3
import time


class SocialStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, generation INTEGER, paused INTEGER);
            INSERT OR IGNORE INTO settings VALUES(1, 0, 0);
            CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY, owner TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS posts (
                owner TEXT, tid TEXT, root_owner TEXT, root_tid TEXT, content TEXT,
                created REAL, active INTEGER DEFAULT 1, PRIMARY KEY(owner,tid));
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, kind TEXT, payload TEXT, state TEXT,
                content TEXT DEFAULT '', remote_id TEXT DEFAULT '', generation INTEGER,
                created REAL, due REAL, quota_actor TEXT DEFAULT '', sent_at REAL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,due);
        """)
        with self.db:
            self.db.execute("UPDATE jobs SET state='unknown' WHERE state='sending'")
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='generating'")

    @property
    def generation(self):
        return self.db.execute("SELECT generation FROM settings WHERE id=1").fetchone()[0]

    @property
    def paused(self):
        return bool(self.db.execute("SELECT paused FROM settings WHERE id=1").fetchone()[0])

    def bind_owner(self, owner):
        """Changing the logged-in bot cannot take over another account's journal."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO identity VALUES(1,?)", (owner,))
        if self.db.execute("SELECT owner FROM identity WHERE id=1").fetchone()[0] != owner:
            raise ValueError("Social account changed")

    def write_allowed(self, job):
        if self.paused or job["generation"] != self.generation:
            return False
        row = self.db.execute("SELECT state FROM jobs WHERE key=?", (job["key"],)).fetchone()
        if not row or row[0] != "sending":
            return False
        if job["kind"] in ("reply", "repost"):
            p = job["payload"]["post"]
            return self.get_post(p["owner"], p["tid"]) is not None
        return True

    def pause(self, paused):
        with self.db:
            self.db.execute(
                "UPDATE settings SET paused=?,generation=generation+1 WHERE id=1", (int(paused),)
            )
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE state IN ('queued','generating')"
            )

    def enqueue(self, key, kind, payload, delay=0, quota_actor=""):
        if self.paused:
            return False
        if (
            self.db.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','generating')"
            ).fetchone()[0]
            >= 200
        ):
            return False
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(key,kind,payload,state,generation,created,due,quota_actor) VALUES(?,?,?,'queued',?,?,?,?)",
                (
                    key,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    self.generation,
                    time.time(),
                    time.time() + delay,
                    quota_actor,
                ),
            )
        return cursor.rowcount == 1

    def next_job(self):
        row = self.db.execute(
            "SELECT * FROM jobs WHERE state='queued' AND due<=? ORDER BY due LIMIT 1",
            (time.time(),),
        ).fetchone()
        return self.unpack(row) if row else None

    @staticmethod
    def unpack(row):
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def set_job(self, key, state, content=None, remote_id=None):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?,content=coalesce(?,content),remote_id=coalesce(?,remote_id) WHERE key=? AND state!='redacted'",
                (state, content, remote_id, key),
            )

    def active(self, job):
        if self.paused or job["generation"] != self.generation:
            return False
        row = self.db.execute("SELECT state FROM jobs WHERE key=?", (job["key"],)).fetchone()
        if not row or row[0] not in ("queued", "generating"):
            return False
        if job["kind"] in ("reply", "repost"):
            p = job["payload"]["post"]
            return self.get_post(p["owner"], p["tid"]) is not None
        return True

    def reserve_send(self, job):
        if not self.active(job):
            return False
        now = time.time()
        # Rolling limits count uncertain sends too. A timeout is not a free retry.
        sent = self.db.execute(
            "SELECT kind,quota_actor,sent_at FROM jobs WHERE sent_at>?", (now - 86400,)
        ).fetchall()
        kind = job["kind"]
        if kind == "publish":
            limited = sum(x[0] == "publish" for x in sent) >= 10
        else:
            limited = (
                sum(x[0] != "publish" for x in sent) >= 30
                or sum(x[1] == job["quota_actor"] for x in sent) >= 5
            )
        if limited or any(x[2] > now - 30 for x in sent):
            with self.db:
                self.db.execute(
                    "UPDATE jobs SET state='queued',due=? WHERE key=?", (now + 120, job["key"])
                )
            return False
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='sending',sent_at=? WHERE key=?", (now, job["key"])
            )
        return True

    def add_post(self, owner, tid, content, created=None, root=None):
        root = root or (owner, tid)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO posts(owner,tid,root_owner,root_tid,content,created) VALUES(?,?,?,?,?,?)",
                (owner, tid, *root, content, created or time.time()),
            )

    def posts(self):
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT * FROM posts WHERE active=1 AND created>? ORDER BY created DESC LIMIT 64",
                (time.time() - 7 * 86400,),
            )
        ]

    def get_post(self, owner, tid):
        return next((x for x in self.posts() if x["owner"] == owner and x["tid"] == tid), None)

    def stop(self, tid):
        with self.db:
            self.db.execute("UPDATE posts SET active=0 WHERE tid=? OR root_tid=?", (tid, tid))
            self.db.execute("UPDATE settings SET generation=generation+1 WHERE id=1")
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE state IN ('queued','generating')"
            )

    def forget_actor(self, owner, quota_actor):
        """Retain only dedup tombstones; revoked text cannot re-enter a slow reply."""
        keys = []
        for row in self.db.execute("SELECT key,payload,quota_actor FROM jobs"):
            payload = json.loads(row[1])
            if row[2] == quota_actor or payload.get("post", {}).get("owner") == owner:
                keys.append((row[0],))
        with self.db:
            self.db.execute("DELETE FROM posts WHERE owner=?", (owner,))
            self.db.executemany(
                "UPDATE jobs SET state='redacted',payload='{}',content='',remote_id='' WHERE key=?",
                keys,
            )
            self.db.execute("UPDATE settings SET generation=generation+1 WHERE id=1")
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE state IN ('queued','generating')"
            )

    def recent(self, state=None):
        if state:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY created DESC LIMIT 30", (state,)
            )
        else:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 8")
        return [self.unpack(x) for x in rows]

    def branch(self, owner, tid, parent):
        result = []
        for x in self.db.execute(
            "SELECT payload,content FROM jobs WHERE kind='reply' AND state='succeeded' ORDER BY created DESC LIMIT 100"
        ):
            p = json.loads(x[0])
            post = p["post"]
            c = p["comment"]
            if (post["owner"], post["tid"], c["parent"] or c["id"]) == (owner, tid, parent):
                result.append({"comment": c["text"], "reply": x[1]})
        return list(reversed(result[:6]))

    def prune(self):
        # Keep dedup records longer than the seven-day watch window.
        with self.db:
            self.db.execute(
                "DELETE FROM jobs WHERE created<? AND state NOT IN ('sending','unknown')",
                (time.time() - 30 * 86400,),
            )
            self.db.execute("DELETE FROM posts WHERE created<?", (time.time() - 30 * 86400,))

    def close(self):
        self.db.close()
