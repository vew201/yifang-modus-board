import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from server import WorkbenchServer,connect_database
from codex_delivery import atomic_json,canonical,sign
from test_backend import Client

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('test_runtime',ROOT/'codex-skill/runtime.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)


class CodexUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.directory=Path(self.temp.name)
        self.server=WorkbenchServer(('127.0.0.1',0),self.directory/'backend')
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.client=Client(self.url)
        status,session,_=self.client.request('/api/setup','POST',{'accounts':[{'name':'甲','username':'a','password':'abcdefgh'},{'name':'乙','username':'b','password':'ijklmnop'}]})
        assert status==200;self.client.csrf=session['csrf']
        self.publisher=self.directory/'publisher'
        shutil.copytree(ROOT/'codex-skill',self.publisher/'codex-skill',ignore=shutil.ignore_patterns('__pycache__','connection.json'))
        shutil.copytree(ROOT/'codex-bootstrap',self.publisher/'codex-bootstrap',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(ROOT/'codex-release.json',self.publisher/'codex-release.json')
        self.server.delivery.root=self.publisher;self.server.delivery.publish()
        _,result,_=self.client.request('/api/agent-token','POST',{})
        self.token=result['token'];self.home=self.directory/'client';self.home.mkdir()
        self.config={'url':self.url,'token':self.token,**self.server.delivery.identity,'user':{'id':1,'name':'甲'}}
        atomic_json(self.home/'connection.json',self.config)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join(3);self.temp.cleanup()

    def bump(self,text='新增规则：按实际成果确认'):
        path=self.publisher/'codex-skill/references/看板专用对话说明.txt'
        path.write_text(path.read_text(encoding='utf-8')+'\n'+text,encoding='utf-8')
        return self.server.delivery.publish()

    def test_authenticated_manifest_etag_and_one_request_unchanged(self):
        self.assertEqual(Client(self.url).request('/api/codex/manifest')[0],401)
        first=runtime.sync(self.home)
        self.assertEqual(first['downloaded_files'],5)
        config_before=(self.home/'connection.json').read_bytes()
        with patch.object(runtime,'http',wraps=runtime.http) as requests:
            second=runtime.sync(self.home)
        self.assertEqual(requests.call_count,1)
        self.assertFalse(second['updated']);self.assertEqual(second['downloaded_files'],0)
        self.assertEqual((self.home/'connection.json').read_bytes(),config_before)

    def test_only_changed_file_downloaded_and_credentials_preserved(self):
        first=runtime.sync(self.home);before=(self.home/'connection.json').read_bytes();self.bump()
        with patch.object(runtime,'http',wraps=runtime.http) as requests:second=runtime.sync(self.home)
        self.assertTrue(second['updated']);self.assertEqual(second['downloaded_files'],1)
        self.assertEqual(requests.call_count,3) # manifest, changed file, register current version
        self.assertEqual(second['previous_release'],first['release'])
        self.assertTrue((self.home/'releases'/first['release']).is_dir())
        self.assertEqual((self.home/'connection.json').read_bytes(),before)

    def test_bad_file_rolls_back_active_version(self):
        first=runtime.sync(self.home);manifest=self.bump()
        target=manifest['files']['references/看板专用对话说明.txt']['sha256']
        self.server.delivery.file_cache[target]=b'corrupted'
        with self.assertRaises(ValueError):runtime.sync(self.home)
        self.assertEqual(runtime.read(self.home/'active.json')['release'],first['release'])
        self.assertFalse(list((self.home/'releases').glob('.pending-*')))

    def test_signature_and_path_escape_rejected(self):
        manifest=dict(self.server.delivery.manifest);manifest['version']='forged'
        with self.assertRaises(ValueError):runtime.verify(manifest,self.config)
        manifest=json.loads(json.dumps(self.server.delivery.manifest))
        manifest['files']['../connection.json']=next(iter(manifest['files'].values()))
        unsigned={k:v for k,v in manifest.items() if k!='signature'}
        manifest['signature']=sign(unsigned,self.config['verification_key'])
        with self.assertRaises(ValueError):runtime.verify(manifest,self.config)

    def test_old_managed_client_write_denied_and_new_succeeds(self):
        first=runtime.sync(self.home)
        payload={'title':'版本校验测试','owner_id':1,'start_date':'','end_date':''}
        headers={'Authorization':'Bearer '+self.token,'X-Modus-Release':first['release']}
        self.bump()
        self.assertEqual(self.client.request('/api/tasks','POST',payload,headers)[0],428)
        second=runtime.sync(self.home);headers['X-Modus-Release']=second['release']
        self.assertEqual(self.client.request('/api/tasks','POST',payload,headers)[0],200)

    def test_activation_is_scoped_single_install_and_retry_safe(self):
        status,body,_=self.client.request('/api/codex/bootstrap','POST',{'url':self.url})
        self.assertEqual(status,200,body)
        import zipfile
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(body['content']))) as archive:
            config=json.loads(archive.read('bootstrap.json'))
            self.assertNotIn('token',config)
            self.assertIn('seed/runtime.py',archive.namelist())
        data={'activation':config['activation'],'claim_id':'a'*32}
        outsider=Client(self.url)
        status,first,_=outsider.request('/api/codex/activate','POST',data)
        self.assertEqual(status,200,first);self.assertEqual(first['user']['id'],1)
        status,second,_=outsider.request('/api/codex/activate','POST',data)
        self.assertEqual(first['token'],second['token'])
        self.assertEqual(outsider.request('/api/codex/activate','POST',{**data,'claim_id':'b'*32})[0],400)

    def test_hook_scoped_dedup_and_rule_reload_ack(self):
        workspace=self.directory/'workspace';workspace.mkdir()
        atomic_json(workspace/'.modus-board.json',{'connector_home':str(self.home)})
        event={'cwd':str(workspace),'session_id':'test-session','turn_id':'turn-1','prompt':'登记项目','hook_event_name':'UserPromptSubmit'}
        output=io.StringIO()
        with redirect_stdout(output):runtime.hook(self.home,event)
        self.assertIn('SKILL.md',output.getvalue())
        state=runtime.read(self.home/'active.json')
        runtime.run(self.home,['ack','--session','test-session','--release',state['release']])
        with patch.object(runtime,'http',wraps=runtime.http) as requests,redirect_stdout(io.StringIO()):runtime.hook(self.home,event)
        self.assertEqual(requests.call_count,0)
        with patch.object(runtime,'http',wraps=runtime.http) as requests,redirect_stdout(io.StringIO()):runtime.hook(self.home,{**event,'turn_id':'turn-2'})
        self.assertEqual(requests.call_count,1)
        self.bump()
        output=io.StringIO()
        with redirect_stdout(output):runtime.hook(self.home,{**event,'turn_id':'turn-3'})
        self.assertIn('当前有效接入规则',output.getvalue())
        other=self.directory/'unrelated';other.mkdir()
        with patch.object(runtime,'http',wraps=runtime.http) as requests:runtime.hook(self.home,{**event,'cwd':str(other)})
        self.assertEqual(requests.call_count,0)

    def test_offline_hook_keeps_state_and_reports_pause(self):
        first=runtime.sync(self.home)
        workspace=self.directory/'offline';workspace.mkdir();atomic_json(workspace/'.modus-board.json',{'connector_home':str(self.home)})
        output=io.StringIO()
        with patch.object(runtime,'http',side_effect=OSError('offline')),redirect_stdout(output):
            runtime.hook(self.home,{'cwd':str(workspace),'session_id':'s','turn_id':'t','prompt':'登记','hook_event_name':'UserPromptSubmit'})
        self.assertIn('暂不写入',output.getvalue())
        self.assertEqual(runtime.read(self.home/'active.json')['release'],first['release'])

    def test_signed_discovery_accepts_only_own_board(self):
        record={'b':self.config['board_id'],'u':'https://new-modus-test.trycloudflare.com'}
        record['s']=sign(record,self.config['verification_key'])
        encoded={'data':json.dumps(json.dumps(record))}
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps({'Answer':[{'type':16,'data':encoded['data']}]}).encode()
        opener=type('Opener',(),{'open':lambda *args,**kwargs:Response()})()
        with patch.object(runtime,'build_opener',return_value=opener):
            self.assertEqual(runtime.discover({**self.config,'discovery_domain':'modus-test.duckdns.org'}),record['u'])
            encoded['data']=json.dumps(record) # Actual Google DNS JSON format: raw JSON TXT data.
            self.assertEqual(runtime.discover({**self.config,'discovery_domain':'modus-test.duckdns.org'}),record['u'])
            encoded['data']=json.dumps(json.dumps(record).replace('"','\\"'))
            self.assertEqual(runtime.discover({**self.config,'discovery_domain':'modus-test.duckdns.org'}),record['u'])
            record['b']='different-board'
            encoded['data']=json.dumps(record)
            with self.assertRaises(ValueError):runtime.discover({**self.config,'discovery_domain':'modus-test.duckdns.org'})

    def test_metadata_updates_do_not_download_code(self):
        first=runtime.sync(self.home)
        atomic_json(self.server.delivery.data/'discovery.json',{'domain':'modus-fixture.duckdns.org','token':'fixture-private-token'})
        self.server.delivery.publish()
        with patch.object(runtime,'http',wraps=runtime.http) as requests:second=runtime.sync(self.home)
        self.assertEqual(second['release'],first['release']);self.assertEqual(second['downloaded_files'],0)
        self.assertEqual(requests.call_count,1)
        self.assertEqual(runtime.read(self.home/'connection.json')['discovery_domain'],'modus-fixture.duckdns.org')

    def test_full_bootstrap_installs_without_password_and_preserves_hooks(self):
        status,body,_=self.client.request('/api/codex/bootstrap','POST',{'url':self.url});self.assertEqual(status,200)
        import zipfile
        package=self.directory/'package';package.mkdir()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(body['content']))) as archive:archive.extractall(package)
        codex=self.directory/'codex';codex.mkdir();atomic_json(codex/'hooks.json',{'hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing-safe-hook'}]}]}})
        workspace=self.directory/'working';client_home=codex/'modus-board'
        result=subprocess.run([sys.executable,str(package/'bootstrap.py'),'--workspace',str(workspace),'--codex-home',str(codex)],capture_output=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        hook_config=json.loads((codex/'hooks.json').read_text(encoding='utf-8'))
        self.assertEqual(hook_config['hooks']['Stop'][0]['hooks'][0]['command'],'existing-safe-hook')
        self.assertIn('UserPromptSubmit',hook_config['hooks'])
        self.assertEqual(runtime.read(client_home/'installation.json')['hook_trust'],'pending_user_review')
        command=[sys.executable,str(client_home/'launcher.py'),'status']
        status=subprocess.run(command,capture_output=True,encoding='utf-8',timeout=20)
        self.assertEqual(status.returncode,0,status.stderr)
        self.assertEqual(json.loads(status.stdout)['id'],1)
        self.bump()
        result=subprocess.run([sys.executable,str(client_home/'launcher.py'),'check'],capture_output=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['updated'])

    def test_writes_require_current_session_rule_ack(self):
        state=runtime.sync(self.home)
        file=self.directory/'payload.json';file.write_text(json.dumps({'title':'先读规则','owner_id':1,'start_date':'','end_date':''}),encoding='utf-8')
        with patch.dict(os.environ,{'CODEX_THREAD_ID':'test-ack'}):
            with self.assertRaises(ValueError):runtime.business(self.home,['request','POST','/api/tasks','--json-file',str(file)])
            runtime.run(self.home,['ack','--session','test-ack','--release',state['release']])
            with redirect_stdout(io.StringIO()):result=runtime.business(self.home,['request','POST','/api/tasks','--json-file',str(file)])
        self.assertEqual(result,0)

    def test_discovery_publisher_keeps_provider_token_private(self):
        atomic_json(self.server.delivery.data/'discovery.json',{'domain':'modus-fixture.duckdns.org','token':'fixture-private-token'})
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'OK'
        with patch('codex_delivery.urlopen',return_value=Response()) as request:
            self.server.delivery.publish_address('https://new-fixture.trycloudflare.com')
        url=request.call_args.args[0]
        self.assertIn('www.duckdns.org/update?',url)
        from urllib.parse import parse_qs,urlparse
        record=json.loads(parse_qs(urlparse(url).query)['txt'][0])
        self.assertNotIn('fixture-private-token',json.dumps(record))
        signature=record.pop('s')
        self.assertEqual(signature,sign(record,self.config['verification_key']))

    def test_setup_link_requires_one_time_ticket(self):
        _,ticket,_=self.client.request('/api/codex/setup-ticket','POST',{})
        payload={'domain':'modus-fixture','token':'12345678-1234-1234-1234-123456789abc','via_link':True,'ticket':ticket['ticket']}
        status,result,_=self.client.request('/api/codex/discovery','POST',payload)
        self.assertEqual(status,200,result) # no running public tunnel: save pending configuration
        self.assertEqual(self.client.request('/api/codex/discovery','POST',payload)[0],400)


if __name__=='__main__':unittest.main()
