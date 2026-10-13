"""Retryable source-first consolidation and periodic decay.

An actor lock and generation check bracket slow extraction. Revoked jobs cannot
recreate deleted facts after an API call returns. Model training is absent here.
"""

import asyncio
import time
import logging
import numpy as np

log = logging.getLogger("memory")


class Consolidator:
    def __init__(self, service):
        self.service = service

    async def worker(self):
        while True:
            if time.monotonic() - self.service.last_maintenance > 600:
                try:
                    await asyncio.to_thread(self.service.graph.maintenance)
                except Exception as e:
                    log.warning("Memory maintenance deferred (%s)", type(e).__name__)
                self.service.last_maintenance = time.monotonic()
            job = self.service.jobs.next()
            if not job:
                await asyncio.sleep(1)
                continue
            self.service.processing = True
            try:
                if not self.service.jobs.allowed(job["actor"], job["generation"]):
                    self.service.jobs.finish(job)
                    continue
                # Persist the source episode first; extraction outages must not lose it.
                async with self.service.locks[job["actor"]]:
                    if self.service.jobs.allowed(job["actor"], job["generation"]):
                        raw = {
                            "summary": job["event"]["question"][:240],
                            "topics": [],
                            "claims": [],
                        }
                        raw_nodes, raw_edges = self.service.graph.prepare(
                            job["actor"], job["scope"], job["event"], raw
                        )
                        raw_vectors = await asyncio.to_thread(
                            self.service.encoder.encode, [n["text"] for n in raw_nodes]
                        )
                        for n, v in zip(raw_nodes, raw_vectors):
                            n["vector"] = v.tolist()
                        await asyncio.to_thread(
                            self.service.graph.add, job["actor"], job["scope"], raw_nodes, raw_edges
                        )
                extracted = await self.service.extractor.extract(self.service.session, job["event"])
                async with self.service.locks[job["actor"]]:
                    # Re-check after slow model/encoder work. Forget/pause revokes old jobs.
                    if self.service.jobs.allowed(job["actor"], job["generation"]):
                        nodes, edges = self.service.graph.prepare(
                            job["actor"], job["scope"], job["event"], extracted
                        )
                        vectors = await asyncio.to_thread(
                            self.service.encoder.encode, [n["text"] for n in nodes]
                        )
                        for n, vector in zip(nodes, vectors):
                            n["vector"] = vector.tolist()
                        await asyncio.to_thread(
                            self.service.graph.add, job["actor"], job["scope"], nodes, edges
                        )
                        for update in extracted.get("updates", []):
                            if update["operation"] == "correct":
                                update["vector"] = (
                                    await asyncio.to_thread(
                                        self.service.encoder.encode, [update["value"]]
                                    )
                                )[0].tolist()
                            changed = await asyncio.to_thread(
                                self.service.graph.update,
                                job["actor"],
                                job["scope"],
                                update,
                                job["event"]["id"],
                                job["event"]["created"],
                                job["event"].get("private") is True,
                            )
                            self.service.learner.metric(
                                "natural_update_success", int(changed["ok"])
                            )
                        existing, _ = await asyncio.to_thread(
                            self.service.graph.snapshot, job["actor"], job["scope"]
                        )
                        topics = [n for n in existing if n["kind"] == "topic"]
                        for n in nodes:
                            if n["kind"] != "topic":
                                continue
                            matches = [
                                (float(np.dot(n["vector"], old["vector"])), old)
                                for old in topics
                                if old["id"] != n["id"] and old.get("vector")
                            ]
                            for similarity, other in sorted(
                                matches, key=lambda x: x[0], reverse=True
                            )[:2]:
                                if similarity >= 0.70:
                                    await asyncio.to_thread(
                                        self.service.graph.associate,
                                        job["actor"],
                                        job["scope"],
                                        n["id"],
                                        other["id"],
                                        similarity,
                                    )
                    self.service.jobs.finish(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # No prompts, response bodies, tokens or account identifiers in logs.
                log.warning("Memory consolidation deferred (%s)", type(e).__name__)
                self.service.jobs.finish(job, error=True)
            finally:
                self.service.processing = False
