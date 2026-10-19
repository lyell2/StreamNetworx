import os
import unittest

os.environ["MESH_INTERNAL_SECRET"] = "unit-test-mesh-shared-key-32bytes"

from scripts import mesh_agent


class MeshMessageTests(unittest.TestCase):
    def test_signed_message_round_trips(self):
        original = {"type": "HEARTBEAT", "pubkey": "node-key", "mesh_ip": "10.10.0.2"}
        self.assertEqual(mesh_agent.decode_message(mesh_agent.encode_message(original)), original)

    def test_unsigned_message_is_rejected(self):
        with self.assertRaises(ValueError):
            mesh_agent.decode_message(b'{"type":"DISCOVER","pubkey":"node-key"}')

    def test_tampered_message_is_rejected(self):
        encoded = mesh_agent.encode_message({"type": "JOINED", "mesh_ip": "10.10.0.2"})
        tampered = encoded.replace(b"10.10.0.2", b"10.10.0.3")
        with self.assertRaises(ValueError):
            mesh_agent.decode_message(tampered)

    def test_private_docker_source_uses_container_endpoint(self):
        endpoint = mesh_agent.observed_peer_endpoint(
            {"wg_port": 51821, "public_endpoint": "198.51.100.2:51821"},
            "172.18.0.10",
        )
        self.assertEqual(endpoint, "172.18.0.10:51821")

    def test_public_source_uses_advertised_endpoint(self):
        endpoint = mesh_agent.observed_peer_endpoint(
            {"wg_port": 51821, "public_endpoint": "198.51.100.2:51821"},
            "198.51.100.2",
        )
        self.assertEqual(endpoint, "198.51.100.2:51821")


if __name__ == "__main__":
    unittest.main()
