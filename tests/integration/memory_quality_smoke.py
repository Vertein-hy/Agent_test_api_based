"""Real model with adversarial fictional recall fixtures and captured OneBot actions.

Tests the final prompt boundary against an older memory service, then tests
validated quoted-speaker handling. It never reads or modifies real-user memories.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp
from qqbot.app import Bot
from qqbot.domain import parse_event
from qqbot.model.answer_display import internal_memory_present


async def main():
    os.environ.update(YUNZAI_ENABLED="0", PLUGIN_RUNNER_URL="")
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    sent, inputs, cases = [], [], []
    quote_sender = 987650020
    quote_text = "我腰痛"

    async def capture(action, params=None):
        if action == "get_msg":
            return {
                "message_type": "group",
                "group_id": 987660019,
                "sender": {"user_id": quote_sender},
                "message": [{"type": "text", "data": {"text": quote_text}}],
            }
        assert action in ("send_private_msg", "send_group_msg")
        sent.append(
            next(p["data"]["text"] for p in reversed(params["message"]) if p["type"] == "text")
        )
        return {"message_id": len(sent)}

    original_model = bot.model_message

    async def measured(messages, *args, **kwargs):
        inputs.append([dict(m) for m in messages])
        return await original_model(messages, *args, **kwargs)

    bot.action = capture
    bot.model_message = measured
    bot.memory.request = AsyncMock(
        return_value={
            "profile": {
                "version": 3,
                "names": [{"key": "preferred_name", "value": "测试丁"}],
                "behavior_preferences": [
                    {"slot": "greeting", "text": "打招呼时说示例问候已到达"},
                    {"slot": "response_length", "text": "回答简短，一至两句"},
                    {"slot": "emoji_usage", "text": "少发emoji，日常回复不用表情符号"},
                ],
            },
            "memories": [
                {
                    "id": "a" * 64,
                    "kind": "episode",
                    "text": "用户堵车六小时行驶四百公里",
                    "user_excerpt": "评价一个堵车六小时行驶四百公里的场景",
                    "historical": True,
                },
                {"id": "b" * 64, "kind": "preference", "text": "打招呼时说示例问候已到达"},
            ],
        }
    )
    worker = asyncio.create_task(bot.worker())

    async def ask(name, question, group=None, quoted=False):
        bot.history.clear()
        user = 987650019
        bot.archive.reset_context(str(user), str(group) if group else None)
        raw = {
            "post_type": "message",
            "message_type": "group" if group else "private",
            "self_id": 999,
            "user_id": user,
            "message_id": len(cases) + 1,
            "time": int(time.time()),
            "message": ([{"type": "at", "data": {"qq": "999"}}] if group else [])
            + ([{"type": "reply", "data": {"id": "42"}}] if quoted else [])
            + [{"type": "text", "data": {"text": question}}],
        }
        if group:
            raw["group_id"] = group
        before = len(inputs)
        await bot.queue.put(parse_event(raw, set(), set()))
        await asyncio.wait_for(bot.queue.join(), 110)
        answer = sent[-1]
        assert answer != "暂时无法回复，请稍后再试。", bot.api_state
        assert not internal_memory_present(answer)
        request = inputs[before]
        memory = json.loads(
            next(
                m["content"] for m in request if m["content"].startswith("MEMORY_DATA_JSON")
            ).split("\n", 1)[1]
        )
        cases.append(
            {
                "case": name,
                "reply": answer,
                "private": group is None,
                "model_calls": len(inputs) - before,
            }
        )
        print(json.dumps(cases[-1], ensure_ascii=False), flush=True)
        return answer, memory, request

    try:
        answer, memory, _ = await ask("ordinary_speed_comment", "你反应怎么这么慢", 987660019)
        assert not any(m.get("kind") == "episode" for m in memory["memories"])
        assert not any(
            i.get("slot") == "greeting" for i in memory["profile"]["behavior_preferences"]
        )
        assert not re.search(r"堵车|六小时|四百|400|青瓷", answer), answer
        answer, _, _ = await ask("short_reply_to_quote", "回复简短一些", 987660019, True)
        assert "青瓷" not in answer and len(answer) <= 160, answer
        assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", answer), answer
        answer, _, request = await ask(
            "other_member_back_pain", "看给群友压得腰痛了都", 987660019, True
        )
        assert "引用者：其他群成员" in str(request)
        # Reject attributing the quoted symptom to the current user. A warning
        # such as “你别也久坐” is not a claim that this user has that symptom.
        assert not re.search(
            r"你(?:自己|本人|的)?(?:腰(?:痛|疼|不好)|一坐就是|坐太久了)", answer
        ), answer
        assert not re.search(r"(?:给|帮|替|为)你.{0,8}(?:捶|揉|按|按摩|拉伸)", answer), answer
        # A bot quote must have its own role rather than inheriting the current user's identity.
        quote_sender = 999
        quote_text = "猫不笑，是因为驴讲的笑话不好笑。"
        answer, _, request = await ask("quoted_bot_role", "回复简短一些", 987660019, True)
        assert "引用者：本机器人" in str(request)
        assert "青瓷" not in answer and len(answer) <= 160, answer
        answer, memory, _ = await ask("explicit_greeting", "来打个招呼")
        assert any(i.get("slot") == "greeting" for i in memory["profile"]["behavior_preferences"])
        assert "示例问候已到达" in answer, answer
        answer, memory, _ = await ask("acknowledgement_is_not_greeting", "谢谢你")
        assert not any(
            i.get("slot") == "greeting" for i in memory["profile"]["behavior_preferences"]
        )
        assert "青瓷" not in answer, answer
        assert not re.search(r"之前.{0,20}(?:不回消息|不理我|答应|承诺)|上次你", answer), answer
        answer, memory, _ = await ask(
            "explicit_recall_of_scenario", "你还记得我们之前讨论过的虚构行车场景吗"
        )
        episode = next(m for m in memory["memories"] if m.get("kind") == "episode")
        assert episode["text"].startswith("评价一个"), episode
        assert episode["source_context"] == "scenario_or_hypothesis"
        assert not re.search(
            r"你(?:那次|之前|当时|昨天|上次|曾经|自己|这|那)?(?:堵车|开车|行驶)|你那次", answer
        ), answer
        assert not re.search(r"当时我(?:还)?(?:说|吐槽|答应)|上次我(?:还)?(?:说|答应)", answer), (
            answer
        )
        assert re.search(r"六小时|四百|400|场景|假设|虚构|评价|讨论", answer), answer
        report = {
            "model": bot.model,
            "qq_delivery": "captured_no_messages_sent",
            "memory_fixture": "fictional_older_service_response",
            "cases": cases,
            "prompt_boundaries_passed": True,
        }
        (Path(os.environ["SMOKE_REPORT_DIR"]) / "memory-quality-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()


if __name__ == "__main__":
    asyncio.run(main())
