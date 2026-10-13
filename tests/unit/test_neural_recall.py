from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import Mock
import numpy as np
from qqmemory.app import Service
from qqmemory.neural import Ranker
from qqmemory.recall import rank


class NeuralRecallTests(unittest.TestCase):
    def test_ordinary_ranking_excludes_episode_and_conditional_greeting_even_at_high_similarity(
        self,
    ):
        service = Mock()
        service.ranker.score.side_effect = lambda nodes, *args: np.zeros(len(nodes))
        q = np.array([1.0, 0.0], dtype="float32")
        nodes = [
            {
                "id": "a",
                "kind": "episode",
                "text": "旧助手摘要误称用户有出行经历",
                "question": "评价一个虚构出行场景",
                "created": 1,
                "vector": q.tolist(),
            },
            {
                "id": "b",
                "kind": "preference",
                "text": "打招呼时说问候暗号",
                "created": 2,
                "vector": q.tolist(),
            },
            {
                "id": "c",
                "kind": "preference",
                "text": "回复简短",
                "created": 3,
                "vector": q.tolist(),
            },
        ]
        ordinary = rank(service, nodes, [], q, "回复简短一些")
        self.assertEqual([m["id"] for m in ordinary], ["c"])
        recalled = rank(service, nodes, [], q, "上次讨论的出行场景是什么")
        episode = next(m for m in recalled if m["kind"] == "episode")
        self.assertEqual(episode["text"], nodes[0]["question"])
        self.assertNotIn("旧助手摘要", str(episode))
        self.assertEqual(episode["attribution"], "dialogue_not_verified_experience")
        self.assertEqual(rank(service, nodes[:1], [], q, "你反应慢"), [])

    def test_actual_network_and_graph_link_activate_low_similarity_source_episode(self):
        service = Service.__new__(Service)
        service.ranker = Ranker("/models", 1024)
        now = time.time()
        q = np.zeros(1024, dtype="float32")
        q[0] = 1
        low = np.zeros(1024, dtype="float32")
        low[1] = 1
        nodes = [
            {
                "id": "a" * 64,
                "kind": "task",
                "text": "周末去公园拍树",
                "created": now,
                "vector": q.tolist(),
                "source": "c" * 64,
                "evidence": "周末去公园拍树",
            },
            {
                "id": "b" * 64,
                "kind": "episode",
                "text": "那次生活计划的讨论",
                "created": now,
                "vector": low.tolist(),
                "source": "c" * 64,
                "question": "周末去公园拍树",
            },
        ]
        edges = [{"src": nodes[0]["id"], "dst": nodes[1]["id"], "relation": 2}]
        linked = service._rank(nodes, edges, q, "之前的拍树计划来自哪段讨论")
        unlinked = service._rank(nodes, [], q, "之前的拍树计划来自哪段讨论")
        self.assertEqual({m["kind"] for m in linked}, {"task", "episode"})
        self.assertEqual({m["kind"] for m in unlinked}, {"task"})
        self.assertEqual(
            next(m for m in linked if m["kind"] == "episode")["associated_via"], [nodes[0]["id"]]
        )
        vectors = np.array([q, low])
        with_edges = service.ranker.score(nodes, edges, vectors, q, now)
        without = service.ranker.score(nodes, [], vectors, q, now)
        self.assertGreater(float(np.abs(with_edges - without).max()), 0.001)
        self.assertEqual(service.ranker.calls, 4)

    def test_modified_weights_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("rgcn.pt", "rgcn-metadata.json"):
                shutil.copyfile(Path("/models") / name, Path(tmp) / name)
            path = Path(tmp) / "rgcn.pt"
            data = bytearray(path.read_bytes())
            data[100] ^= 1
            path.write_bytes(data)
            with self.assertRaises(AssertionError):
                Ranker(tmp, 1024)


if __name__ == "__main__":
    unittest.main()
