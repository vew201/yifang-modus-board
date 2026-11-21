"""Authenticated MODUS client. Python standard library only."""

import argparse
import base64
from getpass import getpass
from http.cookiejar import CookieJar
import json
import os
import re
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import build_opener, HTTPCookieProcessor, Request


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "connection.json"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def response_json(opener, request):
    try:
        with opener.open(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get("error", "请求失败")
        except (ValueError, AttributeError):
            message = "请求失败"
        raise RuntimeError(f"HTTP {error.code}，{message}") from None


def connect(arguments, config_path):
    parsed = urlparse(arguments.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请填写 http 或 https 后端地址")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程连接请使用 HTTPS 地址")
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    base = arguments.url.rstrip("/")
    password = getpass("看板账号密码（输入不显示） ")
    request = Request(base + "/api/login", data=json.dumps({"username": arguments.username, "password": password}).encode(), headers={"Content-Type": "application/json"})
    session = response_json(opener, request)
    request = Request(base + "/api/agent-token", data=b"{}", headers={"Content-Type": "application/json", "X-CSRF-Token": session["csrf"]})
    token = response_json(opener, request)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"url": base, "token": token["token"]}, indent=2), encoding="utf-8")
    print(json.dumps({"connected": True, "user": session["user"], "config": str(config_path)}, ensure_ascii=False))


def authenticated_request(config, path, method="GET", data=None, retry=False):
    parsed = urlparse(config["url"])
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程连接请使用 HTTPS 地址")
    if not path.startswith("/api/"):
        raise ValueError("接口路径需要以 /api/ 开头")
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    for attempt in range(3 if retry else 1):
        request = Request(config["url"].rstrip("/") + path, data=body, method=method, headers={"Content-Type": "application/json", "Authorization": "Bearer " + config["token"], "X-Modus-Release": os.environ.get("MODUS_RELEASE", "")})
        try:
            return response_json(build_opener(), request)
        except (URLError, TimeoutError, ConnectionError):
            if attempt + 1 >= (3 if retry else 1):
                raise
            time.sleep(attempt + 1)


def prepare_submission(path):
    payload = read_json(path)
    if not isinstance(payload.get("request_id"), str) or not payload["request_id"].strip():
        raise ValueError("请为提交文件设置固定的 request_id，重试时沿用")
    for operation in payload.get("operations", []):
        data = operation.get("data", {})
        if "file_path" in data:
            file_path = Path(data.pop("file_path"))
            if not file_path.is_absolute():
                file_path = Path(path).resolve().parent / file_path
            if not file_path.is_file() or not 0 < file_path.stat().st_size <= 20 * 1024 * 1024:
                raise ValueError("上传文件需存在，且大小为 1 字节至 20 MB")
            data.setdefault("name", file_path.name)
            data["content"] = base64.b64encode(file_path.read_bytes()).decode()
    return payload


def download_file(config, path, destination):
    parsed = urlparse(config["url"])
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程连接请使用 HTTPS 地址")
    if not re.fullmatch(r"/api/(?:documents|opportunity_files)/[0-9]+", path):
        raise ValueError("请选择具体的项目或意向文件接口")
    if destination.exists():
        raise ValueError("目标文件已存在，请换一个路径，避免覆盖原文件")
    request = Request(config["url"].rstrip("/") + path, headers={"Authorization": "Bearer " + config["token"]})
    try:
        with build_opener().open(request, timeout=60) as response:
            content = response.read(20 * 1024 * 1024 + 1)
    except HTTPError as error:
        raise ValueError(f"文件下载失败，HTTP {error.code}") from None
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("文件超出 20 MB 限制")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        output.write(content)
    return {"downloaded": str(destination.resolve()), "size": len(content)}


def main():
    parser = argparse.ArgumentParser(description="易方 / MODUS 看板操作")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("MODUS_CONFIG", DEFAULT_CONFIG)))
    commands = parser.add_subparsers(dest="command", required=True)
    connect_command = commands.add_parser("connect")
    connect_command.add_argument("--url", required=True)
    connect_command.add_argument("--username", required=True)
    commands.add_parser("status")
    commands.add_parser("state")
    download = commands.add_parser("download")
    download.add_argument("path")
    download.add_argument("destination", type=Path)
    new_url = commands.add_parser("set-url")
    new_url.add_argument("url")
    submit = commands.add_parser("submit")
    submit.add_argument("manifest", type=Path)
    request = commands.add_parser("request")
    request.add_argument("method", choices=["GET", "POST", "PATCH", "DELETE"])
    request.add_argument("path")
    request.add_argument("--json-file", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "connect":
            connect(arguments, arguments.config)
            return 0
        if not arguments.config.exists():
            raise ValueError("尚未配置连接，请先执行 connect 或提供 --config")
        config = read_json(arguments.config)
        if arguments.command == "set-url":
            parsed = urlparse(arguments.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("请填写有效的后端地址")
            if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("远程连接请使用 HTTPS 地址")
            config["url"] = arguments.url.rstrip("/")
            arguments.config.write_text(json.dumps(config, indent=2), encoding="utf-8")
            result = {"updated": True, "url": config["url"]}
        elif arguments.command == "status":
            result = authenticated_request(config, "/api/agent/me")
        elif arguments.command == "state":
            result = authenticated_request(config, "/api/state")
        elif arguments.command == "download":
            result = download_file(config, arguments.path, arguments.destination)
        elif arguments.command == "submit":
            result = authenticated_request(config, "/api/agent/submit", "POST", prepare_submission(arguments.manifest), retry=True)
        else:
            data = read_json(arguments.json_file) if arguments.json_file else None
            result = authenticated_request(config, arguments.path, arguments.method, data)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError, OSError, KeyError, URLError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
