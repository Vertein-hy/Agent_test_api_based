"""Authenticated memory endpoints; generation and audience checks precede graph access."""

import asyncio
import time
from aiohttp import web
from qqbot.memory.policy import identity, checked_event, clean, panel, attention, checked_update
from .dossier import attended_profile


class MemoryAPI:
    def __init__(self, service):
        self.service = service

    async def control(self, request):
        body = await request.json()
        actor, _, generation = identity(body)
        enabled, forget = body.get("enabled"), body.get("forget")
        if type(enabled) is not bool or type(forget) is not bool:
            raise ValueError("Invalid control")
        async with self.service.locks[actor]:
            if not self.service.jobs.control(actor, generation, enabled, forget):
                return web.json_response({"error": "stale_generation"}, status=409)
            if forget:
                await asyncio.to_thread(self.service.graph.forget, actor)
                await asyncio.to_thread(self.service.encoder.purge_all)
                self.service.learner.forget(actor)
        return web.json_response({"ok": True, "generation": generation, "enabled": enabled})

    async def ingest(self, request):
        body = await request.json()
        actor, scope, generation = identity(body)
        if scope is None:
            raise ValueError("Missing scope")
        event = checked_event(body.get("event"))
        if event is None:
            return web.json_response({"ok": True, "skipped": True})
        async with self.service.locks[actor]:
            if not self.service.jobs.put(actor, scope, generation, event):
                return web.json_response({"error": "disabled_or_stale"}, status=409)
        return web.json_response({"ok": True, "queued": True})

    async def status(self, request):
        body = await request.json()
        actor, scope, generation = identity(body)
        if scope is None:
            raise ValueError("Missing scope")
        async with self.service.locks[actor]:
            state = self.service.jobs.state(actor)
            if generation != state[0]:
                return web.json_response({"error": "stale_generation"}, status=409)
            result = await asyncio.to_thread(self.service.graph.status, actor, scope)
            if "private" in body:
                nodes, _ = await asyncio.to_thread(
                    self.service.graph.view, actor, scope, body.get("private") is True, "recall"
                )
                result["facts"] = [
                    {"id": n["id"], "text": n["text"], "created": n.get("last_seen", n["created"])}
                    for n in nodes
                    if n["kind"] in ("fact", "preference", "task")
                ][:8]
            result["profile"] = attended_profile(
                await asyncio.to_thread(
                    self.service.graph.audience_profile, actor, scope, body.get("private") is True
                ),
                "recall",
            )
            result["attention_samples"] = self.service.learner.state(actor)[1]
            result.update(
                {
                    "enabled": state[1],
                    "pending": self.service.jobs.db.execute(
                        "SELECT count(*) FROM jobs WHERE actor=? AND scope=? AND status IN ('pending','processing')",
                        (actor, scope),
                    ).fetchone()[0],
                }
            )
        return web.json_response(result)

    async def recall(self, request):
        started = time.monotonic()
        body = await request.json()
        actor, scope, generation = identity(body)
        query = body.get("query")
        if scope is None or not isinstance(query, str) or len(query) > 4000:
            raise ValueError("Invalid recall")
        if panel(query):
            return web.json_response({"memories": [], "skipped": "panel"})
        query = clean(query)
        if not query:
            return web.json_response({"memories": []})
        mode = attention(query)
        async with self.service.locks[actor]:
            if not self.service.jobs.allowed(actor, generation):
                return web.json_response({"memories": []})
            private = body.get("private") is True
            # Older trusted clients keep the original scoped behavior until upgraded.
            profile = (
                await asyncio.to_thread(
                    self.service.graph.audience_profile, actor, scope, private, mode
                )
                if "private" in body
                else await asyncio.to_thread(self.service.graph.profile, actor, scope)
            )
            selected_profile = attended_profile(profile, mode, query)
            if mode == "social":
                return web.json_response(
                    {
                        "memories": [],
                        "profile": selected_profile,
                        "attention": mode,
                        "ranking": "profile_only",
                    }
                )
            nodes, edges = (
                await asyncio.to_thread(self.service.graph.view, actor, scope, private, mode)
                if "private" in body
                else await asyncio.to_thread(self.service.graph.snapshot, actor, scope)
            )
            nodes = [n for n in nodes if n.get("vector")]
            ids = {n["id"] for n in nodes}
            edges = [e for e in edges if e["src"] in ids and e["dst"] in ids]
            if not nodes:
                return web.json_response(
                    {"memories": [], "profile": selected_profile, "attention": mode}
                )
            q = (await asyncio.to_thread(self.service.encoder.encode, [query], True))[0]
            result = await asyncio.to_thread(
                self.service._rank, nodes, edges, q, query, actor, self.service.learner.state(actor)
            )
            await asyncio.to_thread(
                self.service.graph.touch, actor, scope, [m["id"] for m in result]
            )
            exposure = self.service.learner.expose(
                actor,
                scope,
                [{"id": m["id"], "features": m.pop("_attention_features")} for m in result],
            )
            self.service.learner.metric("recall_ms", (time.monotonic() - started) * 1000)
            self.service.learner.metric("recall_items", len(result))
        return web.json_response(
            {
                "memories": result,
                "profile": selected_profile,
                "attention": mode,
                "recall_id": exposure,
                "ranking": "semantic+relational_neural",
                "training_source": "authored_synthetic_only",
            }
        )

    async def update(self, request):
        body = await request.json()
        actor, scope, generation = identity(body)
        if scope is None:
            raise ValueError("Missing scope")
        event = checked_event(body.get("event"))
        if not event:
            raise ValueError("Missing evidence")
        update = checked_update(body.get("update"), event["question"])
        if not update:
            raise ValueError("Invalid update")
        async with self.service.locks[actor]:
            if not self.service.jobs.allowed(actor, generation):
                return web.json_response({"error": "disabled_or_stale"}, status=409)
            if update["operation"] == "correct":
                update["vector"] = (
                    await asyncio.to_thread(self.service.encoder.encode, [update["value"]])
                )[0].tolist()
            result = await asyncio.to_thread(
                self.service.graph.update,
                actor,
                scope,
                update,
                event["id"],
                event["created"],
                body.get("private") is True,
            )
            self.service.learner.metric("explicit_update_success", int(result["ok"]))
        return web.json_response(result)

    async def feedback(self, request):
        body = await request.json()
        actor, scope, generation = identity(body)
        if (
            scope is None
            or type(body.get("useful")) is not bool
            or not isinstance(body.get("recall_id"), str)
        ):
            raise ValueError("Invalid feedback")
        async with self.service.locks[actor]:
            if not self.service.jobs.allowed(actor, generation):
                return web.json_response({"error": "disabled_or_stale"}, status=409)
            ids = self.service.learner.feedback(actor, scope, body["recall_id"], body["useful"])
            if ids:
                await asyncio.to_thread(self.service.graph.reinforce, actor, ids, body["useful"])
        return web.json_response(
            {"ok": bool(ids), "attention_samples": self.service.learner.state(actor)[1]}
        )

    async def metrics(self, request):
        return web.json_response(self.service.learner.report())
