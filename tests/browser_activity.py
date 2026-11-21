"""Card aging and project-independent reminders, using isolated records and clock fixtures."""
import argparse
from datetime import datetime,timedelta
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from server import WorkbenchServer,connect_database,LOCAL_TZ
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
        for index,days in enumerate([0,3,6]):
            status,item,_=client.request('/api/opportunities','POST',{'name':['新发现合作','三天未更新','六天未更新'][index],'owner_id':1,'description':'我的总结和客户诉求','channel':'行业帖子','company':'意向客户','post_text':'客户发布的需求信息'})
            assert status==200
            with connect_database(server.database) as c:
                c.execute('UPDATE opportunities SET last_activity_at=? WHERE id=?',((datetime.now(LOCAL_TZ)-timedelta(days=days,minutes=1)).isoformat(timespec='seconds'),item['id']))
        _,state,_=client.request('/api/state')
        client.request('/api/tasks','POST',{'title':'合伙人的日常提醒','owner_id':2,'start_date':state['today'],'end_date':state['today']})
        errors,checks=[],[]
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(url)
                page.locator('[name="username"]').fill('a');page.locator('[name="password"]').fill('abcdefgh')
                page.get_by_role('button',name='登录',exact=True).click()
                page.get_by_role('button',name='沟通意向',exact=True).click()
                expected={'white':('rgb(255, 255, 255)','rgb(23, 23, 23)'),'gray':('rgb(215, 215, 215)','rgb(23, 23, 23)'),'black':('rgb(23, 23, 23)','rgb(255, 255, 255)')}
                for band,(background,color) in expected.items():
                    card=page.locator(f'.opportunity-card.age-{band}')
                    expect(card).to_have_count(1)
                    assert card.evaluate('(element)=>getComputedStyle(element).backgroundColor')==background
                    assert card.locator('.opportunity-title').evaluate('(element)=>getComputedStyle(element).color')==color
                page.screenshot(path=str(output/'white-gray-black.png'),full_page=True)
                page.reload()
                expect(page.locator('.age-black')).to_have_count(1)
                page.locator('.age-black .opportunity-title').click()
                page.get_by_role('button',name='关闭',exact=True).click()
                expect(page.locator('.age-black')).to_have_count(1)
                checks.append('白灰黑三档及文字对比度，刷新和打开查看不重置时间')
                page.locator('.age-black .opportunity-title').click()
                page.get_by_role('button',name='编辑合作意向',exact=True).click()
                page.locator('[name="description"]').fill('客户需求有了新进展')
                page.get_by_role('button',name='保存修改',exact=True).click()
                expect(page.locator('.age-black')).to_have_count(0)
                changed=page.locator('.opportunity-card').filter(has_text='六天未更新')
                expect(changed).to_have_attribute('data-age-band','white')
                expect(changed).to_contain_text('距上次更新 0 天')
                checks.append('有效修改后从黑色立即恢复白色并重新计时')
                page.get_by_role('button',name='项目外待办事项',exact=True).click()
                expect(page.locator('.navigation .nav-item')).to_have_count(7)
                page.get_by_role('button',name='新增事项',exact=True).click()
                page.locator('[name="title"]').fill('明天采购办公用品')
                page.locator('[name="description"]').fill('提醒详情：核对清单，并带上需要报销的资料')
                page.get_by_role('button',name='添加事项',exact=True).click()
                card=page.locator('.extra-task-card').filter(has_text='明天采购办公用品')
                card.hover()
                expect(page.locator('.task-tooltip')).to_contain_text('核对清单')
                page.mouse.move(0,0)
                card.locator('.extra-task-title').click()
                expect(page.get_by_role('dialog')).to_contain_text('提醒详情：核对清单')
                page.get_by_role('button',name='关闭',exact=True).click()
                expect(page.get_by_role('checkbox',name='完成 合伙人的日常提醒',exact=True)).to_be_disabled()
                page.get_by_role('button',name='工作首页',exact=True).click()
                expect(page.locator('.home-grid .panel').first).to_contain_text('明天采购办公用品')
                expect(page.locator('.timeline-row-label',has_text='明天采购办公用品')).to_have_count(1)
                page.get_by_role('button',name='项目外待办事项',exact=True).click()
                page.screenshot(path=str(output/'extra-tasks.png'),full_page=True)
                card.get_by_role('checkbox').check()
                expect(card).to_have_count(0)
                page.get_by_role('checkbox',name='显示已完成',exact=True).check()
                expect(card).to_have_count(1)
                page.get_by_role('button',name='工作归档',exact=True).click()
                expect(page.locator('.archive-grid .panel').first).to_contain_text('明天采购办公用品')
                checks.append('项目外待办小方块、悬停和点击详情、负责人权限、首页时间轴与完成归档')
                for width in [1440,1024,390]:
                    page.set_viewport_size({'width':width,'height':950})
                    for title in ['项目外待办事项','沟通意向']:
                        page.get_by_role('button',name=title,exact=True).click()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(width,title)
                    if width==390:page.screenshot(path=str(output/'mobile-aging.png'),full_page=True)
                checks.append('新增页面及三档卡片在1440/1024/390宽度下无整体横向溢出')
                assert not errors,errors
                report={'passed':True,'checks':checks,'browser_errors':errors}
                (output/'activity-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(report,ensure_ascii=False,indent=2))
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(3)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('test-results-activity'))
    run(parser.parse_args().output.resolve())
