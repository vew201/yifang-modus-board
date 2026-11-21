"""Install the supplied MODUS skill and optionally connect a user's account."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="安装易方 MODUS Codex 接入技能")
    parser.add_argument("--install-only", action="store_true")
    parser.add_argument("--destination", type=Path)
    arguments = parser.parse_args()
    source = Path(__file__).resolve().parent / "codex-skill"
    codex_directory = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    destination = arguments.destination or codex_directory / "skills" / "modus-board"
    destination.mkdir(parents=True, exist_ok=True)
    for relative in [Path("SKILL.md"), Path("scripts/modus.py"), Path("references/看板专用对话说明.txt"), Path("references/接口与脚本.txt")]:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    print(f"技能已安装到 {destination}")
    if arguments.install_only:
        return 0
    url = input("后端地址，直接回车使用 http://127.0.0.1:8765\n").strip() or "http://127.0.0.1:8765"
    username = input("你自己的看板登录账号\n").strip()
    if not username:
        print("技能已安装。可以稍后使用 connect 命令连接账号。")
        return 0
    result = subprocess.run([sys.executable, str(destination / "scripts/modus.py"), "connect", "--url", url, "--username", username], check=False)
    if result.returncode == 0:
        print("请新建一个专用 Codex 聊天，使用 $modus-board 录入与维护看板。")
    return result.returncode


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
