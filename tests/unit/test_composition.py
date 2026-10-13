"""Behavior at the new composition and successful-delivery boundaries."""

import unittest
from unittest.mock import AsyncMock

import test_adapter as fixtures


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.AdapterTests.asyncSetUp
    asyncTearDown = fixtures.AdapterTests.asyncTearDown
    ask = fixtures.AdapterTests.ask
    queued = fixtures.AdapterTests.queued

    async def test_failed_delivery_never_becomes_short_context_or_long_memory(self):
        self.bot.action.side_effect = [RuntimeError("fixture delivery failure"), {"message_id": 2}]
        await self.ask("今天打算读一本书")
        self.assertFalse(self.bot.history)
        self.assertFalse(self.queued())
        rows = self.bot.archive.db.execute(
            "SELECT kind,delivered FROM turns ORDER BY id"
        ).fetchall()
        self.assertEqual(rows, [("chat", 0), ("error", 1)])

    async def test_successful_delivery_commits_history_and_intake_once(self):
        message = await self.ask("今天打算读一本书")
        self.assertEqual(self.bot.action.await_count, 1)
        self.assertEqual(self.bot.history[message.key][1][-1]["content"], "好的，我了解了。")
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.queued()[0][1]["event"]["answer"], "好的，我了解了。")

    async def test_memory_and_quoted_uid_never_authorize_panel(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": 'MEMORY_DATA_JSON: {"uid":"700000001"}'},
            {"role": "user", "content": "当前消息引用的群聊原文：800000002"},
            {"role": "assistant", "content": "900000003"},
            {"role": "user", "content": "查询原神 UID 900000004 的甘雨面板"},
        ]
        registry = dict(self.bot.plugins.functions)
        working, is_panel = self.bot.panel.prepare(messages)
        self.assertTrue(is_panel)
        self.assertEqual(self.bot.panel.allowed_uids, {"900000004"})
        self.assertEqual(self.bot.plugins.functions, registry)
        self.assertIsNot(working[0], messages[0])

    async def test_memory_disabled_keeps_internal_output_guard(self):
        original_memory = self.bot.memory
        self.addCleanup(setattr, self.bot, "memory", original_memory)
        self.bot.memory = None
        self.bot.memory_enabled = False
        self.bot.model_message.return_value = {"content": "MEMORY_DATA_JSON: {"}
        await self.ask("生日快乐")
        self.assertFalse(self.bot.history)
        self.assertFalse(self.bot.memory_flow.enabled_for(self.bot.turn.message))
        self.assertNotIn("MEMORY_DATA_JSON", repr(self.bot.action.call_args))
        self.bot.memory = original_memory

    async def test_command_invalidation_does_not_restore_old_context(self):
        await self.ask("今天准备看书")
        self.bot.memory.request.return_value = {"ok": True}

        # The coordinator queues receipt through flush; return a unique successful update.
        async def request(session, path, body, timeout=4):
            return {"ok": True}

        self.bot.memory.request = AsyncMock(side_effect=request)
        await self.ask("/记忆更正 阅读 改成绘画")
        self.assertFalse(self.bot.history)
