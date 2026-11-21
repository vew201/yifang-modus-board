"""Human-launched MODUS upgrade; --verify-only never changes the running service."""
import argparse
import ast
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request,urlopen

BASE = Path(__file__).resolve().parents[1]
ROOT = Path(__import__('os').environ.get('MODUS_RUNTIME_DIR', '.missing-runtime-configuration')).resolve()
VERSION = json.loads((BASE/'codex-release.json').read_text(encoding='utf-8'))['version']
FILES = json.loads((BASE/'deployment-files.json').read_text(encoding='utf-8'))
TABLES = ['users','projects','opportunities','tasks','stages','sections','documents','opportunity_files','conversations','completions','project_archives','stage_history','conversation_history','project_model_migrations']


def digest(value):return hashlib.sha256(value).hexdigest()


def verify():
    if ROOT==BASE or ROOT.is_relative_to(BASE) or BASE.is_relative_to(ROOT):raise RuntimeError('Source checkout and production runtime must be separate')
    if not (ROOT/'data/modus.sqlite3').is_file() or not (ROOT/'server.py').is_file():raise RuntimeError('Existing production runtime and database are required')
    manifest={}
    for name in FILES:
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts or set(relative.parts)&{'data','.git','.github','scripts','tests','work','outputs'} or relative.name in {'connection.json','bootstrap.json'}:raise RuntimeError('Unsafe deployment file')
        path=(BASE/name).resolve()
        if not path.is_relative_to(BASE) or not path.is_file():raise RuntimeError('Deployment source is missing')
        if path.suffix=='.py':ast.parse(path.read_text(encoding='utf-8'))
        manifest[name]=digest(path.read_bytes())
    return manifest


def powershell(script):
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('本机服务管理未完成，请保留窗口并联系开发端；未清空业务数据')
    return result.stdout.strip()


def backend():
    expected=str(ROOT/'server.py').replace('\\','/').replace("'","''")
    script="$ErrorActionPreference='Stop'; $expected='"+expected+"'; @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\\.exe$' -and $_.CommandLine -and $_.CommandLine.Replace('\\','/').Contains($expected) -and $_.CommandLine -match '--port\\s+8765' } | Select-Object ProcessId) | ConvertTo-Json -Compress"
    raw=powershell(script)
    records=json.loads(raw) if raw else []
    records=records if isinstance(records,list) else [records]
    if len(records)>1:raise RuntimeError('发现多个工作台后端，停止自动操作，请联系开发端')
    return records[0]['ProcessId'] if records else None


def stop_backend(identifier):
    if backend()!=identifier:raise RuntimeError('后端进程已经变化，未停止其他程序')
    powershell("$ErrorActionPreference='Stop'; Stop-Process -Id "+str(int(identifier))+" -ErrorAction Stop")


def start_backend():
    if backend():return None  # Existing tunnel supervisor may already have restarted it.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',8765))==0:raise RuntimeError('8765端口被其他程序使用，未停止其他程序')
    with (ROOT/'data/trash-upgrade-server.log').open('ab') as output,(ROOT/'data/trash-upgrade-server-errors.log').open('ab') as errors:
        return subprocess.Popen([sys.executable,str(ROOT/'server.py'),'--port','8765'],cwd=ROOT,stdout=output,stderr=errors,creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)


def get(path,token=None):
    headers={'Authorization':'Bearer '+token} if token else {}
    with urlopen(Request('http://127.0.0.1:8765'+path,headers=headers),timeout=2) as response:return json.load(response)


def wait_ready():
    for _ in range(40):
        try:
            if get('/api/health').get('ok'):return
        except Exception:pass
        time.sleep(.25)
    raise RuntimeError('后端启动未通过检查')


def snapshot():
    result={}
    with closing(sqlite3.connect(ROOT/'data/modus.sqlite3',timeout=15)) as connection:
        for table in TABLES:
            columns=[row[1] for row in connection.execute(f'PRAGMA table_info({table})')]
            rows=connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
            result[table]={'columns':columns,'count':len(rows),'hash':digest(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode())}
    return result


def validate_data(before):
    with closing(sqlite3.connect(ROOT/'data/modus.sqlite3',timeout=15)) as connection:
        for table,value in before.items():
            if table != 'users':continue
            rows=connection.execute(f"SELECT {','.join(value['columns'])} FROM {table} ORDER BY 1").fetchall()
            if digest(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode())!=value['hash']:
                raise RuntimeError('数据核对有变化，请联系开发端查看备份；未自动覆盖当前数据库')
        if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or list(connection.execute('PRAGMA foreign_key_check')):raise RuntimeError('数据库完整性检查未通过')
        for table in ['projects','opportunities','tasks','sections','documents','opportunity_files']:
            if 'deleted_at' not in [row[1] for row in connection.execute(f'PRAGMA table_info({table})')]:raise RuntimeError('回收站迁移未完成')


