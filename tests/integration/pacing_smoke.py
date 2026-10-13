"""Check the real model's pacing with a fictional dossier; no delivery or ingestion."""

import asyncio
import json
import os
from pathlib import Path
import uuid
from unittest.mock import AsyncMock
import aiohttp
from qqbot.domain import Message
from qqbot.app import Bot


async def main():
    os.environ["ARCHIVE_SALT_FILE"] = ""
    os.environ["ARCHIVE_SALT"] = "fictional-pacing-" + uuid.uuid4().hex
    bot = Bot()
    bot.session = aiohttp.ClientSession()
    bot.action = AsyncMock(side_effect=AssertionError("QQ actions prohibited"))
    bot.memory.request = AsyncMock(
        return_value={
            "memories": [],
            "profile": {
                "version": 2,
                "names": [
                    {
                        "key": "preferred_name",
                        "value": "测试乙",
                        "evidence": "以后叫我测试乙",
                        "source": "a" * 64,
                        "recorded_at": 1,
                    }
                ],
                "behavior_preferences": [],
            },
        }
    )
    answers = []
    try:
        for text in ("晚上好", "你还记得我希望你怎么称呼我吗？"):
            message = Message("987650003", None, text)
            await bot.quoted_context(message)
            for attempt in range(3):
                try:
                    answer = await bot.completion(
                        [
                            {"role": "system", "content": bot.persona},
                            {"role": "user", "content": text},
                        ]
                    )
                    break
                except RuntimeError:
                    if bot.api_state not in ("http_429", "http_502") or attempt == 2:
                        raise
                    await asyncio.sleep(30)
            assert "测试乙" in answer, answer
            assert len(answer) <= 100, answer
            assert not any(t in answer for t in ("炸鸡", "夜宵", "上次", "memory")), answer
            answers.append({"question": text, "answer": answer, "characters": len(answer)})
        bot.action.assert_not_called()
        assert bot.memory.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        report = {"qq_delivery": "no_actions", "no_ingestion": True, "actual_model_pacing": answers}
        (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "pacing-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await bot.session.close()
        bot.memory.close()
        bot.archive.close()


if __name__ == "__main__":
    asyncio.run(main())
