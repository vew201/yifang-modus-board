"""Codex update panel and personal package browser acceptance, isolated backend."""
import argparse
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from server import WorkbenchServer
from test_backend import Client


def run(output):
    from playwright.sync_api import sync_playwright,expect
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        server=WorkbenchServer(('127.0.0.1',0),directory)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        url=f'http://127.0.0.1:{server.server_port}'
        client=Client(url)
        _,session,_=client.request('/api/setup','POST',{'accounts':[{'name':'甲','username':'a','password':'abcdefgh'},{'name':'乙','username':'b','password':'ijklmnop'}]})
        client.csrf=session['csrf']
        (Path(directory)/'network.json').write_text(json.dumps({'status':'running','url':'https://codex-fixture.trycloudflare.com','updated_epoch':time.time()}),encoding='utf-8')
        errors,checks=[],[]
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url)
                page.locator('[name="username"]').fill('a');page.locator('[name="password"]').fill('abcdefgh')
                page.get_by_role('button',name='登录',exact=True).click()
                page.get_by_role('button',name='Codex 接入',exact=True).click()
                expect(page.get_by_role('heading',name='Codex 接入与更新',exact=True)).to_be_visible()
                expect(page.locator('.codex-file-list .file-item')).to_have_count(5)
                expect(page.get_by_role('dialog')).to_contain_text('无定时任务')
                checks.append('工作台集中展示接入版本、最新技能说明文件和首次授权规则')
                with page.expect_download() as event:
                    page.get_by_role('button',name='生成并下载本人首次接入包',exact=True).click()
                payload=Path(event.value.path()).read_bytes()
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    names=archive.namelist()
                    assert 'bootstrap.py' in names and 'seed/manifest.json' in names
                    config=json.loads(archive.read('bootstrap.json'))
                    assert config['url']=='https://codex-fixture.trycloudflare.com'
                    assert 'activation' in config and 'token' not in config
                checks.append('从界面生成本人限时接入包，包含必要技能、安装器和一次性连接信息')
                page.get_by_text('固定发现入口 · 首次设置',exact=True).click()
                page.locator('[name="domain"]').fill('modus-browser-fixture')
                page.locator('[name="token"]').fill('12345678-1234-1234-1234-123456789abc')
                class Response:
                    def __enter__(self):return self
                    def __exit__(self,*args):pass
                    def read(self,*args):return b'OK'
                with patch('codex_delivery.urlopen',return_value=Response()):
                    page.get_by_role('button',name='保存并验证固定入口',exact=True).click()
                    expect(page.get_by_role('dialog')).to_contain_text('modus-browser-fixture.duckdns.org')
                expect(page.locator('[name="token"]')).to_have_value('')
                page.screenshot(path=str(output/'codex-updates.png'),full_page=True)
                checks.append('固定入口保存、状态展示及管理Token不回显')
                _,ticket,_=client.request('/api/codex/setup-ticket','POST',{})
                setup=base64.b64encode(json.dumps({'domain':'modus-link-fixture','token':'12345678-1234-1234-1234-123456789abc','ticket':ticket['ticket']}).encode()).decode()
                with patch('codex_delivery.urlopen',return_value=Response()):
                    page.goto(url+'/?codex-setup=1#dns-setup='+setup)
                    expect(page.get_by_role('dialog')).to_contain_text('modus-link-fixture.duckdns.org')
                assert 'dns-setup=' not in page.url and 'ticket' not in page.url
                checks.append('一次授权配置链接自动完成设置，凭据片段立即从浏览器地址清除')
                for width in [1024,390]:
                    page.set_viewport_size({'width':width,'height':950})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    assert page.locator('#modal').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
                assert not errors,errors
                report={'passed':True,'checks':checks,'browser_errors':errors}
                (output/'codex-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(report,ensure_ascii=False,indent=2));browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(3)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('test-results-codex'))
    run(parser.parse_args().output.resolve())
