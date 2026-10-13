"""Real extraction + dossiers for fictional people only; never sends QQ messages."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import aiohttp


async def main():
    root = "http://127.0.0.1:8083"
    token = Path(os.environ["MEMORY_TOKEN_FILE"]).read_text().strip()
    actors = [
        hashlib.sha256(("fictional-person-v2-" + uuid.uuid4().hex).encode()).hexdigest()
        for _ in range(2)
    ]
    scope, private = (
        hashlib.sha256(s.encode()).hexdigest()
        for s in ("fictional-group-v2", "fictional-private-v2")
    )
    report = {"qq_delivery": "no_QQ_connection"}
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + token}) as session:

        async def call(path, body):
            async with session.post(
                root + "/" + path, json=body, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                result = await r.json()
                assert r.status == 200, (path, r.status, result)
                return result

        def ident(actor, s=scope, gen=1):
            return {"actor": actor, "scope": s, "generation": gen}

        async def health():
            async with session.get(root + "/healthz") as r:
                return await r.json()

        async def wait(actor):
            async with asyncio.timeout(180):
                while True:
                    status = await call("status", ident(actor))
                    if status["pending"] == 0:
                        return status
                    await asyncio.sleep(2)

        for actor in actors:
            await call(
                "control", {"actor": actor, "generation": 1, "enabled": True, "forget": True}
            )
        try:
            for actor, question in zip(
                actors,
                (
                    "请记住：以后叫我测试乙。我喜欢摄影，希望你平时回答简短一点。",
                    "请记住：以后叫我测试戊。我喜欢游泳，希望你平时回答详细一点。",
                ),
            ):
                event = {
                    "id": uuid.uuid4().hex * 2,
                    "created": time.time(),
                    "question": question,
                    "answer": "好，我记住了。",
                    "display_name": "同名昵称",
                }
                await call("ingest", {**ident(actor), "event": event})
            states = [await wait(a) for a in actors]
            for state, name, interest in zip(states, ("测试乙", "测试戊"), ("摄影", "游泳")):
                profile = state["profile"]
                assert profile["names"][0]["value"] == name, profile
                assert interest in json.dumps(profile, ensure_ascii=False), profile
            for actor, mine, other in zip(actors, ("测试乙", "测试戊"), ("测试戊", "测试乙")):
                result = await call(
                    "recall",
                    {**ident(actor), "query": "你认识我吗，我希望你怎么称呼我，我喜欢什么？"},
                )
                content = json.dumps(result, ensure_ascii=False)
                assert mine in content and other not in content
            before = (await health())["neural_calls"]
            start = time.perf_counter()
            greeting = await call("recall", {**ident(actors[0]), "query": "晚上好"})
            report["social_profile_seconds"] = round(time.perf_counter() - start, 4)
            assert greeting["ranking"] == "profile_only" and not greeting["memories"]
            assert (await health())["neural_calls"] == before
            private_result = await call(
                "recall", {**ident(actors[0], private), "query": "你认识我吗"}
            )
            assert not private_result["profile"] and not private_result["memories"]
            panel = await call(
                "recall", {**ident(actors[0]), "query": "查询原神 UID 700000001 的雷神面板"}
            )
            assert panel["skipped"] == "panel" and "profile" not in panel
            await call(
                "ingest",
                {
                    **ident(actors[0]),
                    "event": {
                        "id": uuid.uuid4().hex * 2,
                        "created": time.time(),
                        "question": "谢谢你",
                        "answer": "不客气。",
                        "display_name": "换过昵称",
                    },
                },
            )
            renamed = (await wait(actors[0]))["profile"]
            assert renamed["platform_name"] == "换过昵称"
            assert renamed["names"][0]["value"] == "测试乙"
            report.update(
                {
                    "same_nickname_different_people": True,
                    "preferred_names_extracted": True,
                    "preferences_reach_profile": True,
                    "rename_keeps_identity": True,
                    "social_no_encoder_or_neural": True,
                    "private_group_isolation": True,
                    "panel_excluded": True,
                }
            )
        finally:
            for actor in actors:
                await call(
                    "control", {"actor": actor, "generation": 100, "enabled": False, "forget": True}
                )
                state = await call("status", ident(actor, gen=100))
                assert state["counts"] == {} and state["profile"] == {}
            report["fictional_people_deleted"] = True
            (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "person-memory-smoke.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
