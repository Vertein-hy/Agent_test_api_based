import tempfile
import time
import unittest
from pathlib import Path
from qqmemory.attention_model import AttentionLearner, features
from qqmemory.lifecycle import strength, visible
from qqbot.memory.policy import checked_extraction, portable, attention
import test_graph as graph_cases


class LearningLifecycleTests(unittest.TestCase):
    def test_decay_repetition_and_explicit_strengthening(self):
        n = {"kind": "episode", "created": 1, "access": 0}
        later = 1 + 86400 * 90
        self.assertLess(strength(n, later), 0.15)
        self.assertGreater(strength(dict(n, observations=8), later), strength(n, later))
        self.assertGreater(strength(dict(n, useful=5, reinforced_at=later), later), 0.9)
        self.assertEqual(visible([n], "chat", later), [])
        self.assertEqual(len(visible([n], "recall", later)), 1)

    def test_names_pending_tasks_and_pins_are_retained_but_retracted_never_return(self):
        for n in (
            {"kind": "fact", "key": "preferred_name"},
            {"kind": "task"},
            {"kind": "episode", "pinned": True},
        ):
            self.assertEqual(strength(dict(n, created=1), 86400 * 900), 1)
        self.assertEqual(visible([{"kind": "fact", "active": False, "created": 1}], "recall"), [])

    def test_explicit_feedback_is_account_bound_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            learner = AttentionLearner(Path(d) / "a.sqlite3")
            x = features({"kind": "fact"}, 0.8, 0.6, "recall")
            first = learner.expose("a", "s", [{"id": "n", "features": x}])
            self.assertEqual(learner.feedback("b", "s", first, True), [])
            self.assertEqual(learner.feedback("a", "other", first, True), [])
            self.assertEqual(learner.feedback("a", "s", first, True), ["n"])
            self.assertEqual(learner.feedback("a", "s", first, False), [])
            for _ in range(4):
                learner.feedback(
                    "a", "s", learner.expose("a", "s", [{"id": "n", "features": x}]), True
                )
            self.assertGreater(learner.adjust("a", x), 0)
            self.assertLessEqual(learner.adjust("a", x), 0.1)
            learner.db.close()
            learner = AttentionLearner(Path(d) / "a.sqlite3")
            self.assertEqual(learner.state("a")[1], 5)
            learner.forget("a")
            self.assertEqual(learner.state("a")[1], 0)
            self.assertEqual(learner.feedback("a", "s", first, True), [])
            learner.db.close()

    def test_feedback_negative_changes_attention_without_facts(self):
        with tempfile.TemporaryDirectory() as d:
            learner = AttentionLearner(Path(d) / "a.db")
            x = [1] * 7
            for _ in range(5):
                learner.feedback(
                    "a", "s", learner.expose("a", "s", [{"id": "n", "features": x}]), False
                )
            self.assertLess(learner.adjust("a", x), 0)
            learner.db.close()

    def test_update_requires_exact_nonsynthetic_evidence(self):
        q = "我已经不喜欢摄影了"
        valid = {"operation": "retract", "target": "摄影", "key": "interest", "evidence": q}
        self.assertEqual(len(checked_extraction({"updates": [valid]}, q)["updates"]), 1)
        self.assertEqual(
            checked_extraction({"updates": [dict(valid, evidence="助手说摄影")]}, q)["updates"], []
        )
        self.assertEqual(
            checked_extraction({"updates": [dict(valid, operation="drop_database")]}, q)["updates"],
            [],
        )

    def test_only_names_and_specific_expression_preferences_are_portable(self):
        self.assertTrue(portable({"key": "preferred_name"}))
        self.assertTrue(
            portable({"key": "preference", "evidence": "打招呼的时候可以说嗨🎶想我了吗"})
        )
        self.assertTrue(portable({"key": "preference", "value": "不要这个口癖"}))
        self.assertFalse(portable({"key": "city", "value": "私下地点"}))
        self.assertFalse(portable({"key": "relationship", "value": "朋友"}))
        self.assertEqual(attention("来打个招呼"), "social")


