"""Explicit live Qzone reads. No write authorization, no raw data in reports."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import aiohttp
from qqbot.transport.onebot import OneBot
from qqbot.transport.qzone import Qzone


async def main():
    async with aiohttp.ClientSession() as session:
        bot = SimpleNamespace(session=session, http_url="http://napcat:3000")
        onebot = OneBot(bot)

        async def read_action(action, params=None):
            if action not in ("get_login_info", "get_cookies", "get_version_info"):
                raise AssertionError("Write actions forbidden")
            return await onebot.action(action, params)

        bot.action = read_action
        qzone = Qzone(bot)
        await qzone.auth()
        own = await qzone.own_list()
        comments = 0
        for item in own[:2]:
            comments += len(await qzone.comment_list(qzone.owner, str(item["tid"])))
        feeds = await qzone.feeds()
        report = {
            "passed": True,
            "version": (await read_action("get_version_info")).get("app_version"),
            "own_posts_count": len(own),
            "details_checked": min(2, len(own)),
            "visible_comments_count": comments,
            "friend_feed_count": len(feeds),
            "write_actions": 0,
            "limits": "No live write tested. Empty friend feed cannot validate real repost fields.",
        }
        target = (
            Path(os.getenv("SMOKE_REPORT_DIR", "evidence/social-2026-10-12"))
            / "social-read-smoke.json"
        )
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
