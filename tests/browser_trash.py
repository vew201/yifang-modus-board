"""Real browser checks for recovery, scope review and irreversible confirmation."""
import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from server import WorkbenchServer,connect_database
from test_backend import Client


def run(output):
    from playwright.sync_api import sync_playwright,expect
    output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[]
    with tempfile.TemporaryDirectory() as temporary:
        server=WorkbenchServer(('127.0.0.1',0),temporary)
        server.trash_next_sweep=float('inf')
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        url=f'http://127.0.0.1:{server.server_port}'
        client=Client(url)
        _,session,_=client.request('/api/setup','POST',{'accounts':[{'name':'甲','username':'a','password':'abcdefgh'},{'name':'乙','username':'b','password':'ijklmnop'}]})
        client.csrf=session['csrf']
        _,project,_=client.request('/api/projects','POST',{'name':'待清理项目','owner_id':1,'start_date':'','end_date':''})
        _,task,_=client.request('/api/tasks','POST',{'title':'关联工作','project_id':project['id'],'owner_id':1,'start_date':'','end_date':''})
        with connect_database(server.database) as db:section=db.execute('SELECT id FROM sections WHERE project_id=? LIMIT 1',(project['id'],)).fetchone()[0]
        client.request('/api/documents','POST',{'name':'原件.txt','owner_id':1,'section_id':section,'content':base64.b64encode(b'browser-fixture').decode()})
        client.request(f"/api/projects/{project['id']}",'DELETE',{})
        _,recover,_=client.request('/api/tasks','POST',{'title':'可以恢复','owner_id':1,'start_date':'','end_date':''})
        client.request(f"/api/tasks/{recover['id']}",'DELETE',{})
        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url);page.locator('[name="username"]').fill('a');page.locator('[name="password"]').fill('abcdefgh');page.get_by_role('button',name='登录',exact=True).click()
                page.get_by_role('button',name='回收站',exact=True).click()
                dialog=page.get_by_role('dialog')
                expect(dialog).to_contain_text('保留 30 天');expect(dialog).to_contain_text('到期：')
                row=dialog.locator('.trash-row').filter(has_text='可以恢复')
                row.get_by_role('button',name='恢复',exact=True).click()
                expect(page.get_by_role('button',name='可以恢复',exact=True).first).to_be_visible()
                checks.append('显示30天保留及到期时间，恢复后返回首页')

                page.get_by_role('button',name='回收站',exact=True).click()
                dialog.locator('.trash-row').filter(has_text='待清理项目').get_by_role('button',name='彻底删除',exact=True).click()
                expect(dialog).to_contain_text('事项 1 条');expect(dialog).to_contain_text('文件 1 条');expect(dialog).to_contain_text('阶段 8 条')
                page.screenshot(path=str(output/'trash-confirmation.png'))
                page.get_by_role('button',name='取消',exact=True).click()
                with connect_database(server.database) as db:assert db.execute('SELECT 1 FROM projects WHERE id=?',(project['id'],)).fetchone()
                checks.append('删除项目预览全部关联范围，取消不删除')

                page.get_by_role('button',name='回收站',exact=True).click()
                dialog.locator('.trash-row').filter(has_text='待清理项目').get_by_role('button',name='彻底删除',exact=True).click()
                page.locator('[name="confirm_text"]').fill('确认')
                page.get_by_role('button',name='确认彻底删除',exact=True).click()
                expect(dialog.locator('.form-error')).to_contain_text('请输入“永久删除”')
                page.locator('[name="confirm_text"]').fill('永久删除')
                page.get_by_role('button',name='确认彻底删除',exact=True).click()
                expect(dialog).not_to_be_visible()
                with connect_database(server.database) as db:
                    assert not db.execute('SELECT 1 FROM projects WHERE id=?',(project['id'],)).fetchone()
                    assert not db.execute('SELECT 1 FROM documents').fetchone()
                assert not list(server.uploads.iterdir())
                checks.append('输入确认短语后永久删除，关联数据及磁盘附件同步清理')

                for name in ['清空项目外事项甲','清空项目外事项乙']:
                    _,item,_=client.request('/api/tasks','POST',{'title':name,'owner_id':1,'start_date':'','end_date':''})
                    client.request(f"/api/tasks/{item['id']}",'DELETE',{})
                page.get_by_role('button',name='回收站',exact=True).click()
                page.get_by_role('button',name='清空回收站',exact=True).click()
                expect(dialog).to_contain_text('清空项目外事项甲');expect(dialog).to_contain_text('清空项目外事项乙')
                page.locator('[name="confirm_text"]').fill('永久删除')
                page.get_by_role('button',name='确认清空回收站',exact=True).click()
                expect(dialog).not_to_be_visible()
                page.get_by_role('button',name='回收站',exact=True).click()
                expect(dialog).to_contain_text('回收站为空');expect(page.get_by_role('button',name='清空回收站',exact=True)).to_be_disabled()
                expect(page.get_by_role('button',name='可以恢复',exact=True).first).to_be_visible()
                checks.append('清空全部回收记录，保留正常事项，空回收站禁止重复清空')

                page.set_viewport_size({'width':390,'height':844})
                expect(dialog).to_be_visible()
                overflow=page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
                assert not overflow
                page.screenshot(path=str(output/'trash-mobile.png'))
                checks.append('手机宽度下回收站可读，页面无横向溢出')
                assert not errors,errors
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(3)
    report={'passed':True,'checks':checks,'browser_errors':errors}
    (output/'trash-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);run(parser.parse_args().output)
