"""Production adapter + real DeepSeek + local memory; QQ delivery is captured."""

import asyncio
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import AsyncMock, patch
import aiohttp
from qqbot.domain import parse_event
from qqbot.app import Bot


async def main():
    os.environ["ARCHIVE_SALT"] = "fictional-memory-smoke-" + uuid.uuid4().hex
    os.environ["ARCHIVE_SALT_FILE"] = ""
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    sent = []

    async def capture(action, params=None):
        assert action == "send_private_msg", "No other QQ action is permitted in this test"
        sent.append(params["message"][-1]["data"]["text"])
        return {"message_id": len(sent)}

    bot.action = capture
    worker = asyncio.create_task(bot.worker())
    user = "987650001"
    actor = bot.archive.actor(user)
    actors = {actor}
    scope = bot.archive.scope(user, None)
    report = {"qq_delivery": "captured_no_messages_sent"}
    serial = 0

    async def ask(text):
        nonlocal serial
        for attempt in range(3):
            serial += 1
            message = parse_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 999,
                    "user_id": int(user),
                    "message_id": serial,
                    "time": int(time.time()),
                    "message": [{"type": "text", "data": {"text": text}}],
                },
                set(),
                set(),
            )
            await bot.queue.put(message)
            await asyncio.wait_for(bot.queue.join(), 100)
            if bot.api_state != "http_429" or text.startswith("/") or attempt == 2:
                return sent[-1]
            print(
                json.dumps({"provider_rate_limited": True, "retry_in_seconds": 30 * (attempt + 1)}),
                flush=True,
            )
            await asyncio.sleep(30 * (attempt + 1))

    try:
        bot.memory.control(actor, True, True, True)
        assert await bot.memory.flush(bot.session)
        await ask("请记住：以后叫我测试丙，我现在住在成都，我正在开发QQ机器人的图记忆系统。")
        assert await bot.memory.flush(bot.session)
        async with asyncio.timeout(150):
            while True:
                status = await bot.memory.request(
                    bot.session, "status", bot.memory.identity(actor, scope)
                )
                if status["pending"] == 0 and status["counts"].get("fact", 0) >= 3:
                    break
                await asyncio.sleep(2)
        await ask("/reset")
        assert not bot.history
        answer = await ask("你还记得我叫什么名字，住在哪个城市，正在做什么项目吗？")
        for expected in ("测试丙", "成都", "图记忆"):
            assert expected in answer, answer
        assert any(t["name"] == "memory_recall" for t in bot.last_trace)
        report["recall_after_context_reset"] = answer
        print(json.dumps({"recall_after_context_reset": answer}, ensure_ascii=False), flush=True)
        trace = list(bot.last_trace)
        user = "987650002"
        other_actor = bot.archive.actor(user)
        actors.add(other_actor)
        other_scope = bot.archive.scope(user, None)
        bot.memory.control(other_actor, True, True, True)
        assert await bot.memory.flush(bot.session)
        await ask("请记住：以后叫我测试己，我现在住在苏州，我喜欢游泳。")
        assert await bot.memory.flush(bot.session)
        async with asyncio.timeout(150):
            while True:
                status = await bot.memory.request(
                    bot.session, "status", bot.memory.identity(other_actor, other_scope)
                )
                if status["pending"] == 0 and status["counts"].get("fact", 0) >= 3:
                    break
                await asyncio.sleep(2)
        await ask("/reset")
        other_answer = await ask("你还记得我叫什么，住在哪，喜欢什么吗？")
        for expected in ("测试己", "苏州", "游泳"):
            assert expected in other_answer, other_answer
        assert "叫我测试己" not in other_answer and "我叫测试己" not in other_answer, other_answer
        assert "测试丙" not in other_answer and "成都" not in other_answer
        user = "987650001"
        await ask("/reset")
        first_again = await ask("你还记得我叫什么，住在哪吗？")
        assert "测试丙" in first_again and "成都" in first_again
        assert "测试己" not in first_again and "苏州" not in first_again
        greeting = await ask("晚上好")
        report.update(
            {
                "different_people_actual_replies": [first_again, other_answer],
                "social_reply": greeting,
                "social_reply_characters": len(greeting),
            }
        )
        assert await bot.memory.flush(bot.session)
        await ask("/reset")
        with patch.object(
            bot.panel, "call", AsyncMock(return_value={"error": "测试期间只检查调用，不发送图片。"})
        ) as tool:
            with patch.object(bot.memory, "request", wraps=bot.memory.request) as memory_requests:
                panel_answer = await ask("查询原神 UID 700000001 的雷神面板")
                assert tool.called, (
                    "Actual model did not call panel tool",
                    panel_answer,
                    bot.last_trace,
                )
                assert tool.call_args.args[1]["uid"] == "700000001"
                assert not memory_requests.called, "Panel must not read memory"
                assert (
                    bot.memory.db.execute(
                        "SELECT count(*) FROM outbox WHERE path='ingest'"
                    ).fetchone()[0]
                    == 0
                )
        report.update(
            {
                "actual_model_panel_call_unchanged": True,
                "memory_trace_ids_only": trace,
                "requested_model": bot.model,
                "reported_model": bot.reported_model,
            }
        )
    finally:
        for a in actors:
            bot.memory.control(a, False, False, True)
        assert await bot.memory.flush(bot.session)
        for a in actors:
            status = await bot.memory.request(bot.session, "status", bot.memory.identity(a, scope))
            assert status["counts"] == {} and status.get("profile", {}) == {}
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()
        report["test_memories_deleted"] = True
        (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "bot-memory-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
