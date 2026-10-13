import base64
import hashlib
import json
import os
import unittest
from unittest.mock import patch
from aiohttp import web
import test_bot as fixtures
from test_bot import event
from qqbot.app import Bot
from qqbot.tools.panel import checked_images


def result():
    image = bytes.fromhex("89504e470d0a1a0a0000000000000000")
    return {
        "ok": True,
        "texts": [],
        "image_count": 1,
        "images": [
            {
                "mime": "image/png",
                "sha256": hashlib.sha256(image).hexdigest(),
                "base64": base64.b64encode(image).decode(),
            }
        ],
    }


class ImageTests(unittest.TestCase):
    def test_bad_checksum_format_and_oversize_images_are_rejected(self):
        self.assertEqual(len(checked_images(result())), 1)
        for mutation in (
            lambda r: r["images"][0].update(sha256="bad"),
            lambda r: r["images"][0].update(base64="https://internal/secret"),
            lambda r: r["images"][0].update(mime="image/svg+xml"),
            lambda r: r.update(images=result()["images"] * 3),
        ):
            r = result()
            mutation(r)
            with self.assertRaises(ValueError):
                checked_images(r)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    ws = fixtures.IntegrationTests.ws
    qq_status = fixtures.IntegrationTests.qq_status
    wait_sent = fixtures.IntegrationTests.wait_sent

    async def asyncSetUp(self):
        self.addon_env = patch.dict(
            os.environ,
            {
                "YUNZAI_ENABLED": "1",
                "YUNZAI_TOKEN": "test-bridge-token",
                "YUNZAI_TOKEN_FILE": "",
                "YUNZAI_BRIDGE_URL": "http://yunzai-bridge:8082",
            },
        )
        self.addon_env.start()
        self.replace_bot = patch.object(fixtures, "Bot", Bot)
        self.replace_bot.start()
        self.model_calls = 0
        self.bridge_calls = []
        self.bridge_result = result()
        self.query_uid = "700000001"
        await fixtures.IntegrationTests.asyncSetUp(self)
        self.bot.plugin_url = self.bot.http_url
        original_post = self.bot.session.post

        def post(url, **kwargs):
            if url == "http://yunzai-bridge:8082/call":
                url = self.bot.http_url + "/send_group_msg"
            return original_post(url, **kwargs)

        self.route = patch.object(self.bot.session, "post", side_effect=post)
        self.route.start()

    async def asyncTearDown(self):
        self.route.stop()
        await fixtures.IntegrationTests.asyncTearDown(self)
        self.replace_bot.stop()
        self.addon_env.stop()

    async def complete(self, request):
        body = await request.json()
        self.requests.append(body)
        self.model_calls += 1
        if self.model_calls == 1:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "yz1",
                        "type": "function",
                        "function": {
                            "name": "yunzai_query",
                            "arguments": json.dumps(
                                {"operation": "profile", "uid": self.query_uid, "character": "雷神"}
                            ),
                        },
                    }
                ],
            }
        else:
            message = {"content": "查询结果已返回。"}
        return web.json_response({"choices": [{"message": message}]})

    async def send(self, request):
        if request.headers.get("Authorization") == "Bearer test-bridge-token":
            self.bridge_calls.append(await request.json())
            return web.json_response(self.bridge_result)
        return await fixtures.IntegrationTests.send(self, request)

    async def ask(self, text="查一下原神700000001雷神面板"):
        await self.socket.send_json(event(text))
        await self.wait_sent(1)

    async def test_actual_tool_loop_delivers_one_reply_with_image_and_keeps_media_out_of_model_and_archive(
        self,
    ):
        await self.ask()
        self.assertEqual(len(self.bridge_calls), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["group_id"], 456)
        self.assertEqual(self.sent[0]["message"][-1]["type"], "image")
        self.assertEqual(self.sent[0]["message"][0]["type"], "reply")
        self.assertEqual(len(self.bridge_calls[0]["context"]["actor"]), 64)
        self.assertNotIn("user_id", self.bridge_calls[0])
        encoded = result()["images"][0]["base64"]
        self.assertNotIn(encoded, json.dumps(self.requests))
        self.assertNotIn(encoded, json.dumps(self.bot.last_trace))
        self.assertNotIn(encoded, repr(self.bot.history))
        self.assertFalse(self.bot.panel.images)
        self.assertNotIn("web_search", [t["function"]["name"] for t in self.requests[0]["tools"]])
        self.assertIn("web_search", self.bot.plugins.functions)
        await self.socket.send_json(event("你好", user=234, group=987, message_id=2))
        await self.wait_sent(2)
        self.assertFalse(any(p["type"] == "image" for p in self.sent[1]["message"]))
        self.assertEqual(self.sent[1]["group_id"], 987)

    async def test_model_cannot_query_an_invented_uid(self):
        self.query_uid = "800000002"
        await self.ask()
        self.assertFalse(self.bridge_calls)
        self.assertIn("不能猜测", self.requests[1]["messages"][-1]["content"])
        self.assertFalse(any(p["type"] == "image" for p in self.sent[0]["message"]))

    async def test_invalid_media_never_reaches_qq(self):
        self.bridge_result["images"][0]["sha256"] = "wrong"
        await self.ask()
        self.assertEqual(len(self.bridge_calls), 1)
        self.assertFalse(any(p["type"] == "image" for p in self.sent[0]["message"]))
        self.assertIn("被拒绝", self.requests[1]["messages"][-1]["content"])

    async def test_unavailable_addon_does_not_break_the_reply_or_other_tools(self):
        self.bridge_result = {"error": "busy_retry_later"}
        await self.ask()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(any(p["type"] == "image" for p in self.sent[0]["message"]))
        self.assertIn("current_time", self.bot.plugins.functions)

    async def test_switch_off_restores_original_bot_toolset(self):
        self.bot.panel.enabled = False
        self.bot.plugins.functions.pop("yunzai_query")
        await self.ask("你好")
        self.assertFalse(self.bridge_calls)
        self.assertNotIn("yunzai_query", [t["function"]["name"] for t in self.requests[0]["tools"]])


if __name__ == "__main__":
    unittest.main()
