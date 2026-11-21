"""Local workbench and optional Cloudflare Quick Tunnel supervisor.

Run by the user through 启动公网共享.cmd. Uses only the standard library.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler, Request
import webbrowser


ROOT = Path(__file__).resolve().parent
PUBLIC_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b", re.IGNORECASE)
EXPECTED_BINARY_HASH = "341deda3db5ec498a0482254d6c38c0b7cf01a30c37bb5dd95d2216d08955e35"


def read_network_status(data_dir):
    try:
        result = json.loads((Path(data_dir) / "network.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        if time.time() - float(result.get("updated_epoch", 0)) > 120:
            return {"status": "stopped", "url": "", "message": "共享进程未运行，请双击启动公网共享。"}
        url = result.get("url", "")
        if url and (not isinstance(url, str) or not PUBLIC_URL.fullmatch(url)):
            raise ValueError
        return {key: result.get(key, "") for key in ("status", "url", "message", "updated_at")}
    except (OSError, ValueError, TypeError):
        return {"status": "stopped", "url": "", "message": "尚未启动公网共享。"}


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def probe(url, local=False):
    opener = build_opener(ProxyHandler({})) if local else build_opener()
    try:
        request = Request(url.rstrip("/") + "/api/health", headers={"Cache-Control": "no-cache"})
        with opener.open(request, timeout=5) as response:
            result = json.load(response)
        return result.get("app") == "modus-workbench"
    except (OSError, ValueError, AttributeError, URLError):
        return False


class Supervisor:
    def __init__(self, port=8765, data_dir=None):
        self.port = port
        self.data_dir = Path(data_dir or ROOT / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.stop_file = self.data_dir / "network.stop"
        self.local_url = f"http://127.0.0.1:{port}"
        self.public_url = ""
        self.status = "starting"
        self.message = "正在准备本机服务。"
        self.events = queue.Queue()
        self.child = None
        self.logger = logging.getLogger("modus-network")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = RotatingFileHandler(self.data_dir / "network.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.logger.addHandler(handler)

    def publish(self):
        now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        result = {"status": self.status, "url": self.public_url, "message": self.message,
                  "updated_at": now, "updated_epoch": time.time(), "pid": os.getpid()}
        atomic_text(self.data_dir / "network.json", json.dumps(result, ensure_ascii=False, indent=2))
        address = self.public_url if self.public_url else "地址尚未生成"
        atomic_text(ROOT / "当前外网地址.txt", f"易方 MODUS 看板外网访问\n\n{address}\n\n{self.message}\n更新时间 {now}\n\n使用自己的看板账号登录。临时网址可能随共享进程重启变化。\n")

    def ensure_backend(self):
        if probe(self.local_url, local=True):
            return True
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with (self.data_dir / "server.log").open("a", encoding="utf-8") as log:
            subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(self.port), "--data-dir", str(self.data_dir)], cwd=ROOT, stdout=log, stderr=log, creationflags=flags)
        for _ in range(12):
            if self.stop_file.exists():
                return False
            time.sleep(0.5)
            if probe(self.local_url, local=True):
                return True
        return False

    def initialized(self):
        try:
            with build_opener(ProxyHandler({})).open(self.local_url + "/api/session", timeout=5) as response:
                return json.load(response).get("needs_setup") is False
        except (OSError, ValueError, AttributeError):
            return False

    def start_tunnel(self):
        binary = ROOT / "tools" / "cloudflared.exe"
        if not binary.is_file():
            raise RuntimeError("tools/cloudflared.exe 不存在，请使用完整程序目录。")
        import hashlib
        digest = hashlib.sha256()
        with binary.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != EXPECTED_BINARY_HASH:
            raise RuntimeError("隧道程序校验失败，请重新获取官方版本。")
        command = [str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2", "--edge-ip-version", "4", "--url", self.local_url]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.child = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace", creationflags=flags)
        child = self.child
        def read_output():
            for line in child.stdout:
                self.logger.info(line.rstrip())
                self.events.put(line)
        threading.Thread(target=read_output, daemon=True).start()
        self.public_url = ""
        self.status = "connecting"
        self.message = "正在建立外网连接，首次启动可能需要稍候。"
        self.publish()

    def consume_events(self):
        while True:
            try:
                line = self.events.get_nowait()
            except queue.Empty:
                return
            match = PUBLIC_URL.search(line)
            if match:
                self.public_url = match.group(0).lower()
                self.status = "checking"
                self.message = "外网地址已生成，正在验证连接。"
                self.publish()

    def run(self):
        # This socket holds a local single-instance lock, not an HTTP listener.
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            lock.bind(("127.0.0.1", self.port + 1))
        except OSError:
            lock.close()
            return 0
        try:
            self.stop_file.unlink(missing_ok=True)
            self.publish()
            if not self.ensure_backend():
                raise RuntimeError("本机服务未能启动，请检查端口或 data/server.log。")
            if not self.initialized():
                raise RuntimeError("请先在本机工作台设置两人账号，再启动公网共享。")
            backoff = 2
            next_heartbeat = next_probe = next_backend_check = 0
            while not self.stop_file.exists():
                if self.child is None or self.child.poll() is not None:
                    if self.child is not None:
                        self.logger.info("隧道进程退出，将在 %s 秒后重试", backoff)
                        self.status, self.message, self.public_url = "reconnecting", "正在重新建立连接，网址可能更新。", ""
                        self.publish()
                        for _ in range(backoff):
                            if self.stop_file.exists():
                                return 0
                            time.sleep(1)
                        backoff = min(backoff * 2, 60)
                    self.start_tunnel()
                self.consume_events()
                now = time.time()
                if self.public_url and now >= next_probe:
                    if probe(self.public_url):
                        self.status, self.message = "running", "外网连接可用，请使用账号登录。"
                        backoff = 2
                        next_probe = now + 30
                    else:
                        self.status, self.message = "checking", "外网连接尚待确认，正在自动重试。"
                        next_probe = now + 10
                if now >= next_backend_check:
                    if not self.ensure_backend():
                        raise RuntimeError("本机服务暂时不可用，请查看 data/server.log。")
                    next_backend_check = now + 30
                if now >= next_heartbeat:
                    self.publish()
                    next_heartbeat = now + 10
                time.sleep(1)
            return 0
        except Exception as error:
            self.status, self.message = "error", str(error)
            self.logger.exception("公网共享停止")
            return 1
        finally:
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
            if self.status != "error":
                self.status, self.message = "stopped", "公网共享已停止，本机工作台可继续使用。"
            self.public_url = ""
            self.publish()
            lock.close()


def startup_file():
    if os.name != "nt":
        raise RuntimeError("登录启动配置适用于 Windows。")
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/MODUS看板共享.vbs"


def install_startup():
    target = startup_file()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("找不到 pythonw.exe，请检查 Python 安装。")
    command = subprocess.list2cmdline([str(pythonw), str(ROOT / "share.py")])
    # VBScript files use UTF-16 so Windows paths and labels remain readable.
    target.write_text('Set shell = CreateObject("WScript.Shell")\n' + 'shell.Run "' + command.replace('"', '""') + '", 0, False\n', encoding="utf-16")
    return target


def main():
    parser = argparse.ArgumentParser(description="MODUS 本机公网共享")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--install-startup", action="store_true")
    parser.add_argument("--remove-startup", action="store_true")
    arguments = parser.parse_args()
    if arguments.install_startup:
        print(f"已设置 Windows 登录后启动共享\n{install_startup()}")
        return 0
    if arguments.remove_startup:
        startup_file().unlink(missing_ok=True)
        print("已关闭本工具的登录自动启动。")
        return 0
    if arguments.status:
        print(json.dumps(read_network_status(arguments.data_dir), ensure_ascii=False, indent=2))
        return 0
    if arguments.stop:
        arguments.data_dir.mkdir(parents=True, exist_ok=True)
        (arguments.data_dir / "network.stop").touch()
        print("已请求停止公网共享。")
        return 0
    if arguments.open:
        webbrowser.open(f"http://127.0.0.1:{arguments.port}/#home")
    return Supervisor(arguments.port, arguments.data_dir).run()


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
