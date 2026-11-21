"""Stable MODUS entry. Update content lives in versioned releases, credentials stay outside."""
import importlib.util
import json
from pathlib import Path
import re
import sys


def main():
    home=Path(__file__).resolve().parent
    try:
        state=json.loads((home/'active.json').read_text(encoding='utf-8'))
        release=state.get('release','')
        if not re.fullmatch('[a-f0-9]{64}',release):raise ValueError('本地生效版本记录不正确')
        script=home/'releases'/release/'runtime.py'
        spec=importlib.util.spec_from_file_location('modus_runtime',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.run(home,sys.argv[1:])
    except Exception as error:
        print(json.dumps({'error':str(error),'action':'保留本地文件及账号，检查连接后重试'},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
