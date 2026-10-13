"""Actual vision model and streaming adapter; capture both group/private replies."""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch
import aiohttp
from qqbot.app import Bot
from qqbot.domain import parse_event


async def main():
    os.environ["ARCHIVE_SALT"] = "v3-fictional-" + uuid.uuid4().hex
    os.environ["ARCHIVE_SALT_FILE"] = ""
    os.environ["PLUGIN_RUNNER_URL"] = "http://plugins:8081"
    os.environ["ENABLED_PLUGINS"] = "current_time"
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    sent = []
    user = "987650001"
    group = None
    serial = 0
    if os.getenv("V3_CANDIDATE_TEST") == "1":
        bot.memory.url = "http://qq-memory-v3-candidate:8083"

    async def capture(action, params=None):
        assert action in ("send_private_msg", "send_group_msg")
        texts = [p["data"]["text"] for p in params["message"] if p["type"] == "text"]
        sent.append(texts[-1])
        return {"message_id": len(sent)}

    bot.action = capture
    worker = asyncio.create_task(bot.worker())
    actors = set()
    timings = []
    report = {"qq_delivery": "captured", "model": bot.model}

    async def ask(text):
        nonlocal serial
        serial += 1
        segments = ([{"type": "at", "data": {"qq": "999"}}] if group else []) + [
            {"type": "text", "data": {"text": text}}
        ]
        raw = {
            "post_type": "message",
            "message_type": "group" if group else "private",
            "self_id": 999,
            "user_id": int(user),
            "message_id": serial,
            "time": int(time.time()),
            "message": segments,
            "sender": {"nickname": "同名昵称"},
        }
        if group:
            raw["group_id"] = int(group)
        msg = parse_event(raw, set(), set())
        assert msg is not None
        await bot.queue.put(msg)
        await asyncio.wait_for(bot.queue.join(), 100)
        assert bot.api_state not in ("http_429", "http_502", "truncated_response", "http_401"), (
            bot.api_state
        )
        if not text.startswith("/"):
            assert bot.turn.timings and all(x["success"] for x in bot.turn.timings)
            assert all(x["first_token_ms"] is not None for x in bot.turn.timings), bot.turn.timings
            timings.extend(bot.turn.timings)
        return sent[-1]

    async def wait():
        await bot.memory.flush(bot.session, 16)
        async with asyncio.timeout(200):
            while True:
                ident = bot.context(bot.turn.message)
                status = await bot.memory.request(bot.session, "status", ident)
                if status["pending"] == 0:
                    return status
                await asyncio.sleep(2)

    try:
        actor = bot.archive.actor(user)
        actors.add(actor)
        bot.memory.control(actor, True, True, True)
        assert await bot.memory.flush(bot.session)
        await ask("以后请叫我测试乙。我希望日常回答简短一些，打招呼的时候说：嗨🎶想我了吗。")
        await wait()
        group = "987660001"
        await ask("/reset")
        greeting = await ask("来打个招呼")
        assert "嗨" in greeting and "想我了吗" in greeting and len(greeting) < 120, greeting
        user = "987650002"
        actor = bot.archive.actor(user)
        actors.add(actor)
        bot.memory.control(actor, True, True, True)
        assert await bot.memory.flush(bot.session)
        await ask("以后请叫我测试戊。")
        await wait()
        await ask("/reset")
        other = await ask("你还记得我希望你怎么称呼我吗？")
        assert "测试戊" in other and "测试乙" not in other, other
        user = "987650001"
        group = None
        await ask("/reset")
        mine = await ask("你还记得我希望你怎么称呼我吗？")
        assert "测试乙" in mine and "测试戊" not in mine, mine
        # Exact command update is synchronous and generation-bound.
        status = await bot.memory.request(bot.session, "status", bot.context(bot.turn.message))
        name = next(n for n in status["facts"] if "希望的称呼" in n["text"])
        changed = await ask("/记忆更正 " + name["id"][:10] + " 测试甲")
        assert changed == "已更新这条记忆。", changed
        await ask("/reset")
        updated = await ask("你还记得我希望你怎么称呼我吗？")
        assert "测试甲" in updated and "测试乙" not in updated, updated
        with patch.object(
            bot.panel, "call", AsyncMock(return_value={"error": "只检查参数"})
        ) as panel_tool:
            with patch.object(bot.memory, "request", wraps=bot.memory.request) as requests:
                await ask("查询原神 UID 700000001 的雷神面板")
                assert panel_tool.call_args.args[1]["uid"] == "700000001"
                assert requests.call_count == 0
        report.update(
            group_greeting=greeting,
            different_person=other,
            private_name=mine,
            corrected_name=updated,
            cross_context_continuity=True,
            account_isolation=True,
            command_correction=True,
            panel_independent=True,
            timings=timings,
        )
    finally:
        for actor in actors:
            bot.memory.control(actor, False, True, True)
            assert await bot.memory.flush(bot.session, 32)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()
    (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "v3-adapter-smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))


asyncio.run(main())
