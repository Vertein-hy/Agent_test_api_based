"""Real model attribution probe, fictional conversations and captured QQ delivery only."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
from qqbot.app import Bot
from qqbot.domain import Message
from qqbot.addressing import addressing_revision_reason
from qqbot.model.answer_display import internal_memory_present


async def main():
    os.environ.update(YUNZAI_ENABLED="0", PLUGIN_RUNNER_URL="")
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    bot.memory.request = AsyncMock(return_value={"profile": {}, "memories": []})
    sent, inputs, cases = [], [], []

    async def capture(action, params=None):
        assert action in ("send_private_msg", "send_group_msg")
        sent.append(params["message"][-1]["data"]["text"])
        return {"message_id": len(sent)}

    original = bot.model_message

    async def measured(messages, *args, **kwargs):
        inputs.append([dict(m) for m in messages])
        return await original(messages, *args, **kwargs)

    bot.action = capture
    bot.model_message = measured
    worker = asyncio.create_task(bot.worker())

    async def ask(name, text, group=None, history=None, memory=True):
        bot.history.clear()
        user = "987650031"
        bot.archive.reset_context(user, group)
        bot.archive.configure(user, capture=memory)
        message = Message(user, group, text, str(len(cases) + 1), bot_id="999")
        if history:
            bot.history[message.key] = (time.time(), history)
        before = len(inputs)
        await bot.queue.put(message)
        await asyncio.wait_for(bot.queue.join(), 110)
        answer = sent[-1]
        assert bot.api_state == "ok", bot.api_state
        assert not internal_memory_present(answer)
        assert addressing_revision_reason(inputs[before], answer) is None
        assert answer != "暂时无法回复，请稍后再试。"
        result = {
            "case": name,
            "reply": answer,
            "model_calls": len(inputs) - before,
            "private": group is None,
            "memory_enabled_for_actor": memory,
            "revision_reasons": [
                t["arguments"]["reason"] for t in bot.last_trace if t.get("name") == "chat_revision"
            ],
        }
        cases.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return answer

    try:
        await ask("private_greeting_vocative", "你好示例甲")
        answer = await ask("group_greeting_vocative", "下午好，示例甲！", "987660031")
        # Check the observed escaped greeting independently of the policy function.
        assert "示例甲下午好" not in answer, answer
        await ask(
            "previous_assistant_misnaming_is_not_evidence",
            "借我一点钱可以吗",
            history=[
                {"role": "user", "content": "你好示例甲"},
                {"role": "assistant", "content": "哟，示例甲今天怎么想起找我了？"},
            ],
            memory=False,
        )
        answer = await ask("explicit_sender_name_preserved", "我叫示例甲，请按这个称呼和我打个招呼")
        assert "示例甲" in answer, answer
        report = {
            "model": bot.model,
            "reported_model": bot.reported_model,
            "qq_delivery": "captured_no_messages_sent",
            "data": "fictional_only_temporary_stores",
            "cases": cases,
            "passed": True,
        }
        (Path(os.environ["SMOKE_REPORT_DIR"]) / "addressing-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()


if __name__ == "__main__":
    asyncio.run(main())
