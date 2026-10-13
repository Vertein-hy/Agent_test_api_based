"""Per-account logistic attention, learned only from explicit memory feedback.
The relation network remains frozen; this small, bounded layer learns relevance.
"""

import json
import math
import sqlite3
import time
import uuid


def features(node, semantic, relational, mode):
    return [
        1.0,
        max(0.0, min(1.0, float(semantic))),
        max(0.0, min(1.0, float(relational))),
        float(node.get("retention", 1)),
        float(node.get("kind") in ("fact", "preference", "task")),
        float(mode == "recall"),
        min(float(node.get("access", 0)), 20) / 20,
    ]


class AttentionLearner:
    """Learn bounded ranking adjustments from explicit, actor/scope-bound recall feedback."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS attention(actor TEXT PRIMARY KEY,weights TEXT,samples INTEGER);
            CREATE TABLE IF NOT EXISTS exposures(id TEXT PRIMARY KEY,actor TEXT,scope TEXT,created REAL,payload TEXT,label INTEGER);
            CREATE TABLE IF NOT EXISTS metrics(day TEXT,name TEXT,value REAL,count INTEGER,PRIMARY KEY(day,name));
        """)
        self.db.commit()

    def state(self, actor):
        row = self.db.execute(
            "SELECT weights,samples FROM attention WHERE actor=?", (actor,)
        ).fetchone()
        return (json.loads(row[0]), row[1]) if row else ([0.0] * 7, 0)

    def adjust(self, actor, vector):
        return self.adjust_state(self.state(actor), vector)

    @staticmethod
    def adjust_state(state, vector):
        w, count = state
        if count < 4:
            return 0.0
        z = sum(a * b for a, b in zip(w, vector))
        return 0.10 * (2 / (1 + math.exp(-max(-20, min(20, z)))) - 1)

    def expose(self, actor, scope, items):
        if not items:
            return None
        ident = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO exposures VALUES (?,?,?,?,?,NULL)",
                (ident, actor, scope, time.time(), json.dumps(items)),
            )
            self.db.execute("DELETE FROM exposures WHERE created<?", (time.time() - 86400 * 30,))
            self.db.execute(
                "DELETE FROM exposures WHERE id IN (SELECT id FROM exposures ORDER BY created DESC LIMIT -1 OFFSET 10000)"
            )
        return ident

    def feedback(self, actor, scope, ident, useful):
        row = self.db.execute(
            "SELECT payload,label FROM exposures WHERE id=? AND actor=? AND scope=?",
            (ident, actor, scope),
        ).fetchone()
        if not row or row[1] is not None:
            return []
        w, count = self.state(actor)
        items = json.loads(row[0])
        for item in items:
            x = item["features"]
            z = max(-20, min(20, sum(a * b for a, b in zip(w, x))))
            p = 1 / (1 + math.exp(-z))
            w = [
                max(-2.0, min(2.0, a + 0.12 * (int(useful) - p) * b - 0.002 * a))
                for a, b in zip(w, x)
            ]
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO attention VALUES (?,?,?)", (actor, json.dumps(w), count + 1)
            )
            self.db.execute("UPDATE exposures SET label=? WHERE id=?", (int(useful), ident))
        self.metric("memory_feedback_useful", int(useful))
        return [i["id"] for i in items]

    def metric(self, name, value):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.db:
            self.db.execute(
                """INSERT INTO metrics VALUES (?,?,?,1) ON CONFLICT(day,name)
                DO UPDATE SET value=value+excluded.value,count=count+1""",
                (day, name, float(value)),
            )

    def report(self):
        return {
            "metrics": [
                dict(day=r[0], name=r[1], mean=round(r[2] / r[3], 4), count=r[3])
                for r in self.db.execute("SELECT * FROM metrics ORDER BY day DESC LIMIT 100")
            ],
            "feedback_accounts": self.db.execute("SELECT count(*) FROM attention").fetchone()[0],
        }

    def forget(self, actor):
        with self.db:
            self.db.execute("DELETE FROM attention WHERE actor=?", (actor,))
            self.db.execute("DELETE FROM exposures WHERE actor=?", (actor,))
