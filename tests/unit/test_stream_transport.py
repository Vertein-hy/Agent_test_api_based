import asyncio
import json
import os
import unittest
from unittest.mock import patch
import aiohttp
from aiohttp import web
from qqbot.app import Bot
from test_tool_protocol import DIRECT, WRAPPED
from test_answer_display import MEMORY_ECHO


class StreamTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = None
        self.contents = []
        self.payloads = []

        async def handler(request):
            payload = await request.json()
            self.payloads.append(payload)
            self.assertTrue(payload["stream"])
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b": keepalive\n\n")
            await asyncio.sleep(0.025)
            chunks = (
                ({"role": "assistant"}, None),
                ({"content": "hello "}, None),
                ({"content": "world"}, "stop"),
            )
            if self.contents:
                chunks = (({"content": self.contents.pop(0)}, "stop"),)
            for delta, finish in chunks:
                data = (
                    "data: "
                    + json.dumps(
                        {"model": "fixture", "choices": [{"delta": delta, "finish_reason": finish}]}
                    )
                    + "\n\n"
                ).encode()
                # Deliberately fragmented transport boundaries.
                await response.write(data[:11])
                await response.write(data[11:])
                await asyncio.sleep(0.01)
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/chat/completions", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.bot = Bot()
        self.bot.session = aiohttp.ClientSession()
        self.bot.api_url = "http://127.0.0.1:" + str(port)
        self.bot.model = "fixture"
        self.bot.thinking = "disabled"
        self.bot.api_state = "unknown"
        self.bot.turn.timings = []
        self.env = patch.dict(
            os.environ, {"DEEPSEEK_API_KEY_FILE": "", "DEEPSEEK_API_KEY": "fictional"}
        )
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.bot.session.close()
        await self.runner.cleanup()

    async def test_first_token_is_measured_after_headers_and_before_completion(self):
        reply = await self.bot.model_message([{"role": "user", "content": "test"}], [])
        timing = self.bot.turn.timings[-1]
        self.assertEqual(reply["content"], "hello world")
        self.assertEqual(self.bot.reported_model, "fixture")
        self.assertGreater(timing["first_token_ms"], timing["headers_ms"])
        self.assertGreater(timing["total_ms"], timing["first_token_ms"])
        self.assertTrue(timing["success"])

    async def test_logged_dsml_becomes_structured_tool_call_without_visible_draft(self):
        self.contents = [WRAPPED]
        reply = await self.bot.model_message(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "查询B站UP主"}],
            [{"type": "function", "function": {"name": "web_search"}}],
        )
        self.assertEqual(reply["content"], "")
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(len(self.payloads), 1)

    async def test_no_tool_round_recovers_final_answer_once_and_never_emits_dsml(self):
        self.contents = [DIRECT, "目前检索到的资料不足以确认Mac版，建议核对官方公告。"]
        reply = await self.bot.model_message(
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "如何在Mac上玩终末地"},
            ],
            [],
        )
        self.assertNotIn("DSML", reply["content"])
        self.assertEqual(len(self.payloads), 2)
        self.assertNotIn("tools", self.payloads[-1])

    async def test_repeated_protocol_fails_closed_after_one_retry(self):
        self.contents = [WRAPPED, WRAPPED]
        with self.assertRaises(RuntimeError):
            await self.bot.model_message(
                [{"role": "system", "content": "s"}, {"role": "user", "content": "查询UP主"}], []
            )
        self.assertEqual(self.bot.api_state, "invalid_tool_protocol")
        self.assertEqual(len(self.payloads), 2)

    async def test_unknown_name_is_not_executed_and_code_requested_by_user_is_preserved(self):
        self.contents = [WRAPPED.replace("web_search", "delete_everything"), "资料不足。"]
        reply = await self.bot.model_message(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "查询"}],
            [{"type": "function", "function": {"name": "web_search"}}],
        )
        self.assertNotIn("tool_calls", reply)
        self.contents = ["```xml\n" + WRAPPED + "\n```"]
        reply = await self.bot.model_message(
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "解释DSML格式并给代码示例"},
            ],
            [],
        )
        self.assertIn("```xml", reply["content"])

    async def test_memory_echo_in_stream_is_removed_before_it_is_returned(self):
        self.contents = [MEMORY_ECHO + "生日快乐！🎂"]
        reply = await self.bot.model_message(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "我今天过生日"}], []
        )
        self.assertEqual(reply["content"], "生日快乐！🎂")
        self.assertEqual(len(self.payloads), 1)

    async def test_partial_memory_recovers_once_and_repeated_output_fails_closed(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "我今天过生日"}]
        self.contents = ["MEMORY_DATA_JSON: {", "生日快乐！"]
        reply = await self.bot.model_message(messages, [])
        self.assertEqual(reply["content"], "生日快乐！")
        self.assertEqual(len(self.payloads), 2)
        self.contents = [MEMORY_ECHO, MEMORY_ECHO]
        with self.assertRaises(RuntimeError):
            await self.bot.model_message(messages, [])
        self.assertEqual(self.bot.api_state, "invalid_memory_output")
        self.assertEqual(len(self.payloads), 4)
