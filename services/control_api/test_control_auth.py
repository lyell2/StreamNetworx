import hashlib
import hmac
import json
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main as control


class ControlAuthTests(unittest.TestCase):
    def setUp(self):
        self.key_id = "test_admin"
        self.secret = "unit-test-control-secret"
        self.registry = {
            self.key_id: {
                "secret": self.secret,
                "allowed_stations": ["*"],
                "role": "network_admin",
            }
        }
        self.client = TestClient(control.app)
        self.registry_patch = patch.object(control, "load_client_registry", return_value=self.registry)
        self.registry_patch.start()
        self.nonce_patch = patch.object(control.redis_client, "set", new=AsyncMock(return_value=True))
        self.nonce_patch.start()
        self.hset_patch = patch.object(control.redis_client, "hset", new=AsyncMock())
        self.hset_patch.start()
        self.hgetall_patch = patch.object(control.redis_client, "hgetall", new=AsyncMock(return_value={}))
        self.hgetall_patch.start()

    def tearDown(self):
        self.hgetall_patch.stop()
        self.hset_patch.stop()
        self.nonce_patch.stop()
        self.registry_patch.stop()

    def signed_headers(self, method, path, body, signature_override=None):
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        canonical = f"{method}:{path}:{timestamp}:{nonce}:{body.decode()}"
        signature = hmac.new(self.secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return {
            "X-API-Key": self.key_id,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature_override or signature,
            "Content-Type": "application/json",
        }

    def test_provisioning_requires_authentication(self):
        response = self.client.post("/api/v1/stations", json={"station_id": "testfm"})
        self.assertEqual(response.status_code, 422)
        control.redis_client.hset.assert_not_awaited()

    def test_invalid_signature_is_rejected(self):
        body = b'{"station_id":"testfm"}'
        headers = self.signed_headers("POST", "/api/v1/stations", body, "0" * 64)
        response = self.client.post("/api/v1/stations", content=body, headers=headers)
        self.assertEqual(response.status_code, 403)
        control.redis_client.hset.assert_not_awaited()

    def test_signed_network_admin_can_provision(self):
        body = b'{"station_id":"testfm"}'
        headers = self.signed_headers("POST", "/api/v1/stations", body)
        response = self.client.post("/api/v1/stations", content=body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["station_id"], "testfm")
        control.redis_client.hset.assert_awaited_once()

    def test_station_manager_cannot_provision(self):
        self.registry[self.key_id]["role"] = "station_manager"
        self.registry[self.key_id]["allowed_stations"] = ["testfm"]
        body = b'{"station_id":"testfm"}'
        headers = self.signed_headers("POST", "/api/v1/stations", body)
        response = self.client.post("/api/v1/stations", content=body, headers=headers)
        self.assertEqual(response.status_code, 403)
        control.redis_client.hset.assert_not_awaited()

    def test_station_manager_can_activate_own_suspended_station(self):
        self.registry[self.key_id]["role"] = "station_manager"
        self.registry[self.key_id]["allowed_stations"] = ["pukfm"]
        control.redis_client.hgetall = AsyncMock(return_value={
            "station": "pukfm",
            "billing_status": "suspended",
            "studio_password": "preserved-password",
        })
        path = "/api/v1/stations/pukfm/activate"
        headers = self.signed_headers("POST", path, b"")
        response = self.client.post(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "active")
        self.assertTrue(response.json()["updated_ms"])
        control.redis_client.hset.assert_awaited_once()

    def test_station_manager_cannot_activate_other_station(self):
        self.registry[self.key_id]["role"] = "station_manager"
        self.registry[self.key_id]["allowed_stations"] = ["edenradio"]
        path = "/api/v1/stations/pukfm/activate"
        headers = self.signed_headers("POST", path, b"")
        response = self.client.post(path, headers=headers)
        self.assertEqual(response.status_code, 403)
        control.redis_client.hgetall.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
