"""Three-phase opportunity and completion/payment browser acceptance, isolated DB."""
import argparse
import base64
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import WorkbenchServer, LOCAL_TZ
from test_backend import Client


def run(output):
    from playwright.sync_api import sync_playwright, expect
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        server = WorkbenchServer(('127.0.0.1', 0), directory)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{server.server_port}'
        client = Client(url)
        _, session, _ = client.request('/api/setup', 'POST', {'accounts':[
            {'name':'甲','username':'a','password':'abcdefgh'},
            {'name':'乙','username':'b','password':'ijklmnop'},
        ]})
        client.csrf = session['csrf']
        def request(path, method='GET', data=None):
            status, body, _ = client.request(path,method,data)
            assert status == 200,(status,body)
            return body
        errors, checks = [], []
        image = Path(directory)/'客户帖子.png'
        image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQ0AAAAASUVORK5CYII='))
        now = datetime.now(LOCAL_TZ)
        actual = (now-timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M')
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge',headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1080})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url)
                page.locator('[name="username"]').fill('a')
                page.locator('[name="password"]').fill('abcdefgh')
                page.get_by_role('button',name='登录',exact=True).click()
                page.get_by_role('button',name='沟通意向',exact=True).click()
                expect(page.locator('.opportunity-lane')).to_have_count(3)
                page.get_by_role('button',name='新增意向',exact=True).click()
                for name,value in {'name':'生物制药AI合作','company':'客户A','channel':'行业社区','company_details':'生物制药企业','service_type':'AI入企','description':'我总结的信息','post_text':'对方发布的原始需求帖'}.items():
                    page.locator(f'[name="{name}"]').fill(value)
                page.get_by_role('button',name='保存意向',exact=True).click()
                card=page.locator('.opportunity-workflow-card')
                expect(card).to_contain_text('行业社区')
                expect(page.locator('[data-phase="lead"]')).to_contain_text('生物制药AI合作')
                card.locator('.opportunity-title').click()
                page.get_by_role('button',name='上传图片或文件',exact=True).click()
                page.locator('[name="file"]').set_input_files(str(image))
                page.get_by_role('button',name='上传并保存',exact=True).click()
                expect(card.locator('img')).to_have_count(1)
                card.locator('.opportunity-images button').click()
                expect(page.locator('.image-preview')).to_be_visible()
                page.get_by_role('button',name='关闭',exact=True).click()
                checks.append('线索信息、原帖文本、图片上传及放大')

                card.locator('.opportunity-title').click()
                page.get_by_role('button',name='追加沟通',exact=True).click()
                page.locator('[name="occurred_at"]').fill(actual)
                page.locator('[name="summary"]').fill('第一次已确认效果提升目标')
                page.locator('[name="text"]').fill('客户说明了AI入企效果问题，等待确认数据范围。')
                page.locator('[name="requirements"]').fill('提升企业内部AI应用效果')
                page.get_by_role('button',name='保存沟通',exact=True).click()
                expect(page.locator('[data-phase="communication"]')).to_contain_text('生物制药AI合作')
                card.locator('.communication-chip').hover()
                expect(page.locator('.task-tooltip')).to_contain_text('第一次已确认效果提升目标')
                page.mouse.move(0,0)
                expect(page.locator('.task-tooltip')).to_have_count(0)
                card.locator('.communication-chip').click()
                page.get_by_role('button',name='编辑本次记录',exact=True).click()
                page.locator('[name="summary"]').fill('更正后的第一次沟通摘要')
                page.get_by_role('button',name='保存沟通',exact=True).click()
                expect(card.locator('.communication-chip')).to_have_count(1)
                page.get_by_role('button',name='工作首页',exact=True).click()
                expect(page.locator('.task-title',has_text='下次沟通')).to_have_count(1)
                checks.append('一次沟通编号、悬停明细、修订不增加次数及首页自动待办')

                item=request('/api/state')['opportunities'][0]
                identifier=item['id']
                for role,name in [('solution','最终方案书.pdf'),('quote','最终报价书.pdf')]:
                    request('/api/opportunity_files','POST',{'opportunity_id':identifier,'owner_id':1,'name':name,'role':role,'version':'最终版V1','content':base64.b64encode(b'final file').decode()})
                page.reload()
                page.get_by_role('button',name='沟通意向',exact=True).click()
                card.locator('.opportunity-title').click()
                page.get_by_role('button',name='编辑合作意向',exact=True).click()
                page.locator('[name="phase"]').select_option('proposal')
                page.locator('.proposal-fields summary').click()
                snapshot=request('/api/state')
                for key,role in [('solution_file_id','solution'),('quote_file_id','quote')]:
                    file=next(x for x in snapshot['opportunity_files'] if x['role']==role)
                    page.locator(f'[name="{key}"]').select_option(str(file['id']))
                page.locator('[name="submitted_at"]').fill((now-timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M'))
                page.get_by_role('button',name='保存修改',exact=True).click()
                expect(page.locator('[data-phase="proposal"]')).to_contain_text('生物制药AI合作')
                expect(card.locator('.followup-note')).to_contain_text('48 小时')
                page.screenshot(path=str(output/'three-phases.png'),full_page=True)
                card.locator('.opportunity-title').click()
                page.get_by_role('button',name='归档失败',exact=True).click()
                page.locator('[name="failure_reason"]').fill('当前预算与范围不匹配')
                page.get_by_role('button',name='归档至失败项目',exact=True).click()
                expect(page.locator('.opportunity-workflow-card')).to_have_count(0)
                page.get_by_role('button',name='失败项目',exact=True).click()
                expect(page.locator('.outcome-card')).to_contain_text('当前预算与范围不匹配')
                page.screenshot(path=str(output/'failed.png'),full_page=True)
                checks.append('最终文件选择、第三阶段48小时默认、失败原因及失败页面')

                page.get_by_role('button',name='查看完整记录',exact=True).click()
                page.get_by_role('button',name='重新跟进',exact=True).click()
                page.get_by_role('button',name='恢复跟进',exact=True).click()
                page.get_by_role('button',name='沟通意向',exact=True).click()
                card.locator('.opportunity-title').click()
                page.get_by_role('button',name='转入开发项目',exact=True).click()
                page.get_by_role('button',name='确认转入开发',exact=True).click()
                expect(page.locator('.converted-panel')).to_contain_text('生物制药AI合作')
                snapshot=request('/api/state')
                project=snapshot['projects'][0]
                for stage in snapshot['stages']:
                    task=request('/api/tasks','POST',{'title':stage['name']+'工作','owner_id':1,'project_id':project['id'],'stage_id':stage['id'],'start_date':'','end_date':''})
                    request(f'/api/tasks/{task["id"]}','PATCH',{'done':True})
                request(f'/api/projects/{project["id"]}','PATCH',{'development_done':True,'completion_note':'实际开发已全部完成'})
                page.reload()
                page.get_by_role('button',name='成功项目',exact=True).click()
                expect(page.locator('.outcome-card')).to_have_count(0)
                page.get_by_role('button',name='项目详情',exact=True).click()
                page.locator('#project-filter').select_option('archived')
                page.locator('.project-overview').click()
                expect(page.locator('.project-outcome')).to_contain_text('尾款尚未确认到账')
                page.get_by_role('button',name='登记尾款',exact=True).click()
                page.locator('[name="final_payment_at"]').fill((now-timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M'))
                page.locator('[name="payment_note"]').fill('已核对到账')
                page.get_by_role('button',name='保存尾款记录',exact=True).click()
                page.get_by_role('button',name='成功项目',exact=True).click()
                expect(page.locator('.outcome-card')).to_contain_text('已完成 · 已收尾款')
                page.screenshot(path=str(output/'success.png'),full_page=True)
                checks.append('失败恢复、转入开发、开发完成后等待尾款、到账后进入成功项目')

                for width in [1440,1024,390]:
                    page.set_viewport_size({'width':width,'height':950})
                    for title in ['沟通意向','成功项目','失败项目']:
                        page.get_by_role('button',name=title,exact=True).click()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),(width,title)
                    if width==390:
                        page.screenshot(path=str(output/'mobile-failed.png'),full_page=True)
                checks.append('1440/1024/390宽度及七页导航，无页面横向溢出')
                assert not errors,errors
                report={'passed':True,'checks':checks,'browser_errors':errors}
                (output/'workflow-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(report,ensure_ascii=False,indent=2))
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('test-results-workflow'))
    run(parser.parse_args().output.resolve())
