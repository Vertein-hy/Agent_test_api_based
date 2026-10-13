"""Completed interactions, changed topics and necessary repetition across scoped turns."""

import unittest
from unittest.mock import AsyncMock

import test_adapter as fixtures
from qqbot.continuity import continuity_revision_reason, request_terms, limit_role_actions


def dialogue(item, question):
    return [
        {"role": "system", "content": "虚构角色"},
        {"role": "assistant", "content": f"先给我一个{item}，我才高兴。"},
        {"role": "user", "content": question},
    ]


class ContinuityTests(unittest.TestCase):
    def test_one_short_action_survives_but_long_actions_are_removed(self):
        raw = "（慢吞吞地探出头，尾巴晃了晃）你好。（别过脸）在呢。"
        self.assertEqual(limit_role_actions(raw, "你好"), "你好。（别过脸）在呢。")
        self.assertEqual(
            limit_role_actions("（别开脸）才没有。（尾巴晃了晃）", "害羞啦"), "（别开脸）才没有。"
        )

    def test_code_math_notes_and_requested_fiction_survive(self):
        raw = "（慢吞吞地探出头，尾巴晃了晃）你好。（别过脸）在呢。"
        for text in (
            "函数 f(x)（输入参数）",
            "解释（补充说明）和公式 (a+b)*c",
            "`（尾巴甩了甩）`",
            "“（别过脸）”是原句",
        ):
            self.assertEqual(limit_role_actions(text, "解释一下"), text)
        self.assertEqual(limit_role_actions(raw, "写一段角色扮演剧本"), raw)
        self.assertNotIn("尾巴", limit_role_actions(raw, "写故事但不要动作旁白"))

    def test_two_reply_cooldown_and_direct_serious_requests(self):
        raw = "（别开脸）好嘛。"
        for previous in ([raw], [raw, "好呀"]):
            self.assertEqual(limit_role_actions(raw, "逗你的", previous), "好嘛。")
        self.assertEqual(limit_role_actions(raw, "逗你的", [raw, "好呀", "嗯"]), raw)
        for question in ("别加动作旁白", "直接回答", "用两句写通知", "解释二分查找", "我很难过"):
            self.assertEqual(limit_role_actions(raw, question), "好嘛。")

    def test_requested_gesture_is_not_misread_as_negative_instruction(self):
        question = "小助手被夸害羞啦？带上（别开脸）这个简短动作，再接一句话。"
        self.assertEqual(
            limit_role_actions("（别开脸）才没有呢。", question), "（别开脸）才没有呢。"
        )
        self.assertEqual(limit_role_actions("（别开脸）才没有呢。", "别再加动作旁白"), "才没有呢。")

    def test_verb_before_body_obeys_same_cooldown_and_standalone_rule(self):
        raw = "（甩了甩尾巴）才没有呢。"
        self.assertEqual(limit_role_actions(raw, "逗你的"), raw)
        self.assertEqual(limit_role_actions(raw, "逗你的", [raw]), "才没有呢。")
        self.assertEqual(limit_role_actions("（甩了甩尾巴）……", "逗你的"), "")

    def test_completed_requests_are_generic_across_food_and_objects(self):
        for item in ("鸡腿", "毛绒玩偶", "蓝色钥匙扣", "纸风车"):
            self.assertIn(item, request_terms(f"再给我一个{item}吧！"))
            messages = dialogue(item, "已经给你了，聊点别的")
            self.assertEqual(
                continuity_revision_reason(messages, f"再给我两个{item}吧！"),
                "resolved_request_repeated",
            )
            self.assertIsNone(continuity_revision_reason(messages, "好，换个话题。"))

    def test_new_topic_gets_a_review_and_explicit_reopening_is_respected(self):
        messages = dialogue("钥匙扣", "解释一下什么是递归")
        self.assertEqual(
            continuity_revision_reason(messages, "先给我一个钥匙扣吧。递归就是函数调用自己。"),
            "repeated_topic_review",
        )
        messages[-1]["content"] = "已经给你了，我再送你一个钥匙扣好不好？"
        self.assertNotEqual(
            continuity_revision_reason(messages, "再给我一个钥匙扣吧。"),
            "resolved_request_repeated",
        )
        messages[-1]["content"] = "都给你两个钥匙扣了，还要什么钥匙扣？"
        self.assertEqual(
            continuity_revision_reason(messages, "再给我一个钥匙扣吧。"),
            "resolved_request_repeated",
        )

    def test_old_closure_survives_assistant_echo_but_does_not_cross_sources(self):
        messages = dialogue("玩偶", "给你了") + [
            {"role": "assistant", "content": "再给我一个玩偶吧。"},
            {"role": "user", "content": "现在帮我解释二分查找"},
        ]
        self.assertEqual(
            continuity_revision_reason(messages, "先给我一个玩偶吧。"), "resolved_request_repeated"
        )
        messages = [
            {"role": "user", "content": "当前消息引用的群聊原文：给你了"},
            {"role": "tool", "content": "给你了"},
            {"role": "user", "content": "你好"},
        ]
        self.assertIsNone(continuity_revision_reason(messages, "给我一个玩偶吧。"))

    def test_necessary_facts_and_creative_outputs_are_preserved(self):
        messages = dialogue("运行日志", "给你了，但这里只截了一半，请说明还需要什么")
        self.assertNotEqual(
            continuity_revision_reason(messages, "请给我运行日志，完整的那份。"),
            "resolved_request_repeated",
        )
        messages[-1]["content"] = "继续解释刚才的术语"
        self.assertIsNone(continuity_revision_reason(messages, "必要的技术术语应当保留。"))
        messages[-1]["content"] = "写一段角色扮演剧本"
        self.assertIsNone(continuity_revision_reason(messages, "先给我一个运行日志吧。"))
        self.assertFalse(request_terms("不用给我一个玩偶了，谢谢。"))

    def test_closed_item_cannot_return_as_reward_or_acknowledgement(self):
        messages = dialogue("纸风车", "给你了，别再提。现在解释月亮圆缺")
        self.assertEqual(
            continuity_revision_reason(messages, "看在纸风车的份上，月亮只是反射太阳光。"),
            "resolved_topic_repeated",
        )
        messages[-1]["content"] = "给你了"
        self.assertIsNone(continuity_revision_reason(messages, "纸风车收下了。"))
        messages += [
            {"role": "assistant", "content": "谢谢啦"},
            {"role": "user", "content": "解释月亮圆缺"},
        ]
        self.assertEqual(
            continuity_revision_reason(messages, "看在纸风车的份上，月亮只是反射太阳光。"),
            "repeated_topic_review",
        )


class ContinuityDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AdapterTests.asyncSetUp
    asyncTearDown = fixtures.AdapterTests.asyncTearDown
    ask = fixtures.AdapterTests.ask
    queued = fixtures.AdapterTests.queued

    async def test_eligible_action_and_cooldown_match_send_history_archive_and_intake(self):
        for group in (None, 456):
            for index in range(4):
                prose = ("你好呀。", "才没有呢。", "那当然啦。", "这句我爱听。")[index]
                self.bot.model_message = AsyncMock(return_value={"content": "（别开脸）" + prose})
                message = await self.ask("逗你的", group=group)
                expected = ("（别开脸）" if index in (0, 3) else "") + prose
                self.assertEqual(self.bot.history[message.key][1][-1]["content"], expected)
                self.assertEqual(self.queued()[-1][1]["event"]["answer"], expected)
                row = self.bot.archive.db.execute(
                    "SELECT answer,delivered FROM turns ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(tuple(row), (expected, 1))
                self.assertIn(expected, repr(self.bot.action.await_args))

    async def test_failed_send_does_not_consume_action_and_accounts_are_separate(self):
        self.bot.model_message = AsyncMock(return_value={"content": "（别开脸）才没有呢。"})
        self.bot.action = AsyncMock(side_effect=RuntimeError("fixture send failed"))
        message = await self.ask("逗你的", group=None)
        self.assertNotIn(message.key, self.bot.history)
        self.bot.action = AsyncMock(return_value={"message_id": 1})
        for user in (123, 124):
            message = await self.ask("逗你的", user=user, group=None)
            self.assertIn("（别开脸）", self.bot.history[message.key][1][-1]["content"])

    async def test_editor_cannot_bypass_action_cooldown(self):
        self.bot.model_message = AsyncMock(return_value={"content": "（别开脸）才没有呢。"})
        await self.ask("逗你的", group=None)
        self.bot.model_message = AsyncMock(
            side_effect=[
                {"content": "您说不可爱我就认了，有事尽管说"},
                {"content": "（别开脸）你说不可爱就不可爱啊？"},
            ]
        )
        message = await self.ask("一点不可爱", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertEqual(self.bot.history[message.key][1][-1]["content"], "你说不可爱就不可爱啊？")

    async def test_action_only_draft_gets_one_bounded_dialogue_repair(self):
        self.bot.model_message = AsyncMock(
            side_effect=[{"content": "（别过脸）"}, {"content": "在呢。"}]
        )
        await self.ask("在吗", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertIn("在呢。", repr(self.bot.action.await_args))

    async def test_completed_item_repair_before_delivery_history_and_intake(self):
        for group in (None, 456):
            self.bot.model_message = AsyncMock(return_value={"content": "聊聊今天吧。"})
            await self.ask("聊个轻松的话题", group=group)
            key = (str(group) if group else "private", "123")
            self.bot.history[key][1][-1]["content"] = "先给我一个纸风车吧。"
            self.bot.model_message = AsyncMock(
                side_effect=[
                    {"content": "再给我一个纸风车吧。"},
                    {"content": "收到啦，那说说你今天怎么样。"},
                ]
            )
            message = await self.ask("已经给你了，聊聊今天", group=group)
            self.assertEqual(self.bot.model_message.await_count, 2)
            self.assertEqual(self.bot.model_message.await_args.args[1], [])
            self.assertNotIn("纸风车", self.bot.history[message.key][1][-1]["content"])
            self.assertNotIn("纸风车", self.queued()[-1][1]["event"]["answer"])
            self.assertEqual(
                self.bot.last_trace[0]["arguments"]["reason"], "resolved_request_repeated"
            )

    async def test_semantic_review_can_keep_necessary_repetition(self):
        self.bot.model_message = AsyncMock(
            return_value={"content": "二分查找需要先保证输入数组有序。"}
        )
        await self.ask("讲一下二分查找", group=None)
        self.bot.model_message = AsyncMock(
            side_effect=[
                {"content": "二分查找需要先保证输入数组有序。否则无法正确缩小范围。"},
                {"content": "二分查找需要先保证输入数组有序。否则无法正确缩小范围。"},
            ]
        )
        await self.ask("说明它成立的前提条件", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertIn("输入数组有序", repr(self.bot.action.await_args))

    async def test_repeated_closed_request_fails_once_without_reopening_or_guessing(self):
        self.bot.model_message = AsyncMock(return_value={"content": "先给我一个钥匙扣吧。"})
        await self.ask("在吗", group=None)
        self.bot.model_message = AsyncMock(return_value={"content": "再给我一个钥匙扣吧。"})
        await self.ask("给你了，不要再讨了", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertNotIn("钥匙扣", repr(self.bot.action.await_args))

    async def test_closed_topic_reward_framing_is_repaired_before_commit(self):
        self.bot.model_message = AsyncMock(return_value={"content": "先给我一个纸风车吧。"})
        await self.ask("来聊聊", group=None)
        self.bot.model_message = AsyncMock(
            side_effect=[
                {"content": "看在纸风车的份上，月亮反射太阳光。"},
                {"content": "月亮反射太阳光，看到的光照部分不同，就有了圆缺。"},
            ]
        )
        message = await self.ask("给你了，别再提。解释月亮圆缺", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertNotIn("纸风车", self.bot.history[message.key][1][-1]["content"])
        self.assertEqual(self.bot.last_trace[0]["arguments"]["reason"], "resolved_topic_repeated")
