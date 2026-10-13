import asyncio
import os
import time
import unittest
from unittest.mock import patch, AsyncMock

from aiohttp import web

from qqbot.app import Bot
from qqbot.domain import parse_event


def event(text="测试", *, user=123, group=456, mention=True, message_id=1):
    segments = [{"type": "text", "data": {"text": text}}]
    if mention:
        segments.insert(0, {"type": "at", "data": {"qq": "999"}})
    result = {
        "post_type": "message",
        "self_id": 999,
        "user_id": user,
        "message_type": "group" if group else "private",
        "message": segments,
        "message_id": message_id,
        "time": int(time.time()),
    }
    if group:
        result["group_id"] = group
    return result


class FilteringTests(unittest.TestCase):
    def test_group_trigger_whitelist_and_self(self):
        self.assertIsNone(parse_event(event(mention=False), set(), set()))
        self.assertIsNone(parse_event(event(user=999), set(), set()))
        self.assertIsNone(parse_event(event(), {"234"}, set()))
        self.assertIsNone(parse_event(event(), set(), {"567"}))
        self.assertEqual(parse_event(event("/ai hello", mention=False), set(), set()).text, "hello")
        self.assertIsNone(parse_event(event("/air", mention=False), set(), set()))
        self.assertEqual(parse_event(event(group=None, mention=False), set(), set()).text, "测试")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.sent = []
        self.socket = None
        self.socket_ready = asyncio.Event()
        self.status = 200
        self.finish_reason = "stop"
        self.app = web.Application()
        self.app.router.add_get("/ws", self.ws)
        self.app.router.add_post("/chat/completions", self.complete)
        self.app.router.add_post("/send_group_msg", self.send)
        self.app.router.add_post("/send_private_msg", self.send)
        self.app.router.add_post("/get_status", self.qq_status)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        self.env = patch.dict(
            os.environ,
            {
                "ONEBOT_WS_URL": url.replace("http", "ws") + "/ws",
                "ONEBOT_HTTP_URL": url,
                "DEEPSEEK_BASE_URL": url,
                "ONEBOT_TOKEN": "onebot-test",
                "DEEPSEEK_API_KEY": "deepseek-test",
                "ONEBOT_TOKEN_FILE": "",
                "DEEPSEEK_API_KEY_FILE": "",
                "ARCHIVE_PATH": "",
                "PLUGIN_RUNNER_URL": "",
                "ALLOWED_USERS": "",
                "ALLOWED_GROUPS": "",
            },
        )
        self.env.start()
        self.bot = Bot()
        await self.bot.start(None)
        await asyncio.wait_for(self.socket_ready.wait(), 3)

    async def asyncTearDown(self):
        await self.bot.stop(None)
        await self.runner.cleanup()
        self.env.stop()

    async def ws(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer onebot-test")
        self.socket = web.WebSocketResponse()
        await self.socket.prepare(request)
        self.socket_ready.set()
        async for _ in self.socket:
            pass
        return self.socket

    async def complete(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer deepseek-test")
        self.requests.append(await request.json())
        return web.json_response(
            {
                "choices": [
                    {
                        "finish_reason": self.finish_reason,
                        "message": {"content": "回答[CQ:at,qq=all]"},
                    }
                ]
            },
            status=self.status,
        )

    async def send(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer onebot-test")
        self.sent.append(await request.json())
        return web.json_response({"status": "ok", "retcode": 0, "data": {"message_id": 42}})

    async def qq_status(self, request):
        return web.json_response({"status": "ok", "retcode": 0, "data": {"online": True}})

    async def wait_sent(self, number):
        async with asyncio.timeout(3):
            while len(self.sent) < number:
                await asyncio.sleep(0.01)
            # Receiving HTTP bytes precedes the client's success/cleanup path.
            # Observe committed turn state only after that path has completed.
            await self.bot.queue.join()

    async def test_websocket_api_reply_context_and_reset(self):
        await self.socket.send_json(event("第一个问题"))
        await self.wait_sent(1)
        self.assertEqual(self.requests[0]["messages"][-1]["content"], "第一个问题")
        self.assertEqual(
            self.sent[0]["message"][-1], {"type": "text", "data": {"text": "回答[CQ:at,qq=all]"}}
        )
        self.assertEqual(self.sent[0]["group_id"], 456)
        self.assertEqual(self.sent[0]["message"][0], {"type": "reply", "data": {"id": "1"}})
        self.bot.cooldown.clear()
        await self.socket.send_json(event("第二个问题", message_id=2))
        await self.wait_sent(2)
        self.assertEqual(len(self.requests[1]["messages"]), 4)
        self.bot.cooldown.clear()
        await self.socket.send_json(event("/reset", message_id=3))
        await self.wait_sent(3)
        self.assertFalse(self.bot.history)
        self.assertEqual(len(self.requests), 2)

    async def test_replay_self_and_group_noise_do_not_call_api(self):
        self.bot.accept(event(mention=False))
        self.bot.accept(event(user=999))
        stale = event()
        stale["time"] -= 600
        self.bot.accept(stale)
        self.assertTrue(self.bot.queue.empty())
        await self.socket.send_json(event(group=None, mention=False))
        await self.wait_sent(1)
        self.bot.cooldown.clear()
        await self.socket.send_json(event(group=None, mention=False))
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.sent[0]["user_id"], 123)

    async def test_auth_failure_gets_generic_message_and_no_context(self):
        self.status = 401
        await self.socket.send_json(event())
        await self.wait_sent(1)
        self.assertEqual(self.bot.api_state, "http_401")
        self.assertFalse(self.bot.history)
        self.assertEqual(self.sent[0]["message"][-1]["data"]["text"], "暂时无法回复，请稍后再试。")

    async def test_truncated_thinking_response_is_not_delivered_as_a_complete_answer(self):
        self.finish_reason = "length"
        await self.socket.send_json(event("一个问题"))
        await self.wait_sent(1)
        self.assertEqual(self.bot.api_state, "truncated_response")
        self.assertFalse(self.bot.history)
        self.assertEqual(self.sent[0]["message"][-1]["data"]["text"], "暂时无法回复，请稍后再试。")

    async def test_private_and_different_groups_never_share_context(self):
        turns = [
            (None, "私聊里的话题"),
            (456, "甲群里的话题"),
            (789, "乙群里的话题"),
            (None, "继续私聊"),
            (456, "继续甲群"),
        ]
        for number, (group, question) in enumerate(turns, 1):
            self.bot.cooldown.clear()
            await self.socket.send_json(event(question, group=group, message_id=number))
            await self.wait_sent(number)
        self.assertEqual([len(r["messages"]) for r in self.requests], [2, 2, 2, 4, 4])
        private = self.requests[3]["messages"]
        first_group = self.requests[4]["messages"]
        self.assertEqual(private[1]["content"], "私聊里的话题")
        self.assertEqual(first_group[1]["content"], "甲群里的话题")
        self.assertNotIn("甲群里的话题", str(private))
        self.assertNotIn("私聊里的话题", str(first_group))
        self.assertNotIn("乙群里的话题", str(first_group))

    async def test_quote_bot_triggers_and_context_is_not_user_style_sample(self):
        incoming = event("这句话什么意思", mention=False)
        incoming["message"].insert(0, {"type": "reply", "data": {"id": "42"}})
        original = self.bot.action

        async def action(name, params=None):
            if name == "get_msg":
                return {
                    "message_type": "group",
                    "group_id": 456,
                    "sender": {"user_id": 999},
                    "message": [{"type": "text", "data": {"text": "这是被引用的回复"}}],
                }
            return await original(name, params)

        with patch.object(self.bot, "action", side_effect=action):
            await self.socket.send_json(incoming)
            await self.wait_sent(1)
        self.assertIn("这是被引用的回复", self.requests[0]["messages"][-2]["content"])
        self.assertEqual(self.requests[0]["messages"][-1]["content"], "这句话什么意思")
        self.assertEqual(self.sent[0]["message"][0]["data"]["id"], "1")

    async def test_quote_other_user_or_other_group_does_not_trigger(self):
        incoming = event("普通群聊", mention=False)
        incoming["message"].insert(0, {"type": "reply", "data": {"id": "42"}})
        message = parse_event(incoming, set(), set())
        self.assertTrue(message.reply_only)
        for group, sender in ((456, 234), (987, 999)):
            with patch.object(
                self.bot,
                "action",
                new=AsyncMock(
                    return_value={
                        "message_type": "group",
                        "group_id": group,
                        "sender": {"user_id": sender},
                        "message": [{"type": "text", "data": {"text": "原话"}}],
                    }
                ),
            ):
                self.assertIsNone(await self.bot.quoted_context(message))

    async def test_validated_quote_preserves_speaker_role_without_account_ids(self):
        incoming = event("这句话什么意思")
        incoming["message"].insert(0, {"type": "reply", "data": {"id": "42"}})
        message = parse_event(incoming, set(), set())
        for sender, role in (
            (999, "bot"),
            (123, "current_speaker"),
            (124, "other_member"),
            (None, "unknown"),
        ):
            with patch.object(
                self.bot,
                "action",
                AsyncMock(
                    return_value={
                        "message_type": "group",
                        "group_id": 456,
                        "sender": {"user_id": sender},
                        "message": [{"type": "text", "data": {"text": "我腰痛"}}],
                    }
                ),
            ):
                quote = await self.bot.quoted_context(message)
            self.assertEqual(quote["speaker_role"], role)
            self.assertNotIn("user_id", quote)


if __name__ == "__main__":
    unittest.main()
