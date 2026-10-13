import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from qqbot.memory.policy import checked_event, checked_extraction, clean, identity
from qqmemory.jobs import Jobs

A, S = "a" * 64, "b" * 64


def event(question="我现在住在杭州", created=None):
    return {
        "id": hashlib.sha256((question + str(created)).encode()).hexdigest(),
        "created": created or time.time(),
        "question": question,
        "answer": "知道了。",
    }


class PolicyTests(unittest.TestCase):
    def test_panel_and_credentials_do_not_enter_memory(self):
        for text in ("查询原神 UID 700000001 的雷神面板", "查看展柜", "使用yunzai", "密码是123456"):
            self.assertIsNone(checked_event(event(text)))
        value = clean(
            "我喜欢游泳\n密码123456\nhttps://example.com?key=secret\nfixture@example.com\nsk-1234567890abcdef"
        )
        self.assertIn("游泳", value)
        for secret in ("123456", "secret", "fixture@example.com", "sk-123"):
            self.assertNotIn(secret, value)

    def test_only_exact_user_evidence_and_nontemporary_claims_are_accepted(self):
        q = "我现在住在杭州。假如我住在北京。今天只要简短回答。"
        claims = [
            {"key": "city", "value": "杭州", "evidence": "我现在住在杭州", "temporary": False},
            {"key": "city", "value": "北京", "evidence": "假如我住在北京", "temporary": False},
            {"key": "name", "value": "虚构", "evidence": "机器人叫我虚构", "temporary": False},
            {
                "key": "preference",
                "value": "简短",
                "evidence": "今天只要简短回答",
                "temporary": True,
            },
            {"key": ["city"], "value": "坏格式", "evidence": "我现在住在杭州", "temporary": False},
        ]
        result = checked_extraction({"summary": q, "topics": ["居住地"], "claims": claims}, q)
        self.assertEqual([c["value"] for c in result["claims"]], ["杭州"])

    def test_identity_and_event_boundaries(self):
        self.assertEqual(identity({"actor": A, "scope": S, "generation": 0}), (A, S, 0))
        for body in (
            {"actor": "123", "generation": 0},
            {"actor": A, "scope": "bad", "generation": 0},
            {"actor": A, "generation": True},
            {"actor": A, "generation": -1},
        ):
            with self.assertRaises(ValueError):
                identity(body)
        with self.assertRaises(ValueError):
            checked_event(event(created=time.time() + 500))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.db"
        self.jobs = Jobs(self.path)

    def tearDown(self):
        self.jobs.db.close()
        self.tmp.cleanup()

    def test_durable_idempotence_and_processing_restart(self):
        e = event()
        self.assertTrue(self.jobs.put(A, S, 0, e))
        self.assertTrue(self.jobs.put(A, S, 0, e))
        self.assertEqual(self.jobs.counts(), {"pending": 1})
        self.jobs.next()
        self.jobs.db.close()
        self.jobs = Jobs(self.path)
        job = self.jobs.next()
        self.assertEqual(job["event"], e)
        self.jobs.finish(job)
        self.assertIsNone(self.jobs.next())
        self.assertEqual(self.jobs.db.execute("SELECT payload FROM jobs").fetchone()[0], "")

    def test_pause_forget_and_generation_revoke_old_work(self):
        self.jobs.put(A, S, 0, event())
        old = self.jobs.next()
        self.assertTrue(self.jobs.control(A, 1, False, True))
        self.assertFalse(self.jobs.allowed(A, old["generation"]))
        self.jobs.finish(old)
        self.assertEqual(self.jobs.counts(), {})
        self.assertFalse(self.jobs.put(A, S, 0, event("我喜欢阅读")))
        self.assertFalse(self.jobs.control(A, 0, True, False))
        self.assertTrue(self.jobs.control(A, 2, True, False))
        self.assertFalse(self.jobs.put(A, S, 1, event()))
        self.assertTrue(self.jobs.put(A, S, 2, event()))
        self.assertFalse(self.jobs.allowed("c" * 64, 2))


if __name__ == "__main__":
    unittest.main()
