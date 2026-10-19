import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MESH_INTERNAL_SECRET", "unit-test-control-mesh-secret-32bytes")
os.environ.setdefault("REDIS_PASSWORD", "unit-test-redis-secret")

import main as control


class StationStateSyncTests(unittest.TestCase):
    def test_newer_station_version_replaces_existing_state(self):
        metadata = {
            "station": "testfm",
            "mount": "/testfm.mp3",
            "billing_status": "suspended",
            "studio_password": "station-secret",
            "updated_ms": 200,
            "origin_node": "node-b",
            "unexpected": "discard-me",
        }
        control.redis_client.hgetall = AsyncMock(return_value={
            "updated_ms": "100",
            "origin_node": "node-a",
            "billing_status": "active",
        })
        control.redis_client.hset = AsyncMock()

        changed = asyncio.run(control.merge_station_state({"station_id": "testfm", "metadata": metadata}))

        self.assertTrue(changed)
        control.redis_client.hset.assert_awaited_once_with(
            "streamnet:station:testfm:meta",
            mapping={
                "station": "testfm",
                "mount": "/testfm.mp3",
                "billing_status": "suspended",
                "studio_password": "station-secret",
                "updated_ms": "200",
                "origin_node": "node-b",
            },
        )

    def test_stale_station_version_is_ignored(self):
        control.redis_client.hgetall = AsyncMock(return_value={
            "updated_ms": "200",
            "origin_node": "node-b",
        })
        control.redis_client.hset = AsyncMock()
        changed = asyncio.run(control.merge_station_state({
            "station_id": "testfm",
            "metadata": {"updated_ms": 100, "origin_node": "node-a", "billing_status": "active"},
        }))
        self.assertFalse(changed)
        control.redis_client.hset.assert_not_awaited()

    def test_equal_version_uses_stable_origin_tiebreaker(self):
        control.redis_client.hgetall = AsyncMock(return_value={
            "updated_ms": "200",
            "origin_node": "node-z",
        })
        control.redis_client.hset = AsyncMock()
        changed = asyncio.run(control.merge_station_state({
            "station_id": "testfm",
            "metadata": {"updated_ms": 200, "origin_node": "node-a", "billing_status": "active"},
        }))
        self.assertFalse(changed)
        control.redis_client.hset.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
