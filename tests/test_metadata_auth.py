import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("METADATA_WEB_DIR", tempfile.mkdtemp(prefix="streamnetworx-test-"))
os.environ.setdefault("ICECAST_ADMIN_PASS", "unit-test-icecast-secret")
os.environ.setdefault("CLICKHOUSE_PASSWORD", "unit-test-clickhouse-secret")

from scripts import metadata_sidecar


class MetadataAuthTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(metadata_sidecar.Handler)
        self.token_patch = patch.object(metadata_sidecar, "station_token", return_value="expected-token")
        self.token_patch.start()

    def tearDown(self):
        self.token_patch.stop()

    def test_valid_station_token_is_accepted(self):
        self.handler.headers = {"X-Station-Token": "expected-token"}
        self.assertTrue(self.handler._authorize_station("edenradio"))

    def test_missing_or_wrong_token_is_rejected(self):
        self.handler.headers = {}
        self.assertFalse(self.handler._authorize_station("edenradio"))
        self.handler.headers = {"X-Station-Token": "wrong-token"}
        self.assertFalse(self.handler._authorize_station("edenradio"))

    def test_invalid_station_slug_is_rejected(self):
        self.handler.headers = {"X-Station-Token": "expected-token"}
        self.assertFalse(self.handler._authorize_station("../edenradio"))

    def test_bearer_station_token_is_accepted(self):
        self.handler.headers = {"Authorization": "Bearer expected-token"}
        self.assertTrue(self.handler._authorize_station("edenradio"))


if __name__ == "__main__":
    unittest.main()
