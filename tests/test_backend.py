"""Run with: python -m unittest discover -s tests -v"""

import base64
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import WorkbenchServer, connect_database


class Client:
    def __init__(self, url):
        self.url = url
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, path, method="GET", data=None, headers=None):
        request_headers = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf}
        request_headers.update(headers or {})
        request = Request(self.url + path, data=None if data is None else json.dumps(data).encode(), headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        content = response.read()
        result = json.loads(content) if response.headers.get_content_type() == "application/json" else content
        return response.status, result, response.headers

    def login(self, username, password):
        status, body, _ = self.request("/api/login", "POST", {"username": username, "password": password})
        if status != 200:
            raise AssertionError(body)
        self.csrf = body["csrf"]
        return body["user"]


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = WorkbenchServer(("127.0.0.1", 0), cls.directory.name)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.first = Client(cls.url)
        status, body, _ = cls.first.request("/api/setup", "POST", {
            "accounts": [
                {"name": "测试甲", "username": "alpha", "password": "Test-alpha-2026"},
                {"name": "测试乙", "username": "beta", "password": "Test-beta-2026"},
            ], "demo": True,
        })
        if status != 200:
            raise AssertionError(body)
        cls.first.csrf = body["csrf"]
        cls.first_id = body["user"]["id"]
        cls.second = Client(cls.url)
        cls.second_id = cls.second.login("beta", "Test-beta-2026")["id"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.directory.cleanup()

    def snapshot(self):
        status, body, _ = self.first.request("/api/state")
        self.assertEqual(status, 200)
        return body

    def new_task(self, owner=None, **overrides):
        today = self.snapshot()["today"]
        data = {"title": "测试事项", "owner_id": owner or self.first_id, "start_date": today, "end_date": today}
        data.update(overrides)
        status, body, _ = self.first.request("/api/tasks", "POST", data)
        self.assertEqual(status, 200, body)
        return body["id"]

    def test_login_required(self):
        outsider = Client(self.url)
        self.assertEqual(outsider.request("/api/state")[0], 401)
        self.assertEqual(outsider.request("/api/documents/1")[0], 401)

    def test_setup_cannot_replace_accounts(self):
        status, _, _ = self.first.request("/api/setup", "POST", {
            "accounts": [{"name": "A", "username": "a", "password": "abcdefgh"}, {"name": "B", "username": "b", "password": "abcdefgh"}],
        })
        self.assertEqual(status, 409)
        self.assertEqual(len(self.snapshot()["users"]), 2)

    def test_password_failure_and_case_insensitive_login(self):
        client = Client(self.url)
        self.assertEqual(client.request("/api/login", "POST", {"username": "alpha", "password": "wrong"})[0], 401)
        self.assertEqual(client.login("ALPHA", "Test-alpha-2026")["id"], self.first_id)

    def test_cookie_and_csrf(self):
        client = Client(self.url)
        status, body, headers = client.request("/api/login", "POST", {"username": "alpha", "password": "Test-alpha-2026"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        self.assertEqual(client.request("/api/tasks", "POST", {})[0], 403)
        client.csrf = body["csrf"]
        self.assertEqual(client.request("/api/tasks", "POST", {}, {"Origin": "https://example.com"})[0], 403)

    def test_session_logout(self):
        client = Client(self.url)
        client.login("alpha", "Test-alpha-2026")
        self.assertEqual(client.request("/api/logout", "POST", {})[0], 200)
        self.assertEqual(client.request("/api/state")[0], 401)

    def test_complete_idempotent_and_undo(self):
        identifier = self.new_task()
        for _ in range(3):
            self.assertEqual(self.first.request(f"/api/tasks/{identifier}", "PATCH", {"done": True})[0], 200)
        records = [record for record in self.snapshot()["completions"] if record["task_id"] == identifier]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["owner_id"], self.first_id)
        self.assertEqual(self.first.request(f"/api/tasks/{identifier}", "PATCH", {"done": False})[0], 200)
        self.assertFalse(any(record["task_id"] == identifier for record in self.snapshot()["completions"]))

    def test_concurrent_completion(self):
        identifier = self.new_task(title="并发完成测试")
        def complete(_):
            client = Client(self.url)
            client.login("alpha", "Test-alpha-2026")
            return client.request(f"/api/tasks/{identifier}", "PATCH", {"done": True})[0]
        with ThreadPoolExecutor(max_workers=3) as executor:
            self.assertEqual(list(executor.map(complete, range(3))), [200, 200, 200])
        self.assertEqual(sum(record["task_id"] == identifier for record in self.snapshot()["completions"]), 1)

    def test_partner_owns_completion(self):
        identifier = self.new_task(owner=self.second_id)
        self.assertEqual(self.first.request(f"/api/tasks/{identifier}", "PATCH", {"done": True})[0], 400)
        self.assertEqual(self.second.request(f"/api/tasks/{identifier}", "PATCH", {"done": True})[0], 200)
        record = next(record for record in self.snapshot()["completions"] if record["task_id"] == identifier)
        self.assertEqual(record["owner_id"], self.second_id)

    def test_task_validation(self):
        today = self.snapshot()["today"]
        data = {"title": "日期错误", "owner_id": self.first_id, "start_date": "2026-02-30", "end_date": today}
        self.assertEqual(self.first.request("/api/tasks", "POST", data)[0], 400)
        data.update(start_date=today, end_date=today, owner_id=999999)
        self.assertEqual(self.first.request("/api/tasks", "POST", data)[0], 400)
        data.update(owner_id=self.first_id, time="25:12")
        self.assertEqual(self.first.request("/api/tasks", "POST", data)[0], 400)
        data.update(time="", start_date="2026-01-03", end_date="2026-01-02")
        self.assertEqual(self.first.request("/api/tasks", "POST", data)[0], 400)

    def test_long_task_cross_year(self):
        identifier = self.new_task(kind="long", start_date="2026-12-30", end_date="2027-01-08")
        task = next(task for task in self.snapshot()["tasks"] if task["id"] == identifier)
        self.assertEqual(task["kind"], "long")
        self.assertEqual(task["end_date"], "2027-01-08")

    def test_project_stages_and_dynamic_sections(self):
        status, result, _ = self.first.request("/api/projects", "POST", {"name": "测试项目", "owner_id": self.first_id, "start_date": "2026-10-01", "end_date": "2027-05-01", "color": "#56836D"})
        self.assertEqual(status, 200)
        project_id = result["id"]
        self.assertEqual(len([section for section in self.snapshot()["sections"] if section["project_id"] == project_id]), 4)
        stage = {"project_id": project_id, "name": "阶段一", "start_date": "2026-10-01", "end_date": "2026-10-31"}
        self.assertEqual(self.first.request("/api/stages", "POST", stage)[0], 400)
        self.assertEqual(len([x for x in self.snapshot()["stages"] if x["project_id"]==project_id]),8)
        stage["end_date"] = "2027-06-01"
        self.assertEqual(self.first.request("/api/stages", "POST", stage)[0], 400)
        status, result, _ = self.first.request("/api/sections", "POST", {"project_id": project_id, "name": "合同"})
        self.assertEqual(status, 200)
        self.assertEqual(self.first.request(f'/api/sections/{result["id"]}', "DELETE")[0], 200)

    def test_upload_download_integrity(self):
        section = self.snapshot()["sections"][0]
        content = "真实文件内容，含中文及换行\nMODUS\n".encode()
        status, result, _ = self.first.request("/api/documents", "POST", {"section_id": section["id"], "owner_id": self.second_id, "name": "需求书.txt", "content": base64.b64encode(content).decode()})
        self.assertEqual(status, 200, result)
        status, downloaded, headers = self.first.request(f'/api/documents/{result["id"]}')
        self.assertEqual(status, 200)
        self.assertEqual(downloaded, content)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.first.request(f'/api/sections/{section["id"]}', "DELETE")[0], 400)

    def test_bad_file_and_color_rejected(self):
        section = self.snapshot()["sections"][0]
        self.assertEqual(self.first.request("/api/documents", "POST", {"section_id": section["id"], "owner_id": self.first_id, "name": "x", "content": "%%%"})[0], 400)
        self.assertEqual(self.first.request("/api/projects", "POST", {"name": "x", "owner_id": self.first_id, "start_date": "2026-01-01", "end_date": "2026-01-02", "color": 'red; background:url(x)'})[0], 400)

    def test_opportunity_source(self):
        status, result, _ = self.first.request("/api/opportunities", "POST", {"name": "潜在客户 A", "company": "客户公司", "owner_id": self.first_id, "description": "沟通结论", "next_step": "电话联系", "color": "#4A789B"})
        self.assertEqual(status, 200)
        identifier = self.new_task(opportunity_id=result["id"])
        task = next(task for task in self.snapshot()["tasks"] if task["id"] == identifier)
        self.assertEqual(task["opportunity_id"], result["id"])

    def test_database_persists_and_password_is_hashed(self):
        identifier = self.new_task(title="持久化检查")
        with connect_database(self.server.database) as connection:
            self.assertEqual(connection.execute("SELECT title FROM tasks WHERE id=?", (identifier,)).fetchone()[0], "持久化检查")
            stored = connection.execute("SELECT password_hash FROM users WHERE id=?", (self.first_id,)).fetchone()[0]
            self.assertNotIn("Test-alpha-2026", stored)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_static_path_and_security_headers(self):
        self.assertEqual(self.first.request("/server.py")[0], 404)
        self.assertEqual(self.first.request("/%2e%2e/server.py")[0], 404)
        status, body, headers = self.first.request("/")
        self.assertEqual(status, 200)
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("易方".encode(), body)
        status, _, headers = self.first.request("/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/javascript")


if __name__ == "__main__":
    unittest.main(verbosity=2)
