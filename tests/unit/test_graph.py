import hashlib
import os
import time
import unittest
from qqmemory.graph import GraphStore


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = GraphStore(os.getenv("GRAPH_URI", "bolt://memory-graph:7687"))
        cls.actor = hashlib.sha256(b"memory-verification-fictional-actor").hexdigest()
        cls.other = hashlib.sha256(b"memory-verification-other-fictional-actor").hexdigest()
        cls.scope = hashlib.sha256(b"memory-verification-group").hexdigest()
        cls.private = hashlib.sha256(b"memory-verification-private").hexdigest()

    def setUp(self):
        for actor in (self.actor, self.other):
            self.store.forget(actor)

    @classmethod
    def tearDownClass(cls):
        for actor in (cls.actor, cls.other):
            cls.store.forget(actor)
        cls.store.driver.close()

    def add(self, actor, scope, value, created, key="city"):
        question = (
            "我希望你叫我"
            if key == "preferred_name"
            else "我喜欢"
            if key == "interest"
            else "我现在住在"
        ) + value
        event = {
            "id": hashlib.sha256((actor + scope + question + str(created)).encode()).hexdigest(),
            "created": created,
            "question": question,
            "answer": "明白。",
        }
        extracted = {
            "summary": question,
            "topics": ["居住地"],
            "claims": [{"kind": "fact", "key": key, "value": value, "evidence": question}],
        }
        nodes, edges = self.store.prepare(actor, scope, event, extracted)
        for n in nodes:
            n["vector"] = [0.0, 1.0]
        self.store.add(actor, scope, nodes, edges)
        return nodes, edges

    def test_temporal_update_history_idempotence_and_backfilled_old_fact(self):
        now = time.time()
        self.add(self.actor, self.scope, "苏州", now - 100)
        nodes, edges = self.add(self.actor, self.scope, "杭州", now)
        self.store.add(self.actor, self.scope, nodes, edges)
        self.add(self.actor, self.scope, "南京", now - 200)
        active, links = self.store.snapshot(self.actor, self.scope)
        self.assertEqual([n["value"] for n in active if n["kind"] == "fact"], ["杭州"])
        self.assertEqual(len([n for n in active if n["kind"] == "episode"]), 3)
        self.assertTrue(any(e["relation"] == 2 for e in links))
        with self.store.driver.session() as s:
            inactive = s.run(
                "MATCH (n:Memory {actor:$actor,active:false}) RETURN n.value AS value,n.valid_to AS until",
                actor=self.actor,
            ).data()
        self.assertEqual({n["value"] for n in inactive}, {"苏州", "南京"})
        self.assertTrue(all(n["until"] > 0 for n in inactive))

    def test_users_scopes_cross_links_and_delete_isolation(self):
        one, _ = self.add(self.actor, self.scope, "杭州", time.time())
        private, _ = self.add(self.actor, self.private, "成都", time.time())
        other, _ = self.add(self.other, self.scope, "武汉", time.time())
        # Even an explicitly supplied cross-tenant edge cannot be persisted.
        self.store.add(
            self.actor,
            self.scope,
            [],
            [{"src": one[0]["id"], "dst": other[0]["id"], "relation": 5}],
        )
        public, edges = self.store.snapshot(self.actor, self.scope)
        self.assertNotIn("成都", repr(public))
        self.assertNotIn("武汉", repr(public))
        self.assertFalse(any(e["dst"] == other[0]["id"] for e in edges))
        self.store.forget(self.actor)
        self.assertEqual(self.store.snapshot(self.actor, self.scope), ([], []))
        self.assertEqual(self.store.snapshot(self.actor, self.private), ([], []))
        self.assertTrue(self.store.snapshot(self.other, self.scope)[0])

    def test_dossiers_consolidate_repeated_evidence_and_remain_isolated(self):
        self.add(self.actor, self.scope, "摄影", time.time() - 20, key="interest")
        self.add(self.actor, self.scope, "摄影", time.time() - 10, key="interest")
        self.add(self.other, self.scope, "游泳", time.time(), key="interest")
        self.add(self.actor, self.private, "阅读", time.time(), key="interest")
        profile = self.store.profile(self.actor, self.scope)
        self.assertEqual(profile["current_facts"][0]["observations"], 2)
        self.assertEqual(profile["successful_interactions"], 2)
        self.assertNotIn("游泳", repr(profile))
        self.assertNotIn("阅读", repr(profile))
        self.store.forget(self.actor)
        self.assertEqual(self.store.profile(self.actor, self.scope), {})
        self.assertEqual(self.store.profile(self.actor, self.private), {})
        self.assertTrue(self.store.profile(self.other, self.scope))

    def test_names_and_display_metadata_update_without_changing_identity(self):
        now = time.time()
        self.add(self.actor, self.scope, "测试甲", now - 10, key="preferred_name")
        self.add(self.actor, self.scope, "测试乙", now, key="preferred_name")
        self.add(self.actor, self.scope, "旧称呼", now - 20, key="preferred_name")
        p = self.store.profile(self.actor, self.scope)
        self.assertEqual(p["names"][0]["value"], "测试乙")
        for label, when in (("测试甲", now), ("改名了", now + 1), ("过期昵称", now - 30)):
            event = {
                "id": hashlib.sha256((label + str(when)).encode()).hexdigest(),
                "created": when,
                "question": "你好",
                "answer": "晚上好",
                "display_name": label,
            }
            nodes, edges = self.store.prepare(
                self.actor, self.scope, event, {"summary": "打招呼", "topics": [], "claims": []}
            )
            for n in nodes:
                n["vector"] = [0.0, 1.0]
            self.store.add(self.actor, self.scope, nodes, edges)
        self.assertEqual(self.store.profile(self.actor, self.scope)["platform_name"], "改名了")


if __name__ == "__main__":
    unittest.main()
