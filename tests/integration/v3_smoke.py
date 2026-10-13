"""Real extraction + CPU neural recall, no QQ action; fictional accounts only."""

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
import aiohttp


async def main():
    token = Path(os.environ["MEMORY_TOKEN_FILE"]).read_text().strip()
    actors = [
        hashlib.sha256(("v3-fake-" + uuid.uuid4().hex).encode()).hexdigest() for _ in range(2)
    ]
    private, group, othergroup = [
        hashlib.sha256(("v3-" + str(i)).encode()).hexdigest() for i in range(3)
    ]
    report = {"qq_actions": 0, "fictional_only": True}
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + token}) as session:

        async def call(path, body):
            async with session.post(
                "http://127.0.0.1:8083/" + path, json=body, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                data = await r.json()
                assert r.status == 200, (path, r.status, data)
                return data

        def ident(actor, scope=private, priv=True, gen=1):
            return dict(actor=actor, scope=scope, generation=gen, private=priv)

        async def event(actor, text, scope=private, priv=True):
            e = dict(
                id=uuid.uuid4().hex * 2,
                created=time.time(),
                question=text,
                answer="知道了。",
                display_name="同名昵称",
                private=priv,
            )
            await call("ingest", {**ident(actor, scope, priv), "event": e})
            async with asyncio.timeout(240):
                while True:
                    s = await call("status", ident(actor, scope, priv))
                    if s["pending"] == 0:
                        return s
                    await asyncio.sleep(2)

        for a in actors:
            await call("control", dict(actor=a, generation=1, enabled=True, forget=True))
        try:
            await event(
                actors[0],
                "以后请叫我测试乙，我现在住在成都。我希望回答简短些，打招呼的时候说：嗨🎶想我了吗。我计划学习游泳。",
            )
            await event(actors[1], "以后请叫我测试戊，我现在住在苏州。")
            r = await call("recall", {**ident(actors[0], group, False), "query": "来打个招呼"})
            text = json.dumps(r, ensure_ascii=False)
            assert "测试乙" in text and "嗨" in text, text
            assert "成都" not in text and "测试戊" not in text, text
            assert r["ranking"] == "profile_only"
            r = await call("recall", {**ident(actors[1], group, False), "query": "你好"})
            assert "测试戊" in json.dumps(r, ensure_ascii=False) and "测试乙" not in json.dumps(
                r, ensure_ascii=False
            )
            await event(actors[0], "我喜欢摄影，正在研究图记忆网络。", group, False)
            r = await call(
                "recall", {**ident(actors[0]), "query": "还记得我在做什么项目，住哪里吗？"}
            )
            assert r["memories"] and r["recall_id"], r
            text = json.dumps(r, ensure_ascii=False)
            assert "成都" in text and "摄影" in text, text
            f = await call(
                "feedback", {**ident(actors[0]), "recall_id": r["recall_id"], "useful": True}
            )
            assert f["ok"], f
            assert not (
                await call(
                    "feedback", {**ident(actors[1]), "recall_id": r["recall_id"], "useful": True}
                )
            )["ok"]
            await event(actors[0], "我已经不喜欢摄影了，学习游泳的计划已经完成了。")
            s = await call("status", ident(actors[0]))
            p = json.dumps(s["profile"], ensure_ascii=False)
            assert "学习游泳" not in p and "摄影" not in p, p
            r = await call(
                "recall", {**ident(actors[0], othergroup, False), "query": "你还记得我住在哪吗？"}
            )
            assert "成都" not in json.dumps(r, ensure_ascii=False), r
            r = await call(
                "recall", {**ident(actors[0]), "query": "查询原神 UID 700000001 的雷神面板"}
            )
            assert r["skipped"] == "panel" and "profile" not in r
            report.update(
                cross_context_name_and_greeting=True,
                private_details_not_disclosed=True,
                other_account_isolation=True,
                real_neural_recall=True,
                explicit_feedback=True,
                natural_retraction_and_completion=True,
                panel_excluded=True,
            )
        finally:
            for a in actors:
                await call("control", dict(actor=a, generation=2, enabled=False, forget=True))
                assert not (await call("status", ident(a, gen=2)))["profile"]
    (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "v3-smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report))


asyncio.run(main())
