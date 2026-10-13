"""Explicit API-only test on one self-visible disposable post, with cleanup.

No model, contacts, chat messages, existing-post writes or production journal.
Only a nonce-bearing fixture may be published, always ugc_right=64. All subsequent
writes are tied to that exact returned post. An uncertain write is not retried.
"""

import asyncio
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import uuid
import aiohttp
from qqbot.transport.onebot import OneBot
from qqbot.transport.qzone import Qzone


async def main():
    report = {
        "passed": False,
        "only_self": True,
        "model_calls": 0,
        "qq_contact_messages": 0,
        "cleanup_confirmed": False,
    }
    target = (
        Path(os.getenv("SMOKE_REPORT_DIR", "evidence/social-2026-10-12")) / "social-self-smoke.json"
    )
    nonce = uuid.uuid4().hex
    fixture = "接口验证（仅自己可见，验证后删除）：" + nonce
    started = time.time()
    tid = None
    async with aiohttp.ClientSession() as session:
        bot = SimpleNamespace(session=session, http_url="http://napcat:3000")
        onebot = OneBot(bot)

        async def limited_action(action, params=None):
            if action not in ("get_login_info", "get_cookies", "delete_qzone_msg"):
                raise AssertionError("Contact actions forbidden")
            if action == "delete_qzone_msg" and (not tid or params != {"tid": tid}):
                raise AssertionError("Unrelated deletion forbidden")
            return await onebot.action(action, params)

        bot.action = limited_action
        q = Qzone(bot)
        q.authorize_write = lambda: True
        try:
            tid = await q.publish(fixture, 64)
            detail = await q.detail(q.owner, tid)
            report["visibility_confirmed"] = int(detail.get("ugc_right", 0)) == 64
            assert report["visibility_confirmed"], "Self visibility not confirmed"
            await q.comment(q.owner, tid, "虚构评论样本：你好。")
            values = await q.comment_list(q.owner, tid)
            root = next(
                c
                for c in values
                if c["author"] == q.owner
                and c["text"] == "虚构评论样本：你好。"
                and not c["parent"]
            )
            await q.comment(q.owner, tid, "虚构回复样本：收到。", root)
            values = await q.comment_list(q.owner, tid)
            report["root_comment_confirmed"] = True
            report["threaded_reply_confirmed"] = any(
                c["author"] == q.owner
                and c["text"] == "虚构回复样本：收到。"
                and c["parent"] == root["id"]
                for c in values
            )
            assert report["threaded_reply_confirmed"], "Threaded reply not confirmed"
            report["passed"] = True
        except Exception as error:
            report["error_category"] = type(error).__name__
        finally:
            if not tid:
                own = await q.own_list()
                matches = [
                    x
                    for x in own
                    if x.get("content") == fixture
                    and float(x.get("created_time") or 0) >= started - 5
                ]
                if len(matches) == 1:
                    tid = str(matches[0]["tid"])
            if tid:
                try:
                    await limited_action("delete_qzone_msg", {"tid": tid})
                    report["cleanup_confirmed"] = not any(
                        str(x.get("tid")) == tid for x in await q.own_list()
                    )
                except Exception as error:
                    report["cleanup_error_category"] = type(error).__name__
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(report, ensure_ascii=False))
    assert report["passed"] and report["cleanup_confirmed"], (
        "Self-only API probe failed; inspect metadata report"
    )


if __name__ == "__main__":
    asyncio.run(main())
