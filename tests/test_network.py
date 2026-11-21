import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_backend import Client
import test_maintenance
from share import read_network_status, Supervisor


class NetworkStateTests(unittest.TestCase):
    def test_status_missing_stale_and_invalid_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network.json"
            self.assertEqual(read_network_status(directory)["status"], "stopped")
            path.write_text(json.dumps({"updated_epoch": time.time() - 200, "url": "https://test.example"}), encoding="utf-8")
            self.assertEqual(read_network_status(directory)["url"], "")
            path.write_text(json.dumps({"updated_epoch": time.time(), "url": "javascript:alert(1)"}), encoding="utf-8")
            self.assertEqual(read_network_status(directory)["url"], "")

    def test_address_parse_and_atomic_status_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch("share.ROOT", Path(directory)):
            supervisor = Supervisor(data_dir=directory)
            try:
                supervisor.events.put("Your quick tunnel https://demo-board-test.trycloudflare.com")
                supervisor.consume_events()
                result = read_network_status(directory)
                self.assertEqual(result["url"], "https://demo-board-test.trycloudflare.com")
                self.assertEqual(result["status"], "checking")
                self.assertIn(result["url"], (Path(directory) / "当前外网地址.txt").read_text(encoding="utf-8"))
            finally:
                for handler in list(supervisor.logger.handlers):
                    handler.close()
                    supervisor.logger.removeHandler(handler)

    def test_tunnel_command_uses_local_origin_without_launching(self):
        with tempfile.TemporaryDirectory() as directory, patch("share.ROOT", Path(directory)):
            binary = Path(directory) / "tools/cloudflared.exe"
            binary.parent.mkdir()
            binary.write_bytes(b"test-fixture")
            supervisor = Supervisor(data_dir=directory)
            try:
                with patch("share.EXPECTED_BINARY_HASH", hashlib.sha256(b"test-fixture").hexdigest()), patch("share.subprocess.Popen") as process:
                    process.return_value = SimpleNamespace(stdout=iter([]))
                    supervisor.start_tunnel()
                    command = process.call_args.args[0]
                    self.assertIn("http://127.0.0.1:8765", command)
                    self.assertIn("http2", command)
                    self.assertEqual(read_network_status(directory)["status"], "connecting")
            finally:
                for handler in list(supervisor.logger.handlers):
                    handler.close()
                    supervisor.logger.removeHandler(handler)


class NetworkApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_maintenance.MaintenanceTests.setUpClass()
        cls.client = test_maintenance.MaintenanceTests.client
        cls.url = test_maintenance.MaintenanceTests.url
        cls.directory = Path(test_maintenance.MaintenanceTests.directory.name)

    @classmethod
    def tearDownClass(cls):
        test_maintenance.MaintenanceTests.tearDownClass()

    def test_network_status_requires_login(self):
        self.assertEqual(Client(self.url).request("/api/network")[0], 401)
        self.assertEqual(self.client.request("/api/network")[0], 200)

    def test_proxy_https_cookie_and_public_origin(self):
        network = self.directory / "network.json"
        network.write_text(json.dumps({"url": "https://modus-test.trycloudflare.com", "status": "running", "updated_epoch": time.time()}), encoding="utf-8")
        try:
            client = Client(self.url)
            status, body, headers = client.request("/api/login", "POST", {"username": "a", "password": "abcdefgh"}, {"X-Forwarded-Proto": "https", "Origin": "https://modus-test.trycloudflare.com"})
            self.assertEqual(status, 200, body)
            self.assertIn("Secure", headers["Set-Cookie"])
            self.assertEqual(client.request("/api/login", "POST", {"username": "a", "password": "abcdefgh"}, {"Origin": "https://other.example"})[0], 403)
        finally:
            network.unlink(missing_ok=True)

    def test_health_identity_and_font_cache(self):
        self.assertEqual(self.client.request("/api/health")[1]["app"], "modus-workbench")
        self.assertIn("max-age", self.client.request("/assets/mark.svg")[2]["Cache-Control"])
        self.assertEqual(self.client.request("/app.js")[2]["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main(verbosity=2)
