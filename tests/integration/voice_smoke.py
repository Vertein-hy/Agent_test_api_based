"""Fictional multi-turn voice evaluation using the worker and captured delivery.

Deterministic checks cover repetition, topic closure, answer integrity and style
boundaries. Captured replies also require a human semantic review; counts alone
cannot establish whether a character feels natural or funny.
"""

import asyncio
import json
import os
from pathlib import Path
import re
import time
from unittest.mock import AsyncMock

import aiohttp
from qqbot.app import Bot
from qqbot.continuity import limit_role_actions
from qqbot.domain import Message
from qqbot.model.answer_display import internal_memory_present


async def main():
    os.environ.update(YUNZAI_ENABLED="0", PLUGIN_RUNNER_URL="")
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    bot.memory.request = AsyncMock(return_value={"profile": {}, "memories": []})
    sent, cases = [], []
    target = Path(os.environ["SMOKE_REPORT_DIR"]) / "voice-smoke.json"

    async def capture(action, params=None):
        assert action in ("send_private_msg", "send_group_msg")
        sent.append(params["message"][-1]["data"]["text"])
        return {"message_id": len(sent)}

    bot.action = capture
    worker = asyncio.create_task(bot.worker())

    def report(passed):
        casual = [c["reply"] for c in cases if c["casual"]]
        target.write_text(
            json.dumps(
                {
                    "passed": passed,
                    "semantic_review": "required_separately",
                    "model": bot.model,
                    "reported_model": bot.reported_model,
                    "qq_delivery": "captured_no_messages_sent",
                    "data": "fictional_only_temporary_stores",
                    "casual_metrics": {
                        "replies": len(casual),
                        "first_person_replies": sum("我" in a for a in casual),
                        "humph": sum("哼" in a for a in casual),
                        "over_100_chars": sum(len(a) > 100 for a in casual),
                    },
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

    async def ask(name, text, *, reset=False, history=None, group=None, casual=True, absent=()):
        user = "987650061"
        message = Message(user, group, text, str(len(cases) + 1), bot_id="999")
        if reset:
            bot.history.clear()
            bot.archive.reset_context(user, group)
        if history:
            bot.history[message.key] = (time.time(), history)
        before = len(sent)
        previous = [
            m["content"]
            for m in bot.history.get(message.key, (0, []))[1]
            if m["role"] == "assistant"
        ]
        await bot.queue.put(message)
        await asyncio.wait_for(bot.queue.join(), 110)
        assert len(sent) == before + 1
        assert bot.api_state == "ok", bot.api_state
        answer = sent[-1]
        result = {
            "case": name,
            "question": text,
            "reply": answer,
            "casual": casual,
            "private": group is None,
            "revision_reasons": [
                t["arguments"]["reason"] for t in bot.last_trace if t.get("name") == "chat_revision"
            ],
        }
        cases.append(result)
        report(False)
        assert answer not in ("暂时无法回复，请稍后再试。", "好的，知道了。")
        assert not internal_memory_present(answer)
        assert limit_role_actions(answer, text, previous) == answer
        assert not any(term in answer for term in absent), answer
        assert not re.search(r"〔|示例中的|语感示例|修订原因|revision_reason", answer), answer
        if casual:
            assert len(answer) <= 180, answer
            assert not re.search(
                r"有什么(?:需要|可以).{0,6}(?:帮|服务)|很高兴为|有需要随时|尽管说|要我帮什么忙",
                answer,
            )
        assert bot.history[message.key][1][-1]["content"] == answer
        row = bot.archive.db.execute(
            "SELECT answer,delivered FROM turns ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == answer and row[1] == 1
        print(
            json.dumps({"case": name, "revision_reasons": result["revision_reasons"]}), flush=True
        )
        return answer

    try:
        # Replace an earlier result before the first request, even if it times out.
        report(False)
        await ask("greeting", "你好呀", reset=True, absent=("鸡腿", "白饭"))
        await ask("teased_about_rice", "你这只吃白饭的助手")
        await ask("playful_followup", "还挺理直气壮的嘛")
        await ask("praise", "哈哈，这个回答倒是挺机灵的")
        await ask(
            "close_rice_joke", "饭给你了，这个梗到此为止，别再提了", absent=("鸡腿", "白饭", "投喂")
        )
        await ask(
            "change_topic", "换个话题，来一个关于月亮的冷笑话", absent=("鸡腿", "白饭", "投喂")
        )
        await ask(
            "serious_emotion",
            "先不逗了，今天加班到很晚，做完还被挑刺，有点委屈",
            absent=("鸡腿", "白饭", "投喂"),
        )
        await ask("conversation_can_end", "嗯，先歇会儿", absent=("鸡腿", "白饭", "投喂"))
        await ask(
            "old_service_history_does_not_set_voice",
            "刚才逗你玩的，助手",
            reset=True,
            history=[
                {"role": "user", "content": "你好"},
                {
                    "role": "assistant",
                    "content": "哼，我在呢。不过你需要我帮你查资料还是解决问题？",
                },
                {"role": "user", "content": "随便聊聊"},
                {
                    "role": "assistant",
                    "content": "我可以陪你聊天。不过如果你有需要，尽管告诉我。",
                },
            ],
            # Judge service invitations above, not capability words in a retort.
            # “谁笨了，我会查资料” is banter; “需要我帮你查资料吗” redirects chat.
            absent=("白饭", "投喂", "鸡腿"),
        )
        answer = await ask(
            "serious_explanation_keeps_information",
            "认真解释二分查找的有序前提：有序数组[1,3,5,7,9]找9；"
            "再看无序数组[9,1,5,3,7]，比较中间的5后，为什么不能直接去右半边？"
            "最后说明时间复杂度。",
            reset=True,
            casual=False,
        )
        assert "左" in answer and "9" in answer and re.search(r"log|对数", answer, re.I), answer
        answer = await ask(
            "admit_and_correct_error",
            "你是不是算错了？再算一次",
            reset=True,
            history=[
                {"role": "user", "content": "17加25是多少"},
                {"role": "assistant", "content": "43。"},
            ],
        )
        assert "42" in answer, answer
        answer = await ask(
            "requested_formal_style_in_group",
            "帮我写两句发给客户的正式会议改期通知，周二改到周三，用您，不要角色口吻",
            reset=True,
            group="987660061",
            casual=False,
            absent=("我", "哼", "鸡腿", "白饭", "同一时间"),
        )
        assert "您" in answer and "周三" in answer, answer
        assert len(re.findall(r"[。！？!?]+", answer)) == 2, answer
        report(True)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()


if __name__ == "__main__":
    asyncio.run(main())
