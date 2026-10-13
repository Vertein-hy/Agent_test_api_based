import json
import unittest
from qqbot.memory.policy import attention, display_name, checked_event
from qqmemory.dossier import consolidate, attended_profile


class DossierTests(unittest.TestCase):
    def claim(self, value, created=1, key="preference", sources=("a",)):
        return {
            "node": {
                "id": value,
                "key": key,
                "value": value,
                "text": value,
                "evidence": "我希望" + value,
                "source": sources[0],
                "kind": "preference" if key == "preference" else "fact",
            },
            "proof": [{"id": s} for s in sources],
            "latest": created,
        }

    def test_newest_behavior_wins_and_repeated_sources_count_once(self):
        rows = [
            self.claim("简短", 1),
            self.claim("详细展开", 3, sources=("b", "c", "c")),
            self.claim("不要客套", 2),
            self.claim("游泳", 4, key="interest"),
        ]
        p = consolidate({}, rows, [], 4)
        prefs = {n["slot"]: n for n in p["behavior_preferences"]}
        self.assertEqual(prefs["response_length"]["text"], "详细展开")
        self.assertEqual(prefs["response_length"]["observations"], 2)
        self.assertEqual(len(p["current_facts"]), 1)

    def test_profile_without_evidence_does_not_invent_facts_or_relationships(self):
        row = self.claim("假的")
        row["proof"] = []
        p = consolidate({"display_name": "测试甲"}, [row], [], 1)
        self.assertEqual(p["current_facts"], [])
        self.assertEqual(p["platform_name"], "测试甲")
        self.assertNotIn("亲密", json.dumps(p, ensure_ascii=False))

    def test_attention_limits_context_and_keeps_facts_out_of_greetings(self):
        rows = [self.claim("测试甲", 2, key="preferred_name"), self.claim("简短", 3)]
        rows += [self.claim("兴趣" + str(i) + "x" * 160, i + 5, key="interest") for i in range(10)]
        p = consolidate({}, rows, [], 10)
        for mode, bound in (("social", 1000), ("chat", 1000), ("recall", 1800)):
            selected = attended_profile(p, mode)
            self.assertLessEqual(len(json.dumps(selected, ensure_ascii=False)), bound)
            self.assertEqual(selected["names"][0]["value"], "测试甲")
            if mode != "recall":
                self.assertNotIn("current_facts", selected)

    def test_social_short_circuit_does_not_swallow_real_questions(self):
        for q in ("晚上好！", "嗯", "谢谢你", "hi"):
            self.assertEqual(attention(q), "social")
        self.assertEqual(attention("你还记得我是谁吗"), "recall")
        self.assertEqual(attention("晚上好，你还记得昨天的事吗"), "recall")
        self.assertEqual(attention("详细解释图神经网络"), "focused")
        self.assertEqual(attention("今天堵车堵得烦死了"), "chat")

    def test_request_projection_filters_greeting_but_retains_emoji_and_length(self):
        p = consolidate(
            {},
            [self.claim("打招呼时说问候暗号"), self.claim("少发emoji"), self.claim("简短")],
            [],
            0,
        )
        ordinary = attended_profile(p, "chat", "回复简短一些")
        self.assertEqual(
            {i["slot"] for i in ordinary["behavior_preferences"]},
            {"emoji_usage", "response_length"},
        )
        self.assertIn(
            "greeting",
            {i["slot"] for i in attended_profile(p, "social", "你好")["behavior_preferences"]},
        )
        self.assertNotIn(
            "greeting",
            {i["slot"] for i in attended_profile(p, "social", "谢谢你")["behavior_preferences"]},
        )

    def test_platform_labels_are_sanitized_and_short_greetings_can_form_episodes(self):
        for value in ("密码123456", "UID700000001", "https://example.com", "123456789"):
            self.assertEqual(display_name(value), "")
        event = checked_event(
            {
                "id": "a" * 64,
                "created": 1,
                "question": "你好",
                "answer": "晚上好。",
                "display_name": "测试甲",
            }
        )
        self.assertEqual(event["display_name"], "测试甲")


if __name__ == "__main__":
    unittest.main()