def apply():
    expected_files=verify()
    changed=[name for name in FILES if not (ROOT/name).is_file() or digest((ROOT/name).read_bytes())!=expected_files[name]]
    revision=__import__('os').environ.get('MODUS_DEPLOY_REVISION','')
    if not __import__('re').fullmatch('[a-f0-9]{40}',revision):raise RuntimeError('Verified Git revision is required')
    actual=subprocess.run(['git','rev-parse','HEAD'],cwd=BASE,capture_output=True,text=True,check=True).stdout.strip()
    if actual!=revision:raise RuntimeError('Source revision changed')
    old_pid=backend()
    backup=ROOT/'data/upgrade-backups'/('trash-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    if not backup.resolve().is_relative_to((ROOT/'data').resolve()):raise RuntimeError('备份目录不正确')
    backup.mkdir(parents=True)
    print('[1/5] 校验通过，正在备份代码和数据库。',flush=True)
    old_files=[]
    for name in changed:
        path=ROOT/name
        if path.is_file():
            target=backup/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(path.read_bytes());old_files.append(name)
    with closing(sqlite3.connect(ROOT/'data/modus.sqlite3',timeout=15)) as source,closing(sqlite3.connect(backup/'modus.sqlite3')) as destination:source.backup(destination)
    process=None;copied=False
    try:
        print('[2/5] 正在应用升级文件，不修改账号和个人连接。',flush=True)
        copied=True
        for name in changed:
            target=(ROOT/name)
            if not target.resolve().is_relative_to(ROOT):raise RuntimeError('目标路径不正确')
            target.parent.mkdir(parents=True,exist_ok=True)
            pending=target.with_name(target.name+'.upgrade-pending');pending.write_bytes((BASE/name).read_bytes());pending.replace(target)
        print('[3/5] 正在重启看板后端，公网隧道保持运行。',flush=True)
        if old_pid:stop_backend(old_pid)
        # Capture the latest committed work after stopping the old backend.
        with closing(sqlite3.connect(ROOT/'data/modus.sqlite3',timeout=15)) as source,closing(sqlite3.connect(backup/'modus.sqlite3')) as destination:source.backup(destination)
        before=snapshot()
        (backup/'snapshot.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
        process=start_backend();wait_ready()
        print('[4/5] 正在验证账号保留、数据库完整性和回收站保留时间。',flush=True)
        validate_data(before)
        info=None
        config_path=Path.home()/'.codex/modus-board/connection.json'
        if config_path.is_file():
            config=json.loads(config_path.read_text(encoding='utf-8'))
            info=get('/api/codex/info',config['token'])
            if info['version']!=VERSION:raise RuntimeError('在线规则版本不正确')
            trash=get('/api/trash',config['token'])
            if trash['retention_days']!=30:raise RuntimeError('回收站保留期不正确')
        report={'applied':True,'version':VERSION,'accounts_preserved':True,'database_integrity':'ok','backup':str(backup),'retention_days':30,'tunnel_restarted':False}
        report.update({'revision':revision,'accounts_preserved':True})
        (ROOT/'data/deployment-state.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('[5/5] Deployment completed: '+VERSION+' / '+revision,flush=True)
        print('Production account data and tunnel configuration were retained.',flush=True)
        return 0
    except Exception:
        print('升级未完成，正在恢复此前代码；不会用旧数据库覆盖你的新数据。',flush=True)
        if process and process.poll() is None:
            process.terminate();process.wait(timeout=10)
        elif copied:
            current=backend()
            if current:stop_backend(current)
        if copied:
            for name in old_files:
                target=ROOT/name;pending=target.with_name(target.name+'.restore-pending');pending.write_bytes((backup/'source'/name).read_bytes());pending.replace(target)
        if not backend():start_backend()
        raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--verify-only',action='store_true');args=parser.parse_args()
    if args.verify_only:
        manifest=verify()
        print(json.dumps({'verified':True,'files':len(manifest),'version':VERSION,'backend_pid':backend(),'service_changed':False},ensure_ascii=False));return 0
    lock=(ROOT/'data/deploy.lock').open('a+b')
    try:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return apply()
    finally:lock.close()


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    try:raise SystemExit(main())
    except Exception as error:
        print('未完成：'+str(error),file=sys.stderr,flush=True)
        raise SystemExit(1)
