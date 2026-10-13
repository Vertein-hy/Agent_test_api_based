"""Graph service composition and lifecycle. python -m qqmemory.app is the entrypoint."""

import asyncio
import hmac
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
import aiohttp
from aiohttp import web
from .encoding import Encoder
from .extract import Extractor
from .graph import GraphStore
from .jobs import Jobs
from .neural import Ranker
from .attention_model import AttentionLearner
from .api import MemoryAPI
from .consolidation import Consolidator
from .recall import rank

log = logging.getLogger("memory")


class Service:
    """Compose stores, model resources, authenticated API and consolidation worker."""

    def __init__(self):
        self.token = Path(os.environ["MEMORY_TOKEN_FILE"]).read_text().strip()
        if len(self.token) < 32:
            raise ValueError("Missing memory token")
        data = Path(os.getenv("MEMORY_DATA", "/data"))
        data.mkdir(parents=True, exist_ok=True)
        self.jobs = Jobs(data / "jobs.sqlite3")
        self.learner = AttentionLearner(data / "attention.sqlite3")
        self.graph = GraphStore(os.getenv("GRAPH_URI", "bolt://memory-graph:7687"))
        self.encoder = Encoder(
            os.getenv("EMBEDDING_PATH", "/models/embedding"), data / "vectors.sqlite3"
        )
        self.ranker = Ranker("/models", self.encoder.dimension)
        assert self.ranker.metadata["encoder_revision"] == self.encoder.revision
        self.extractor = Extractor(
            os.environ["DEEPSEEK_BASE_URL"],
            os.environ["DEEPSEEK_MODEL"],
            os.environ["DEEPSEEK_API_KEY_FILE"],
        )
        self.locks = defaultdict(asyncio.Lock)
        self.session = None
        self.worker_task = None
        self.processing = False
        self.last_maintenance = 0

        self.api = MemoryAPI(self)
        self.consolidator = Consolidator(self)

    async def start(self, app):
        self.session = aiohttp.ClientSession()
        self.worker_task = asyncio.create_task(self.consolidator.worker())

    async def stop(self, app):
        self.worker_task.cancel()
        await asyncio.gather(self.worker_task, return_exceptions=True)
        await self.session.close()
        self.graph.driver.close()
        self.learner.db.close()

        self.jobs.db.close()
        self.encoder.db.close()

    async def health(self, request):
        ok = await asyncio.to_thread(self.graph.ping)
        return web.json_response(
            {
                "ready": ok,
                "encoder": "Qwen3-Embedding-0.6B",
                "memory_version": 3,
                "person_recognition": "qq_account_cross_context",
                "consolidation": "source_linked_temporal",
                "attention": "explicit_feedback_online_logistic",
                "encoder_revision": self.encoder.revision,
                "device": "cpu",
                "graph": "neo4j",
                "neural": "R-GCN",
                "neural_calls": self.ranker.calls,
                "training_source": self.ranker.metadata["training_source"],
                "evaluation": self.ranker.metadata["evaluation"],
                "jobs": self.jobs.counts(),
                "processing": self.processing,
                "panel_integration": False,
            },
            status=200 if ok else 503,
        )

    def _rank(self, *args, **kwargs):
        return rank(self, *args, **kwargs)

    async def control(self, request):
        return await self.api.control(request)

    async def ingest(self, request):
        return await self.api.ingest(request)

    async def status(self, request):
        return await self.api.status(request)

    async def recall(self, request):
        return await self.api.recall(request)

    async def update(self, request):
        return await self.api.update(request)

    async def feedback(self, request):
        return await self.api.feedback(request)

    async def metrics(self, request):
        return await self.api.metrics(request)


@web.middleware
async def boundary(request, handler):
    if request.path != "/healthz":
        token = request.app["memory"].token
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            return web.json_response({"error": "unauthorized"}, status=401)
    try:
        return await handler(request)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return web.json_response({"error": "invalid_request"}, status=400)
    except Exception as e:
        log.warning("Memory request unavailable (%s)", type(e).__name__)
        return web.json_response({"error": "temporarily_unavailable"}, status=503)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = Service()
    app = web.Application(client_max_size=16384, middlewares=[boundary])
    app["memory"] = service
    app.router.add_get("/healthz", service.health)
    for path in ("control", "ingest", "recall", "status", "update", "feedback", "metrics"):
        app.router.add_post("/" + path, getattr(service, path))
    app.on_startup.append(service.start)
    app.on_cleanup.append(service.stop)
    web.run_app(app, host="0.0.0.0", port=8083, access_log=None)


if __name__ == "__main__":
    main()
