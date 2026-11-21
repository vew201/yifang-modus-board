"""Project directories and automatic lifecycle end-to-end checks."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import WorkbenchServer
from test_backend import Client


def run(output):
    from playwright.sync_api import sync_playwright, expect

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        server = WorkbenchServer(("127.0.0.1", 0), directory)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        client = Client(url)
        _, session, _ = client.request("/api/setup", "POST", {"accounts": [
            {"name": "甲", "username": "a", "password": "abcdefgh"},
            {"name": "乙", "username": "b", "password": "ijklmnop"},
        ]})
        client.csrf = session["csrf"]
        _, project, _ = client.request("/api/projects", "POST", {"name": "固定阶段与独立进度验收", "owner_id": 1, "start_date": "2026-10-01", "end_date": "2027-05-01"})
        _, snapshot, _ = client.request("/api/state")
        stages = snapshot["stages"]
        stage = stages[2]
        for title in ['整理问题清单','等待客户确认范围']:
            client.request("/api/tasks", "POST", {"title":title,"owner_id":1,"project_id":project['id'],"stage_id":stage['id'],"start_date":"","end_date":""})
        errors, checks = [], []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.locator('[name="username"]').fill("a")
                page.locator('[name="password"]').fill("abcdefgh")
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_role("button", name="项目详情", exact=True).click()
                page.locator(".project-overview").click()
                expect(page.locator(".phase-card")).to_have_count(8)
                expect(page.get_by_role("heading",name="项目主目录与子任务",exact=True)).to_have_count(0)
                phase=page.locator(f'.phase-card[data-stage-id="{stage["id"]}"]')
                expect(phase.locator('.phase-status')).to_have_text('未开始')
                phase.get_by_role('button',name='查看与更新阶段',exact=True).click()
                page.locator('[name="status"]').select_option('completed')
                page.locator('[name="work_summary"]').fill('已与客户核对需求范围和验收标准')
                page.locator('[name="completion_note"]').fill('客户已确认需求边界，实际需求澄清完成')
                page.get_by_role('button',name='保存阶段记录',exact=True).click()
                expect(phase.locator('.phase-status')).to_have_text('已完成')
                expect(phase).to_contain_text('客户已确认需求边界')
                checks.append('固定八阶段、移除主目录子任务区、按实际成果独立确认阶段完成')
                page.screenshot(path=str(output/'eight-stages.png'),full_page=True)
                page.get_by_role('button',name='工作首页',exact=True).click()
                page.get_by_role('button',name='等待客户确认范围',exact=True).click()
                page.get_by_role('button',name='标记完成',exact=True).click()
                page.get_by_role('button',name='工作归档',exact=True).click()
                page.get_by_role('button',name='等待客户确认范围',exact=True).click()
                page.get_by_role('button',name='恢复为待办',exact=True).click()
                page.get_by_role('button',name='项目详情',exact=True).click()
                expect(phase.locator('.phase-status')).to_have_text('已完成')
                expect(page.locator('.project-card')).to_have_count(1)
                checks.append('待办完成和撤回均不改变阶段或项目状态')
                page.get_by_role('button',name='确认开发完成',exact=True).click()
                page.locator('[name="completion_note"]').fill('开发成果已实际确认完成，剩余提醒另行跟进')
                page.get_by_role('dialog').get_by_role('button',name='确认开发完成',exact=True).click()
                expect(page.locator('.project-card')).to_have_count(0)
                page.get_by_role('button',name='工作归档',exact=True).click()
                expect(page.locator('.project-archive-panel')).to_contain_text('固定阶段与独立进度验收')
                page.get_by_role('button',name='查看项目',exact=True).click()
                expect(page.locator('.project-outcome')).to_contain_text('开发已全部完成')
                page.get_by_role('button',name='恢复开发',exact=True).click()
                page.get_by_role('button',name='确认恢复开发',exact=True).click()
                page.locator('#project-filter').select_option('active')
                expect(phase.locator('.phase-status')).to_have_text('已完成')
                checks.append('项目开发完成独立确认、归档及恢复开发不联动阶段')
                page.get_by_role('button',name='上传到 售前需求',exact=True).click()
                page.locator('[name="file"]').set_input_files({'name':'最终需求书.txt','mimeType':'text/plain','buffer':'需求范围原件'.encode()})
                page.get_by_role('button',name='上传并保存',exact=True).click()
                phase.get_by_text('阶段资料 · 1',exact=True).click()
                expect(phase.get_by_role('link',name='最终需求书.txt',exact=True)).to_have_count(1)
                phase.get_by_role('button',name='查看与更新阶段',exact=True).click()
                page.locator('[name="work_summary"]').fill('需求书原件已归入需求澄清')
                page.get_by_role('button',name='保存阶段记录',exact=True).click()
                expect(phase).to_contain_text('需求书原件已归入需求澄清')
                page.screenshot(path=str(output/'phase-files.png'),full_page=True)
                checks.append('阶段内容与文件归类、进展留痕和实际状态持久保存')
                assert not errors, errors
                report = {"passed": True, "checks": checks, "browser_errors": errors}
                (output / "hierarchy-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False, indent=2))
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("test-results-hierarchy"))
    run(parser.parse_args().output.resolve())
