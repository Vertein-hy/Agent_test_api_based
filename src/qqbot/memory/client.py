"""Durable delivery and revocation; no neural dependencies in the QQ process."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import aiohttp


class MemoryClient:
    """Own durable revocation, retryable intake and per-scope feedback receipts."""

    def __init__(self, path, token, url="http://memory-service:8083"):
        if url != "http://memory-service:8083":
            raise ValueError("Invalid memory service address")
        self.url, self.token = url, token
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS actors(actor TEXT PRIMARY KEY,generation INTEGER,enabled INTEGER);
          CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,actor TEXT,path TEXT,payload TEXT);
          CREATE TABLE IF NOT EXISTS recalls(actor TEXT,scope TEXT,recall_id TEXT,PRIMARY KEY(actor,scope));
          CREATE TABLE IF NOT EXISTS receipts(actor TEXT PRIMARY KEY,payload TEXT);
        """)
        self.db.commit()
        self.lock = asyncio.Lock()
        self.state = "idle"

    def preference(self, actor):
        row = self.db.execute(
            "SELECT generation,enabled FROM actors WHERE actor=?", (actor,)
        ).fetchone()
        return (row[0], bool(row[1])) if row else (0, True)

    def identity(self, actor, scope):
        return {"actor": actor, "scope": scope, "generation": self.preference(actor)[0]}

    def control(self, actor, desired, capture, forget=False):
        """Advance actor generation; preserve pending deletion across pause/resume and outages."""
        generation = self.preference(actor)[0] + 1
        body = {
            "actor": actor,
            "generation": generation,
            "enabled": desired and capture,
            "forget": forget,
        }
        # A pending forget must survive a subsequent pause/resume or network outage.
        prior = self.db.execute(
            "SELECT payload FROM outbox WHERE actor=? AND path='control'", (actor,)
        ).fetchall()
        body["forget"] = forget or any(json.loads(r[0])["forget"] for r in prior)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO actors VALUES (?,?,?)", (actor, generation, int(desired))
            )
            self.db.execute("DELETE FROM outbox WHERE actor=?", (actor,))
            if forget:
                self.db.execute("DELETE FROM recalls WHERE actor=?", (actor,))
                self.db.execute("DELETE FROM receipts WHERE actor=?", (actor,))
            self.db.execute(
                "INSERT INTO outbox(actor,path,payload) VALUES (?,'control',?)",
                (actor, json.dumps(body)),
            )
        return generation

    def enqueue(self, body, path="ingest"):
        if path not in ("ingest", "update", "feedback"):
            raise ValueError("Invalid outbox path")
        if self.db.execute("SELECT count(*) FROM outbox").fetchone()[0] >= 1000:
            raise RuntimeError("Memory outbox full")
        with self.db:
            self.db.execute(
                "INSERT INTO outbox(actor,path,payload) VALUES (?,?,?)",
                (body["actor"], path, json.dumps(body, ensure_ascii=False)),
            )

    def recall_id(self, actor, scope, value=None):
        if value is not None:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO recalls VALUES (?,?,?)", (actor, scope, value)
                )
        row = self.db.execute(
            "SELECT recall_id FROM recalls WHERE actor=? AND scope=?", (actor, scope)
        ).fetchone()
        return row[0] if row else ""

    def has_control(self, actor):
        return bool(
            self.db.execute(
                "SELECT 1 FROM outbox WHERE actor=? AND path='control'", (actor,)
            ).fetchone()
        )

    async def request(self, session, path, body, timeout=4):
        async with session.post(
            self.url + "/" + path,
            json=body,
            headers={"Authorization": "Bearer " + self.token},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            raw = await response.content.read(65537)
            if len(raw) > 65536:
                raise ValueError("Memory response too large")
            if response.status == 409:
                return {"stale": True}
            if response.status != 200:
                raise RuntimeError("Memory service unavailable")
            return json.loads(raw)

    async def flush(self, session, limit=8):
        """Send control before data, rejecting stale restored generations instead of guessing."""
        async with self.lock:
            for _ in range(limit):
                row = self.db.execute(
                    "SELECT id,path,payload FROM outbox ORDER BY CASE WHEN path='control' THEN 0 ELSE 1 END,id LIMIT 1"
                ).fetchone()
                if not row:
                    return True
                try:
                    result = await self.request(session, row[1], json.loads(row[2]))
                    self.state = "ready"
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
                    self.state = "deferred"
                    return False
                if result.get("stale") and row[1] == "control":
                    # Fail closed if the persistent bot state was restored from an older backup.
                    self.state = "state_conflict"
                    return False
                with self.db:
                    body = json.loads(row[2])
                    if row[1] in ("update", "feedback"):
                        self.db.execute(
                            "INSERT OR REPLACE INTO receipts VALUES (?,?)",
                            (body["actor"], json.dumps(result)),
                        )
                    self.db.execute("DELETE FROM outbox WHERE id=?", (row[0],))
            return True

    async def run(self, session):
        while True:
            await self.flush(session)
            await asyncio.sleep(5)

    def close(self):
        self.db.close()
