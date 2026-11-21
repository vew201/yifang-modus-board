"""Fixed project stages, explicit completion and opportunity activity timestamps."""
from datetime import datetime
import json
import trash_bin

STAGES = ["项目准备", "资料接收", "需求澄清", "方案与开工对齐", "开发实现", "整体验证", "交付验收", "复盘改进"]
MEANINGS = [
    "确认目标、参与人、分工及启动条件。", "接收核对文件、数据、账号和权限，记录资料缺口。",
    "澄清业务需求、范围边界、待确认问题和验收标准。", "确认方案、交付内容、排期、分工及开工安排。",
    "实现功能、配置集成并修复问题。", "验证完整业务流程、功能效果和运行环境。",
    "部署上线、移交文件、培训并完成客户验收。", "总结经验、遗留问题和后续改进措施。",
]


def initialize(connection):
    from server import timestamp
    additions = {
        "stages": {"stage_key": "TEXT", "work_summary": "TEXT NOT NULL DEFAULT ''", "completion_note": "TEXT NOT NULL DEFAULT ''", "status": "TEXT NOT NULL DEFAULT 'pending'", "completed_by": "INTEGER REFERENCES users(id)", "updated_at": "TEXT NOT NULL DEFAULT ''"},
        "projects": {"completion_note": "TEXT NOT NULL DEFAULT ''", "completed_by": "INTEGER REFERENCES users(id)"},
        "documents": {"stage_id": "INTEGER REFERENCES stages(id)"},
        "conversations": {"stage_id": "INTEGER REFERENCES stages(id)"},
        "opportunities": {"last_activity_at": "TEXT NOT NULL DEFAULT ''", "activity_note": "TEXT NOT NULL DEFAULT ''"},
    }
    for table, fields in additions.items():
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for key, declaration in fields.items():
            if key not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {key} {declaration}")
    connection.execute("CREATE TABLE IF NOT EXISTS project_model_migrations(project_id INTEGER PRIMARY KEY REFERENCES projects(id),snapshot TEXT NOT NULL,saved_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS stage_history(id INTEGER PRIMARY KEY,stage_id INTEGER REFERENCES stages(id),snapshot TEXT NOT NULL,saved_at TEXT NOT NULL,actor_id INTEGER REFERENCES users(id))")
    for project in list(connection.execute("SELECT * FROM projects")):
        if connection.execute("SELECT 1 FROM project_model_migrations WHERE project_id=?", (project["id"],)).fetchone():
            continue
        legacy = [dict(row) for row in connection.execute("SELECT * FROM stages WHERE project_id=?", (project["id"],))]
        connection.execute("INSERT INTO project_model_migrations VALUES(?,?,?)", (project["id"], json.dumps({"project":dict(project),"stages":legacy},ensure_ascii=False),timestamp()))
        connection.execute("UPDATE stages SET deleted=1 WHERE project_id=?", (project["id"],))
        ensure_stages(connection, project["id"])
        # Keep old records in the migration snapshot. Broad old phases do not prove
        # any of the new eight phases complete; tasks retain their own done state.
        for task in list(connection.execute("SELECT id,title FROM tasks WHERE project_id=?", (project["id"],))):
            connection.execute("UPDATE tasks SET stage_id=? WHERE id=?", (classify(connection,project["id"],task["title"]),task["id"]))
    for item in list(connection.execute("SELECT * FROM opportunities WHERE last_activity_at=''")):
        candidates = [item["created_at"]]
        candidates += [row[0] for row in connection.execute("SELECT created_at FROM conversations WHERE opportunity_id=?",(item["id"],))]
        candidates += [row[0] for row in connection.execute("SELECT uploaded_at FROM opportunity_files WHERE opportunity_id=?",(item["id"],))]
        # Old revisions do not contain a reliable edit timestamp.
        stamp = timestamp() if item["revision"] else max(candidates)
        note = "历史修改时间未记录，已以上线时间作为计时起点" if item["revision"] else ""
        connection.execute("UPDATE opportunities SET last_activity_at=?,activity_note=? WHERE id=?", (stamp,note,item["id"]))
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS fixed_stage_key ON stages(project_id,stage_key) WHERE deleted=0 AND stage_key IS NOT NULL")


def ensure_stages(connection, project_id):
    from server import timestamp
    for position,name in enumerate(STAGES):
        key = str(position+1)
        if not connection.execute("SELECT id FROM stages WHERE project_id=? AND stage_key=? AND deleted=0",(project_id,key)).fetchone():
            connection.execute("INSERT INTO stages(id,project_id,name,start_date,end_date,position,stage_key,updated_at) VALUES(?,?,?, '', '',?,?,?)",(trash_bin.next_id(connection,'stages'),project_id,name,position,key,timestamp()))


