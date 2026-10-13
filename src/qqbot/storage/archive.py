"""Private SQLite archive, explicit recording choices, and reviewed SFT exports."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time


def redact(text):
    # Best effort only. Dataset approval still requires human review.
    patterns = [
        r"\bsk-[A-Za-z0-9_-]{12,}\b",
        r"(?i)bearer\s+[A-Za-z0-9._~-]{12,}",
        r"\b1[3-9]\d{9}\b",
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "[已脱敏]", text)
    return text


class Archive:
    """Own recording consent, pseudonymous scopes and delivered/reviewed export eligibility."""

    def __init__(self, path, salt, default_capture=False):
        self.salt = salt
        self.default_capture = default_capture
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS preferences (
                actor TEXT PRIMARY KEY, capture INTEGER NOT NULL DEFAULT 0,
                training INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, actor TEXT NOT NULL, scope TEXT NOT NULL,
                created REAL NOT NULL, system TEXT NOT NULL, question TEXT NOT NULL,
                answer TEXT NOT NULL, requested_model TEXT NOT NULL, reported_model TEXT,
                trace TEXT NOT NULL, prompt_messages TEXT NOT NULL DEFAULT '[]',
                kind TEXT NOT NULL DEFAULT 'chat', delivered INTEGER NOT NULL DEFAULT 0,
                reviewed INTEGER NOT NULL DEFAULT 0, approved INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS contexts (scope TEXT PRIMARY KEY, reset_at REAL NOT NULL);
        """)
        self.db.commit()

    def pseudonym(self, value):
        return hmac.new(self.salt, value.encode(), hashlib.sha256).hexdigest()

    def actor(self, user):
        return self.pseudonym("user:" + str(user))

    def scope(self, user, group):
        return self.pseudonym("scope:" + str(group or "private") + ":" + str(user))

    def preferences(self, user):
        row = self.db.execute(
            "SELECT capture,training FROM preferences WHERE actor=?", (self.actor(user),)
        ).fetchone()
        return tuple(bool(x) for x in row) if row else (self.default_capture, False)

    def configure(self, user, *, capture=None, training=None):
        old_capture, old_training = self.preferences(user)
        capture = old_capture if capture is None else bool(capture)
        training = old_training if training is None else bool(training)
        self.db.execute(
            "INSERT OR REPLACE INTO preferences VALUES (?,?,?)",
            (self.actor(user), int(capture), int(training)),
        )
        self.db.commit()

    def append(
        self,
        user,
        group,
        system,
        question,
        answer,
        requested_model,
        reported_model=None,
        trace=None,
        kind="chat",
        prompt_messages=None,
    ):
        if not self.preferences(user)[0]:
            return None
        row = self.db.execute(
            """INSERT INTO turns
            (actor,scope,created,system,question,answer,requested_model,reported_model,trace,kind,prompt_messages)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.actor(user),
                self.scope(user, group),
                time.time(),
                system,
                question[:4000],
                answer,
                requested_model,
                reported_model,
                json.dumps(trace or [], ensure_ascii=False),
                kind,
                json.dumps(prompt_messages or [], ensure_ascii=False),
            ),
        )
        self.db.commit()
        return row.lastrowid

    def delivered(self, turn_id):
        if turn_id is not None:
            self.db.execute("UPDATE turns SET delivered=1 WHERE id=?", (turn_id,))
            self.db.commit()

    def recent(self, user, group, limit=6):
        """Restore only delivered chat answers after the latest reset within the short TTL."""
        if not self.preferences(user)[0]:
            return []
        scope = self.scope(user, group)
        reset = self.db.execute("SELECT reset_at FROM contexts WHERE scope=?", (scope,)).fetchone()
        since = max(time.time() - 1800, reset[0] if reset else 0)
        rows = self.db.execute(
            """SELECT question,answer FROM turns
            WHERE scope=? AND delivered=1 AND kind='chat' AND created>? ORDER BY id DESC LIMIT ?""",
            (scope, since, limit),
        ).fetchall()
        return [
            m
            for question, answer in reversed(rows)
            for m in (
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            )
        ]

    def forget(self, user):
        """Delete actor records while retaining opt-out so default capture cannot recreate them."""
        actor = self.actor(user)
        self.db.execute("DELETE FROM turns WHERE actor=?", (actor,))
        for table in ("style_profiles", "style_evidence", "style_events", "style_settings"):
            if self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                self.db.execute(f"DELETE FROM {table} WHERE actor=?", (actor,))
        # Preserve an opt-out flag so default recording does not immediately restart.
        self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,0,0)", (actor,))
        self.db.commit()
        self.db.execute("VACUUM")

    def reset_context(self, user, group):
        self.db.execute(
            "INSERT OR REPLACE INTO contexts VALUES (?,?)", (self.scope(user, group), time.time())
        )
        self.db.commit()

    def review(self, turn_id, approved):
        row = self.db.execute("SELECT delivered,kind FROM turns WHERE id=?", (turn_id,)).fetchone()
        if not row or not row[0] or row[1] != "chat":
            raise ValueError("Only delivered successful chat turns can be reviewed")
        self.db.execute(
            "UPDATE turns SET reviewed=1,approved=? WHERE id=?", (int(approved), turn_id)
        )
        self.db.commit()

    def export(self, path, format="messages"):
        """Export only consented, delivered and manually approved plain conversation turns."""
        if format not in ("messages", "sharegpt"):
            raise ValueError("Unsupported export format")
        rows = self.db.execute("""SELECT t.system,t.question,t.answer,t.trace,t.prompt_messages FROM turns t
            JOIN preferences p ON p.actor=t.actor
            WHERE t.delivered=1 AND t.kind='chat' AND t.reviewed=1 AND t.approved=1 AND p.training=1
            ORDER BY t.id""").fetchall()
        count, skipped = 0, 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            for system, question, answer, trace, prompt in rows:
                # Tool-grounded turns need a dedicated tool-training dataset, not
                # an ungrounded plain SFT pair. Preserve their raw trace for later.
                if json.loads(trace):
                    skipped += 1
                    continue
                original = json.loads(prompt) or [
                    {"role": "system", "content": system},
                    {"role": "user", "content": question},
                ]
                original = original + [{"role": "assistant", "content": answer}]
                if any(
                    m.get("role") not in ("system", "user", "assistant")
                    or not isinstance(m.get("content"), str)
                    for m in original
                ):
                    skipped += 1
                    continue
                messages = [{"role": m["role"], "content": redact(m["content"])} for m in original]
                if format == "messages":
                    item = {"messages": messages}
                else:
                    item = {
                        "conversations": [
                            {
                                "from": {"system": "system", "user": "human", "assistant": "gpt"}[
                                    m["role"]
                                ],
                                "value": m["content"],
                            }
                            for m in messages
                        ]
                    }
                file.write(json.dumps(item, ensure_ascii=False) + "\n")
                count += 1
        return count, skipped

    def close(self):
        self.db.close()