class CrossContextGraphTests(unittest.TestCase):
    setUpClass = classmethod(graph_cases.GraphTests.setUpClass.__func__)
    tearDownClass = classmethod(graph_cases.GraphTests.tearDownClass.__func__)
    setUp = graph_cases.GraphTests.setUp
    add = graph_cases.GraphTests.add

    def test_account_preferences_cross_to_group_and_private_details_stay_private(self):
        now = time.time()
        self.add(self.actor, self.private, "测试乙", now, key="preferred_name")
        self.add(self.actor, self.private, "成都", now, key="city")
        self.add(self.other, self.scope, "测试戊", now, key="preferred_name")
        p = self.store.audience_profile(self.actor, self.scope, False)
        self.assertEqual(p["names"][0]["value"], "测试乙")
        self.assertNotIn("成都", repr(p))
        self.assertNotIn("测试戊", repr(p))
        self.add(self.actor, self.scope, "阅读", now, key="interest")
        p = self.store.audience_profile(self.actor, self.private, True)
        self.assertIn("阅读", repr(p))
        self.assertIn("成都", repr(p))

    def test_newer_global_name_wins_even_after_nickname_and_scope_changes(self):
        now = time.time()
        self.add(self.actor, self.private, "旧名字", now - 10, key="preferred_name")
        self.add(self.actor, self.scope, "新名字", now, key="preferred_name")
        self.assertEqual(
            self.store.audience_profile(self.actor, self.private, True)["names"][0]["value"],
            "新名字",
        )
        self.assertEqual(
            self.store.audience_profile(self.actor, self.private, False)["names"][0]["value"],
            "新名字",
        )

    def test_greeting_type_survives_redaction_of_private_source_quote(self):
        event = {
            "id": "a" * 64,
            "created": time.time(),
            "question": "打招呼时说嗨，我私下住在成都",
            "answer": "知道了",
        }
        extracted = {
            "summary": "约定问候",
            "topics": [],
            "claims": [
                {
                    "kind": "preference",
                    "key": "preference",
                    "value": "嗨",
                    "evidence": "打招呼时说嗨，我私下住在成都",
                }
            ],
        }
        nodes, edges = self.store.prepare(self.actor, self.private, event, extracted)
        for n in nodes:
            n["vector"] = [0.0, 1.0]
        self.store.add(self.actor, self.private, nodes, edges)
        p = self.store.audience_profile(self.actor, self.scope, False, "social")
        self.assertEqual(p["behavior_preferences"][0]["slot"], "greeting")
        self.assertNotIn("成都", repr(p))

    def test_legacy_emoji_preference_projects_without_migration_or_private_quote(self):
        event = {
            "id": "e" * 64,
            "created": time.time(),
            "question": "少发emoji，我私下住在成都",
            "answer": "知道了",
        }
        extracted = {
            "summary": "约定少用表情",
            "topics": [],
            "claims": [
                {
                    "kind": "preference",
                    "key": "preference",
                    "value": "少发emoji",
                    "evidence": event["question"],
                }
            ],
        }
        nodes, edges = self.store.prepare(self.actor, self.private, event, extracted)
        for n in nodes:
            n["vector"] = [0.0, 1.0]
        self.store.add(self.actor, self.private, nodes, edges)
        profile = self.store.audience_profile(self.actor, self.scope, False, "chat")
        self.assertEqual(profile["behavior_preferences"][0]["slot"], "emoji_usage")
        self.assertNotIn("成都", repr(profile))
        self.assertNotIn("成都", repr(self.store.view(self.actor, self.scope, False, "chat")[0]))

    def test_retraction_dedup_late_jobs_and_reassertion(self):
        now = time.time()
        nodes, _ = self.add(self.actor, self.scope, "摄影", now - 100, key="interest")
        target = next(n for n in nodes if n.get("key") == "interest")["id"]
        u = {"operation": "retract", "target": target[:10], "evidence": "我已不喜欢摄影了"}
        r = self.store.update(self.actor, self.scope, u, "z" * 64, now)
        self.assertTrue(r["ok"])
        self.assertEqual(r, self.store.update(self.actor, self.scope, u, "z" * 64, now))
        self.add(self.actor, self.scope, "摄影", now - 50, key="interest")
        self.assertFalse(
            any(n.get("key") == "interest" for n in self.store.view(self.actor, self.scope)[0])
        )
        self.add(self.actor, self.scope, "摄影", now + 10, key="interest")
        self.assertTrue(
            any(n.get("key") == "interest" for n in self.store.view(self.actor, self.scope)[0])
        )

    def test_plan_completion_and_correction_keep_audit_and_cannot_edit_other_people(self):
        now = time.time()
        self.add(self.actor, self.scope, "学游泳", now - 100, key="goal")
        # Test helper originally emits kind=fact; correct it here to match real extraction.
        with self.store.driver.session() as s:
            s.run(
                "MATCH (n:Memory {actor:$actor,key:'goal'}) SET n.kind='task'", actor=self.actor
            ).consume()
        self.assertTrue(
            self.store.update(
                self.actor,
                self.scope,
                {"operation": "complete", "target": "学游泳", "evidence": "我的学游泳计划完成了"},
                "1" * 64,
                now,
            )["ok"]
        )
        self.assertEqual(self.store.audience_profile(self.actor, self.scope)["ongoing_plans"], [])
        self.add(self.other, self.scope, "保密姓名", now - 100, key="name")
        self.assertFalse(
            self.store.update(
                self.actor,
                self.scope,
                {"operation": "retract", "target": "保密姓名", "evidence": "这个姓名记错了"},
                "2" * 64,
                now,
            )["ok"]
        )
        self.add(self.actor, self.scope, "杭州", now - 100, key="city")
        r = self.store.update(
            self.actor,
            self.scope,
            {
                "operation": "correct",
                "target": "杭州",
                "value": "成都",
                "evidence": "记错了我住成都",
            },
            "3" * 64,
            now,
        )
        self.assertTrue(r["ok"])
        p = self.store.audience_profile(self.actor, self.scope)
        self.assertIn("成都", repr(p))
        self.assertNotIn("杭州", repr(p))
        self.store.forget(self.actor)
        with self.store.driver.session() as s:
            self.assertEqual(
                s.run(
                    "MATCH (r:Revision {actor:$actor}) RETURN count(r) AS n", actor=self.actor
                ).single()["n"],
                0,
            )

    def test_ambiguous_update_does_not_retract_multiple_unrelated_plans(self):
        now = time.time()
        self.add(self.actor, self.scope, "学习游泳", now - 100, key="todo")
        self.add(self.actor, self.scope, "学习摄影", now - 100, key="todo")
        r = self.store.update(
            self.actor,
            self.scope,
            {"operation": "retract", "target": "学习", "evidence": "学习的计划我记错了"},
            "4" * 64,
            now,
        )
        self.assertEqual(r["reason"], "ambiguous")
        self.assertEqual(
            len([n for n in self.store.view(self.actor, self.scope)[0] if n.get("key") == "todo"]),
            2,
        )

    def test_pin_and_dormancy_lifecycle(self):
        nodes, _ = self.add(
            self.actor, self.scope, "摄影", time.time() - 86400 * 900, key="interest"
        )
        fact = next(n for n in nodes if n.get("key") == "interest")
        self.assertFalse(
            any(
                n.get("key") == "interest"
                for n in self.store.view(self.actor, self.scope, False, "chat")[0]
            )
        )
        self.store.update(
            self.actor,
            self.scope,
            {"operation": "pin", "target": fact["id"][:10], "evidence": "永久保留这个兴趣"},
            "5" * 64,
            time.time(),
        )
        self.assertTrue(
            any(
                n.get("key") == "interest"
                for n in self.store.view(self.actor, self.scope, False, "chat")[0]
            )
        )
        self.store.maintenance()
