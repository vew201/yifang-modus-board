"""Opportunity workflow. Standard library, event-driven scheduling, no background polling."""

import base64
from datetime import datetime, timedelta
import secrets
import trash_bin


def initialize(connection):
    additions = {
        "opportunities": {
            "phase": "TEXT NOT NULL DEFAULT 'lead'", "channel": "TEXT NOT NULL DEFAULT ''",
            "source_url": "TEXT NOT NULL DEFAULT ''", "company_details": "TEXT NOT NULL DEFAULT ''",
            "service_type": "TEXT NOT NULL DEFAULT ''", "post_text": "TEXT NOT NULL DEFAULT ''",
            "requirements": "TEXT NOT NULL DEFAULT ''", "submitted_at": "TEXT NOT NULL DEFAULT ''",
            "feedback_at": "TEXT NOT NULL DEFAULT ''", "solution_file_id": "INTEGER",
            "quote_file_id": "INTEGER", "submission_recipient": "TEXT NOT NULL DEFAULT ''",
            "outcome": "TEXT NOT NULL DEFAULT 'active'", "failure_reason": "TEXT NOT NULL DEFAULT ''",
            "closed_at": "TEXT NOT NULL DEFAULT ''", "linked_project_id": "INTEGER REFERENCES projects(id)",
            "schedule_anchor": "TEXT NOT NULL DEFAULT ''", "next_followup_at": "TEXT NOT NULL DEFAULT ''",
            "time_basis": "TEXT NOT NULL DEFAULT ''",
        },
        "conversations": {
            "occurred_at": "TEXT NOT NULL DEFAULT ''", "participants": "TEXT NOT NULL DEFAULT ''",
            "requirements": "TEXT NOT NULL DEFAULT ''", "open_questions": "TEXT NOT NULL DEFAULT ''",
            "decisions": "TEXT NOT NULL DEFAULT ''", "next_action": "TEXT NOT NULL DEFAULT ''",
            "agreed_next_at": "TEXT NOT NULL DEFAULT ''", "client_key": "TEXT",
            "revision": "INTEGER NOT NULL DEFAULT 0",
        },
        "tasks": {"auto_followup": "INTEGER NOT NULL DEFAULT 0", "cancelled_at": "TEXT NOT NULL DEFAULT ''", "time_basis": "TEXT NOT NULL DEFAULT ''", "deleted": "INTEGER NOT NULL DEFAULT 0"},
        "projects": {"final_payment_at": "TEXT NOT NULL DEFAULT ''", "payment_note": "TEXT NOT NULL DEFAULT ''"},
    }
    for table, fields in additions.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, declaration in fields.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS opportunity_files (
            id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
            conversation_id INTEGER REFERENCES conversations(id), owner_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL, role TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',
            storage_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, uploaded_at TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS conversation_client_key ON conversations(opportunity_id,client_key) WHERE client_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS conversation_history (
            id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            revision INTEGER NOT NULL, snapshot TEXT NOT NULL, saved_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_pending_followup ON tasks(opportunity_id)
            WHERE auto_followup=1 AND done=0 AND deleted=0 AND cancelled_at='';
    """)


def instant(value, label, optional=True, historical=False):
    from server import LOCAL_TZ
    if not value and optional:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        parsed = parsed.astimezone(LOCAL_TZ)
        if historical and parsed > datetime.now(LOCAL_TZ) + timedelta(minutes=1):
            raise ValueError
        return parsed.isoformat(timespec="seconds")
    except (ValueError, TypeError):
        raise ValueError(f"请填写有效的{label}" + ("，实际发生时间不能晚于现在" if historical else "")) from None


def opportunity_values(connection, data):
    from server import text_value, reference
    values = {key: text_value(data, key, label, limit) for key, label, limit in [
        ("channel", "发现渠道", 300), ("source_url", "来源链接", 2000),
        ("company_details", "公司详情", 10000), ("service_type", "所需服务类型", 1000),
        ("post_text", "帖子原文", 50000), ("requirements", "整理后的需求", 20000),
        ("submission_recipient", "提交对象及方式", 1000), ("failure_reason", "失败原因", 10000),
    ]}
    if values["source_url"]:
        from urllib.parse import urlparse
        if urlparse(values["source_url"]).scheme not in {"https", "http"}:
            raise ValueError("来源链接请使用 http 或 https")
    phase = data.get("phase", "lead")
    if phase not in {"lead", "communication", "proposal"}:
        raise ValueError("意向阶段不正确")
    values["phase"] = phase
    if phase == "communication" and not connection.execute("SELECT id FROM conversations WHERE opportunity_id=? AND occurred_at<>'' LIMIT 1", (data.get("id"),)).fetchone():
        raise ValueError("请先追加一次实际沟通记录，系统将自动进入沟通阶段")
    for key in ("submitted_at", "feedback_at"):
        values[key] = instant(data.get(key, ""), "提交时间" if key == "submitted_at" else "反馈时间", historical=key == "submitted_at")
    for key in ("solution_file_id", "quote_file_id"):
        values[key] = int(data[key]) if data.get(key) else None
    if phase == "proposal":
        if not values["requirements"] or not values["submitted_at"]:
            raise ValueError("进入第三阶段前，请补齐已确认需求和最终方案、报价的实际提交时间")
        for key, role in (("solution_file_id", "solution"), ("quote_file_id", "quote")):
            file = connection.execute("SELECT * FROM opportunity_files WHERE id=? AND deleted=0", (values[key],)).fetchone()
            if not file or file["opportunity_id"] != data.get("id") or file["role"] != role:
                raise ValueError("请为本意向选择已上传的最终方案书和报价书")
    values["linked_project_id"] = reference(connection, "projects", data["linked_project_id"], "开发项目") if data.get("linked_project_id") else None
    # Outcome is changed only through dedicated fail/convert/reopen operations.
    return values


def schedule(connection, opportunity_id, anchor, agreed="", hours=48):
    from server import timestamp
    item = connection.execute("SELECT * FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if item["deleted"] or item["outcome"] != "active" or item["phase"] == "lead":
        return
    anchor = instant(anchor, "跟进起点", optional=False)
    agreed = instant(agreed, "约定时间")
    due = agreed or (datetime.fromisoformat(anchor) + timedelta(hours=hours)).isoformat(timespec="seconds")
    basis = "按沟通约定" if agreed else f"实际{'沟通结束' if hours == 24 else '提交或最近跟进结束'}后 {hours} 小时"
    title = f"{'方案报价反馈跟进' if item['phase'] == 'proposal' else '下次沟通'} · {item['name']}"
    description = (item["next_step"] or "联系客户，确认进展并记录沟通结果") + "\n时间依据：" + basis + "\n起点：" + anchor
    pending = connection.execute("SELECT id FROM tasks WHERE opportunity_id=? AND auto_followup=1 AND done=0 AND deleted=0 AND cancelled_at=''", (opportunity_id,)).fetchone()
    date, clock = due[:10], due[11:16]
    if pending:
        connection.execute("UPDATE tasks SET title=?,description=?,owner_id=?,start_date=?,end_date=?,time=?,time_basis=?,revision=revision+1 WHERE id=?", (title, description, item["owner_id"], date, date, clock, basis, pending["id"]))
    else:
        connection.execute("INSERT INTO tasks(id,title,description,owner_id,opportunity_id,kind,start_date,end_date,time,created_at,auto_followup,time_basis) VALUES(?,?,?,?,?,'daily',?,?,?,?,1,?)", (trash_bin.next_id(connection,'tasks'),title, description, item["owner_id"], opportunity_id, date, date, clock, timestamp(), basis))
    connection.execute("UPDATE opportunities SET schedule_anchor=?,next_followup_at=?,time_basis=?,revision=revision+1 WHERE id=?", (anchor, due, basis, opportunity_id))


def cancel_followups(connection, opportunity_id):
    from server import timestamp
    connection.execute("UPDATE tasks SET cancelled_at=?,revision=revision+1 WHERE opportunity_id=? AND auto_followup=1 AND done=0 AND cancelled_at=''", (timestamp(), opportunity_id))


def after_opportunity_update(connection, old, new):
    if new["outcome"] != "active" or new["phase"] == "lead":
        cancel_followups(connection, new["id"])
        connection.execute("UPDATE opportunities SET schedule_anchor='',next_followup_at='',time_basis='' WHERE id=?", (new["id"],))
        return
    if new["phase"] == "proposal" and any(old[key] != new[key] for key in ("phase", "submitted_at", "feedback_at", "solution_file_id", "quote_file_id")):
        schedule(connection, new["id"], new["submitted_at"], new["feedback_at"], 48)
    elif new["phase"] == "communication" and old["phase"] != "communication":
        last = connection.execute("SELECT * FROM conversations WHERE opportunity_id=? AND occurred_at<>'' ORDER BY occurred_at DESC,id DESC LIMIT 1", (new["id"],)).fetchone()
        if last:
            schedule(connection, new["id"], last["occurred_at"], last["agreed_next_at"], 24)
    elif old["owner_id"] != new["owner_id"] or old["name"] != new["name"] or old["next_step"] != new["next_step"]:
        if new["schedule_anchor"]:
            # Preserve the actual due time when only owner/label changes.
            schedule(connection, new["id"], new["schedule_anchor"], new["next_followup_at"])
            connection.execute("UPDATE opportunities SET time_basis=? WHERE id=?", (old["time_basis"], new["id"]))
            connection.execute("UPDATE tasks SET time_basis=? WHERE opportunity_id=? AND auto_followup=1 AND done=0 AND cancelled_at=''", (old["time_basis"], new["id"]))


def save_conversation(connection, data, identifier=None, actor_id=None):
    from server import text_value, reference, timestamp, record_for_update, RecordConflict
    opportunity_id = reference(connection, "opportunities", data.get("opportunity_id"), "合作意向")
    item = connection.execute("SELECT * FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    old = None
    if identifier:
        old = record_for_update(connection, "conversations", identifier, data)
        if old["opportunity_id"] != opportunity_id:
            raise ValueError("沟通记录不能移动到其他意向")
    key = text_value(data, "client_key", "记录唯一编号", 120) or None
    duplicate = None
    if not old and key:
        duplicate = connection.execute("SELECT * FROM conversations WHERE opportunity_id=? AND client_key=?", (opportunity_id, key)).fetchone()
    merged = {**(old or {}), **data}
    values = {key: text_value(merged, key, label, limit, required) for key, label, limit, required in [
        ("text", "沟通原文", 100000, True), ("summary", "摘要", 10000, False),
        ("participants", "参与人及方式", 1000, False), ("requirements", "需求整理", 20000, False),
        ("open_questions", "待确认问题", 10000, False), ("decisions", "沟通结论", 10000, False), ("next_action", "下一步", 1000, False),
    ]}
    values["owner_id"] = reference(connection, "users", merged.get("owner_id"), "记录负责人")
    values["occurred_at"] = instant(merged.get("occurred_at", ""), "实际沟通结束时间", historical=True)
    values["agreed_next_at"] = instant(merged.get("agreed_next_at", ""), "下次约定时间")
    if values["agreed_next_at"] and not values["occurred_at"]:
        raise ValueError("请先补充实际沟通结束时间，再设置下一次约定")
    if values["agreed_next_at"] and values["agreed_next_at"] <= values["occurred_at"]:
        raise ValueError("下次约定时间应晚于本次沟通结束时间")
    if duplicate:
        if any(duplicate[key] != value for key, value in values.items()):
            raise RecordConflict("此沟通编号已用于不同内容，请用 PATCH 和 revision 修订原记录")
        return {"id": duplicate["id"], "replayed": True}
    if old:
        import json
        connection.execute("INSERT INTO conversation_history(conversation_id,revision,snapshot,saved_at) VALUES(?,?,?,?)", (identifier, old["revision"], json.dumps(old, ensure_ascii=False), timestamp()))
        assignments = ",".join(f"{key}=?" for key in values)
        connection.execute(f"UPDATE conversations SET {assignments},revision=revision+1 WHERE id=?", (*values.values(), identifier))
    else:
        identifier = connection.execute(f"INSERT INTO conversations(id,opportunity_id,created_at,client_key,{','.join(values)}) VALUES({','.join('?' for _ in range(len(values)+4))})", (trash_bin.next_id(connection,'conversations'),opportunity_id, timestamp(), key, *values.values())).lastrowid
    latest = connection.execute("SELECT * FROM conversations WHERE opportunity_id=? ORDER BY CASE WHEN occurred_at='' THEN created_at ELSE occurred_at END DESC,id DESC LIMIT 1", (opportunity_id,)).fetchone()
    current = not item["schedule_anchor"] or values["occurred_at"] >= item["schedule_anchor"] or (old and item["schedule_anchor"] == old["occurred_at"])
    if latest["id"] == identifier and item["outcome"] == "active" and values["occurred_at"] and current:
        if item["phase"] == "lead":
            connection.execute("UPDATE opportunities SET phase='communication' WHERE id=?", (opportunity_id,))
        if values["requirements"]:
            connection.execute("UPDATE opportunities SET requirements=? WHERE id=?", (values["requirements"], opportunity_id))
        connection.execute("UPDATE opportunities SET next_step=?,revision=revision+1 WHERE id=?", (values["next_action"], opportunity_id))
        # Uploading information does not itself complete work. The owner's checkbox
        # remains the explicit completion record; new arrangements update the pending task.
        # A historical communication must not move a later submission/arrangement backwards.
        schedule(connection, opportunity_id, values["occurred_at"], values["agreed_next_at"], 48 if item["phase"] == "proposal" else 24)
    return {"id": identifier, "revision": (old["revision"] + 1) if old else 0}


def mutate(handler, connection, method, path, data, actor):
    from server import record_for_update, text_value, reference, timestamp, add_project
    parts = path.strip("/").split("/")
    if path == "/api/conversations" and method == "POST" and data.get("opportunity_id"):
        return save_conversation(connection, data, actor_id=actor["user_id"])
    if len(parts) == 3 and parts[1] == "conversations" and method == "PATCH":
        old = record_for_update(connection, "conversations", parts[2], data)
        return save_conversation(connection, {**old, **data}, old["id"], actor_id=actor["user_id"])
    if len(parts) == 4 and parts[1] == "opportunities" and method == "POST":
        old = record_for_update(connection, "opportunities", parts[2], data)
        action = parts[3]
        if action == "fail":
            if old["phase"] != "proposal" or old["outcome"] != "active":
                raise ValueError("只有第三阶段的进行中意向可以归档失败")
            reason = text_value(data, "failure_reason", "失败原因", 10000, True)
            connection.execute("UPDATE opportunities SET outcome='failed',failure_reason=?,closed_at=?,revision=revision+1 WHERE id=?", (reason, timestamp(), old["id"]))
            cancel_followups(connection, old["id"])
        elif action == "convert":
            if old["phase"] != "proposal" or old["outcome"] != "active":
                raise ValueError("请先完成最终方案及报价提交，再转入开发")
            project = reference(connection, "projects", data["project_id"], "开发项目") if data.get("project_id") else add_project(connection, {"name": old["name"], "client": old["company"], "description": old["requirements"], "owner_id": old["owner_id"], "color": old["color"]})
            connection.execute("UPDATE opportunities SET outcome='converted',linked_project_id=?,closed_at=?,revision=revision+1 WHERE id=?", (project, timestamp(), old["id"]))
            cancel_followups(connection, old["id"])
            return {"id": old["id"], "project_id": project}
        elif action == "reopen":
            if old["outcome"] != "failed":
                raise ValueError("只有失败归档的意向需要重新跟进")
            connection.execute("UPDATE opportunities SET outcome='active',closed_at='',revision=revision+1 WHERE id=?", (old["id"],))
            schedule(connection, old["id"], timestamp(), instant(data.get("agreed_next_at", ""), "约定时间"), 48)
        else:
            return None
        return {"id": old["id"], "ok": True}
    if path == "/api/opportunity_files" and method == "POST":
        opportunity = reference(connection, "opportunities", data.get("opportunity_id"), "合作意向")
        owner = reference(connection, "users", data.get("owner_id"), "负责人")
        conversation = data.get("conversation_id") or None
        if conversation:
            conversation = reference(connection, "conversations", conversation, "沟通记录")
            if connection.execute("SELECT opportunity_id FROM conversations WHERE id=?", (conversation,)).fetchone()[0] != opportunity:
                raise ValueError("文件和沟通记录需要属于同一个意向")
        role = data.get("role", "attachment")
        if role not in {"image", "attachment", "recording", "solution", "quote"}:
            raise ValueError("文件类型不正确")
        name = text_value(data, "name", "文件名", 200, True).replace("\\", "/").rsplit("/", 1)[-1]
        if any(ord(char) < 32 for char in name) or not name:
            raise ValueError("文件名不正确")
        try:
            content = base64.b64decode(data.get("content", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError("文件内容不完整") from None
        if not 0 < len(content) <= 20 * 1024 * 1024:
            raise ValueError("每个文件需为 1 字节至 20 MB")
        if role == "image" and image_mime(content) is None:
            raise ValueError("帖子图片支持 PNG、JPEG、GIF、WebP 格式")
        version = text_value(data, "version", "版本说明", 200)
        storage = secrets.token_hex(24)
        target = handler.server.uploads / storage
        try:
            target.write_bytes(content)
            identifier = connection.execute("INSERT INTO opportunity_files(id,opportunity_id,conversation_id,owner_id,name,role,version,storage_name,size,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (trash_bin.next_id(connection,'opportunity_files'),opportunity, conversation, owner, name, role, version, storage, len(content), timestamp())).lastrowid
            if hasattr(handler, "submission_files"):
                handler.submission_files.append(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"id": identifier}
    return None


def image_mime(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def download(handler, connection, path):
    from server import reference
    from urllib.parse import quote
    identifier = reference(connection, "opportunity_files", path.rsplit("/", 1)[-1], "意向文件")
    item = connection.execute("SELECT f.*,o.deleted AS parent_deleted FROM opportunity_files f JOIN opportunities o ON o.id=f.opportunity_id WHERE f.id=?", (identifier,)).fetchone()
    if item["parent_deleted"]:
        raise ValueError("请先恢复所属意向")
    target = handler.server.uploads / item["storage_name"]
    if not target.is_file():
        return handler.reply(404, {"error": "原文件不存在"})
    content = target.read_bytes()
    mime = image_mime(content) if item["role"] == "image" else None
    return handler.reply(200, content, {"Content-Disposition": ("inline" if mime else "attachment") + "; filename*=UTF-8''" + quote(item["name"])}, mime or "application/octet-stream")