def classify(connection, project_id, text):
    """Conservative filename/title hints; unclear content stays unclassified."""
    if not project_id:
        return None
    terms = [("复盘",7),("验收",6),("培训",6),("交付",6),("测试",5),("验证",5),("开发",4),("代码",4),("需求",2),("边界",2),("方案",3),("资料接收",1),("项目准备",0)]
    index = next((index for keyword,index in terms if keyword in text),None)
    if index is None:
        return None
    row = connection.execute("SELECT id FROM stages WHERE project_id=? AND stage_key=? AND deleted=0",(project_id,str(index+1))).fetchone()
    return row[0] if row else None


def stage_values(connection, old, data, actor):
    from server import text_value, date_range, timestamp
    from workflow import instant
    for key in ("name","position","project_id","stage_key"):
        if key in data and str(data[key]) != str(old[key]):
            raise ValueError("项目固定八个阶段，名称、顺序和归属不可变更")
    merged = {**old, **data}
    start,end = date_range(merged,allow_pending=True)
    project = connection.execute("SELECT * FROM projects WHERE id=?",(old["project_id"],)).fetchone()
    if start and (not project["start_date"] or start<project["start_date"] or end>project["end_date"]):
        raise ValueError("阶段起止日期需要位于项目周期内")
    status = merged["status"]
    if status not in {"pending","in_progress","completed","not_applicable"}:
        raise ValueError("阶段状态不正确")
    note = text_value(merged,"completion_note","完成说明或依据",10000,status in {"completed","not_applicable"})
    completed_at = None
    if status == "completed":
        completed_at = instant(data.get("completed_at") or old["completed_at"] or timestamp(),"实际完成时间",historical=True)
    return {"start_date":start,"end_date":end,"status":status,"work_summary":text_value(merged,"work_summary","阶段工作内容",20000),"completion_note":note,"completed_at":completed_at,"completed_by":actor["user_id"] if status != old["status"] else old["completed_by"],"updated_at":timestamp()}


def project_completion(old, data, actor):
    from server import timestamp, text_value
    from workflow import instant
    if "development_done" not in data:
        if "completed_at" in data and data["completed_at"] != old["completed_at"]:
            raise ValueError("请通过明确的开发完成或恢复开发操作更新项目状态")
        return {}
    if not isinstance(data["development_done"],bool):
        raise ValueError("开发完成状态格式不正确")
    done = data["development_done"]
    note = text_value(data,"completion_note","开发完成说明",10000,done)
    return {"completed_at":instant(data.get("development_completed_at") or old["completed_at"] or timestamp(),"开发实际完成时间",historical=True) if done else None,"completion_note":note,"completed_by":actor["user_id"]}


def synchronize_archives(connection):
    # Task changes never alter stage or project completion.
    for project in list(connection.execute("SELECT * FROM projects WHERE deleted=0")):
        if project["completed_at"]:
            connection.execute("INSERT INTO project_archives VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET owner_id=excluded.owner_id,completed_at=excluded.completed_at",(project["id"],project["owner_id"],project["completed_at"]))
        else:
            connection.execute("DELETE FROM project_archives WHERE project_id=?",(project["id"],))


def activity_context(connection, method, path, data):
    parts=path.strip('/').split('/')
    table=parts[1] if len(parts)>1 else ''
    identifier=parts[2] if len(parts)>2 else None
    if table=='restore' and len(parts)==4:
        table,identifier=parts[2:4]
    if table not in {'opportunities','conversations','opportunity_files','tasks'}:
        return None
    old=None
    if identifier:
        old=connection.execute(f'SELECT * FROM {table} WHERE id=?',(identifier,)).fetchone()
    return table,dict(old) if old else None


def touch_activity(connection, context, result):
    from server import timestamp
    if not context or result.get('replayed'):
        return
    table,old=context
    identifier=(old or {}).get('id') or result.get('id')
    new=connection.execute(f'SELECT * FROM {table} WHERE id=?',(identifier,)).fetchone()
    if not new:
        return
    new=dict(new)
    ignored={'revision','last_activity_at','activity_note'}
    if old and all(old[key]==new[key] for key in new if key not in ignored):
        return
    opportunity_id=new['id'] if table=='opportunities' else new.get('opportunity_id')
    if opportunity_id:
        connection.execute("UPDATE opportunities SET last_activity_at=?,activity_note='' WHERE id=?",(timestamp(),opportunity_id))


def aging(item, now=None):
    from server import LOCAL_TZ
    now=now or datetime.now(LOCAL_TZ)
    try:
        last=datetime.fromisoformat(item['last_activity_at'] or item['created_at'])
        if last.tzinfo is None:
            last=last.replace(tzinfo=LOCAL_TZ)
        seconds=max(0,(now-last).total_seconds())
    except (ValueError,TypeError):
        seconds=0
    return {**item,'age_band':'black' if seconds>=6*86400 else 'gray' if seconds>=3*86400 else 'white','idle_days':int(seconds//86400)}
