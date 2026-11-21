"""Thirty-day recoverable trash and transactional, scoped permanent deletion."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
import re
import secrets
import threading
import time

TZ = timezone(timedelta(hours=8))
RETENTION_DAYS = 30
TABLES = ('projects', 'opportunities', 'sections', 'tasks', 'documents', 'opportunity_files')
PARENTS = {'sections': [('project_id','projects')], 'tasks': [('project_id','projects'),('opportunity_id','opportunities')], 'documents': [('project_id','projects'),('section_id','sections')], 'opportunity_files': [('opportunity_id','opportunities')]}
LABELS = {'projects':'项目','opportunities':'合作意向','sections':'文件区域','tasks':'事项','documents':'文件','opportunity_files':'意向附件','stages':'阶段','conversations':'沟通记录','conversation_history':'沟通历史','stage_history':'阶段历史','completions':'完成记录','project_archives':'项目归档','project_model_migrations':'旧阶段记录'}
KEYS = {'completions':'task_id','project_archives':'project_id','project_model_migrations':'project_id'}


class TrashConflict(Exception):
    pass


def stamp(now=None):
    return (now or datetime.now(TZ)).astimezone(TZ).isoformat(timespec='seconds')


def initialize(connection):
    # Historical soft deletes have no reliable deletion date. Start a full window now.
    for table in TABLES:
        columns = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
        if 'deleted_at' not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        if 'deleted_by' not in columns:
            connection.execute(f'ALTER TABLE {table} ADD COLUMN deleted_by INTEGER REFERENCES users(id)')
        connection.execute(f"UPDATE {table} SET deleted_at=? WHERE deleted=1 AND deleted_at=''", (stamp(),))
        connection.execute(f'CREATE INDEX IF NOT EXISTS trash_expiry_{table} ON {table}(deleted,deleted_at)')
    connection.execute('CREATE TABLE IF NOT EXISTS trash_file_gc(storage_name TEXT PRIMARY KEY,size INTEGER NOT NULL)')
    for table in (*TABLES,'stages','conversations'):
        reserve_id(connection,table,connection.execute(f'SELECT COALESCE(MAX(id),0) FROM {table}').fetchone()[0])


def reserve_id(connection, table, value):
    # Retain only the high-water integer, never the purged customer data.
    connection.execute('CREATE TABLE IF NOT EXISTS record_id_highwater(table_name TEXT PRIMARY KEY,value INTEGER NOT NULL)')
    connection.execute('INSERT INTO record_id_highwater VALUES(?,?) ON CONFLICT(table_name) DO UPDATE SET value=MAX(value,excluded.value)',(table,value))


def next_id(connection, table):
    if table not in (*TABLES,'stages','conversations'):raise ValueError('Invalid record table')
    reserve_id(connection,table,connection.execute(f'SELECT COALESCE(MAX(id),0) FROM {table}').fetchone()[0])
    value=connection.execute('SELECT value+1 FROM record_id_highwater WHERE table_name=?',(table,)).fetchone()[0]
    reserve_id(connection,table,value)
    return value


def selected(connection, table, column, values):
    values = list(values)
    result = {}
    for offset in range(0, len(values), 400):
        chunk = values[offset:offset+400]
        query = f"SELECT * FROM {table} WHERE {column} IN ({','.join('?' for _ in chunk)})"
        for row in connection.execute(query, chunk):
            value = dict(row);result[value[KEYS.get(table,'id')]] = value
    return result


def selection(connection, data):
    if data.get('all') is True:
        return [(table, dict(row)) for table in TABLES for row in connection.execute(f'SELECT * FROM {table} WHERE deleted=1 ORDER BY id')]
    table = data.get('table')
    identifier = data.get('id')
    if table not in TABLES or isinstance(identifier, bool):
        raise ValueError('请选择有效的回收记录')
    try:identifier = int(identifier)
    except (ValueError, TypeError):raise ValueError('请选择有效的回收记录') from None
    row = connection.execute(f'SELECT * FROM {table} WHERE id=? AND deleted=1', (identifier,)).fetchone()
    if not row:raise TrashConflict('回收记录已恢复或已清理，请刷新回收站')
    return [(table, dict(row))]


def listing(connection):
    items = {}
    parent_cache = {}
    for table in TABLES:
        items[table] = []
        name = 'title' if table == 'tasks' else 'name'
        for row in connection.execute(f'SELECT * FROM {table} WHERE deleted=1 ORDER BY deleted_at DESC,id DESC'):
            row = dict(row)
            item = {key:row[key] for key in ('id','revision','deleted_at','deleted_by')};item['name'] = row[name]
            basis = row['deleted_at'];item['retention_basis'] = ''
            for column,parent in PARENTS.get(table,[]):
                if not row.get(column):continue
                key = (parent,row[column])
                if key not in parent_cache:
                    parent_cache[key] = connection.execute(f'SELECT deleted,deleted_at FROM {parent} WHERE id=?',(row[column],)).fetchone()
                record = parent_cache[key]
                if record and record['deleted']:
                    basis = record['deleted_at'];item['retention_basis'] = '随所属'+LABELS[parent]+'到期清理';break
            item['expires_at'] = stamp(datetime.fromisoformat(basis) + timedelta(days=RETENTION_DAYS))
            items[table].append(item)
    return {'retention_days':RETENTION_DAYS,'items':items,'pending_files':connection.execute('SELECT COUNT(*) FROM trash_file_gc').fetchone()[0]}


def plan(connection, roots):
    removed = {table:{} for table in (*TABLES,'stages','conversations','conversation_history','stage_history','completions','project_archives','project_model_migrations')}
    for table, row in roots:removed[table][row['id']] = row
    for table in ('stages','sections','tasks','documents'):
        removed[table].update(selected(connection,table,'project_id',removed['projects']))
    removed['tasks'].update(selected(connection,'tasks','opportunity_id',removed['opportunities']))
    removed['documents'].update(selected(connection,'documents','section_id',removed['sections']))
    removed['opportunity_files'].update(selected(connection,'opportunity_files','opportunity_id',removed['opportunities']))
    retained = {'conversations':{},'opportunity_files':{},'opportunities':{},'tasks':{},'documents':{}}
    for column, table in [('project_id','projects'),('opportunity_id','opportunities'),('task_id','tasks'),('stage_id','stages')]:
        for identifier,row in selected(connection,'conversations',column,removed[table]).items():
            retained['conversations'][identifier] = row
    changes = {table:{} for table in retained}
    for identifier,row in retained['conversations'].items():
        updates = {}
        for column,table in [('project_id','projects'),('opportunity_id','opportunities'),('task_id','tasks'),('stage_id','stages')]:
            if row.get(column) in removed[table]:updates[column] = None
        if any(row.get(column) is not None and column not in updates for column in ('project_id','opportunity_id','task_id')):
            changes['conversations'][identifier] = updates
        else:
            removed['conversations'][identifier] = row
    for table, column in [('conversation_history','conversation_id'),('opportunity_files','conversation_id')]:
        found = selected(connection,table,column,removed['conversations'])
        if table == 'conversation_history':removed[table].update(found)
        else:
            for identifier,row in found.items():
                if identifier not in removed[table]:
                    retained[table][identifier] = row;changes[table][identifier] = {'conversation_id':None}
    for identifier,row in selected(connection,'opportunities','linked_project_id',removed['projects']).items():
        if identifier not in removed['opportunities']:
            retained['opportunities'][identifier] = row;changes['opportunities'][identifier] = {'linked_project_id':None}
    for table in ('tasks','documents'):
        for identifier,row in selected(connection,table,'stage_id',removed['stages']).items():
            if identifier not in removed[table]:
                retained[table][identifier] = row;changes[table][identifier] = {'stage_id':None}
    for table,column,parent in [('completions','task_id','tasks'),('project_archives','project_id','projects'),('project_model_migrations','project_id','projects'),('stage_history','stage_id','stages')]:
        removed[table].update(selected(connection,table,column,removed[parent]))
    for column in ('solution_file_id','quote_file_id'):
        for identifier in selected(connection,'opportunities',column,removed['opportunity_files']):
            if identifier not in removed['opportunities']:
                raise ValueError('附件仍被其他意向作为最终方案或报价引用，请先更换引用')
    candidates = {}
    for table in ('documents','opportunity_files'):
        for row in removed[table].values():
            storage = row['storage_name']
            if not re.fullmatch(r'[a-f0-9]{48}',storage):raise ValueError('附件存储标识异常，请由开发端检查，未删除原件')
            candidates[storage] = max(candidates.get(storage,0),row['size'])
    shared = set()
    for table in ('documents','opportunity_files'):
        for identifier,row in selected(connection,table,'storage_name',candidates).items():
            if identifier not in removed[table]:shared.add(row['storage_name'])
    files = {key:value for key,value in candidates.items() if key not in shared}
    snapshots = {'roots':[(table,row['id']) for table,row in roots],'removed':removed,'retained':retained,'changes':changes,'files':files}
    digest = hashlib.sha256(json.dumps(snapshots,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {**snapshots,'digest':digest,'shared_files':len(shared)}


def public_plan(value):
    return {'counts':{table:len(records) for table,records in value['removed'].items() if records},
            'labels':LABELS,'file_count':len(value['files']),'file_bytes':sum(value['files'].values()),
            'retained_links':sum(len(rows) for rows in value['changes'].values()),'shared_files_preserved':value['shared_files'],
            'roots':[{'table':table,'id':identifier,'name':value['removed'][table][identifier].get('name',value['removed'][table][identifier].get('title',''))} for table,identifier in value['roots']]}


def confirmation(server, value, actor, expires):
    payload = f"{actor}:{expires}:{value['digest']}"
    return str(expires)+'.'+hmac.new(server.trash_confirmation_key,payload.encode(),hashlib.sha256).hexdigest()


def preview(server, connection, data, actor):
    roots = selection(connection,data)
    if not roots:raise ValueError('回收站为空')
    value = plan(connection,roots)
    expires = int(time.time())+300
    return {**public_plan(value),'preview_token':confirmation(server,value,actor,expires),'expires_at':expires}


def delete_rows(connection, table, column, identifiers):
    identifiers = list(identifiers)
    for offset in range(0,len(identifiers),400):
        chunk = identifiers[offset:offset+400]
        connection.execute(f"DELETE FROM {table} WHERE {column} IN ({','.join('?' for _ in chunk)})",chunk)


def apply(connection, value):
    # Keep the relational changes and disk cleanup queue in one database transaction.
    for table,records in value['removed'].items():
        if records and table in (*TABLES,'stages','conversations'):reserve_id(connection,table,max(records))
    for table, records in value['changes'].items():
        for identifier, updates in records.items():
            if not updates:continue
            connection.execute(f"UPDATE {table} SET {','.join(key+'=?' for key in updates)},revision=revision+1 WHERE id=?",(*updates.values(),identifier))
    for table in ('completions','conversation_history','stage_history','project_archives','project_model_migrations','opportunity_files','conversations','tasks','documents','sections','stages','opportunities','projects'):
        delete_rows(connection,table,KEYS.get(table,'id'),value['removed'][table])
    for storage,size in value['files'].items():
        connection.execute('INSERT OR IGNORE INTO trash_file_gc VALUES(?,?)',(storage,size))


def purge(server, connection, data, actor):
    if data.get('confirm') is not True:raise ValueError('请先预览删除范围并确认永久删除')
    token = data.get('preview_token','')
    try:expires = int(token.split('.')[0])
    except (ValueError,AttributeError):raise ValueError('请先预览删除范围') from None
    if expires < int(time.time()) or expires > int(time.time())+300:raise TrashConflict('确认已过期，请重新预览删除范围')
    value = plan(connection,selection(connection,data))
    if not hmac.compare_digest(token,confirmation(server,value,actor,expires)):
        raise TrashConflict('删除范围已变化，请重新预览，避免删除未经确认的内容')
    apply(connection,value)
    return {'ok':True,**public_plan(value)}


def collect_files(server, limit=200):
    from server import connect_database
    removed = 0
    with connect_database(server.database) as connection:
        connection.execute('BEGIN IMMEDIATE')
        for row in list(connection.execute('SELECT storage_name FROM trash_file_gc LIMIT ?', (limit,))):
            storage = row[0]
            if not re.fullmatch(r'[a-f0-9]{48}',storage):continue
            referenced = any(connection.execute(f'SELECT 1 FROM {table} WHERE storage_name=? LIMIT 1',(storage,)).fetchone() for table in ('documents','opportunity_files'))
            if not referenced:
                path = server.uploads/storage
                if path.parent.resolve()!=server.uploads.resolve():continue
                try:path.unlink(missing_ok=True)
                except OSError:continue  # Retried on the next service maintenance pass.
                removed += 1
            connection.execute('DELETE FROM trash_file_gc WHERE storage_name=?',(storage,))
        pending = connection.execute('SELECT COUNT(*) FROM trash_file_gc').fetchone()[0]
    return {'files_removed':removed,'pending_files':pending}


def parents(connection, table, row):
    values = []
    for column,parent in [('project_id','projects'),('opportunity_id','opportunities'),('section_id','sections')]:
        if row.get(column):
            record = connection.execute(f'SELECT * FROM {parent} WHERE id=?',(row[column],)).fetchone()
            if record:values.append((parent,dict(record)))
    return values


def restore(connection, table, identifier):
    if table not in TABLES:raise ValueError('此类型不能从回收站恢复')
    row = connection.execute(f'SELECT * FROM {table} WHERE id=? AND deleted=1',(identifier,)).fetchone()
    if not row:raise ValueError('回收记录不存在')
    row = dict(row)
    if any(parent['deleted'] for _,parent in parents(connection,table,row)):
        raise ValueError('请先恢复所属项目、合作意向或文件区域')
    connection.execute(f"UPDATE {table} SET deleted=0,deleted_at='',deleted_by=NULL,revision=revision+1 WHERE id=?",(identifier,))
    return {'ok':True,'id':identifier}


def expired(connection, now, limit=50):
    cutoff = stamp(now-timedelta(days=RETENTION_DAYS))
    roots = []
    for table in TABLES:
        checks = [f"{table}.deleted=1",f"{table}.deleted_at<>''",f"{table}.deleted_at<=?"]
        parameters = [cutoff]
        for column,parent in PARENTS.get(table,[]):
            checks.append(f'NOT EXISTS(SELECT 1 FROM {parent} parent WHERE parent.id={table}.{column} AND parent.deleted=1 AND parent.deleted_at>?)')
            parameters.append(cutoff)
        parameters.append(limit-len(roots))
        for record in connection.execute(f"SELECT * FROM {table} WHERE {' AND '.join(checks)} ORDER BY deleted_at LIMIT ?",parameters):
            row = dict(record)
            roots.append((table,row))
            if len(roots)>=limit:return roots
    return roots


def sweep(server, now=None):
    from server import connect_database
    now = now or datetime.now(TZ)
    with connect_database(server.database) as connection:roots = expired(connection,now)
    cleaned = 0
    for table,row in roots:
        try:
            with connect_database(server.database) as connection:
                connection.execute('BEGIN IMMEDIATE')
                current = connection.execute(f'SELECT * FROM {table} WHERE id=? AND deleted=1',(row['id'],)).fetchone()
                if not current or current['revision']!=row['revision'] or current['deleted_at']!=row['deleted_at']:continue
                if any(parent['deleted'] and parent['deleted_at']>stamp(now-timedelta(days=RETENTION_DAYS)) for _,parent in parents(connection,table,dict(current))):continue
                apply(connection,plan(connection,[(table,dict(current))]));cleaned+=1
        except ValueError:
            continue  # A live cross-reference must be resolved rather than silently removed.
    return {'roots_removed':cleaned,'batch_full':len(roots)>=50,**collect_files(server)}


def service_actions(server):
    if time.monotonic()<server.trash_next_sweep:return
    if server.trash_worker and server.trash_worker.is_alive():return
    server.trash_next_sweep = time.monotonic()+3600
    def work():
        try:
            result = sweep(server)
            if result['batch_full'] or result['pending_files']:server.trash_next_sweep=time.monotonic()+60
        except Exception as error:
            print('Trash maintenance: '+type(error).__name__,flush=True)
            server.trash_next_sweep=time.monotonic()+60
    server.trash_worker=threading.Thread(target=work,daemon=True,name='modus-trash-cleanup')
    server.trash_worker.start()
