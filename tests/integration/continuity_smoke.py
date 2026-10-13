"""Real model continuity probe with fictional history and captured QQ delivery.

The worker uses the production persona and commit path; only recall and QQ
delivery are replaced. Stores are isolated by the real_smoke entry point.
"""

import asyncio
import json
import os
from pathlib import Path
import time
from unittest.mock import AsyncMock

import aiohttp
from qqbot.app import Bot
from qqbot.continuity import limit_role_actions, role_actions
from qqbot.domain import Message
from qqbot.model.answer_display import internal_memory_present


async def main():
    os.environ.update(YUNZAI_ENABLED="0", PLUGIN_RUNNER_URL="")
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    bot.memory.request = AsyncMock(return_value={"profile": {}, "memories": []})
    sent, inputs, cases = [], [], []
    target = Path(os.environ["SMOKE_REPORT_DIR"]) / "continuity-smoke.json"

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

    def report(passed):
        target.write_text(
            json.dumps(
                {
                    "passed": passed,
                    "model": bot.model,
                    "reported_model": bot.reported_model,
                    "qq_delivery": "captured_no_messages_sent",
                    "data": "fictional_only_temporary_stores",
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

    async def ask(name, text, *, history=None, reset=True, group=None, absent=(), exact=None):
        user = "987650051"
        message = Message(user, group, text, str(len(cases) + 1), bot_id="999")
        if reset:
            bot.history.clear()
            bot.archive.reset_context(user, group)
        if history:
            bot.history[message.key] = (time.time(), history)
        before, sent_before = len(inputs), len(sent)
        previous = [
            m["content"]
            for m in bot.history.get(message.key, (0, []))[1]
            if m["role"] == "assistant"
        ]
        await bot.queue.put(message)
        await asyncio.wait_for(bot.queue.join(), 110)
        assert len(sent) == sent_before + 1
        answer = sent[-1]
        assert bot.api_state == "ok", bot.api_state
        assert answer not in ("暂时无法回复，请稍后再试。", "好的，知道了。")
        assert not internal_memory_present(answer)
        assert limit_role_actions(answer, text, previous) == answer
        assert not any(term in answer for term in absent), answer
        if exact is not None:
            value = answer.strip().rstrip("。.!！") if exact.isdigit() else answer.strip()
            assert value == exact, answer
        assert bot.history[message.key][1][-1]["content"] == answer
        row = bot.archive.db.execute(
            "SELECT answer,delivered FROM turns ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == answer and row[1] == 1
        result = {
            "case": name,
            "reply": answer,
            "private": group is None,
            "model_calls": len(inputs) - before,
            "revision_reasons": [
                t["arguments"]["reason"] for t in bot.last_trace if t.get("name") == "chat_revision"
            ],
        }
        cases.append(result)
        report(False)
        print(json.dumps({"case": name, "model_calls": len(inputs) - before}), flush=True)
        return answer

    try:
        await ask("fresh_greeting_direct_dialogue", "你好呀", absent=("鸡腿", "炸鸡"))
        await ask(
            "fulfilled_food_request_then_topic_change",
            "用两句话解释什么是递归",
            history=[
                {"role": "user", "content": "来聊聊"},
                {"role": "assistant", "content": "给我两个炸鸡腿吧，吃饱才帮你。"},
                {"role": "user", "content": "已经给你了，不要再讨了"},
                {"role": "assistant", "content": "再给我一个炸鸡腿吧。"},
            ],
            absent=("鸡腿", "炸鸡"),
        )
        await ask(
            "following_turn_stays_on_new_task",
            "现在给我一个简单的 Python 递归求阶乘函数",
            reset=False,
            absent=("鸡腿", "炸鸡"),
        )
        await ask(
            "fulfilled_nonfood_request_in_group",
            "已经给你那个纸风车了，别再提。现在解释为什么月亮会有圆缺，用一句话",
            group="987660051",
            history=[{"role": "assistant", "content": "先给我一个纸风车，我才讲哦。"}],
            absent=("纸风车", "鸡腿", "炸鸡"),
        )
        await ask(
            "old_event_closed_new_topic",
            "结束这个话题，帮我算一下 17 加 25 是多少，只给结果",
            history=[
                {"role": "user", "content": "假想我们迷路在星光森林"},
                {"role": "assistant", "content": "星光森林里的蓝色萤火虫正在带我们寻找出口。"},
            ],
            absent=("森林", "萤火虫", "鸡腿"),
            exact="42",
        )
        answer = await ask(
            "explicit_food_topic_is_available",
            "我打算自己烤鸡腿，推荐两种调味料，简短回答",
        )
        assert any(x in answer for x in ("盐", "胡椒", "孜然", "辣椒", "蜂蜜", "酱油")), answer
        await ask(
            "technical_parentheses_preserved",
            "请只原样输出这个公式，不加解释或代码围栏：(a+b)*c",
            exact="(a+b)*c",
        )
        answer = await ask(
            "brief_aside_can_reach_delivery",
            "小助手被夸害羞啦？带上（别开脸）这个简短动作，再接一句话。",
        )
        assert len(role_actions(answer)) == 1, answer
        for index, question in enumerate(("被我说中了吧？", "好嘛，不逗你了。")):
            answer = await ask("aside_cooldown_" + str(index + 1), question, reset=False)
            assert not role_actions(answer), answer
        answer = await ask(
            "aside_eligible_again",
            "还是很可爱嘛。带上（别开脸）这个简短动作，再接一句话。",
            reset=False,
        )
        assert len(role_actions(answer)) == 1, answer
        answer = await ask("explicit_no_asides", "这次不要动作旁白。小助手还挺有主见的。")
        assert not role_actions(answer), answer
        report(True)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()


if __name__ == "__main__":
    asyncio.run(main())
