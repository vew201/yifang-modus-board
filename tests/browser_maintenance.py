"""Browser checks for editing, reassignment, recycle/restore and conversations."""

import argparse
import base64
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
    with tempfile.TemporaryDirectory() as temporary:
        server = WorkbenchServer(("127.0.0.1", 0), temporary)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}"
        client = Client(url)
        _, session, _ = client.request("/api/setup", "POST", {"accounts": [
            {"name": "甲", "username": "a", "password": "abcdefgh"},
            {"name": "乙", "username": "b", "password": "ijklmnop"},
        ], "demo": True})
        client.csrf = session["csrf"]
        _, snapshot, _ = client.request("/api/state")
        first_section, second_section = snapshot["sections"][:2]
        client.request("/api/documents", "POST", {"section_id": first_section["id"], "owner_id": 1, "name": "旧名称.txt", "content": base64.b64encode(b"test-file").decode()})
        checks, errors = [], []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.locator('[name="username"]').fill("a")
                page.locator('[name="password"]').fill("abcdefgh")
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_role("button", name="制作项目需求书", exact=True).first.click()
                page.get_by_role("button", name="编辑事项", exact=True).click()
                page.locator('[name="title"]').fill("改派后的需求书")
                page.locator('[name="owner_id"]').select_option("2")
                page.get_by_role("button", name="保存修改", exact=True).click()
                expect(page.locator(".home-grid .panel").first).not_to_contain_text("改派后的需求书")
                expect(page.locator(".home-grid .panel").nth(1)).to_contain_text("改派后的需求书")
                checks.append("事项编辑、改派后待办和个人时间轴同步")

                page.get_by_role("button", name="新增事项", exact=True).click()
                page.locator('[name="title"]').fill("尚未安排日期")
                page.locator('[name="schedule_pending"]').check()
                page.get_by_role("button", name="添加事项", exact=True).click()
                page.get_by_role("button", name="尚未安排日期", exact=True).click()
                expect(page.get_by_role("dialog")).to_contain_text("日期待定")
                page.get_by_role("button", name="编辑事项", exact=True).click()
                page.locator('[name="schedule_pending"]').uncheck()
                page.locator('[name="start_date"]').fill(snapshot["today"])
                page.locator('[name="end_date"]').fill(snapshot["today"])
                page.get_by_role("button", name="保存修改", exact=True).click()
                expect(page.locator('.timeline-row-label', has_text="尚未安排日期")).to_be_visible()
                page.get_by_role("button", name="尚未安排日期", exact=True).first.click()
                page.get_by_role("button", name="移入回收站", exact=True).click()
                page.get_by_role("dialog").get_by_role("button", name="移入回收站", exact=True).click()
                expect(page.get_by_role("button", name="尚未安排日期", exact=True)).to_have_count(0)
                page.get_by_role("button", name="回收站", exact=True).click()
                row = page.locator(".trash-row").filter(has_text="尚未安排日期")
                row.get_by_role("button", name="恢复", exact=True).click()
                expect(page.get_by_role("button", name="尚未安排日期", exact=True).first).to_be_visible()
                checks.append("日期待定、补排日期、回收与恢复")

                page.get_by_role("button", name="项目详情", exact=True).click()
                page.get_by_role("button", name="编辑项目", exact=True).click()
                page.locator('[name="name"]').fill("项目已编辑")
                page.locator('[name="description"]').fill("项目的新说明")
                page.get_by_role("button", name="保存修改", exact=True).click()
                expect(page.get_by_role("heading", name="项目已编辑", exact=True)).to_be_visible()
                page.locator(".project-overview").click()
                page.locator(".phase-card").nth(2).get_by_role("button", name="查看与更新阶段", exact=True).click()
                page.locator('[name="work_summary"]').fill("需求范围已逐项核对")
                page.get_by_role("button", name="保存阶段记录", exact=True).click()
                expect(page.locator(".phase-card").nth(2)).to_contain_text("需求范围已逐项核对")
                page.locator(".file-section").first.get_by_role("button", name="编辑", exact=True).click()
                page.locator('[name="name"]').fill("需求与范围")
                page.get_by_role("button", name="保存修改", exact=True).click()
                expect(page.locator(".file-section").first).to_contain_text("需求与范围")
                page.get_by_role("button", name="编辑文件", exact=True).click()
                page.locator('[name="name"]').fill("新名称.txt")
                page.locator('[name="section_id"]').select_option(str(second_section["id"]))
                page.get_by_role("button", name="保存修改", exact=True).click()
                expect(page.locator(".file-section").nth(1)).to_contain_text("新名称.txt")
                checks.append("项目、阶段、区域编辑及文件重命名和移动")

                page.get_by_role("button", name="追加沟通", exact=True).click()
                page.locator('[name="summary"]').fill("确认本期交付范围")
                page.locator('[name="text"]').fill("讨论原文与已经确认的范围。")
                page.get_by_role("button", name="保存沟通", exact=True).click()
                expect(page.locator(".conversation-item")).to_contain_text("确认本期交付范围")
                page.get_by_role("button", name="沟通意向", exact=True).click()
                page.locator(".opportunity-title").click()
                page.get_by_role("button", name="编辑合作意向", exact=True).click()
                page.locator('[name="next_step"]').fill("下一次确认预算")
                page.get_by_role("button", name="保存修改", exact=True).click()
                page.locator(".opportunity-title").click()
                expect(page.get_by_role("dialog")).to_contain_text("下一次确认预算")
                checks.append("追加沟通及意向信息维护")
                page.get_by_role("button", name="关闭", exact=True).click()
                page.screenshot(path=str(output / "maintenance.png"), full_page=True)
                assert not errors, errors
                report = {"passed": True, "checks": checks, "browser_errors": errors}
                (output / "maintenance-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False, indent=2))
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("test-results-maintenance"))
    run(parser.parse_args().output.resolve())
