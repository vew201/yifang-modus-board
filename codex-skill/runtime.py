"""MODUS on-demand client synchronization. Python standard library only."""
import contextlib
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request,build_opener,HTTPRedirectHandler

PROTOCOL=1
LIMIT=2*1024*1024


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def digest(value):return hashlib.sha256(value).hexdigest()
def read(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return default
def atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp');tmp.write_bytes(canonical(value));tmp.replace(path)
def signature(value,key):return hmac.new(bytes.fromhex(key),canonical(value),hashlib.sha256).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('连接地址重定向已拒绝，请通过固定发现入口更新')


def valid_url(url):
    parsed=urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError('工作台地址格式不正确')
    if parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'}:return url.rstrip('/')
    if parsed.scheme!='https' or not parsed.netloc:raise ValueError('远程连接必须使用HTTPS')
    return url.rstrip('/')


def http(url,path,token='',etag='',method='GET',body=None,timeout=4):
    headers={'Cache-Control':'no-cache'}
    if token:headers['Authorization']='Bearer '+token
    if etag:headers['If-None-Match']=etag
    if body is not None:headers['Content-Type']='application/json'
    request=Request(valid_url(url)+path,headers=headers,method=method,data=None if body is None else canonical(body))
    try:response=build_opener(NoRedirect()).open(request,timeout=timeout)
    except HTTPError as error:
        if error.code==304:return 304,b'',dict(error.headers)
        raise RuntimeError('连接失败，HTTP '+str(error.code)) from None
    with response:
        value=response.read(LIMIT+1)
        if len(value)>LIMIT:raise ValueError('更新响应超过大小限制')
        return response.status,value,dict(response.headers)


def discover(config):
    domain=config.get('discovery_domain','')
    if not re.fullmatch(r'[a-z0-9-]+\.duckdns\.org',domain):raise ValueError('固定发现入口尚未设置')
    # DNS carries signed routing metadata only. Never send a user token to the resolver.
    lookup=secrets.token_hex(4)+'.'+domain
    request=Request('https://dns.google/resolve?name='+quote(lookup)+'&type=TXT',headers={'Accept':'application/dns-json'})
    with build_opener(NoRedirect()).open(request,timeout=4) as response:payload=json.loads(response.read(16384))
    for answer in payload.get('Answer',[]):
        if answer.get('type')!=16:continue
        text=answer.get('data','')
        try:
            # DNS JSON TXT values are quoted; multi-string records are concatenated.
            try:record=json.loads(text)
            except ValueError:
                chunks=re.findall(r'"(?:\\.|[^"\\])*"',text)
                record=json.loads(''.join(json.loads(chunk) for chunk in chunks))
            if isinstance(record,str):
                try:record=json.loads(record)
                except ValueError:record=json.loads(record.replace('\\"','"'))
            signature_value=record.pop('s')
            if record.get('b')!=config['board_id']:continue
            if not hmac.compare_digest(signature(record,config['verification_key']),signature_value):continue
            url=record.get('u','')
            if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com',url):continue
            return url
        except (ValueError,TypeError,KeyError):continue
    raise ValueError('未找到可验证的工作台新地址，保留原连接及本地版本')


def verify(manifest,config):
    unsigned={key:value for key,value in manifest.items() if key!='signature'}
    if manifest.get('board_id')!=config['board_id'] or not hmac.compare_digest(signature(unsigned,config['verification_key']),manifest.get('signature','')):
        raise ValueError('工作台版本签名校验失败')
    if manifest.get('protocol')!=PROTOCOL:raise ValueError('客户端引导协议不兼容，已暂停更新')
    if not re.fullmatch('[a-f0-9]{64}',manifest.get('release','')):raise ValueError('更新版本编号无效')
    files=manifest.get('files',{})
    if not isinstance(files,dict) or not 1<=len(files)<=32:raise ValueError('更新清单无效')
    for name,item in files.items():
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:raise ValueError('更新文件路径超出专用目录')
        if not re.fullmatch('[a-f0-9]{64}',item.get('sha256','')) or not 0<item.get('size',0)<=LIMIT:raise ValueError('更新文件校验信息无效')
    for needed in ['SKILL.md','runtime.py','scripts/modus.py']:
        if needed not in files:raise ValueError('更新清单缺少必要文件')


@contextlib.contextmanager
def lock(home):
    """OS lock releases on crash, avoiding stale lock files and racing active pointers."""
    file=(home/'sync.lock').open('a+b')
    if file.tell()==0:file.write(b'0');file.flush()
    file.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            for attempt in range(40):
                try:msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1);break
                except OSError:
                    if attempt==39:raise ValueError('另一个会话正在更新，请稍后重试')
                    time.sleep(.05)
        else:
            import fcntl;fcntl.flock(file,fcntl.LOCK_EX)
        yield
    finally:
        if os.name=='nt':
            import msvcrt
            try:file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            except OSError:pass
        else:
            import fcntl;fcntl.flock(file,fcntl.LOCK_UN)
        file.close()


