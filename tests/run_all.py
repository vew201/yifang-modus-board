"""Run isolated API and optional browser suites, failing immediately on errors."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('test-results-all'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.browser and importlib.util.find_spec('playwright') is None:
        print('缺少 playwright。请先安装 Python playwright 包，并安装 Microsoft Edge。', flush=True)
        return 1
    suites = [('backend', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])]
    if args.browser:
        for name in ['smoke', 'maintenance', 'hierarchy', 'workflow', 'activity', 'codex', 'trash']:
            suites.append((name, [sys.executable, str(root/'tests'/f'browser_{name}.py'), '--output', str(output/name)]))
    results = []
    for name, command in suites:
        print(f'正在运行 {name}', flush=True)
        with (output/f'{name}.log').open('w',encoding='utf-8') as log:
            result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, encoding='utf-8')
        results.append({'suite':name,'passed':result.returncode==0,'exit_code':result.returncode})
        (output/'summary.json').write_text(json.dumps({'passed':len(results)==len(suites) and all(x['passed'] for x in results),'completed':len(results)==len(suites),'requested_browser':args.browser,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
        if result.returncode:
            print(f'{name} 失败，请查看 {output/name}.log', flush=True)
            return result.returncode
    print(f'全部 {len(suites)} 套测试通过，报告目录 {output}', flush=True)
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
