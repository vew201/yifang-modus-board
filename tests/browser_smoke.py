"""Optional browser acceptance test. Install playwright and use installed Edge.

Uses an isolated temporary database. Does not change the user's workbench.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import WorkbenchServer


def run(output):
    from playwright.sync_api import sync_playwright, expect

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        server = WorkbenchServer(("127.0.0.1", 0), temporary)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        checks = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1080}, device_scale_factor=1)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.get_by_role("heading", name="设置两人账号")).to_be_visible()
                page.screenshot(path=str(output / "setup.png"), full_page=True)
                accounts = [("体验账号", "demo-owner", "Local-demo-2026"), ("协作伙伴", "demo-partner", "Partner-demo-2026")]
                for index, (name, username, password) in enumerate(accounts):
                    page.locator(f'[name="name{index}"]').fill(name)
                    page.locator(f'[name="username{index}"]').fill(username)
                    page.locator(f'[name="password{index}"]').fill(password)
                page.get_by_role("button", name="创建工作台").click()
                expect(page.get_by_role("heading", name="工作首页", exact=True)).to_be_visible(timeout=15000)
                page.evaluate("document.fonts.ready")
                expect(page.locator(".timeline-day")).to_have_count(5)
                expect(page.locator(".timeline-day.today .day-label")).to_have_text("今天")
                block = page.locator('.timeline-track .time-block', has_text="制作项目需求书")
                assert "width:60%" in block.get_attribute("style")
                block.hover()
                expect(page.locator(".task-tooltip")).to_contain_text("3 天")
                block.click()
                expect(page.get_by_role("dialog")).to_contain_text("制作项目需求书")
                page.get_by_role("button", name="关闭", exact=True).click()
                page.mouse.move(0, 0)
                page.screenshot(path=str(output / "home.png"), full_page=True)
                page.get_by_role("button", name="外网地址", exact=True).click()
                expect(page.get_by_role("dialog")).to_contain_text("尚未启动公网共享")
                page.get_by_role("button", name="关闭", exact=True).click()
                checks.append("双人账号设置、首页五天窗口及三天连续长条")

                page.get_by_role("button", name="新增事项", exact=True).click()
                page.locator('[name="title"]').fill("浏览器自动化验收事项")
                page.get_by_role("button", name="添加事项", exact=True).click()
                checkbox = page.get_by_role("checkbox", name="完成 浏览器自动化验收事项", exact=True)
                expect(checkbox).to_be_visible()
                checkbox.check()
                page.get_by_role("button", name="工作归档", exact=True).click()
                expect(page.locator(".archive-grid .panel").first).to_contain_text("浏览器自动化验收事项")
                page.screenshot(path=str(output / "archive.png"), full_page=True)
                checks.append("新建事项、勾选完成及自动归档")

                page.get_by_role("button", name="项目详情", exact=True).click()
                page.locator(".project-overview").first.click()
                expect(page.get_by_role("heading", name="项目时间表与相关待办", exact=True)).to_be_visible()
                expect(page.locator(".phase-card")).to_have_count(8)
                page.get_by_role("button", name="添加区域", exact=True).click()
                page.locator('[name="name"]').fill("自动化测试资料")
                page.get_by_role("dialog").get_by_role("button", name="添加区域", exact=True).click()
                expect(page.locator(".file-section")).to_have_count(5)
                page.get_by_role("button", name="上传到 自动化测试资料", exact=True).click()
                page.locator('[name="file"]').set_input_files({"name": "测试需求书.txt", "mimeType": "text/plain", "buffer": "文件内容完整性检查\nMODUS".encode()})
                page.get_by_role("button", name="上传并保存", exact=True).click()
                download_link = page.locator(".file-grid").get_by_role("link", name="测试需求书.txt", exact=False)
                expect(download_link).to_be_visible()
                with page.expect_download() as event:
                    download_link.click()
                downloaded = Path(event.value.path()).read_bytes()
                assert downloaded == "文件内容完整性检查\nMODUS".encode()
                page.screenshot(path=str(output / "projects.png"), full_page=True)
                checks.append("项目完整周期、阶段展开、动态区域及文件上传下载")

                page.get_by_role("button", name="新建项目", exact=True).click()
                page.locator('[name="name"]').fill("浏览器新建项目")
                page.get_by_role("button", name="创建项目", exact=True).click()
                new_project = page.locator(".project-card").filter(has=page.get_by_role("heading", name="浏览器新建项目", exact=True))
                expect(new_project).to_be_visible()
                expect(new_project.locator(".phase-card")).to_have_count(8)
                expect(new_project.locator(".phase-card").nth(2)).to_contain_text("需求澄清")
                new_project.get_by_role("button", name="移除空区域 售后与培训", exact=True).click()
                page.get_by_role("dialog").get_by_role("button", name="移除区域", exact=True).click()
                expect(new_project.locator(".file-section")).to_have_count(3)
                page.reload()
                expect(page.get_by_role("heading", name="项目详情", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="浏览器新建项目", exact=True)).to_be_visible()
                checks.append("界面新建项目、固定八阶段、减少资料区域及刷新持久保存")

                page.get_by_role("button", name="沟通意向", exact=True).click()
                page.locator(".opportunity-card").first.hover()
                expect(page.locator(".task-tooltip")).to_contain_text("下一步")
                page.mouse.move(0, 0)
                expect(page.locator(".task-tooltip")).to_have_count(0)
                page.get_by_role("button", name="新增意向", exact=True).click()
                page.locator('[name="name"]').fill("<img src=x onerror=alert(1)>测试意向")
                page.locator('[name="description"]').fill("实际沟通信息")
                page.get_by_role("button", name="保存意向", exact=True).click()
                expect(page.locator(".opportunity-card")).to_have_count(2)
                assert page.locator('.opportunity-card img').count() == 0
                page.screenshot(path=str(output / "opportunities.png"), full_page=True)
                checks.append("意向新增、悬停显示与移开隐藏、输入内容转义")

                page.get_by_role("button", name="退出登录", exact=True).click()
                expect(page.get_by_role("heading", name="进入工作台")).to_be_visible()
                page.screenshot(path=str(output / "login.png"), full_page=True)
                page.locator('[name="username"]').fill("demo-partner")
                page.locator('[name="password"]').fill("Partner-demo-2026")
                page.get_by_role("button", name="登录", exact=True).click()
                expect(page.get_by_role("heading", name="工作首页", exact=True)).to_be_visible(timeout=15000)
                expect(page.locator(".home-grid .panel").first).to_contain_text("整理技术方案")
                expect(page.locator(".home-grid .panel").nth(1)).to_contain_text("制作项目需求书")
                assert page.locator('.timeline-row-label', has_text="制作项目需求书").count() == 0
                checks.append("退出、另一账号登录及个人时间轴隔离")

                for width in (1440, 1024, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    for label in ("工作首页", "项目详情", "沟通意向", "工作归档", "成功项目", "失败项目", "项目外待办事项"):
                        page.get_by_role("button", name=label, exact=True).click()
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"Horizontal overflow: {label} {width}"
                page.screenshot(path=str(output / "mobile.png"), full_page=True)
                checks.append("1440、1024 与 390 像素宽度下七页布局无整体横向溢出")
                assert not errors, errors
                report = {"passed": True, "checks": checks, "browser_errors": errors}
                (output / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False, indent=2))
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("test-results"))
    arguments = parser.parse_args()
    run(arguments.output.resolve())
