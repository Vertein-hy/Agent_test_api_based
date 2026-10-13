import asyncio
from collections import defaultdict
import json
import unittest
from unittest.mock import AsyncMock, Mock
from qqmemory.app import Service
from qqmemory.api import MemoryAPI


class AttentionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_social_reads_scoped_profile_without_encoder_snapshot_or_ranker(self):
        service = Service.__new__(Service)
        service.api = MemoryAPI(service)
        service.locks = defaultdict(asyncio.Lock)
        service.jobs = Mock()
        service.jobs.allowed.return_value = True
        service.graph = Mock()
        service.graph.profile.return_value = {"version": 2, "names": [], "behavior_preferences": []}
        service.encoder = Mock()
        service.ranker = Mock()
        request = Mock()
        request.json = AsyncMock(
            return_value={"actor": "a" * 64, "scope": "b" * 64, "generation": 0, "query": "晚上好"}
        )
        response = await service.recall(request)
        data = json.loads(response.text)
        self.assertEqual(data["ranking"], "profile_only")
        self.assertEqual(data["attention"], "social")
        service.graph.profile.assert_called_once_with("a" * 64, "b" * 64)
        service.graph.snapshot.assert_not_called()
        service.encoder.encode.assert_not_called()
        service.ranker.score.assert_not_called()

    async def test_revoked_and_panel_requests_cannot_read_dossier(self):
        service = Service.__new__(Service)
        service.api = MemoryAPI(service)
        service.locks = defaultdict(asyncio.Lock)
        service.jobs = Mock()
        service.jobs.allowed.return_value = False
        service.graph = Mock()
        for query in ("你好", "查询原神 UID 700000001 的雷神面板"):
            request = Mock()
            request.json = AsyncMock(
                return_value={"actor": "a" * 64, "scope": "b" * 64, "generation": 0, "query": query}
            )
            data = json.loads((await service.recall(request)).text)
            self.assertEqual(data["memories"], [])
        service.graph.profile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
