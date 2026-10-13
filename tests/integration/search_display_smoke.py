"""Real provider and real search plugin, captured QQ delivery, no memory writes."""

import asyncio
import os
import json
import time
import uuid
from pathlib import Path
import aiohttp
from qqbot.domain import parse_event
from qqbot.app import Bot


async def main():
    os.environ["ARCHIVE_PATH"] = "/tmp/search-display-" + uuid.uuid4().hex + ".sqlite3"
    os.environ["ARCHIVE_SALT"] = "fictional-" + uuid.uuid4().hex
    os.environ["ARCHIVE_SALT_FILE"] = ""
    os.environ["MEMORY_ENABLED"] = "0"
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    sent = []

    async def capture(action, params=None):
        assert action == "send_private_msg"
        sent.append(params["message"][-1]["data"]["text"])
        return {"message_id": len(sent)}

    original = bot.model_message

    async def traced(*args, **kwargs):
        try:
            result = await original(*args, **kwargs)
            print(
                json.dumps(
                    {
                        "model_call_names": [
                            c.get("function", {}).get("name") for c in result.get("tool_calls", [])
                        ],
                        "model_content_preview": (result.get("content") or "")[:120],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return result
        except Exception as error:
            print(json.dumps({"model_error": str(error), "timings": bot.turn.timings}), flush=True)
            raise

    bot.model_message = traced
    bot.action = capture
    worker = asyncio.create_task(bot.worker())
    report = {"qq_delivery": "captured", "real_model": bot.model, "real_search": True}
    try:
        for i, question in enumerate(
            ("Python编程语言是什么？", "附上来源链接；Python编程语言是什么？")
        ):
            bot.history.clear()
            bot.archive.reset_context("987650003", None)
            msg = parse_event(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 999,
                    "user_id": 987650003,
                    "message_id": i + 1,
                    "time": int(time.time()),
                    "message": [{"type": "text", "data": {"text": question}}],
                },
                set(),
                set(),
            )
            await bot.queue.put(msg)
            await asyncio.wait_for(bot.queue.join(), 150)
            answer = sent[-1]
            print(
                json.dumps(
                    {
                        "case": i,
                        "api_state": bot.api_state,
                        "trace_names": [t.get("name") for t in bot.last_trace],
                        "reply_chars": len(answer),
                    }
                ),
                flush=True,
            )
            assert "DSML" not in answer, answer
            assert any(t.get("name") == "web_search" for t in bot.last_trace), bot.last_trace
            if i == 0:
                assert "搜索资料" not in answer and "http" not in answer, answer
            else:
                assert "http" in answer, answer
            report["default_no_sources" if i == 0 else "sources_when_requested"] = {
                "answer": answer,
                "model_calls": len(bot.turn.timings),
            }
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.archive.close()
    (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "search-display-smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))


asyncio.run(main())
