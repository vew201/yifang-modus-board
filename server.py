"""MODUS two-person workbench. Python 3.10+, standard library only."""

import argparse
from contextlib import contextmanager
import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
import webbrowser
from share import read_network_status
import workflow
import project_model
import codex_delivery
import trash_bin


ROOT = Path(__file__).resolve().parent
LOCAL_TZ = timezone(timedelta(hours=8))
MAX_BODY = 30 * 1024 * 1024
SESSION_LIFETIME = 7 * 24 * 60 * 60
RECORD_TABLES = {"projects", "opportunities", "tasks", "stages", "sections", "documents", "opportunity_files"}
DEFAULT_STAGE_NAMES = project_model.STAGES


class RecordConflict(Exception):
    pass


def timestamp():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + digest.hex()


def password_matches(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def connect_database(path):
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL, password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                csrf TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, client TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '', owner_id INTEGER NOT NULL REFERENCES users(id),
                start_date TEXT NOT NULL, end_date TEXT NOT NULL, color TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stages (
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL, position INTEGER NOT NULL,
                UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
                contact TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '初步接洽', next_step TEXT NOT NULL DEFAULT '',
                owner_id INTEGER NOT NULL REFERENCES users(id), color TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                owner_id INTEGER NOT NULL REFERENCES users(id),
                project_id INTEGER REFERENCES projects(id),
                opportunity_id INTEGER REFERENCES opportunities(id),
                kind TEXT NOT NULL DEFAULT 'daily' CHECK(kind IN ('daily','long')),
                start_date TEXT NOT NULL, end_date TEXT NOT NULL, time TEXT NOT NULL DEFAULT '',
                done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
                created_at TEXT NOT NULL,
                CHECK(project_id IS NULL OR opportunity_id IS NULL)
            );
            CREATE TABLE IF NOT EXISTS completions (
                task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
                owner_id INTEGER NOT NULL REFERENCES users(id),
                actor_id INTEGER NOT NULL REFERENCES users(id), completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                section_id INTEGER NOT NULL REFERENCES sections(id),
                owner_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                storage_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                key TEXT PRIMARY KEY, count INTEGER NOT NULL, first_attempt REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS task_owner_dates ON tasks(owner_id, start_date, end_date);
            CREATE TABLE IF NOT EXISTS agent_tokens (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                request_id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES users(id),
                payload_hash TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id),
                opportunity_id INTEGER REFERENCES opportunities(id), task_id INTEGER REFERENCES tasks(id),
                owner_id INTEGER NOT NULL REFERENCES users(id), text TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_archives (
                project_id INTEGER PRIMARY KEY REFERENCES projects(id),
                owner_id INTEGER NOT NULL REFERENCES users(id), completed_at TEXT NOT NULL
            );
        """)
        workflow.initialize(connection)
        for table in RECORD_TABLES:
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name in ("revision", "deleted"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        for table in ("projects", "stages"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "completed_at" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN completed_at TEXT")
        task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "stage_id" not in task_columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN stage_id INTEGER REFERENCES stages(id)")
        project_model.initialize(connection)
        synchronize_project_completion(connection)
        trash_bin.initialize(connection)


def date_range(data, allow_pending=False):
    start, end = data.get("start_date", ""), data.get("end_date", "")
    if allow_pending and start == end == "":
        return "", ""
    start = date_value(start, "开始日期")
    end = date_value(end, "结束日期")
    if end < start:
        raise ValueError("结束日期应晚于或等于开始日期")
    return start, end


def rows(connection, sql, parameters=()):
    return [dict(row) for row in connection.execute(sql, parameters)]


def date_value(value, field):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(f"请填写有效的{field}") from None
    return value


def text_value(data, name, label, limit=300, required=False):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"{label}格式不正确")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{label}")
    if len(value) > limit:
        raise ValueError(f"{label}请控制在 {limit} 字以内")
    return value


def reference(connection, table, value, label):
    if isinstance(value, bool):
        raise ValueError(f"请选择有效的{label}")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"请选择{label}") from None
    active = " AND deleted=0" if table in RECORD_TABLES else ""
    if not connection.execute(f"SELECT id FROM {table} WHERE id = ?{active}", (value,)).fetchone():
        raise ValueError(f"{label}不存在")
    return value


def add_project(connection, data):
    name = text_value(data, "name", "项目名称", required=True)
    owner = reference(connection, "users", data.get("owner_id"), "负责人")
    start, end = date_range(data, allow_pending=True)
    color = data.get("color", "#7864A5")
    if color not in PALETTE:
        raise ValueError("请选择提供的项目颜色")
    project_id = connection.execute(
        "INSERT INTO projects(id,name,client,description,owner_id,start_date,end_date,color,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (trash_bin.next_id(connection,'projects'),name, text_value(data, "client", "客户名称"), text_value(data, "description", "项目说明", 10000), owner, start, end, color, timestamp()),
    ).lastrowid
    for position, section in enumerate(["售前需求", "技术文件", "交付物", "售后与培训"]):
        connection.execute("INSERT INTO sections(id,project_id,name,position) VALUES(?,?,?,?)", (trash_bin.next_id(connection,'sections'),project_id, section, position))
    project_model.ensure_stages(connection, project_id)
    connection.execute("INSERT INTO project_model_migrations VALUES(?,?,?)", (project_id, '{}', timestamp()))
    return project_id


PALETTE = ["#7864A5", "#4A789B", "#56836D", "#B85E57", "#AB7A3C", "#5C8587", "#97617D", "#6C738D"]


def editable_values(connection, table, data):
    values = {}
    string_fields = {
        "projects": {"name": ("项目名称", 300, True), "client": ("客户名称", 300, False), "description": ("项目说明", 10000, False)},
        "opportunities": {"name": ("意向名称", 300, True), "company": ("公司名称", 300, False), "contact": ("联系人", 300, False), "description": ("沟通说明", 10000, False), "status": ("跟进状态", 40, True), "next_step": ("下一步安排", 1000, False)},
        "tasks": {"title": ("事项名称", 300, True), "description": ("事项说明", 10000, False), "time": ("具体时刻", 5, False)},
        "stages": {"name": ("阶段名称", 100, True)},
        "sections": {"name": ("区域名称", 60, True)},
        "documents": {"name": ("文件名称", 200, True)},
        "opportunity_files": {"name": ("文件名称", 200, True), "version": ("文件版本", 200, False)},
    }
    for name, (label, limit, required) in string_fields[table].items():
        values[name] = text_value(data, name, label, limit, required)
    if table in {"projects", "opportunities", "tasks", "documents", "opportunity_files"}:
        values["owner_id"] = reference(connection, "users", data.get("owner_id"), "负责人")
    if table in {"projects", "opportunities"}:
        if data.get("color") not in PALETTE:
            raise ValueError("请选择提供的颜色")
        values["color"] = data["color"]
    if table == "opportunities":
        values.update(workflow.opportunity_values(connection, data))
    if table == "projects":
        values["final_payment_at"] = workflow.instant(data.get("final_payment_at", ""), "尾款到账时间", historical=True)
        values["payment_note"] = text_value(data, "payment_note", "尾款备注", 2000)
    if table in {"projects", "tasks", "stages"}:
        values["start_date"], values["end_date"] = date_range(data, allow_pending=True)
    if table == "tasks":
        if data.get("kind") not in {"daily", "long"}:
            raise ValueError("任务类型不正确")
        values["kind"] = data["kind"]
        values["project_id"] = reference(connection, "projects", data["project_id"], "项目") if data.get("project_id") else None
        values["opportunity_id"] = reference(connection, "opportunities", data["opportunity_id"], "合作意向") if data.get("opportunity_id") else None
        if values["project_id"] and values["opportunity_id"]:
            raise ValueError("一条任务请选择一个所属项目或合作意向")
        values["stage_id"] = validate_task_stage(connection, values["project_id"], data.get("stage_id"))
        if values["time"]:
            try:
                if datetime.strptime(values["time"], "%H:%M").strftime("%H:%M") != values["time"]:
                    raise ValueError
            except ValueError:
                raise ValueError("请填写有效的具体时刻") from None
            if not values["start_date"] or values["start_date"] != values["end_date"]:
                raise ValueError("具体时刻需要对应明确的同一天")
    if table in {"stages", "sections"}:
        values["project_id"] = reference(connection, "projects", data.get("project_id"), "项目")
        try:
            values["position"] = max(0, int(data.get("position", 0)))
        except (ValueError, TypeError):
            raise ValueError("排列顺序请填写非负整数") from None
    if table == "stages":
        project = connection.execute("SELECT * FROM projects WHERE id=?", (values["project_id"],)).fetchone()
        if values["start_date"] and (not project["start_date"] or values["start_date"] < project["start_date"] or values["end_date"] > project["end_date"]):
            raise ValueError("阶段起止日期需要位于项目周期内，请先完善项目日期")
    if table == "documents":
        values["section_id"] = reference(connection, "sections", data.get("section_id"), "文件区域")
        values["project_id"] = connection.execute("SELECT project_id FROM sections WHERE id=?", (values["section_id"],)).fetchone()[0]
        values["stage_id"] = project_model.classify(connection, values["project_id"], data.get("name", "")) if data.get("stage_id") == "auto" else validate_task_stage(connection, values["project_id"], data.get("stage_id"))
        if any(ord(character) < 32 for character in values["name"]) or any(character in values["name"] for character in "/\\"):
            raise ValueError("文件名不能包含路径或控制字符")
    return values


def record_for_update(connection, table, identifier, data):
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    identifier = reference(connection, table, identifier, "记录")
    record = dict(connection.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone())
    if "revision" in data and data["revision"] != record["revision"]:
        raise RecordConflict("内容已被另一人修改，请刷新后核对再保存")
    return record


def maintain_record(connection, method, table, identifier, data, actor):
    record = record_for_update(connection, table, identifier, data)
    if method == "DELETE":
        if table == "stages":
            raise ValueError("项目固定八个阶段，不能删除")
        if table == "opportunity_files" and connection.execute("SELECT id FROM opportunities WHERE deleted=0 AND (solution_file_id=? OR quote_file_id=?)", (record["id"], record["id"])).fetchone():
            raise ValueError("此文件是当前最终方案或报价，请先选择替代文件")
        connection.execute(f"UPDATE {table} SET deleted=1,deleted_at=?,deleted_by=?,revision=revision+1 WHERE id=?", (timestamp(),actor['user_id'],record["id"]))
        return {"ok": True, "id": record["id"], "recycled": True}
    merged = {**record, **data}
    if table == "opportunities" and "outcome" in data and data["outcome"] != record["outcome"]:
        raise ValueError("请使用失败归档、转入开发或重新跟进操作更新结果")
    if table == "stages":
        values = project_model.stage_values(connection, record, data, actor)
        connection.execute("INSERT INTO stage_history(stage_id,snapshot,saved_at,actor_id) VALUES(?,?,?,?)", (record["id"], json.dumps(record,ensure_ascii=False), timestamp(), actor["user_id"]))
    else:
        values = editable_values(connection, table, merged)
    if table == "projects":
        values.update(project_model.project_completion(record,data,actor))
    if table == "tasks" and record["auto_followup"]:
        if any(values[key] != record[key] for key in ("opportunity_id", "project_id", "owner_id", "kind")):
            raise ValueError("自动跟进的归属和负责人请在合作意向中调整")
        changed_schedule = any(values[key] != record[key] for key in ("start_date", "end_date", "time"))
        if changed_schedule:
            if not values["start_date"] or values["start_date"] != values["end_date"] or not values["time"]:
                raise ValueError("自动跟进需要明确的同一天和具体时刻")
            due = workflow.instant(values["start_date"] + "T" + values["time"], "跟进时间")
            values["time_basis"] = "手动调整跟进时间"
            connection.execute("UPDATE opportunities SET next_followup_at=?,time_basis=?,revision=revision+1 WHERE id=?", (due, values["time_basis"], record["opportunity_id"]))
    if table == "projects":
        outside = connection.execute("SELECT id FROM stages WHERE project_id=? AND deleted=0 AND start_date<>'' AND (start_date<? OR end_date>?)", (record["id"], values["start_date"], values["end_date"])).fetchone()
        if outside:
            raise ValueError("已有阶段超出新的项目周期，请先调整阶段日期")
    if table == "tasks" and "done" in data:
        if not isinstance(data["done"], bool):
            raise ValueError("完成状态格式不正确")
        if record["owner_id"] != actor["user_id"]:
            raise ValueError("请由事项负责人标记完成")
        if values["owner_id"] != record["owner_id"]:
            raise ValueError("请分别进行改派和标记完成")
        values["done"] = int(data["done"])
        if not data["done"] and record["done"] and record["auto_followup"]:
            workflow.cancel_followups(connection, record["opportunity_id"])
            due = workflow.instant(record["start_date"] + "T" + record["time"], "原跟进时间")
            connection.execute("UPDATE opportunities SET next_followup_at=?,time_basis=?,revision=revision+1 WHERE id=?", (due, record["time_basis"], record["opportunity_id"]))
        if data["done"]:
            connection.execute("INSERT OR IGNORE INTO completions VALUES(?,?,?,?)", (record["id"], record["owner_id"], actor["user_id"], timestamp()))
        else:
            connection.execute("DELETE FROM completions WHERE task_id=?", (record["id"],))
    assignments = ",".join(f"{field}=?" for field in values)
    connection.execute(f"UPDATE {table} SET {assignments},revision=revision+1 WHERE id=?", (*values.values(), record["id"]))
    if table == "opportunities":
        updated = dict(connection.execute("SELECT * FROM opportunities WHERE id=?", (record["id"],)).fetchone())
        workflow.after_opportunity_update(connection, record, updated)
    if table == "tasks" and data.get("done") is True and not record["done"] and record["auto_followup"]:
        item = connection.execute("SELECT * FROM opportunities WHERE id=?", (record["opportunity_id"],)).fetchone()
        if item and item["phase"] == "proposal":
            workflow.schedule(connection, item["id"], timestamp(), hours=48)
    return {"ok": True, "id": record["id"], "revision": connection.execute(f"SELECT revision FROM {table} WHERE id=?", (record["id"],)).fetchone()[0]}


def resolve_submission_refs(value, results):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            try:
                index, field = value["$ref"].split(".", 1)
                return results[int(index)][field]
            except (ValueError, IndexError, KeyError, AttributeError, TypeError):
                raise ValueError("提交中的结果引用无效") from None
        return {key: resolve_submission_refs(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_submission_refs(item, results) for item in value]
    return value


def validate_task_stage(connection, project_id, stage_id):
    if not stage_id:
        return None
    stage_id = reference(connection, "stages", stage_id, "关联阶段")
    stage = connection.execute("SELECT project_id FROM stages WHERE id=?", (stage_id,)).fetchone()
    if not project_id or stage["project_id"] != project_id:
        raise ValueError("关联阶段需要属于所选项目")
    return stage_id


def synchronize_project_completion(connection):
    project_model.synchronize_archives(connection)


def seed_demo(connection, first, second):
    today = datetime.now(LOCAL_TZ).date()
    day = lambda delta: (today + timedelta(days=delta)).isoformat()
    project = add_project(connection, {
        "name": "示例 · 园区智能体", "client": "演示客户", "description": "用于体验项目阶段、文件分类和任务时间轴的示例项目。",
        "owner_id": first, "start_date": day(-14), "end_date": day(70), "color": PALETTE[0],
    })
    for position, (start, end) in enumerate([(-14, 6), (7, 45), (46, 70)]):
        connection.execute("UPDATE stages SET start_date=?,end_date=? WHERE project_id=? AND position=?", (day(start), day(end), project, position))
    opportunity = connection.execute(
        "INSERT INTO opportunities(name,company,contact,description,next_step,owner_id,color,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("示例 · AI 入企合作", "演示意向方", "待补充", "希望探索 AI 在企业实际业务中的应用。此记录用于试用。", "电话确认业务场景与合作方式", second, PALETTE[1], timestamp()),
    ).lastrowid
    examples = [
        ("制作项目需求书", "整理需求范围和本期交付清单。", first, project, None, "daily", day(0), day(2), ""),
        ("确认方案边界", "准备问题清单，与项目对接人逐项确认。", first, project, None, "daily", day(1), day(1), "14:00"),
        ("整理技术方案", "梳理模块、依赖条件和实施安排。", second, project, None, "daily", day(0), day(3), ""),
        ("与意向方电话沟通", "确认业务痛点与下一次沟通安排。", second, None, opportunity, "daily", day(1), day(1), "10:30"),
        ("持续发掘潜在客户", "每天将发现的客户分别建立意向和具体跟进事项。", first, None, None, "long", day(0), day(30), ""),
    ]
    for item in examples:
        connection.execute("INSERT INTO tasks(title,description,owner_id,project_id,opportunity_id,kind,start_date,end_date,time,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (*item, timestamp()))
    for task in list(connection.execute("SELECT id,title FROM tasks WHERE project_id=?",(project,))):
        connection.execute("UPDATE tasks SET stage_id=? WHERE id=?",(project_model.classify(connection,project,task["title"]),task["id"]))
    connection.execute("UPDATE opportunities SET last_activity_at=created_at WHERE id=?",(opportunity,))


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.database = self.data_dir / "modus.sqlite3"
        self.uploads = self.data_dir / "files"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.setup_lock = threading.Lock()
        initialize_database(self.database)
        self.delivery = codex_delivery.Delivery(ROOT, self.data_dir)
        with connect_database(self.database) as connection:
            self.delivery.initialize_database(connection)
        self.trash_confirmation_key = secrets.token_bytes(32)
        self.trash_next_sweep = 0
        self.trash_worker = None
        super().__init__(address, Handler)

    def service_actions(self):
        trash_bin.service_actions(self)

    def server_close(self):
        super().server_close()
        if self.trash_worker:
            self.trash_worker.join(timeout=20)


class Handler(BaseHTTPRequestHandler):
    server_version = "MODUS/1.0"

    def log_message(self, format_string, *args):
        # Request bodies and credentials are never logged.
        if self.path.split("?")[0] != "/api/state":
            super().log_message(format_string, *args)

    def reply(self, status, body, headers=None, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (headers or {}).get("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        for name, value in (headers or {}).items():
            if name.lower() != "cache-control":
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            raise ValueError("提交内容为空或超过 30 MB；单文件上限为 20 MB")
        try:
            result = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("提交内容格式不正确") from None
        if not isinstance(result, dict):
            raise ValueError("提交内容应为对象")
        return result

    def session(self, connection):
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            record = connection.execute(
                "SELECT a.user_id,u.name,u.username FROM agent_tokens a JOIN users u ON u.id=a.user_id WHERE a.token_hash=?",
                (token_hash(authorization[7:]),),
            ).fetchone()
            return {**dict(record), "is_agent": True, "csrf": ""} if record else None
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        token = cookie.get("modus_session")
        if not token:
            return None
        return connection.execute(
            "SELECT s.*,u.name,u.username FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?",
            (token_hash(token.value), time.time()),
        ).fetchone()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path = urlparse(self.path).path
        try:
            with connect_database(self.server.database) as connection:
                if not path.startswith("/api/"):
                    return self.serve_static(path, method)
                if method != "GET":
                    origin = self.headers.get("Origin")
                    public_origin = read_network_status(self.server.data_dir).get("url")
                    if origin and urlparse(origin).netloc != self.headers.get("Host") and origin != public_origin:
                        return self.reply(403, {"error": "请求来源不匹配"})
                if path == "/api/health" and method == "GET":
                    self.server.delivery.maybe_publish_address()
                    return self.reply(200, {"ok": True, "app": "modus-workbench"})
                if path == "/api/session" and method == "GET":
                    session = self.session(connection)
                    return self.reply(200, {
                        "needs_setup": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0,
                        "user": {"id": session["user_id"], "name": session["name"]} if session else None,
                        "csrf": session["csrf"] if session else None,
                    })
                if path == "/api/setup" and method == "POST":
                    return self.initialize_accounts(connection)
                if path == "/api/login" and method == "POST":
                    return self.login(connection)
                if path == "/api/codex/activate" and method == "POST":
                    return self.reply(200,self.server.delivery.activate(connection,self.body()))
                session = self.session(connection)
                if not session:
                    return self.reply(401, {"error": "请先登录"})
                is_agent = isinstance(session, dict) and session.get("is_agent")
                if method != "GET" and not is_agent and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                    return self.reply(403, {"error": "登录校验失效，请刷新页面"})
                if path.startswith("/api/codex/"):
                    payload = self.body() if method == "POST" else {}
                    handled = self.server.delivery.route(self,connection,method,path,payload,session)
                    if handled is not False:
                        return handled
                    return self.reply(404,{"error":"接入操作不存在"})
                if method != "GET" and not self.server.delivery.check_write(self,connection,session):
                    return
                if path == "/api/agent/me" and method == "GET":
                    return self.reply(200, {"id": session["user_id"], "name": session["name"], "username": session["username"]})
                if path == "/api/agent-token" and method == "POST":
                    if is_agent:
                        return self.reply(403, {"error": "请通过网页登录账号管理接入凭证"})
                    token = secrets.token_urlsafe(36)
                    connection.execute("INSERT INTO agent_tokens VALUES(?,?,?,?)", (token_hash(token), session["user_id"], "Codex", timestamp()))
                    connection.commit()
                    return self.reply(200, {"token": token, "user_id": session["user_id"], "name": session["name"]})
                if path == "/api/logout" and method == "POST":
                    if is_agent:
                        return self.reply(400, {"error": "此接口用于网页退出登录"})
                    connection.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
                    connection.commit()
                    return self.reply(200, {"ok": True}, {"Set-Cookie": "modus_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"})
                if path == "/api/state" and method == "GET":
                    return self.reply(200, self.state(connection))
                if path == "/api/trash" and method == "GET":
                    return self.reply(200, trash_bin.listing(connection))
                if path in {"/api/trash/preview", "/api/trash/purge"} and method == "POST":
                    data = self.body()
                    connection.execute("BEGIN IMMEDIATE")
                    if path.endswith('/preview'):
                        return self.reply(200,trash_bin.preview(self.server,connection,data,session['user_id']))
                    result = trash_bin.purge(self.server,connection,data,session['user_id'])
                    connection.commit()
                    # Only unlink after commit; failed unlinks remain durably queued.
                    try:result.update(trash_bin.collect_files(self.server))
                    except sqlite3.OperationalError:result['pending_files'] = result['file_count']
                    return self.reply(200,result)
                if path == "/api/network" and method == "GET":
                    return self.reply(200, read_network_status(self.server.data_dir))
                if path.startswith("/api/documents/") and method == "GET":
                    return self.download(connection, path)
                if path.startswith("/api/opportunity_files/") and method == "GET":
                    return workflow.download(self, connection, path)
                data = self.body() if method in ("POST", "PATCH") or (method == "DELETE" and int(self.headers.get("Content-Length", "0")) > 0) else {}
                if path == "/api/agent/submit" and method == "POST":
                    return self.reply(200, self.submit_batch(connection, data, session))
                result = self.mutate(connection, method, path, data, session)
                synchronize_project_completion(connection)
                connection.commit()
                return self.reply(200, result)
        except (RecordConflict, trash_bin.TrashConflict) as error:
            self.reply(409, {"error": str(error)})
        except ValueError as error:
            self.reply(400, {"error": str(error)})
        except sqlite3.IntegrityError:
            self.reply(409, {"error": "名称已存在或数据关系已变化，请刷新后重试"})
        except sqlite3.OperationalError:
            self.reply(503, {"error": "数据库暂时繁忙，请稍后重试"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as error:
            print(f"Server error: {type(error).__name__}", flush=True)
            self.reply(500, {"error": "处理失败，请重试；本次操作未完成"})

    def initialize_accounts(self, connection):
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return self.reply(409, {"error": "工作台已设置，请登录"})
        data = self.body()
        accounts = data.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != 2:
            raise ValueError("请设置两个人的账号")
        prepared = []
        for account in accounts:
            if not isinstance(account, dict):
                raise ValueError("账号格式不正确")
            name = text_value(account, "name", "姓名", 40, True)
            username = text_value(account, "username", "登录账号", 40, True)
            if not all(character.isascii() and (character.isalnum() or character in "_-.@") for character in username):
                raise ValueError("登录账号请使用英文字母、数字或 _-.@")
            password = account.get("password", "")
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise ValueError("密码长度需为 8—128 位")
            prepared.append((username, name, password_hash(password)))
        if prepared[0][0].lower() == prepared[1][0].lower():
            raise ValueError("两人的登录账号需要不同")
        with self.server.setup_lock:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return self.reply(409, {"error": "工作台已设置，请登录"})
            identifiers = []
            for account in prepared:
                identifiers.append(connection.execute("INSERT INTO users(username,name,password_hash) VALUES(?,?,?)", account).lastrowid)
            if data.get("demo") is True:
                seed_demo(connection, *identifiers)
            connection.commit()
        return self.issue_session(connection, identifiers[0])

    def login(self, connection):
        data = self.body()
        username = text_value(data, "username", "登录账号", 40, True)
        password = data.get("password", "")
        if not isinstance(password, str) or len(password) > 128:
            raise ValueError("账号或密码不正确")
        key = self.client_address[0]
        attempt = connection.execute("SELECT * FROM login_attempts WHERE key=?", (key,)).fetchone()
        if attempt and attempt["count"] >= 12 and time.time() - attempt["first_attempt"] < 300:
            return self.reply(429, {"error": "尝试次数较多，请五分钟后重试"})
        user = connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user or not password_matches(password, user["password_hash"]):
            if not attempt or time.time() - attempt["first_attempt"] >= 300:
                connection.execute("INSERT OR REPLACE INTO login_attempts VALUES(?,1,?)", (key, time.time()))
            else:
                connection.execute("UPDATE login_attempts SET count=count+1 WHERE key=?", (key,))
            connection.commit()
            return self.reply(401, {"error": "账号或密码不正确"})
        connection.execute("DELETE FROM login_attempts WHERE key=?", (key,))
        return self.issue_session(connection, user["id"])

    def issue_session(self, connection, user_id):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        connection.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        connection.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), user_id, csrf, time.time() + SESSION_LIFETIME))
        user = connection.execute("SELECT id,name FROM users WHERE id=?", (user_id,)).fetchone()
        connection.commit()
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https" else ""
        self.reply(200, {"user": dict(user), "csrf": csrf}, {"Set-Cookie": f"modus_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_LIFETIME}{secure}"})

    def state(self, connection):
        if not connection.in_transaction:
            connection.execute("BEGIN")
        return {
            "today": datetime.now(LOCAL_TZ).date().isoformat(),
            "server_time": timestamp(),
            "stage_meanings": project_model.MEANINGS,
            "users": rows(connection, "SELECT id,name,username FROM users ORDER BY id"),
            "projects": rows(connection, "SELECT * FROM projects WHERE deleted=0 ORDER BY id DESC"),
            "stages": rows(connection, "SELECT s.*,COUNT(t.id) AS task_count,COALESCE(SUM(t.done),0) AS done_count FROM stages s JOIN projects p ON p.id=s.project_id LEFT JOIN tasks t ON t.stage_id=s.id AND t.project_id=s.project_id AND t.deleted=0 WHERE s.deleted=0 AND p.deleted=0 GROUP BY s.id ORDER BY s.position,s.id"),
            "sections": rows(connection, "SELECT s.* FROM sections s JOIN projects p ON p.id=s.project_id WHERE s.deleted=0 AND p.deleted=0 ORDER BY s.position,s.id"),
            "opportunities": [project_model.aging(item) for item in rows(connection, "SELECT * FROM opportunities WHERE deleted=0 ORDER BY id DESC")],
            "tasks": rows(connection, "SELECT t.* FROM tasks t LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN opportunities o ON o.id=t.opportunity_id WHERE t.deleted=0 AND t.cancelled_at='' AND COALESCE(p.deleted,0)=0 AND COALESCE(o.deleted,0)=0 ORDER BY t.start_date,t.time,t.id"),
            "completions": rows(connection, "SELECT c.* FROM completions c JOIN tasks t ON t.id=c.task_id LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN opportunities o ON o.id=t.opportunity_id WHERE t.deleted=0 AND COALESCE(p.deleted,0)=0 AND COALESCE(o.deleted,0)=0 ORDER BY c.completed_at DESC"),
            "documents": rows(connection, "SELECT d.id,d.project_id,d.section_id,d.owner_id,d.name,d.size,d.uploaded_at,d.revision,d.stage_id FROM documents d JOIN projects p ON p.id=d.project_id JOIN sections s ON s.id=d.section_id WHERE d.deleted=0 AND p.deleted=0 AND s.deleted=0 ORDER BY d.id DESC"),
            "conversations": rows(connection, "SELECT * FROM conversations ORDER BY id DESC"),
            "stage_history": rows(connection, "SELECT * FROM stage_history ORDER BY id DESC"),
            "conversation_history": rows(connection, "SELECT * FROM conversation_history ORDER BY id DESC"),
            "opportunity_files": rows(connection, "SELECT f.* FROM opportunity_files f JOIN opportunities o ON o.id=f.opportunity_id WHERE f.deleted=0 AND o.deleted=0 ORDER BY f.id DESC"),
            "project_archives": rows(connection, "SELECT a.* FROM project_archives a JOIN projects p ON p.id=a.project_id WHERE p.deleted=0 ORDER BY a.completed_at DESC"),
            "trash": trash_bin.listing(connection)['items'],
        }

    def submit_batch(self, connection, data, actor):
        request_id = text_value(data, "request_id", "提交编号", 120, True)
        operations = data.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 30:
            raise ValueError("每次提交需要包含 1—30 个操作")
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute("SELECT * FROM submissions WHERE request_id=?", (request_id,)).fetchone()
        if prior:
            if prior["actor_id"] != actor["user_id"] or prior["payload_hash"] != digest:
                raise RecordConflict("提交编号已用于其他内容，请核对记录后使用新的编号")
            return {**json.loads(prior["result"]), "replayed": True}
        results = []
        self.submission_files = []
        try:
            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("操作格式不正确")
                method = operation.get("method", "POST").upper()
                path = operation.get("path", "")
                if method not in {"POST", "PATCH", "DELETE"} or not isinstance(path, str):
                    raise ValueError("操作方法或路径不正确")
                payload = resolve_submission_refs(operation.get("data", {}), results)
                if not isinstance(payload, dict):
                    raise ValueError("操作内容应为对象")
                results.append(self.mutate(connection, method, path, payload, actor))
            synchronize_project_completion(connection)
            result = {"ok": True, "request_id": request_id, "results": results}
            connection.execute("INSERT INTO submissions VALUES(?,?,?,?,?)", (request_id, actor["user_id"], digest, json.dumps(result, ensure_ascii=False), timestamp()))
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            for file_path in self.submission_files:
                file_path.unlink(missing_ok=True)
            raise
        finally:
            del self.submission_files

    def mutate(self, connection, method, path, data, session):
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        context = project_model.activity_context(connection, method, path, data)
        result = self.mutate_raw(connection, method, path, data, session)
        project_model.touch_activity(connection, context, result)
        return result

    def mutate_raw(self, connection, method, path, data, session):
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        parts = path.strip("/").split("/")
        result = workflow.mutate(self, connection, method, path, data, session)
        if result is not None:
            return result
        if len(parts) == 3 and parts[0] == "api" and parts[1] in RECORD_TABLES and method in {"PATCH", "DELETE"}:
            if method == "DELETE" and parts[1] == "sections":
                if connection.execute("SELECT id FROM documents WHERE section_id=? AND deleted=0", (parts[2],)).fetchone():
                    raise ValueError("请先移动区域中的文件，或明确使用合并区域功能")
            return maintain_record(connection, method, parts[1], parts[2], data, session)
        if len(parts) == 4 and parts[:2] == ["api", "restore"] and method == "POST" and parts[2] in RECORD_TABLES:
            table = parts[2]
            if table == "stages":
                raise ValueError("旧阶段仅作历史保留，当前统一使用固定八阶段")
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            return trash_bin.restore(connection, table, parts[3])
        if path == "/api/conversations" and method == "POST":
            owner = reference(connection, "users", data.get("owner_id"), "负责人")
            related = {}
            for key, table in (("project_id", "projects"), ("opportunity_id", "opportunities"), ("task_id", "tasks")):
                related[key] = reference(connection, table, data[key], "所属记录") if data.get(key) else None
            if not any(related.values()):
                raise ValueError("对谈需要关联项目、意向或任务")
            identifier = connection.execute("INSERT INTO conversations(id,project_id,opportunity_id,task_id,owner_id,text,summary,created_at) VALUES(?,?,?,?,?,?,?,?)", (trash_bin.next_id(connection,'conversations'),*related.values(), owner, text_value(data, "text", "对谈原文", 100000, True), text_value(data, "summary", "对谈摘要", 10000), timestamp())).lastrowid
            if related["project_id"]:
                stage = project_model.classify(connection,related["project_id"],data.get("summary", "")) if data.get("stage_id") == "auto" else validate_task_stage(connection, related["project_id"], data.get("stage_id"))
                connection.execute("UPDATE conversations SET stage_id=? WHERE id=?", (stage,identifier))
            return {"id": identifier}
        if method == "POST" and path == "/api/projects":
            return {"id": add_project(connection, data)}
        if method == "POST" and path == "/api/opportunities":
            name = text_value(data, "name", "意向名称", required=True)
            owner = reference(connection, "users", data.get("owner_id"), "负责人")
            color = data.get("color", PALETTE[1])
            if color not in PALETTE:
                raise ValueError("请选择提供的意向颜色")
            identifier = connection.execute(
                "INSERT INTO opportunities(id,name,company,contact,description,status,next_step,owner_id,color,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (trash_bin.next_id(connection,'opportunities'),name, text_value(data, "company", "公司名称"), text_value(data, "contact", "联系人"),
                 text_value(data, "description", "沟通说明", 10000), text_value(data, "status", "跟进状态", 40) or "初步接洽",
                 text_value(data, "next_step", "下一步安排", 1000), owner, color, timestamp()),
            ).lastrowid
            if data.get("phase", "lead") != "lead":
                raise ValueError("新意向请从线索发现登记，追加实际沟通后进入沟通阶段")
            record = dict(connection.execute("SELECT * FROM opportunities WHERE id=?", (identifier,)).fetchone())
            extra = workflow.opportunity_values(connection, {**record, **data})
            connection.execute(f"UPDATE opportunities SET {','.join(key+'=?' for key in extra)} WHERE id=?", (*extra.values(), identifier))
            return {"id": identifier}
        if method == "POST" and path == "/api/tasks":
            owner = reference(connection, "users", data.get("owner_id"), "负责人")
            title = text_value(data, "title", "事项名称", required=True)
            start, end = date_range(data, allow_pending=True)
            kind = data.get("kind", "daily")
            if kind not in ("daily", "long"):
                raise ValueError("任务类型不正确")
            clock = text_value(data, "time", "具体时刻", 5)
            if clock:
                try:
                    if datetime.strptime(clock, "%H:%M").strftime("%H:%M") != clock:
                        raise ValueError
                except ValueError:
                    raise ValueError("请填写有效的具体时刻") from None
                if not start or start != end:
                    raise ValueError("具体时刻用于同一天的安排；跨天任务请留空时刻")
            project = reference(connection, "projects", data["project_id"], "项目") if data.get("project_id") else None
            opportunity = reference(connection, "opportunities", data["opportunity_id"], "合作意向") if data.get("opportunity_id") else None
            if project and opportunity:
                raise ValueError("一条任务请选择一个所属项目或合作意向")
            stage = validate_task_stage(connection, project, data.get("stage_id"))
            identifier = connection.execute(
                "INSERT INTO tasks(id,title,description,owner_id,project_id,opportunity_id,stage_id,kind,start_date,end_date,time,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (trash_bin.next_id(connection,'tasks'),title, text_value(data, "description", "事项说明", 10000), owner, project, opportunity, stage, kind, start, end, clock, timestamp()),
            ).lastrowid
            return {"id": identifier}
        if method == "POST" and path == "/api/stages":
            raise ValueError("项目固定八个阶段，不能新增阶段")
        if method == "POST" and path == "/api/sections":
            project = reference(connection, "projects", data.get("project_id"), "项目")
            name = text_value(data, "name", "区域名称", 60, True)
            position = connection.execute("SELECT COUNT(*) FROM sections WHERE project_id=?", (project,)).fetchone()[0]
            identifier = connection.execute("INSERT INTO sections(id,project_id,name,position) VALUES(?,?,?,?)", (trash_bin.next_id(connection,'sections'),project, name, position)).lastrowid
            return {"id": identifier}
        if method == "POST" and path == "/api/documents":
            return self.upload(connection, data)
        raise ValueError("未找到此操作")

    def upload(self, connection, data):
        section = reference(connection, "sections", data.get("section_id"), "文件区域")
        project = connection.execute("SELECT project_id FROM sections WHERE id=?", (section,)).fetchone()[0]
        owner = reference(connection, "users", data.get("owner_id"), "负责人")
        stage = validate_task_stage(connection, project, data.get("stage_id")) if "stage_id" in data and data.get("stage_id") != "auto" else project_model.classify(connection, project, data.get("name", ""))
        name = text_value(data, "name", "文件名称", 200, True).replace("\\", "/").rsplit("/", 1)[-1]
        if any(ord(character) < 32 for character in name) or not name:
            raise ValueError("文件名不正确")
        try:
            content = base64.b64decode(data.get("content", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError("文件内容不完整，请重新上传") from None
        if not content or len(content) > 20 * 1024 * 1024:
            raise ValueError("文件需大于 0 字节且不超过 20 MB")
        storage_name = secrets.token_hex(24)
        file_path = self.server.uploads / storage_name
        try:
            file_path.write_bytes(content)
            identifier = connection.execute("INSERT INTO documents(id,project_id,section_id,owner_id,name,storage_name,size,uploaded_at,stage_id) VALUES(?,?,?,?,?,?,?,?,?)", (trash_bin.next_id(connection,'documents'),project, section, owner, name, storage_name, len(content), timestamp(), stage)).lastrowid
            if hasattr(self, "submission_files"):
                self.submission_files.append(file_path)
            else:
                connection.commit()
        except Exception:
            file_path.unlink(missing_ok=True)
            raise
        return {"id": identifier}

    def download(self, connection, path):
        identifier = reference(connection, "documents", path.rsplit("/", 1)[-1], "文件")
        document = connection.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()
        file_path = self.server.uploads / document["storage_name"]
        if not file_path.is_file():
            return self.reply(404, {"error": "文件原件不存在，请检查 data/files 目录"})
        from urllib.parse import quote
        return self.reply(200, file_path.read_bytes(), {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(document["name"])}, "application/octet-stream")

    def serve_static(self, path, method):
        if method != "GET":
            return self.reply(405, {"error": "不支持此方法"})
        relative = unquote(path).lstrip("/") or "index.html"
        base = (ROOT / "static").resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return self.reply(404, {"error": "页面不存在"})
        mime = {
            ".js": "application/javascript", ".css": "text/css", ".html": "text/html",
            ".svg": "image/svg+xml", ".ttf": "font/ttf",
        }.get(target.suffix, mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        if target.suffix in (".html", ".css", ".js", ".svg"):
            mime += "; charset=utf-8"
        cache = {"Cache-Control": "public, max-age=604800"} if target.suffix in {".ttf", ".svg"} else None
        return self.reply(200, target.read_bytes(), headers=cache, content_type=mime)


def main():
    parser = argparse.ArgumentParser(description="易方 / MODUS 工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--connect-codex", metavar="USERNAME", help="为本机已有账号创建 Codex 连接")
    parser.add_argument("--connection-file", type=Path, help="连接配置保存位置")
    args = parser.parse_args()
    if args.connect_codex:
        if not args.connection_file:
            parser.error("--connect-codex 需要同时指定 --connection-file")
        database = Path(args.data_dir) / "modus.sqlite3"
        initialize_database(database)
        with connect_database(database) as connection:
            user = connection.execute("SELECT id,name FROM users WHERE username=?", (args.connect_codex,)).fetchone()
            if not user:
                parser.error("账号不存在，请先在网页中设置账号")
            token = secrets.token_urlsafe(36)
            connection.execute("INSERT INTO agent_tokens VALUES(?,?,?,?)", (token_hash(token), user["id"], "Codex 本机接入", timestamp()))
            args.connection_file.parent.mkdir(parents=True, exist_ok=True)
            configuration = {"url": f"http://127.0.0.1:{args.port}", "token": token}
            temporary = args.connection_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(configuration, indent=2), encoding="utf-8")
            temporary.replace(args.connection_file)
            connection.commit()
            print(f"Codex connected for {user['name']}. Credentials saved locally.", flush=True)
        return 0
    try:
        server = WorkbenchServer((args.host, args.port), args.data_dir)
    except OSError as error:
        print(f"启动失败：{error}\n如已启动，请打开 http://127.0.0.1:{args.port}；也可用 --port 指定其他端口。", flush=True)
        return 1
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"MODUS workbench: {url}\nData: {server.data_dir}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
