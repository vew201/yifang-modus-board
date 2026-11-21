"""Versioned, authenticated MODUS client delivery. No polling thread or model calls."""
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import zipfile

PROTOCOL = 1
CLIENT_FILES = ['SKILL.md','references/看板专用对话说明.txt','references/接口与脚本.txt','scripts/modus.py','runtime.py']


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def sign(value, secret):
    return hmac.new(bytes.fromhex(secret),canonical(value),hashlib.sha256).hexdigest()


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp')
    temporary.write_bytes(canonical(value));temporary.replace(path)


class Delivery:
    def __init__(self,root,data_dir):
        self.root=Path(root);self.data=Path(data_dir)
        identity=self.data/'codex-identity.json'
        if not identity.exists():
            atomic_json(identity,{'board_id':secrets.token_hex(16),'verification_key':secrets.token_hex(32)})
        self.identity=json.loads(identity.read_text(encoding='utf-8'))
        self.lock=threading.RLock()
        self.manifest=None
        self.file_cache={}
        self.address_attempt=0
        self.address_worker=None
        self.publish()

    def publish(self):
        """Explicit build/startup step. Requests serve an immutable cached release."""
        with self.lock:
            specification=json.loads((self.root/'codex-release.json').read_text(encoding='utf-8'))
            content={name:(self.root/'codex-skill'/name).read_bytes() for name in CLIENT_FILES}
            hashes={name:hashlib.sha256(value).hexdigest() for name,value in content.items()}
            release=hashlib.sha256(canonical({'files':hashes,'version':specification['version'],'protocol':PROTOCOL})).hexdigest()
            manifest={'board_id':self.identity['board_id'],'release':release,'version':specification['version'],
                      'protocol':PROTOCOL,'notes':specification['notes'],'discovery_domain':self.discovery().get('domain',''),
                      'files':{name:{'sha256':hashes[name],'size':len(value)} for name,value in content.items()}}
            manifest['signature']=sign(manifest,self.identity['verification_key'])
            directory=self.data/'codex-releases'/release
            directory.mkdir(parents=True,exist_ok=True)
            for name,value in content.items():
                target=directory/name;target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():target.write_bytes(value)
                if target.read_bytes()!=value:raise ValueError('已发布版本的文件被修改，请创建新版本')
            atomic_json(directory/'manifest.json',manifest)
            atomic_json(self.data/'codex-current.json',{'release':release,'version':specification['version']})
            self.manifest=manifest
            self.file_cache={hashes[name]:value for name,value in content.items()}
            self.etag='"'+hashlib.sha256(canonical(manifest)).hexdigest()+'"'
            return manifest

    def initialize_database(self,connection):
        connection.execute('''CREATE TABLE IF NOT EXISTS codex_activations(
            token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),
            expires_at REAL NOT NULL,claim_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS codex_clients(
            token_hash TEXT PRIMARY KEY REFERENCES agent_tokens(token_hash),release TEXT NOT NULL,
            installed_at TEXT NOT NULL,last_updated_at TEXT NOT NULL)''')
        connection.execute('''CREATE TABLE IF NOT EXISTS codex_setup_tickets(
            token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),expires_at REAL NOT NULL)''')

    def discovery(self):
        try:
            config=json.loads((self.data/'discovery.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):config={}
        return config

    def maybe_publish_address(self):
        """Reuse existing health traffic for address changes; no new periodic task."""
        from share import read_network_status
        config=self.discovery()
        if not config.get('domain') or not config.get('token'):return
        network=read_network_status(self.data)
        if network.get('status')!='running' or not network.get('url'):return
        try:published=json.loads((self.data/'discovery-status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):published={}
        if published.get('ok') and published.get('url')==network['url']:return
        if time.time()-self.address_attempt<30 or (self.address_worker and self.address_worker.is_alive()):return
        self.address_attempt=time.time()
        def publish():
            try:self.publish_address(network['url'])
            except ValueError:pass
        self.address_worker=threading.Thread(target=publish,daemon=True,name='modus-address-publish')
        self.address_worker.start()

    def publish_address(self,url):
        """Call only when a usable address changes, never every client interaction."""
        config=self.discovery()
        if not config.get('domain') or not config.get('token'):
            return {'configured':False,'message':'固定入口尚未授权'}
        if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com',url):raise ValueError('共享地址格式不正确')
        record={'b':self.identity['board_id'],'u':url}
        record['s']=sign(record,self.identity['verification_key'])
        text=json.dumps(record,separators=(',',':'))
        # Only the address and its authenticator go into public DNS. No login tokens.
        query=urlencode({'domains':config['domain'].removesuffix('.duckdns.org'),'token':config['token'],'txt':text})
        try:
            with urlopen('https://www.duckdns.org/update?'+query,timeout=8) as response:
                result=response.read(1024).decode().strip()
            if result!='OK':raise ValueError('固定入口发布失败，请核对授权')
            atomic_json(self.data/'discovery-status.json',{'url':url,'published_at':time.time(),'ok':True})
            return {'configured':True,'ok':True,'domain':config['domain']}
        except Exception:
            # Do not surface a URL containing the provider's token in exceptions/logs.
            atomic_json(self.data/'discovery-status.json',{'ok':False,'checked_at':time.time()})
            raise ValueError('固定入口发布失败，现有地址仍保留，请检查网络或授权') from None

    def bootstrap(self,connection,user_id,url):
        from server import timestamp,token_hash
        if not url:raise ValueError('请先启动公网共享，再生成异地接入包')
        activation=secrets.token_urlsafe(32)
        connection.execute('INSERT INTO codex_activations VALUES(?,?,?,?,?)',(token_hash(activation),user_id,time.time()+7*86400,'',timestamp()))
        config={'url':url,'board_id':self.identity['board_id'],'verification_key':self.identity['verification_key'],
                'activation':activation,'discovery_domain':self.discovery().get('domain','')}
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('开始接入.txt','这个包仅供绑定账号本人使用，包含限时一次性连接凭据，不要公开。\n请Codex先阅读，运行 python bootstrap.py --workspace 当前专用会话工作目录。\n安装会写入MODUS专用文件与按消息触发的Hook，不创建定时任务。安装完成后，AI可打开返回的hook_review_launcher首次授权窗口，提示使用者输入/hooks并审阅信任；确认后关闭该窗口，继续原会话。不要修改信任数据库。\n无需再次索取账号密码。\n')
            archive.writestr('bootstrap.json',canonical(config))
            for name in ['bootstrap.py','launcher.py']:
                archive.writestr(name,(self.root/'codex-bootstrap'/name).read_bytes())
            for name in CLIENT_FILES:archive.writestr('seed/'+name,(self.root/'codex-skill'/name).read_bytes())
            archive.writestr('seed/manifest.json',canonical(self.manifest))
        return buffer.getvalue()

    def activate(self,connection,data):
        from server import token_hash,timestamp
        token=data.get('activation','');claim=data.get('claim_id','')
        if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',claim):raise ValueError('接入凭据格式不正确')
        connection.execute('BEGIN IMMEDIATE')
        row=connection.execute('SELECT * FROM codex_activations WHERE token_hash=?',(token_hash(token),)).fetchone()
        if not row or row['expires_at']<time.time():raise ValueError('接入包已过期，请在工作台生成新包')
        if row['claim_id'] and row['claim_id']!=claim:raise ValueError('接入包已在其他安装中使用')
        bearer=hmac.new(bytes.fromhex(self.identity['verification_key']),(token+':'+claim).encode(),hashlib.sha256).hexdigest()
        connection.execute('INSERT OR IGNORE INTO agent_tokens VALUES(?,?,?,?)',(token_hash(bearer),row['user_id'],'MODUS自动更新客户端',timestamp()))
        connection.execute('UPDATE codex_activations SET claim_id=? WHERE token_hash=?',(claim,token_hash(token)))
        connection.execute('INSERT OR IGNORE INTO codex_clients VALUES(?,?,?,?)',(token_hash(bearer),self.manifest['release'],timestamp(),timestamp()))
        user=connection.execute('SELECT id,name,username FROM users WHERE id=?',(row['user_id'],)).fetchone()
        connection.commit()
        return {'token':bearer,'user':dict(user),'board_id':self.identity['board_id']}

    def route(self,handler,connection,method,path,data,session):
        from server import timestamp
        if method=='GET' and path=='/api/codex/manifest':
            if handler.headers.get('If-None-Match')==self.etag:
                return handler.reply(304,b'',{'ETag':self.etag})
            return handler.reply(200,self.manifest,{'ETag':self.etag})
        if method=='GET' and path.startswith('/api/codex/files/'):
            digest=path.rsplit('/',1)[-1]
            if digest not in self.file_cache:return handler.reply(404,{'error':'更新文件不存在'})
            return handler.reply(200,self.file_cache[digest],content_type='application/octet-stream')
        if method=='GET' and path=='/api/codex/connect-info':
            user=connection.execute('SELECT id,name,username FROM users WHERE id=?',(session['user_id'],)).fetchone()
            return handler.reply(200,{**self.identity,'user':dict(user),'discovery_domain':self.discovery().get('domain','')})
        if method=='GET' and path=='/api/codex/info':
            config=self.discovery()
            return handler.reply(200,{'version':self.manifest['version'],'release':self.manifest['release'],'notes':self.manifest['notes'],
                'files':[{'path':key,**value} for key,value in self.manifest['files'].items()],
                'discovery_domain':config.get('domain',''),'discovery_configured':bool(config.get('domain') and config.get('token')),
                'clients': [dict(row) for row in connection.execute('SELECT c.release,c.installed_at,c.last_updated_at,u.name FROM codex_clients c JOIN agent_tokens a ON a.token_hash=c.token_hash JOIN users u ON u.id=a.user_id')]})
        if method=='POST' and path=='/api/codex/bootstrap':
            from share import read_network_status
            url=data.get('url') or read_network_status(self.data).get('url')
            if not url:raise ValueError('请先启动公网共享')
            # Pack URLs may point only at this backend's known current address or local loopback.
            current=read_network_status(self.data).get('url')
            if url not in {current,'http://127.0.0.1:'+str(handler.server.server_port)}:raise ValueError('请选择当前工作台地址')
            payload=self.bootstrap(connection,session['user_id'],url);connection.commit()
            return handler.reply(200,{'name':'MODUS-首次接入包.zip','content':base64.b64encode(payload).decode()})
        if method=='POST' and path=='/api/codex/enroll':
            if not isinstance(session,dict) or not session.get('is_agent'):raise ValueError('此操作需要Codex接入凭证')
            if data.get('release')!=self.manifest['release']:raise ValueError('请先安装当前接入版本')
            digest=handler.headers['Authorization'][7:]
            from server import token_hash
            connection.execute('INSERT INTO codex_clients VALUES(?,?,?,?) ON CONFLICT(token_hash) DO UPDATE SET release=excluded.release,last_updated_at=excluded.last_updated_at',(token_hash(digest),data['release'],timestamp(),timestamp()))
            connection.commit();return handler.reply(200,{'ok':True})
        if method=='POST' and path=='/api/codex/publish':
            # Publishing reads maintained source files; caller cannot upload replacement code here.
            manifest=self.publish()
            return handler.reply(200,{'ok':True,'release':manifest['release'],'version':manifest['version']})
        if method=='POST' and path=='/api/codex/setup-ticket':
            from server import token_hash
            ticket=secrets.token_hex(32)
            connection.execute('DELETE FROM codex_setup_tickets WHERE expires_at<?',(time.time(),))
            connection.execute('INSERT INTO codex_setup_tickets VALUES(?,?,?)',(token_hash(ticket),session['user_id'],time.time()+300))
            connection.commit();return handler.reply(200,{'ticket':ticket})
        if method=='POST' and path=='/api/codex/discovery':
            if data.get('via_link'):
                from server import token_hash
                ticket=connection.execute('SELECT * FROM codex_setup_tickets WHERE token_hash=? AND user_id=? AND expires_at>?',(token_hash(data.get('ticket','')),session['user_id'],time.time())).fetchone()
                if not ticket:raise ValueError('首次配置授权链接无效或已过期')
                connection.execute('DELETE FROM codex_setup_tickets WHERE token_hash=?',(ticket['token_hash'],));connection.commit()
            domain=data.get('domain','').strip().lower().removesuffix('.duckdns.org')
            token=data.get('token','').strip()
            if not re.fullmatch('[a-z0-9-]{3,63}',domain) or not re.fullmatch('[a-fA-F0-9-]{20,100}',token):raise ValueError('请填写DuckDNS名称和授权Token')
            atomic_json(self.data/'discovery.json',{'domain':domain+'.duckdns.org','token':token})
            self.publish()
            from share import read_network_status
            url=read_network_status(self.data).get('url')
            result=self.publish_address(url) if url else {'configured':True,'ok':False,'message':'等待公网共享地址'}
            return handler.reply(200,result)
        return False

    def check_write(self,handler,connection,session):
        from server import token_hash
        if not isinstance(session,dict) or not session.get('is_agent'):return True
        digest=token_hash(handler.headers.get('Authorization','')[7:])
        managed=connection.execute('SELECT 1 FROM codex_clients WHERE token_hash=?',(digest,)).fetchone()
        if managed and handler.headers.get('X-Modus-Release')!=self.manifest['release']:
            handler.reply(428,{'error':'接入规则已更新，请先同步最新版再写入','code':'CLIENT_UPDATE_REQUIRED','release':self.manifest['release']})
            return False
        return True
