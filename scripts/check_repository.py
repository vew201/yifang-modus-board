"""Reject runtime data and credential files before publishing or CI."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS={'data','.local','__pycache__','codex-releases','upgrade-backups','actions-runner','.venv','node_modules'}
FORBIDDEN_NAMES={'connection.json','bootstrap.json','.install-claim.json','.modus-board.json','.modus-deploy.json','当前外网地址.txt'}
SECRET_PATTERNS=[re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),re.compile(r'github_pat_[A-Za-z0-9_]{30,}'),re.compile(r'sk-(?:proj-)?[A-Za-z0-9_-]{35,}')]


def inspect(paths):
    problems=[]
    for path in paths:
        relative=path.relative_to(ROOT)
        if set(relative.parts)&FORBIDDEN_PARTS or path.name in FORBIDDEN_NAMES or path.suffix in {'.sqlite','.sqlite3','.db','.log','.pyc','.zip'} or (path.name.startswith('.env') and path.name!='.env.example'):
            problems.append(str(relative)+': runtime data or private configuration');continue
        if path.suffix.lower() in {'.ttf','.png','.jpg','.ico','.woff','.woff2'}:continue
        try:text=path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):problems.append(str(relative)+': possible credential; value not printed')
    return problems


def main():
    result=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True)
    files=[ROOT/value.decode('utf-8') for value in result.stdout.split(b'\0') if value]
    if not files:files=[path for path in ROOT.rglob('*') if path.is_file() and '.git' not in path.relative_to(ROOT).parts and '__pycache__' not in path.parts]
    problems=inspect(files)
    if problems:
        print('\n'.join(problems));return 1
    print(f'Repository boundary passed: {len(files)} source files, no runtime data or detected credentials')
    return 0


if __name__=='__main__':raise SystemExit(main())
