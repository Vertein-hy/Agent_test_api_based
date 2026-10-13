import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from qqbot.app import Bot
from qqbot.chat_policy import (
    chat_revision_reason,
    is_presence_ping,
    is_light_banter,
    scene_context,
    visible_reply,
)


class ChatQualityTests(unittest.IsolatedAsyncioTestCase):
    def test_casual_service_redirect_review_preserves_real_requests_and_quotes(self):
        self.assertEqual(
            chat_revision_reason("还挺理直气壮的嘛", "总得留点面子。今天要我帮什么忙？"),
            "service_redirect",
        )
        self.assertEqual(
            chat_revision_reason("这个回答挺机灵", "嘿嘿。不过你还没说要我帮什么忙呢。"),
            "service_redirect",
        )
        for question, answer in (
            ("帮我做点事", "需要我做什么？"),
            ("写一句客服台词", "很高兴为你服务。"),
            ("分析这句原话", "今天要我帮什么忙？"),
            ("在吗", "在呀。"),
            ("这个回答挺机灵", "这句我爱听。"),
            ("今天累坏了", "忙到现在吗？"),
        ):
            self.assertIsNone(chat_revision_reason(question, answer))

    async def test_casual_service_tail_is_repaired_once_without_inventing_a_task(self):
        bot = Bot()
        bot.plugin_url = ""
        request = [
            {"role": "system", "content": bot.persona},
            {"role": "user", "content": "还挺理直气壮的嘛"},
        ]
        model = AsyncMock(
            side_effect=[
                {"content": "总得留点面子。今天要我帮什么忙？"},
                {"content": "总得留点面子嘛。"},
            ]
        )
        with patch.object(bot, "model_message", model):
            self.assertEqual(await bot.completion(request), "总得留点面子嘛。")
        self.assertEqual(model.await_count, 2)
        self.assertEqual(model.await_args.args[1], [])
        self.assertEqual(bot.last_trace[0]["arguments"]["reason"], "service_redirect")

    def test_provider_reasoning_markers_never_become_visible_chat(self):
        self.assertEqual(visible_reply("</think>吱，在呢"), "吱，在呢")
        self.assertEqual(visible_reply("<think>synthetic private text</think> 吱"), "吱")
        self.assertEqual(visible_reply("synthetic private text</think> 吱"), "吱")
        with self.assertRaises(ValueError):
            visible_reply("<think>unfinished synthetic text")
        self.assertEqual(visible_reply("示例标签是 <think>"), "示例标签是 <think>")
        self.assertEqual(visible_reply("结束标签是 `</think>`"), "结束标签是 `</think>`")

    def test_clock_and_intent_grounding_do_not_override_substantive_questions(self):
        context = scene_context("出来吱个声", datetime(2026, 10, 4, 17, 20, tzinfo=timezone.utc))
        self.assertIn("2026-10-05T01:20:00+08:00", context)
        self.assertIn("当前时段：凌晨", context)
        self.assertEqual(
            context, scene_context("任意问题", datetime(2026, 10, 4, 17, 20, tzinfo=timezone.utc))
        )
        self.assertTrue(is_presence_ping("在吗？"))
        self.assertFalse(is_presence_ping("出来吱个声，顺便查一下天气"))
        self.assertTrue(is_light_banter("喵喵喵，喵喵喵喵！"))
        self.assertTrue(is_light_banter("纯助手来的"))
        self.assertFalse(is_light_banter("示例助手我喜欢你"))
        self.assertNotIn("简单招呼", scene_context("怎么查官方文档？"))

    def test_overplay_checks_preserve_emotional_and_creative_role_interactions(self):
        self.assertEqual(
            chat_revision_reason("出来吱个声", "吱！我正忙着用尾巴扇风呢"), "ping_overplay"
        )
        self.assertEqual(chat_revision_reason("你好", "大白天的怎么突然找我", 1), "wrong_time")
        self.assertIsNone(chat_revision_reason("昨天白天干什么了", "大白天晒太阳", 1))
        self.assertIsNone(chat_revision_reason("你好", "大白天的怎么突然找我", 14))
        self.assertEqual(chat_revision_reason("喵喵喵", "喵？" * 80), "banter_overplay")
        self.assertEqual(
            chat_revision_reason("纯助手来的", "你才傻，找我有正事还是有吃的？"), "banter_overplay"
        )
        self.assertIsNone(chat_revision_reason("出来吱个声", "吱。叫这么大声，听见啦。", 1))
        affection = "（尾巴尖轻轻晃了一下，别开脸）你突然说这个干嘛……哼，我才没在等你。" * 4
        self.assertIsNone(chat_revision_reason("示例助手我喜欢你", affection, 1))
        self.assertIsNone(chat_revision_reason("写一个凌晨说大白天的剧本", "大白天" * 100, 1))

    async def test_presence_edit_keeps_clock_and_has_bounded_fallback(self):
        bot = Bot()
        bot.plugin_url = ""
        system = "虚构风格模板" + scene_context("出来吱个声")
        request = [{"role": "system", "content": system}, {"role": "user", "content": "出来吱个声"}]
        with patch.object(
            bot, "model_message", new=AsyncMock(return_value={"content": "吱，听见啦。"})
        ) as model:
            self.assertEqual(await bot.revise_chat(request, "我正忙着用尾巴扇风"), "吱，听见啦。")
            self.assertIn("当前北京时间", model.await_args.args[0][0]["content"])
            self.assertEqual(model.await_args.kwargs["thinking"], "disabled")
        bot.last_trace = []
        with patch.object(bot, "model_message", new=AsyncMock(side_effect=TimeoutError())):
            self.assertEqual(await bot.revise_chat(request, "我正忙着用尾巴扇风"), "在的。")

    def test_check_is_contextual_and_allows_relevant_food_and_creative_requests(self):
        self.assertEqual(
            chat_revision_reason("这是我们群的猫娘小甲", "猫娘吃鱼不？"), "unrelated_topic"
        )
        self.assertIsNone(chat_revision_reason("这是我们群的猫娘小甲，她吃什么？", "她吃鱼吗？"))
        self.assertIsNone(chat_revision_reason("示例助手，天天吃我白饭", "吃饱了才有力气聊天"))
        self.assertIsNone(chat_revision_reason("写一段角色扮演剧本", "（默默低头）好"))
        self.assertIsNone(chat_revision_reason("介绍我们的群友", "哦，认识了"))
        self.assertIsNone(chat_revision_reason("你又偷懒", "（假装没听见）啥？"))
        self.assertEqual(
            chat_revision_reason("一点不可爱", "您说不可爱，那我认了"), "customer_tone"
        )
        self.assertEqual(
            chat_revision_reason("正经点", "你是想让我帮你查资料，还是解决问题？尽管说"),
            "customer_tone",
        )
        self.assertIsNone(chat_revision_reason("这封正式邮件需要用您", "谢谢您的支持"))

    async def test_edit_uses_only_current_message_keeps_confirmed_style_and_is_archived(self):
        bot = Bot()
        bot.plugin_url = ""
        request = [
            {"role": "system", "content": "角色背景\n个人表达偏好（版本1）：\n不用表情"},
            {"role": "user", "content": "旧话题白饭"},
            {"role": "assistant", "content": "我吃鱼"},
            {"role": "user", "content": "这是我们群的猫娘小甲"},
        ]
        model = AsyncMock(
            side_effect=[{"content": "猫娘吃鱼不？我这有新鲜的"}, {"content": "哦，小甲，认识了。"}]
        )
        with patch.object(bot, "model_message", model):
            answer = await bot.completion(request)
        self.assertEqual(answer, "哦，小甲，认识了。")
        self.assertEqual(model.await_count, 2)
        edit = model.await_args_list[1].args
        self.assertEqual(edit[1], [])
        self.assertIn("不用表情", edit[0][0]["content"])
        self.assertIn("角色背景", edit[0][0]["content"])
        self.assertEqual(len(edit[0]), 2)
        self.assertEqual(
            json.loads(edit[0][1]["content"])["current_message"], request[-1]["content"]
        )
        self.assertEqual(bot.last_trace[0]["kind"], "internal")

    async def test_normal_or_grounded_answer_not_edited_and_failure_is_bounded(self):
        bot = Bot()
        request = [{"role": "user", "content": "这是我们群的猫娘小甲"}]
        with patch.object(bot, "model_message", new=AsyncMock(side_effect=TimeoutError())) as model:
            self.assertEqual(await bot.revise_chat(request, "哦，认识了"), "哦，认识了")
            model.assert_not_called()
            bot.last_trace = [{"name": "web_search"}]
            self.assertEqual(await bot.revise_chat(request, "猫娘吃鱼不？"), "猫娘吃鱼不？")
            model.assert_not_called()
            bot.last_trace = []
            self.assertEqual(await bot.revise_chat(request, "猫娘吃鱼不？"), "好的，知道了。")
            self.assertEqual(model.await_count, 1)

    async def test_service_voice_edit_preserves_an_eligible_short_action(self):
        bot = Bot()
        bot.plugin_url = ""
        request = [
            {"role": "system", "content": "虚构风格模板"},
            {"role": "user", "content": "一点不可爱"},
        ]
        model = AsyncMock(
            side_effect=[
                {"content": "您说不可爱我就认了，有事尽管说"},
                {"content": "（别开脸）你说不可爱就不可爱啊？哼。"},
            ]
        )
        with patch.object(bot, "model_message", model):
            self.assertEqual(await bot.completion(request), "（别开脸）你说不可爱就不可爱啊？哼。")
        self.assertEqual(bot.last_trace[0]["arguments"]["reason"], "customer_tone")


if __name__ == "__main__":
    unittest.main()
