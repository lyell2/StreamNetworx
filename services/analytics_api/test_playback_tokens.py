import time
import urllib.parse
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main as analytics


class PlaybackTokenTests(unittest.TestCase):
    def setUp(self):
        self.key_id = "test_player"
        self.station = "testfm"
        self.secret = "unit-test-playback-secret"
        self.registry = {
            self.key_id: {
                "secret": self.secret,
                "allowed_stations": [self.station],
                "role": "station_manager",
            }
        }
        self.client = TestClient(analytics.app)
        self.registry_patch = patch.object(analytics, "load_client_registry", return_value=self.registry)
        self.registry_patch.start()

    def tearDown(self):
        self.registry_patch.stop()

    def request_for(self, expires_at, station=None, client_ip="", token_override=None):
        station = station or self.station
        signature = analytics.sign_playback_token(
            self.secret,
            self.key_id,
            station,
            expires_at,
            client_ip,
        )
        params = {
            "st": station,
            "exp": expires_at,
            "kid": self.key_id,
            "token": token_override or signature,
        }
        if client_ip:
            params["ip"] = client_ip
        query = urllib.parse.urlencode(params)
        return self.client.get(
            f"/api/v1/auth/verify?{query}",
            headers={
                "X-Original-URI": f"/geo/{self.station}.mp3?{query}",
                "X-Real-IP": client_ip or "203.0.113.8",
            },
        )

    def test_valid_signature_is_authorized(self):
        response = self.request_for(int(time.time()) + 60)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers.get("x-session-id"))

    def test_subrequest_recovers_query_from_original_uri_header(self):
        expires_at = int(time.time()) + 60
        signature = analytics.sign_playback_token(
            self.secret, self.key_id, self.station, expires_at, ""
        )
        query = urllib.parse.urlencode({
            "st": self.station,
            "exp": expires_at,
            "kid": self.key_id,
            "token": signature,
        })
        response = self.client.get(
            "/api/v1/auth/verify",
            headers={
                "X-Original-URI": f"/geo/{self.station}.mp3?{query}",
                "X-Real-IP": "203.0.113.8",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_tampered_signature_is_rejected(self):
        response = self.request_for(int(time.time()) + 60, token_override="0" * 64)
        self.assertEqual(response.status_code, 401)

    def test_expired_token_is_rejected(self):
        response = self.request_for(1)
        self.assertEqual(response.status_code, 403)

    def test_station_mismatch_is_rejected(self):
        response = self.request_for(int(time.time()) + 60, station="otherfm")
        self.assertEqual(response.status_code, 403)

    def test_ip_bound_token_requires_matching_ip(self):
        expires_at = int(time.time()) + 60
        signature = analytics.sign_playback_token(
            self.secret, self.key_id, self.station, expires_at, "203.0.113.7"
        )
        query = urllib.parse.urlencode({
            "st": self.station,
            "exp": expires_at,
            "kid": self.key_id,
            "ip": "203.0.113.7",
            "token": signature,
        })
        response = self.client.get(
            f"/api/v1/auth/verify?{query}",
            headers={
                "X-Original-URI": f"/geo/{self.station}.mp3?{query}",
                "X-Real-IP": "203.0.113.8",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_mesh_peer_addresses_come_from_shared_roster(self):
        state = {
            "version": 1,
            "local": {"mesh_ip": "10.10.0.2"},
            "peers": {
                "local": {"mesh_ip": "10.10.0.2"},
                "remote": {"mesh_ip": "10.10.0.1"},
                "invalid": {"mesh_ip": "192.0.2.1"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "peers.json"
            state_path.write_text(json.dumps(state))
            with patch.object(analytics, "MESH_STATE_FILE", state_path):
                self.assertEqual(analytics.mesh_peer_addresses(), ["10.10.0.1"])


if __name__ == "__main__":
    unittest.main()
