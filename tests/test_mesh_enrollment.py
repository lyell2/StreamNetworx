import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

os.environ["MESH_INTERNAL_SECRET"] = "unit-test-mesh-shared-key-32bytes"

from scripts import mesh_agent


class MeshEnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.secret = "unit-test-mesh-shared-key-32bytes"
        self.pubkey = "A" * 43 + "="
        self.old_secret = mesh_agent.MESH_SECRET
        self.old_local = dict(mesh_agent.local_info)
        self.old_peers = dict(mesh_agent.peer_table)
        self.old_state_path = mesh_agent.PEER_STATE_FILE
        self.old_routes_path = mesh_agent.NATS_ROUTES_FILE
        mesh_agent.MESH_SECRET = self.secret
        mesh_agent.local_info.update({
            "pubkey": "B" * 43 + "=",
            "mesh_ip": "10.10.0.1",
            "public_endpoint": "203.0.113.1:51820",
        })
        mesh_agent.peer_table.clear()
        mesh_agent.join_nonces.clear()
        mesh_agent.PEER_STATE_FILE = os.path.join(self.state_dir.name, "peers.json")
        mesh_agent.NATS_ROUTES_FILE = os.path.join(self.state_dir.name, "nats_routes.txt")
        self.sync_patch = patch.object(mesh_agent, "sync_peer")
        self.sync_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mesh_agent.JoinHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/join"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.sync_patch.stop()
        mesh_agent.MESH_SECRET = self.old_secret
        mesh_agent.local_info.clear()
        mesh_agent.local_info.update(self.old_local)
        mesh_agent.peer_table.clear()
        mesh_agent.peer_table.update(self.old_peers)
        mesh_agent.PEER_STATE_FILE = self.old_state_path
        mesh_agent.NATS_ROUTES_FILE = self.old_routes_path
        self.state_dir.cleanup()

    def request(self, nonce=None, signature_override=None, pubkey=None, node_id="edge-test-2"):
        body = json.dumps({
            "node_id": node_id,
            "pubkey": pubkey or self.pubkey,
            "public_endpoint": "203.0.113.2:51820",
        }, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = nonce or uuid.uuid4().hex
        canonical = f"POST:/join:{timestamp}:{nonce}:".encode() + body
        signature = hmac.new(self.secret.encode(), canonical, hashlib.sha256).hexdigest()
        request = Request(self.url, data=body, headers={
            "Content-Type": "application/json",
            "X-Mesh-Timestamp": timestamp,
            "X-Mesh-Nonce": nonce,
            "X-Mesh-Signature": signature_override or signature,
        }, method="POST")
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except Exception as error:
            return error.code, json.loads(error.read())

    def test_valid_join_receives_unique_address_and_roster(self):
        status, response = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(response["assigned_ip"], "10.10.0.2")
        self.assertEqual(response["bootstrap_mesh_ip"], "10.10.0.1")
        self.assertEqual(response["peers"][0]["mesh_ip"], "10.10.0.1")

    def test_replayed_join_is_rejected(self):
        nonce = uuid.uuid4().hex
        self.assertEqual(self.request(nonce)[0], 200)
        self.assertEqual(self.request(nonce)[0], 409)

    def test_rejoining_same_public_key_keeps_its_address(self):
        first = self.request()[1]["assigned_ip"]
        second = self.request()[1]["assigned_ip"]
        self.assertEqual(first, second)
        self.assertEqual(len(mesh_agent.peer_table), 1)

    def test_different_nodes_receive_distinct_reserved_addresses(self):
        first = self.request()[1]["assigned_ip"]
        second = self.request(
            pubkey="C" * 43 + "=",
            node_id="edge-test-3",
        )[1]["assigned_ip"]
        self.assertNotEqual(first, second)

    def test_invalid_signature_is_rejected(self):
        self.assertEqual(self.request(signature_override="0" * 64)[0], 403)


if __name__ == "__main__":
    unittest.main()
