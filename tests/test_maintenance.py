import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from test_backend import Client
from server import WorkbenchServer


class MaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = WorkbenchServer(("127.0.0.1", 0), cls.directory.name)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.client = Client(cls.url)
        status, body, _ = cls.client.request("/api/setup", "POST", {"accounts": [
            {"name": "甲", "username": "a", "password": "abcdefgh"},
            {"name": "乙", "username": "b", "password": "ijklmnop"},
        ], "demo": True})
        assert status == 200, body
        cls.client.csrf = body["csrf"]
        status, token, _ = cls.client.request("/api/agent-token", "POST", {})
        assert status == 200
        cls.authorization = {"Authorization": "Bearer " + token["token"]}
        cls.config = Path(cls.directory.name) / "connection.json"
        cls.config.write_text(json.dumps({"url": cls.url, "token": token["token"]}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.directory.cleanup()

    def state(self):
        status, result, _ = self.client.request("/api/state")
        self.assertEqual(status, 200)
        return result

    def create_task(self, **changes):
        values = {"title": "维护测试事项", "owner_id": 1, "start_date": "", "end_date": ""}
        values.update(changes)
        status, result, _ = self.client.request("/api/tasks", "POST", values)
        self.assertEqual(status, 200, result)
        return result["id"]

    def test_pending_schedule_and_reassignment(self):
        identifier = self.create_task()
        status, result, _ = self.client.request(f"/api/tasks/{identifier}", "PATCH", {"owner_id": 2, "start_date": "2026-12-30", "end_date": "2027-01-02", "revision": 0})
        self.assertEqual(status, 200, result)
        task = next(task for task in self.state()["tasks"] if task["id"] == identifier)
        self.assertEqual(task["owner_id"], 2)
        self.assertEqual(task["end_date"], "2027-01-02")
        self.assertEqual(self.client.request(f"/api/tasks/{identifier}", "PATCH", {"title": "过期修改", "revision": 0})[0], 409)
        self.assertEqual(task["revision"], 1)

    def test_recycle_restore_and_archive(self):
        identifier = self.create_task()
        self.assertEqual(self.client.request(f"/api/tasks/{identifier}", "PATCH", {"done": True, "revision": 0})[0], 200)
        self.assertEqual(self.client.request(f"/api/tasks/{identifier}", "DELETE", {"revision": 1})[0], 200)
        snapshot = self.state()
        self.assertFalse(any(task["id"] == identifier for task in snapshot["tasks"]))
        self.assertTrue(any(task["id"] == identifier for task in snapshot["trash"]["tasks"]))
        self.assertFalse(any(record["task_id"] == identifier for record in snapshot["completions"]))
        self.assertEqual(self.client.request(f"/api/restore/tasks/{identifier}", "POST", {})[0], 200)
        self.assertTrue(any(record["task_id"] == identifier for record in self.state()["completions"]))

    def test_project_edit_keeps_phase_boundaries(self):
        project = self.state()["projects"][0]
        self.assertEqual(self.client.request(f'/api/projects/{project["id"]}', "PATCH", {"end_date": project["start_date"], "revision": project["revision"]})[0], 400)
        status, _, _ = self.client.request(f'/api/projects/{project["id"]}', "PATCH", {"description": "更新项目说明", "revision": project["revision"]})
        self.assertEqual(status, 200)

    def test_file_move_rename_and_section_recycle(self):
        first, second = self.state()["sections"][:2]
        status, document, _ = self.client.request("/api/documents", "POST", {"section_id": first["id"], "owner_id": 1, "name": "旧文件.txt", "content": base64.b64encode(b"same-content").decode()})
        self.assertEqual(status, 200)
        self.assertEqual(self.client.request(f'/api/documents/{document["id"]}', "PATCH", {"name": "新文件.txt", "section_id": second["id"], "owner_id": 2, "revision": 0})[0], 200)
        moved = next(item for item in self.state()["documents"] if item["id"] == document["id"])
        self.assertEqual(moved["section_id"], second["id"])
        self.assertEqual(moved["name"], "新文件.txt")
        self.assertEqual(self.client.request(f'/api/documents/{document["id"]}')[1], b"same-content")

    def test_agent_batch_refs_and_replay(self):
        data = {"request_id": "test-batch-replay", "operations": [
            {"method": "POST", "path": "/api/opportunities", "data": {"name": "生物制药意向测试", "owner_id": 2, "description": "具体诉求待确认"}},
            {"method": "POST", "path": "/api/conversations", "data": {"opportunity_id": {"$ref": "0.id"}, "owner_id": 2, "text": "用户提供的相关对谈", "summary": "已知行业，诉求待确认"}},
        ]}
        status, result, _ = self.client.request("/api/agent/submit", "POST", data, self.authorization)
        self.assertEqual(status, 200, result)
        status, replay, _ = self.client.request("/api/agent/submit", "POST", data, self.authorization)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len([item for item in self.state()["opportunities"] if item["name"] == "生物制药意向测试"]), 1)
        data["operations"][0]["data"]["name"] = "不同内容"
        self.assertEqual(self.client.request("/api/agent/submit", "POST", data, self.authorization)[0], 409)

    def test_agent_batch_failure_rolls_back_files_and_records(self):
        before = self.state()
        files_before = set(self.server.uploads.iterdir())
        data = {"request_id": "test-rollback", "operations": [
            {"method": "POST", "path": "/api/documents", "data": {"section_id": before["sections"][0]["id"], "owner_id": 1, "name": "回滚测试.txt", "content": base64.b64encode(b"rollback").decode()}},
            {"method": "POST", "path": "/api/tasks", "data": {"title": "错误负责人", "owner_id": 9999, "start_date": "", "end_date": ""}},
        ]}
        self.assertEqual(self.client.request("/api/agent/submit", "POST", data, self.authorization)[0], 400)
        self.assertEqual(len(self.state()["documents"]), len(before["documents"]))
        self.assertEqual(set(self.server.uploads.iterdir()), files_before)

    def test_agent_auth_and_cli(self):
        outsider = Client(self.url)
        self.assertEqual(outsider.request("/api/agent/me")[0], 401)
        status, me, _ = outsider.request("/api/agent/me", headers=self.authorization)
        self.assertEqual(status, 200)
        self.assertEqual(me["id"], 1)
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / "codex-skill/scripts/modus.py"), "--config", str(self.config)]
        result = subprocess.run(command + ["status"], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], 1)
        manifest = Path(self.directory.name) / "manifest.json"
        manifest.write_text(json.dumps({"request_id": "cli-submission", "operations": [{"method": "POST", "path": "/api/tasks", "data": {"title": "CLI 真实提交测试", "owner_id": 1, "start_date": "", "end_date": ""}}]}), encoding="utf-8")
        result = subprocess.run(command + ["submit", str(manifest)], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
