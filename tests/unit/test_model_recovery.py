"""Transient gateway/stream failures, shared budgets and the delivery boundary."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from qqbot.app import Bot
from qqbot.domain import Message
from qqbot.model.client import RequestBudget
from qqbot.model.errors import StreamError


class ModelRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "ARCHIVE_PATH": self.tmp.name + "/archive.db",
                "ARCHIVE_SALT": "fictional-salt",
                "ARCHIVE_SALT_FILE": "",
                "ARCHIVE_DEFAULT_CAPTURE": "1",
                "MEMORY_ENABLED": "0",
                "YUNZAI_ENABLED": "0",
                "PLUGIN_RUNNER_URL": "",
                "DEEPSEEK_API_KEY": "fixture",
                "DEEPSEEK_API_KEY_FILE": "",
            },
        )
        self.env.start()
        self.steps = []
        self.payloads = []

        async def handle(request):
            self.payloads.append(await request.json())
            step = self.steps.pop(0)
            if isinstance(step, int):
                return web.Response(status=step)
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            if step == "empty":
                await response.write(b"data: [DONE]\n\n")
            elif step == "malformed":
                await response.write(b'data: {"choices":[{"delta":42}]}\n\n')
            elif step == "error":
                await response.write(
                    b'data: {"error":{"type":"server_error","message":"PRIVATE_FIXTURE"}}\n\n'
                )
            else:
                content, finish = step
                event = {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}
                await response.write(("data: " + json.dumps(event) + "\n\n").encode())
                if finish:
                    await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_post("/chat/completions", handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.bot = Bot()
        self.bot.session = aiohttp.ClientSession()
        self.bot.api_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.bot.action = AsyncMock(return_value={"message_id": 1})
        self.messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "测试问候"},
        ]

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.bot.archive.close()
        await self.runner.cleanup()
        self.env.stop()
        self.tmp.cleanup()

    async def test_502_retries_same_request_and_sends_archives_only_once(self):
        self.steps = [502, ("最终回答", "stop")]
        worker = asyncio.create_task(self.bot.worker())
        try:
            await self.bot.queue.put(Message("987650008", None, "测试问候"))
            await asyncio.wait_for(self.bot.queue.join(), 3)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.payloads[0], self.payloads[1])
        self.bot.action.assert_awaited_once()
        rows = self.bot.archive.db.execute("SELECT answer,kind,delivered FROM turns").fetchall()
        self.assertEqual(rows, [("最终回答", "chat", 1)])
        self.assertEqual(self.bot.api_state, "ok")

    async def test_empty_stream_recovers_instead_of_reporting_previous_ok(self):
        self.bot.api_state = "ok"
        self.steps = ["empty", ("恢复回答", "stop")]
        reply = await self.bot.model_message(self.messages, [])
        self.assertEqual(reply["content"], "恢复回答")
        self.assertEqual(
            [t["outcome"] for t in self.bot.turn.timings],
            ["incomplete_stream", "response_received"],
        )

    async def test_partial_disconnected_answer_is_discarded_and_regenerated(self):
        self.steps = [("不能发出去的残缺回答", None), ("完整回答", "stop")]
        reply = await self.bot.model_message(self.messages, [])
        self.assertEqual(reply["content"], "完整回答")
        self.bot.action.assert_not_awaited()

    async def test_repeated_empty_stream_fails_after_two_attempts_with_correct_state(self):
        self.bot.api_state = "ok"
        self.steps = ["empty", "empty"]
        with self.assertRaises(StreamError):
            await self.bot.model_message(self.messages, [])
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.bot.api_state, "incomplete_stream")
        self.bot.action.assert_not_awaited()

    async def test_authentication_failure_is_not_retried(self):
        self.steps = [401]
        with self.assertRaises(RuntimeError):
            await self.bot.model_message(self.messages, [])
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.bot.api_state, "http_401")

    async def test_sse_provider_error_is_classified_without_logging_body(self):
        self.steps = ["error", ("完成", "stop")]
        with self.assertLogs("qqbot", level="INFO") as logs:
            reply = await self.bot.model_message(self.messages, [])
        self.assertEqual(reply["content"], "完成")
        self.assertNotIn("PRIVATE_FIXTURE", repr(logs.output))
        self.assertIn("provider_stream_error", repr(logs.output))

    async def test_expired_budget_does_not_start_another_wait(self):
        self.steps = [502]
        with self.assertRaises(RuntimeError):
            await self.bot.model_message(
                self.messages, [], _budget=RequestBudget(time.monotonic() + 0.2)
            )
        self.assertEqual(len(self.payloads), 1)

    async def test_truncated_stream_is_not_retried_or_delivered(self):
        self.steps = [("被截断", "length")]
        with self.assertRaises(RuntimeError):
            await self.bot.model_message(self.messages, [])
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.bot.api_state, "truncated_response")

    async def test_protocol_repair_cannot_reset_transport_retry_budget(self):
        self.steps = [502, ("MEMORY_DATA_JSON: {", "stop"), "empty"]
        with self.assertRaises(StreamError):
            await self.bot.model_message(self.messages, [])
        self.assertEqual(len(self.payloads), 3)
        self.bot.action.assert_not_awaited()

    async def test_malformed_shape_is_classified_without_retry(self):
        self.steps = ["malformed"]
        with self.assertRaises(AttributeError):
            await self.bot.model_message(self.messages, [])
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.bot.api_state, "invalid_model_response")
        self.bot.action.assert_not_awaited()
