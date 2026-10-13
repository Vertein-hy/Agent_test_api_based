"""Observable greeting attribution, persistence and successful-delivery boundaries."""

import time
import unittest
from unittest.mock import AsyncMock

import test_adapter as fixtures
from qqbot.addressing import greeting_addressee, recipient_names, addressing_revision_reason
from qqbot.memory.policy import checked_extraction


class AddressingTests(unittest.TestCase):
    def test_greeting_names_the_recipient_not_a_self_introduction_or_sentence(self):
        for text in ("你好示例甲", "下午好，示例甲！", "Hi 示例甲"):
            self.assertEqual(greeting_addressee(text), "示例甲")
        for text in ("你好", "你好我是示例甲", "你好，今天帮我写代码", "你好大家", "你好世界"):
            self.assertEqual(greeting_addressee(text), "")

    def test_assistant_echo_does_not_establish_sender_name_in_next_turn(self):
        messages = [
            {"role": "user", "content": "你好示例甲"},
            {"role": "assistant", "content": "示例甲，你好！"},
            {"role": "user", "content": "能借我点钱吗"},
        ]
        self.assertEqual(recipient_names(messages), ["示例甲"])
        for answer in (
            "（探头）哟，示例甲今天来借钱？",
            "喂，示例甲你这开口就要钱",
            "你叫示例甲吧",
            "示例甲下午好呀，今天有什么安排？",
            "示例甲早安！",
        ):
            self.assertEqual(
                addressing_revision_reason(messages, answer), "greeting_addressee_confusion"
            )
        for answer in ("示例甲在呢，你好。", "你喊我示例甲？", "“示例甲，你好”是个问候示例。"):
            self.assertIsNone(addressing_revision_reason(messages, answer))
        messages[-1]["content"] = "写一段示例甲借钱的剧本"
        self.assertIsNone(addressing_revision_reason(messages, "示例甲，你好！"))

    def test_explicit_self_name_and_scoped_profile_remain_valid_sources(self):
        greeting = {"role": "user", "content": "你好示例甲"}
        for source in (
            {"role": "user", "content": "我叫示例甲"},
            {"role": "user", "content": "以后叫我示例甲"},
            {
                "role": "user",
                "content": 'MEMORY_DATA_JSON:\n{"profile":{"names":[{"key":"preferred_name","value":"示例甲"}]}}',
            },
        ):
            self.assertIsNone(addressing_revision_reason([source, greeting], "示例甲，你好！"))
        for source in (
            {"role": "assistant", "content": "你好示例甲"},
            {"role": "user", "content": "当前消息引用的群聊原文：你好示例甲"},
            {"role": "tool", "content": "你好示例甲"},
        ):
            self.assertEqual(recipient_names([source]), [])

    def test_greeting_cannot_become_sender_name_but_self_identification_can(self):
        for key in ("name", "preferred_name"):
            for question, expected in (("你好示例甲", []), ("我叫示例甲", ["示例甲"])):
                result = checked_extraction(
                    {
                        "summary": question,
                        "topics": [],
                        "claims": [
                            {
                                "key": key,
                                "value": "示例甲",
                                "evidence": question,
                                "temporary": False,
                            }
                        ],
                    },
                    question,
                )
                self.assertEqual([c["value"] for c in result["claims"]], expected)


class AddressingDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AdapterTests.asyncSetUp
    asyncTearDown = fixtures.AdapterTests.asyncTearDown
    ask = fixtures.AdapterTests.ask
    queued = fixtures.AdapterTests.queued

    async def test_private_and_group_repair_precedes_history_archive_and_intake(self):
        for group, draft in (
            (None, "（探头）示例甲今天怎么想起我了？"),
            (456, "示例甲下午好呀，今天有什么安排？"),
        ):
            self.bot.model_message = AsyncMock(
                side_effect=[
                    {"content": draft},
                    {"content": "哼，下午好。"},
                ]
            )
            message = await self.ask("你好示例甲", group=group)
            self.assertEqual(self.bot.model_message.await_count, 2)
            edit = self.bot.model_message.await_args.args
            self.assertEqual(edit[1], [])
            self.assertEqual(self.bot.history[message.key][1][-1]["content"], "哼，下午好。")
            self.assertEqual(self.queued()[-1][1]["event"]["answer"], "哼，下午好。")
            self.assertEqual(
                self.bot.archive.db.execute(
                    "SELECT answer FROM turns ORDER BY id DESC LIMIT 1"
                ).fetchone()[0],
                "哼，下午好。",
            )
            self.assertIn(
                "不能反过来当作用户姓名",
                self.bot.model_message.await_args_list[0].args[0][0]["content"],
            )

    async def test_contaminated_history_repair_without_memory_and_no_cross_account_signal(self):
        self.bot.history[("private", "123")] = (
            time.time(),
            [
                {"role": "user", "content": "你好示例甲"},
                {"role": "assistant", "content": "示例甲你来啦。"},
            ],
        )
        self.bot.memory_enabled = False
        self.bot.archive.configure("123", capture=False)
        self.bot.model_message = AsyncMock(
            side_effect=[{"content": "示例甲你开口就要钱？"}, {"content": "哼，我可不能转账。"}]
        )
        await self.ask("借我点钱可以吗", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertEqual(
            self.bot.last_trace[0]["arguments"]["reason"], "greeting_addressee_confusion"
        )
        self.bot.model_message = AsyncMock(return_value={"content": "示例甲，你好。"})
        await self.ask("你好", user=124, group=None)
        self.assertEqual(self.bot.model_message.await_count, 1)
        self.assertNotIn(
            "不能反过来当作用户姓名", self.bot.model_message.await_args.args[0][0]["content"]
        )

    async def test_repeated_error_or_editor_failure_has_only_one_repair(self):
        for correction in ({"content": "示例甲，你好！"}, TimeoutError()):
            self.bot.history.clear()
            self.bot.model_message = AsyncMock(
                side_effect=[{"content": "示例甲，你好。"}, correction]
            )
            await self.ask("你好示例甲", user=125, group=None)
            self.assertEqual(self.bot.model_message.await_count, 2)
            sent = repr(self.bot.action.await_args)
            self.assertNotIn("示例甲", sent)
            self.assertNotIn("MEMORY_DATA_JSON", sent)

    async def test_restored_successful_archive_does_not_authorize_old_misnaming(self):
        turn = self.bot.archive.append(
            "123", None, "虚构角色", "你好示例甲", "示例甲你来啦。", "fixture-model"
        )
        self.bot.archive.delivered(turn)
        self.bot.model_message = AsyncMock(
            side_effect=[{"content": "喂，示例甲你想做什么？"}, {"content": "哼，我不能转账。"}]
        )
        await self.ask("借我点钱", group=None)
        self.assertEqual(self.bot.model_message.await_count, 2)
        self.assertIn("你好示例甲", repr(self.bot.model_message.await_args_list[0].args[0]))
        self.assertEqual(self.bot.history[("private", "123")][1][-1]["content"], "哼，我不能转账。")
