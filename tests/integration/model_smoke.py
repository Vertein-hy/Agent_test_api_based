"""Real model and bridge, captured QQ delivery. Never connects to OneBot."""

import asyncio
import json
import os
from pathlib import Path
import re
import aiohttp
from qqbot.domain import Message
from qqbot.app import Bot


async def main():
    uid = os.environ["SMOKE_PANEL_UID"]
    character = os.environ.get("SMOKE_PANEL_CHARACTER", "雷神")
    if not re.fullmatch(r"(?:[1-9][0-9]{8}|18[0-9]{8})", uid):
        raise ValueError("Invalid SMOKE_PANEL_UID")
    b = Bot()
    b.plugin_url = "http://plugins:8081"
    message = Message("123", None, f"查询原神 UID {uid} 的{character}面板")
    b.turn.message = message
    captured = []

    async def action(name, params=None):
        if name not in ("send_private_msg", "send_group_msg"):
            raise AssertionError("Unexpected QQ action")
        captured.append((name, params))
        return {"message_id": 1}

    b.action = action
    async with aiohttp.ClientSession() as session:
        b.session = session
        answer = await b.completion(
            [{"role": "system", "content": b.persona}, {"role": "user", "content": message.text}]
        )
        assert any(t["name"] == "yunzai_query" for t in b.last_trace), (
            "Model did not select the tool"
        )
        assert any(
            t["name"] == "yunzai_query"
            and t["arguments"] == {"operation": "profile", "uid": uid, "character": character}
            for t in b.last_trace
        ), "Model did not query the requested profile"
        assert b.panel.images, json.dumps(b.last_trace, ensure_ascii=False)[:1500]
        assert not re.search(r"云崽|喵喵|yunzai", answer, re.I), "Reply exposed internal tool names"
        assert "base64://" not in json.dumps(b.last_trace), "Media leaked to archive trace"
        await b.reply(message, answer)
    assert len(captured) == 1
    images = [p for p in captured[0][1]["message"] if p["type"] == "image"]
    assert len(images) == 1
    report = {
        "verified": True,
        "model": b.model,
        "prompt": message.text,
        "tools": [t["name"] for t in b.last_trace],
        "reply_text": answer,
        "captured_reply_count": len(captured),
        "captured_images": len(images),
        "qq_messages_sent": 0,
    }
    (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "panel-smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))
    if b.memory:
        b.memory.close()
    if b.archive:
        b.archive.close()


asyncio.run(asyncio.wait_for(main(), 180))
