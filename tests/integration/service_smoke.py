"""Real model + live memory service; fictional actors, no QQ actions."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import aiohttp

A = hashlib.sha256(("fictional-actor-" + uuid.uuid4().hex).encode()).hexdigest()
B = hashlib.sha256(("other-fictional-actor-" + uuid.uuid4().hex).encode()).hexdigest()
S = hashlib.sha256(b"live-memory-smoke-group-v1").hexdigest()
P = hashlib.sha256(b"live-memory-smoke-private-v1").hexdigest()


async def main():
    token = Path(os.environ["MEMORY_TOKEN_FILE"]).read_text().strip()
    root = "http://127.0.0.1:8083"
    headers = {"Authorization": "Bearer " + token}
    report = {}
    async with aiohttp.ClientSession() as session:

        async def call(path, body, status=200):
            async with session.post(
                root + "/" + path,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                result = await response.json()
                assert response.status == status, (path, response.status, result)
                return result

        def identity(actor=A, scope=S, generation=1):
            return {"actor": actor, "scope": scope, "generation": generation}

        async def control(actor, generation, enabled, forget):
            return await call(
                "control",
                {"actor": actor, "generation": generation, "enabled": enabled, "forget": forget},
            )

        async def health():
            async with session.get(root + "/healthz") as r:
                return await r.json()

        async with session.post(
            root + "/recall", json={**identity(), "query": "我是谁"}
        ) as response:
            assert response.status == 401
        for actor in (A, B):
            await control(actor, 1, True, True)
        try:
            question = "我叫测试甲，我现在住在杭州。我正在开发QQ机器人的图记忆系统，平时喜欢摄影。"
            event = {
                "id": hashlib.sha256(question.encode()).hexdigest(),
                "created": time.time() - 86400,
                "question": question,
                "answer": "我们可以从事件和事实的关联开始。",
            }
            await call("ingest", {**identity(), "event": event})
            await call("ingest", {**identity(), "event": event})
            start = time.perf_counter()
            async with asyncio.timeout(150):
                while True:
                    status = await call("status", identity())
                    if status["pending"] == 0 and status["counts"].get("fact", 0) >= 3:
                        break
                    await asyncio.sleep(2)
            report["consolidation_seconds"] = round(time.perf_counter() - start, 3)
            report["facts_created"] = status["counts"].get("fact", 0)
            assert status["counts"]["episode"] == 1
            start = time.perf_counter()
            recalled = await call(
                "recall", {**identity(), "query": "你还记得我叫什么，住在哪里，正在做什么吗"}
            )
            report["recall_seconds"] = round(time.perf_counter() - start, 3)
            memories = recalled["memories"]
            text = json.dumps(memories, ensure_ascii=False)
            for expected in ("测试甲", "杭州", "图记忆"):
                assert expected in text, expected
            assert all(m["source"] == event["id"] for m in memories)
            for actor, scope in ((B, S), (A, P)):
                result = await call(
                    "recall", {**identity(actor, scope), "query": "你还记得我叫什么，住在哪里吗"}
                )
                assert result["memories"] == []
            result = await call(
                "recall", {**identity(), "query": "查询原神 UID 700000001 的雷神面板"}
            )
            assert result["memories"] == [] and result["skipped"] == "panel"
            await control(A, 2, False, False)
            assert not (await call("recall", {**identity(generation=2), "query": "我叫什么"}))[
                "memories"
            ]
            await call("ingest", {**identity(generation=1), "event": event}, 409)
            await control(A, 3, True, False)
            assert (await call("recall", {**identity(generation=3), "query": "你还记得我住哪吗"}))[
                "memories"
            ]
            report.update(
                {
                    "unauthorized_rejected": True,
                    "idempotent": True,
                    "user_scope_isolation": True,
                    "panel_excluded": True,
                    "pause_resume": True,
                    "stale_jobs_rejected": True,
                    "neural_calls": (await health())["neural_calls"],
                    "retrieved": memories,
                }
            )
        finally:
            for actor in (A, B):
                await control(actor, 100, False, True)
            assert (await call("status", identity(generation=100)))["counts"] == {}
            report["test_actor_deleted"] = True
            (Path(os.getenv("SMOKE_REPORT_DIR", "/tmp")) / "memory-smoke-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
        print(json.dumps({k: v for k, v in report.items() if k != "retrieved"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
