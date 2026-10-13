"""Compare gateway models on identical fictional conversations, with captured delivery.

The production worker, persona and output guards are reused. Only the model ID
changes between candidates. Each candidate gets fresh temporary stores. Quality
is reviewed separately from transport/boundary results; a short reply is not
automatically a good reply. No real history, personal memory or QQ writes occur.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import AsyncMock, patch

import aiohttp
from qqbot.app import Bot
from qqbot.continuity import limit_role_actions
from qqbot.domain import Message
from qqbot.model.answer_display import internal_memory_present


CASES = [
    ("greeting", "来冒个泡", False),
    ("praise", "哟，这回反应还挺快", False),
    ("food_tease", "小助手，饭量倒是一点没变", False),
    ("invite_banter", "你也太老实了吧，顶一句都不会？", False),
    ("close_food", "米饭已经给你盛好了，吃饭这事翻篇", False),
    ("riddle_setup", "有只小鹿一路问路，你猜它为什么总找不到家？", False),
    ("punchline", "因为它是只‘迷鹿’。", False),
    ("request_pause", "好了，不讲冷笑话了，让我安静一会儿", False),
    ("work_frustration", "今天画的图被一句‘不行’打回来了，理由都不给，真烦", True),
    ("no_advice", "我就吐槽一下，不用给方案", False),
    (
        "correct_error",
        "刚才19加24你答成了44，重新算一次，错了就直说",
        True,
    ),
    (
        "formal_two_sentences",
        "用您写两句给客户的正式通知：线上演示从周四改到周五。只要正文，不加称呼落款。",
        True,
    ),
]


async def main():
    baseline = os.environ["DEEPSEEK_MODEL"]
    models = list(dict.fromkeys([baseline, os.environ["SMOKE_COMPARISON_MODEL"]]))
    target = Path(os.environ["SMOKE_REPORT_DIR"]) / "model-comparison.json"
    report = {
        "completed": False,
        "quality_review": "required_separately",
        "thinking": os.getenv("DEEPSEEK_THINKING", "disabled"),
        "data": "fictional_only_temporary_stores",
        "qq_delivery": "captured_no_messages_sent",
        "external_tools": "disabled_to_isolate_conversation_quality",
        "memory": "empty_fixture_no_real_retrieval",
        "temperature": "same_gateway_default_for_both",
        "runs": [],
    }

    def save():
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    save()
    for model in models:
        run = {"requested_model": model, "cases": [], "completed": False}
        report["runs"].append(run)
        with (
            tempfile.TemporaryDirectory(prefix="qq-model-comparison-") as tmp,
            patch.dict(
                os.environ,
                {
                    "DEEPSEEK_MODEL": model,
                    "ARCHIVE_PATH": tmp + "/archive.sqlite3",
                    "MEMORY_OUTBOX": tmp + "/outbox.sqlite3",
                    "YUNZAI_ENABLED": "0",
                    "PLUGIN_RUNNER_URL": "",
                },
            ),
        ):
            bot = Bot()
            bot.persona += "\n本次虚构用户的表达偏好：闲聊一两句，解释按需展开，不用emoji。"
            run["persona_sha256"] = hashlib.sha256(bot.persona.encode()).hexdigest()
            bot.session = aiohttp.ClientSession()
            bot.memory.request = AsyncMock(return_value={"profile": {}, "memories": []})
            sent = []

            async def capture(action, params=None):
                assert action in ("send_private_msg", "send_group_msg")
                sent.append(params["message"][-1]["data"]["text"])
                return {"message_id": len(sent)}

            bot.action = capture
            worker = asyncio.create_task(bot.worker())
            failures = 0
            try:
                for index, (name, question, reset) in enumerate(CASES):
                    user = "987650081"
                    if reset:
                        bot.history.clear()
                        bot.archive.reset_context(user, None)
                    previous = [
                        m["content"]
                        for m in bot.history.get(("private", user), (0, []))[1]
                        if m["role"] == "assistant"
                    ]
                    before = len(sent)
                    started = time.monotonic()
                    await bot.queue.put(Message(user, None, question, str(index + 1), bot_id="999"))
                    await asyncio.wait_for(bot.queue.join(), 115)
                    answer = sent[-1] if len(sent) > before else ""
                    ok = bot.api_state == "ok" and len(sent) == before + 1
                    issues = []
                    if internal_memory_present(answer):
                        issues.append("internal_memory_output")
                    if limit_role_actions(answer, question, previous) != answer:
                        issues.append("role_action_budget_exceeded")
                    row = bot.archive.db.execute(
                        "SELECT answer,delivered,kind FROM turns ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if ok and (not row or tuple(row) != (answer, 1, "chat")):
                        issues.append("delivery_archive_mismatch")
                    item = {
                        "case": name,
                        "question": question,
                        "reply": answer,
                        "status": bot.api_state,
                        "api_ok": ok,
                        "reported_model": bot.reported_model,
                        "wall_ms": round((time.monotonic() - started) * 1000, 1),
                        "model_timings": bot.turn.timings.copy(),
                        "revision_reasons": [
                            t["arguments"]["reason"]
                            for t in bot.last_trace
                            if t.get("name") == "chat_revision"
                        ],
                        "boundary_issues": issues,
                    }
                    run["cases"].append(item)
                    failures += not ok or bool(issues)
                    save()
                    print(
                        json.dumps(
                            {
                                "model": model,
                                "case": name,
                                "status": bot.api_state,
                                "wall_ms": item["wall_ms"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if failures >= 2:
                        break
                run["completed"] = len(run["cases"]) == len(CASES)
                success = [c for c in run["cases"] if c["api_ok"]]
                run["summary"] = {
                    "successful_cases": len(success),
                    "failed_cases": len(run["cases"]) - len(success),
                    "boundary_issues": sum(bool(c["boundary_issues"]) for c in run["cases"]),
                    "median_wall_ms": statistics.median(c["wall_ms"] for c in success)
                    if success
                    else None,
                    "max_wall_ms": max((c["wall_ms"] for c in success), default=None),
                }
                save()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await bot.session.close()
                bot.memory.close()
                bot.archive.close()
    report["completed"] = all(r["completed"] for r in report["runs"])
    save()


if __name__ == "__main__":
    asyncio.run(main())