def sync(home,force=False):
    home=Path(home);home.mkdir(parents=True,exist_ok=True)
    with lock(home):
        config=read(home/'connection.json');old=read(home/'active.json',{})
        if not config or not config.get('token'):raise ValueError('尚未完成首次连接')
        if not force and old.get('release'):
            directory=home/'releases'/old['release']
            if not (directory/'runtime.py').is_file():force=True
        etag='' if force else old.get('etag','')
        try:status,content,headers=http(config['url'],'/api/codex/manifest',config['token'],etag)
        except (OSError,URLError,RuntimeError):
            new_url=discover(config)
            status,content,headers=http(new_url,'/api/codex/manifest',config['token'],etag)
            config['url']=new_url;atomic(home/'connection.json',config)
        if status==304 and old.get('release'):
            return {**old,'updated':False,'downloaded_files':0}
        manifest=json.loads(content);verify(manifest,config)
        if manifest.get('discovery_domain') and manifest['discovery_domain']!=config.get('discovery_domain'):
            if not re.fullmatch(r'[a-z0-9-]+\.duckdns\.org',manifest['discovery_domain']):raise ValueError('固定发现名称无效')
            config['discovery_domain']=manifest['discovery_domain'];atomic(home/'connection.json',config)
        release=manifest['release'];directory=home/'releases'/release
        if old.get('release')==release and directory.is_dir():
            old['etag']=headers.get('ETag','"'+digest(canonical(manifest))+'"');atomic(home/'active.json',old)
            return {**old,'updated':False,'downloaded_files':0}
        temporary=home/'releases'/('.pending-'+secrets.token_hex(8));temporary.mkdir(parents=True)
        old_dir=home/'releases'/old.get('release','missing')
        count=0
        try:
            for name,item in manifest['files'].items():
                old_file=old_dir/name;target=temporary/name;target.parent.mkdir(parents=True,exist_ok=True)
                if old_file.is_file() and digest(old_file.read_bytes())==item['sha256']:
                    shutil.copyfile(old_file,target)
                else:
                    _,value,_=http(config['url'],'/api/codex/files/'+item['sha256'],config['token'])
                    if len(value)!=item['size'] or digest(value)!=item['sha256']:raise ValueError('更新文件校验失败，原版本保持不变')
                    target.write_bytes(value);count+=1
            # Syntax checks do not execute new client code before the whole release verifies.
            for name in manifest['files']:
                if name.endswith('.py'):compile((temporary/name).read_text(encoding='utf-8'),name,'exec')
            atomic(temporary/'manifest.json',manifest)
            if directory.exists():
                for name,item in manifest['files'].items():
                    if not (directory/name).is_file() or digest((directory/name).read_bytes())!=item['sha256']:raise ValueError('已有版本目录校验失败')
            else:temporary.rename(directory)
            state={'release':release,'version':manifest['version'],'etag':headers.get('ETag','"'+digest(canonical(manifest))+'"'),'previous_release':old.get('release',''),'installed_at':time.time()}
            atomic(home/'active.json',state)
            try:http(config['url'],'/api/codex/enroll',config['token'],method='POST',body={'release':release})
            except Exception:
                # A failed registration does not produce a half-installed release; retry explicitly.
                atomic(home/'active.json',old);raise
            keep={release,old.get('release','')}
            for candidate in (home/'releases').iterdir():
                if candidate.name in keep or not re.fullmatch('[a-f0-9]{64}',candidate.name):continue
                if candidate.is_dir() and not candidate.is_symlink() and candidate.resolve().parent==(home/'releases').resolve():
                    shutil.rmtree(candidate)
            return {**state,'updated':True,'downloaded_files':count}
        finally:
            if temporary.exists() and temporary.resolve().parent==(home/'releases').resolve():shutil.rmtree(temporary)


