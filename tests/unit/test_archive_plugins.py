import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from qqbot.storage.archive import Archive
from qqbot.tools.registry import PublicResolver, Registry, checked_url, dispatch
import test_bot as fixtures
from test_bot import event
from aiohttp import web


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "archive.sqlite3"
        self.store = Archive(self.path, b"test-salt", default_capture=True)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def turn(self, user="123", group=None, trace=None, kind="chat"):
        turn = self.store.append(
            user,
            group,
            "system",
            "email: a@example.com",
            "sk-secretsecretsecret",
            "requested",
            "reported",
            trace,
            kind,
        )
        self.store.delivered(turn)
        return turn

    def test_export_requires_training_permission_review_delivery_and_plain_chat(self):
        turn = self.turn()
        self.store.review(turn, True)
        count, skipped = self.store.export(Path(self.folder.name) / "none.jsonl")
        self.assertEqual(count, 0)
        self.store.configure("123", training=True)
        self.turn()  # Unreviewed: excluded.
        tool_turn = self.turn(trace=[{"name": "web_read", "result": "source"}])
        self.store.review(tool_turn, True)
        count, skipped = self.store.export(Path(self.folder.name) / "sft.jsonl")
        self.assertEqual((count, skipped), (1, 1))
        text = (Path(self.folder.name) / "sft.jsonl").read_text()
        self.assertNotIn("a@example.com", text)
        self.assertNotIn("sk-secretsecretsecret", text)
        self.assertNotIn('"actor"', text)
        self.assertEqual(
            [x["role"] for x in json.loads(text)["messages"]], ["system", "user", "assistant"]
        )
        self.store.configure("123", training=False)
        self.assertEqual(self.store.export(Path(self.folder.name) / "revoked.jsonl")[0], 0)

    def test_persistence_scope_reset_and_forget(self):
        self.turn()
        self.assertEqual(len(self.store.recent("123", None)), 2)
        self.assertFalse(self.store.recent("234", None))
        self.assertFalse(self.store.recent("123", "456"))
        self.store.close()
        self.store = Archive(self.path, b"test-salt", default_capture=True)
        self.assertEqual(len(self.store.recent("123", None)), 2)
        self.store.reset_context("123", None)
        self.assertFalse(self.store.recent("123", None))
        self.store.forget("123")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
        self.assertEqual(self.store.preferences("123"), (False, False))
        self.assertIsNone(self.store.append("123", None, "s", "q", "a", "m"))

    def test_failed_delivery_and_control_cannot_be_training_examples(self):
        pending = self.store.append("123", None, "s", "q", "a", "m")
        with self.assertRaises(ValueError):
            self.store.review(pending, True)
        control = self.turn(kind="control")
        with self.assertRaises(ValueError):
            self.store.review(control, True)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_and_unknown_plugin_are_rejected(self):
        registry = Registry()
        with self.assertRaises(ValueError):
            registry.check("shell", {"command": "cat /run/secrets/deepseek_api_key"})
        with self.assertRaises(ValueError):
            registry.check("web_read", {"url": "https://docs.python.org/", "command": "id"})
        with self.assertRaises(ValueError):
            registry.check("wikipedia_search", {"query": "x" * 121})
        result = await dispatch("current_time", {})
        self.assertEqual(result["timezone"], "Asia/Shanghai")

    async def test_ssrf_url_and_dns_destinations_rejected(self):
        for url in (
            "file:///etc/passwd",
            "http://docs.python.org/",
            "https://127.0.0.1/",
            "https://docs.python.org:8080/",
            "https://user:pass@docs.python.org/",
            "https://evil.example/",
        ):
            with self.assertRaises(ValueError):
                checked_url(url, {"docs.python.org"})
        loop = asyncio.get_running_loop()
        for address in ("127.0.0.1", "192.168.1.6", "169.254.169.254", "::1", "224.0.0.1"):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with patch.object(loop, "getaddrinfo", return_value=answers):
                with self.assertRaises(ValueError):
                    await PublicResolver().resolve("docs.python.org", 443)


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    ws = fixtures.IntegrationTests.ws
    qq_status = fixtures.IntegrationTests.qq_status
    wait_sent = fixtures.IntegrationTests.wait_sent

    async def asyncSetUp(self):
        self.model_calls = 0
        self.plugin_calls = []
        self.requested_tool = "current_time"
        self.plugin_result = {"time": "2026-10-04T21:00:00+08:00", "timezone": "Asia/Shanghai"}
        await fixtures.IntegrationTests.asyncSetUp(self)
        self.bot.plugin_url = self.bot.http_url
        self.env2 = patch.dict(os.environ, {"PLUGIN_TOKEN": "plugin-test", "PLUGIN_TOKEN_FILE": ""})
        self.env2.start()

    async def asyncTearDown(self):
        await fixtures.IntegrationTests.asyncTearDown(self)
        self.env2.stop()

    async def complete(self, request):
        body = await request.json()
        self.requests.append(body)
        self.model_calls += 1
        if self.model_calls == 1:
            return web.json_response(
                {
                    "model": "provider-alias",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "reasoning_content": "synthetic-private-protocol-field",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": self.requested_tool,
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                }
            )
        return web.json_response(
            {"model": "provider-alias", "choices": [{"message": {"content": "北京时间21点"}}]}
        )

    async def send(self, request):
        if request.headers.get("Authorization") == "Bearer plugin-test":
            self.plugin_calls.append(await request.json())
            return web.json_response({"result": self.plugin_result})
        return await fixtures.IntegrationTests.send(self, request)

    async def test_roundtrip_calls_enabled_plugin_and_preserves_tool_role(self):
        # Reuse an existing authenticated mock route, keeping registry validation.
        self.bot.thinking = "enabled"
        self.bot.plugin_url = self.bot.http_url
        original_post = self.bot.session.post

        def post(url, **kwargs):
            if url.endswith("/call"):
                url = self.bot.http_url + "/send_group_msg"
            return original_post(url, **kwargs)

        with patch.object(self.bot.session, "post", side_effect=post):
            await self.socket.send_json(event("现在几点？"))
            await self.wait_sent(1)
        self.assertEqual(self.plugin_calls[0]["name"], "current_time")
        self.assertEqual(self.requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(self.requests[0]["thinking"], {"type": "enabled"})
        self.assertEqual(self.requests[0]["reasoning_effort"], "high")
        self.assertEqual(
            self.requests[1]["messages"][-2]["reasoning_content"],
            "synthetic-private-protocol-field",
        )
        self.assertNotIn("synthetic-private-protocol-field", json.dumps(self.sent))
        self.assertNotIn("synthetic-private-protocol-field", json.dumps(self.bot.last_trace))
        self.assertNotIn("synthetic-private-protocol-field", repr(self.bot.history))
        self.assertEqual(self.bot.reported_model, "provider-alias")
        self.assertEqual(self.bot.last_trace[0]["name"], "current_time")

    async def test_unknown_tool_never_reaches_runner(self):
        self.requested_tool = "shell"
        await self.socket.send_json(event("执行命令"))
        await self.wait_sent(1)
        self.assertFalse(self.plugin_calls)
        self.assertIn("被拒绝", self.requests[1]["messages"][-1]["content"])
        self.assertEqual(self.requests[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", self.requests[0])
        self.assertNotIn("reasoning_content", self.requests[1]["messages"][-2])


if __name__ == "__main__":
    unittest.main()