def session_path(home,session):return Path(home)/'sessions'/(digest(session.encode())[:24]+'.json')
def context_text(home,state,session):
    directory=Path(home)/'releases'/state['release']
    return ('MODUS当前有效接入规则 '+state['version']+'（'+state['release'][:12]+'）。本会话先读取 '+str(directory/'SKILL.md')+'，再按需读取该目录 references 中的说明。旧规则以本版本为准；完成读取后执行：python "'+str(Path(home)/'launcher.py')+'" ack --session '+session+' --release '+state['release']+'。账号和业务任务不变。')


def marker_for(cwd):
    current=Path(cwd).resolve()
    for path in [current,*list(current.parents)[:12]]:
        marker=read(path/'.modus-board.json')
        if marker:return marker
    return None


def hook(home,event):
    marker=marker_for(event.get('cwd',os.getcwd()))
    if not marker or Path(marker.get('connector_home','')).resolve()!=Path(home).resolve():return 0
    session=event.get('session_id','default');event_name=event.get('hook_event_name','UserPromptSubmit')
    state=read(Path(home)/'active.json',{})
    receipt=session_path(home,session);cached=read(receipt,{})
    if event_name=='UserPromptSubmit':
        event_key=str(event.get('turn_id',''))+':'+digest(event.get('prompt','').encode())
        if cached.get('event_key')!=event_key:
            try:state=sync(home)
            except Exception as error:
                print(json.dumps({'hookSpecificOutput':{'hookEventName':event_name,'additionalContext':'MODUS更新检查未完成：'+str(error)+'。可以继续整理信息，暂不写入看板；不得声称已使用最新版。'}},ensure_ascii=False));return 0
            atomic(receipt,{**cached,'event_key':event_key,'checked_release':state['release']})
    if state.get('release') and (cached.get('loaded_release')!=state['release'] or event_name in {'SessionStart','PostCompact'}):
        print(json.dumps({'hookSpecificOutput':{'hookEventName':event_name,'additionalContext':context_text(home,state,session)}},ensure_ascii=False))
    return 0


def business(home,args):
    config=read(Path(home)/'connection.json');session=os.environ.get('CODEX_THREAD_ID','cli')
    receipt=read(session_path(home,session),{})
    current=read(Path(home)/'active.json',{})
    if receipt.get('checked_release')!=current.get('release') or not receipt.get('event_key'):
        current=sync(home)
    os.environ['MODUS_CONFIG']=str(Path(home)/'connection.json')
    for attempt in range(2):
        current=read(Path(home)/'active.json')
        writes=bool(args and (args[0]=='submit' or (args[0]=='request' and len(args)>1 and args[1]!='GET')))
        if writes and receipt.get('loaded_release')!=current['release']:
            raise ValueError('当前会话需先读取最新版规则再写入。'+context_text(home,current,session))
        os.environ['MODUS_RELEASE']=current['release']
        script=Path(home)/'releases'/current['release']/'scripts/modus.py'
        spec=importlib.util.spec_from_file_location('modus_business_'+current['release'],script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        sys.argv=[str(script),*args]
        stdout,stderr=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):result=module.main()
        if result and 'HTTP 428' in stderr.getvalue() and attempt==0:
            sync(home,force=True)
            if writes:
                raise ValueError('服务在本轮执行期间更新，原操作未写入。请读取新规则后核对原提交，再重试。'+context_text(home,read(Path(home)/'active.json'),session))
            continue
        print(stdout.getvalue(),end='');print(stderr.getvalue(),end='',file=sys.stderr);return result
    return 1


def run(home,args):
    home=Path(home)
    if args and args[0]=='hook':return hook(home,json.load(sys.stdin))
    if args and args[0]=='ack':
        import argparse
        parser=argparse.ArgumentParser();parser.add_argument('--session',required=True);parser.add_argument('--release',required=True)
        values=parser.parse_args(args[1:]);state=read(home/'active.json',{})
        if values.release!=state.get('release'):raise ValueError('不能确认过期版本')
        path=session_path(home,values.session);record=read(path,{})
        atomic(path,{**record,'loaded_release':values.release});return 0
    if args and args[0] in {'check','sync'}:
        print(json.dumps(sync(home,force='--force' in args),ensure_ascii=False));return 0
    if args and args[0]=='rules':
        state=read(home/'active.json');print(str(home/'releases'/state['release']/'SKILL.md'));return 0
    return business(home,args)
