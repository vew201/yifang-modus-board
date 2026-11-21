"""One-time MODUS setup: install scoped on-message hook, connection and stable entry skill."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from urllib.request import Request,urlopen

BASE=Path(__file__).resolve().parent


def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)


def install(home,workspace,codex_home,config,seed):
    home=Path(home).resolve();workspace=Path(workspace).resolve();codex_home=Path(codex_home).resolve()
    home.mkdir(parents=True,exist_ok=True);workspace.mkdir(parents=True,exist_ok=True)
    if (home/'connection.json').exists():
        previous=load(home/'connection.json')
        if previous.get('board_id')!=config.get('board_id') or previous.get('user',{}).get('id')!=config.get('user',{}).get('id'):
            raise ValueError('此安装目录已绑定另一个看板或账号，请选择独立的 --home')
    # Preserve the existing launcher so ordinary rule updates do not change hook definitions.
    if not (home/'launcher.py').exists():shutil.copyfile(BASE/'launcher.py',home/'launcher.py')
    save(home/'connection.json',config)
    spec=importlib.util.spec_from_file_location('seed_runtime',Path(seed)/'runtime.py')
    runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)
    if not (home/'active.json').exists() and (Path(seed)/'manifest.json').exists():
        manifest=load(Path(seed)/'manifest.json');runtime.verify(manifest,config)
        directory=home/'releases'/manifest['release'];directory.mkdir(parents=True,exist_ok=True)
        for name,item in manifest['files'].items():
            value=(Path(seed)/name).read_bytes()
            if hashlib.sha256(value).hexdigest()!=item['sha256']:raise ValueError('首次接入包文件校验失败')
            target=directory/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(value)
        save(directory/'manifest.json',manifest)
        save(home/'active.json',{'release':manifest['release'],'version':manifest['version'],'etag':'"'+hashlib.sha256(runtime.canonical(manifest)).hexdigest()+'"'})
    state=runtime.sync(home)
    runtime.http(config['url'],'/api/codex/enroll',config['token'],method='POST',body={'release':state['release']})
    skill=codex_home/'skills/modus-board';skill.mkdir(parents=True,exist_ok=True)
    for name in ['SKILL.md','scripts/modus.py']:
        path=skill/name
        if path.exists() and not (home/'original-skill'/name).exists():
            backup=home/'original-skill'/name;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,backup)
    (skill/'SKILL.md').write_text('''---
name: modus-board
description: 将合作意向、项目、待办、沟通和文件录入易方MODUS看板，按最新规则补问信息并维护记录。
---

# MODUS稳定入口

本会话维护MODUS看板时，先运行本目录 scripts/modus.py check，核对在线接入版本。运行 rules 获取当前实际技能路径，读取该SKILL.md并按需读取它引用的说明，再执行业务操作。
首次接入会安装UserPromptSubmit检查；受支持且实际运行时复用本轮回执。若当前运行方式未触发Hook，AI仍须依AGENTS.md每条消息先运行check。已信任不代表已触发，模拟测试不能冒充自动运行。无定时任务，当前会话已有本轮检查回执时复用。规则更新后重新读取当前生效版本；不得沿用旧阶段和完成状态规则。
仅在包含.modus-board.json标记的专用工作目录中使用。账号凭证位于独立客户端目录，不属于更新文件。禁止把凭证、激活包或连接验证密钥分享给第三方。
''',encoding='utf-8')
    wrapper='''import json,os,runpy,sys
from pathlib import Path
p=Path.cwd().resolve()
for d in [p,*p.parents]:
 marker=d/'.modus-board.json'
 if marker.exists():
  config=json.loads(marker.read_text(encoding='utf-8'))
  launcher=Path(config['connector_home'])/'launcher.py'
  sys.argv=[str(launcher),*sys.argv[1:]]
  runpy.run_path(str(launcher),run_name='__main__')
  break
else:
 raise SystemExit('请在已初始化的MODUS专用工作目录运行脚本')
'''
    (skill/'scripts').mkdir(exist_ok=True);(skill/'scripts/modus.py').write_text(wrapper,encoding='utf-8')
    hook_file=codex_home/'hooks.json'
    hooks=load(hook_file) if hook_file.exists() else {'hooks':{}}
    hooks.setdefault('hooks',{})
    command=subprocess.list2cmdline([sys.executable,str(home/'launcher.py'),'hook']) if os.name=='nt' else __import__('shlex').join([sys.executable,str(home/'launcher.py'),'hook'])
    for event in ['UserPromptSubmit','SessionStart','PostCompact']:
        groups=hooks['hooks'].setdefault(event,[])
        if not any(any(item.get('command')==command for item in group.get('hooks',[])) for group in groups):
            groups.append({'hooks':[{'type':'command','command':command,'timeout':20,'additionalContextLimit':600}]})
    save(hook_file,hooks)
    save(workspace/'.modus-board.json',{'connector_home':str(home),'board_id':config['board_id']})
    agents=workspace/'AGENTS.md'
    existing=agents.read_text(encoding='utf-8') if agents.exists() else ''
    marker='<!-- MODUS-CLIENT -->'
    if marker not in existing:
        with agents.open('a',encoding='utf-8') as output:
            output.write('\n'+marker+'\n在本专用会话中，每条新用户消息处理前先执行 python "'+str(home/'launcher.py')+'" check。若Hook已提供本轮检查回执则复用。用 rules 获取并读取当前有效技能。新版本优先于本会话旧的看板业务说明，其他用户授权和上级指令不变。更新失败可整理信息但暂停写入。首次Hook需使用者在Codex的/hooks界面信任，不得伪造信任或关闭检查。\n')
    save(home/'installation.json',{'hook_file':str(hook_file),'hook_trust':'pending_user_review','workspace':str(workspace),'version':state['version']})
    review=None
    executable=shutil.which('codex')
    if os.name=='nt' and executable:
        review=home/'首次确认Hook.cmd'
        review.write_text('@echo off\nchcp 65001 >nul\necho 请在下面的Codex终端输入 /hooks，然后审阅并信任MODUS的三个Hook。\necho 这些Hook只在MODUS专用目录内检查版本；不要选择跳过审查。\necho 确认后可按Ctrl+C退出此授权终端，无需发送业务消息。\n"'+executable+'" -C "'+str(workspace)+'"\n',encoding='utf-8')
    return {'installed':True,'home':str(home),'workspace':str(workspace),'version':state['version'],'user':config['user'],'hook_review_launcher':str(review) if review else None,'hook_trust':'首次在Codex中审阅并信任MODUS hooks，后续业务规则自动更新'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--workspace',type=Path,default=Path.cwd());parser.add_argument('--home',type=Path)
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex')))
    parser.add_argument('--migrate-config',type=Path);args=parser.parse_args()
    home=args.home or args.codex_home/'modus-board'
    if args.migrate_config:
        previous=load(args.migrate_config)
        request=Request(previous['url'].rstrip('/')+'/api/codex/connect-info',headers={'Authorization':'Bearer '+previous['token']})
        with urlopen(request,timeout=10) as response:metadata=json.load(response)
        config={**previous,**metadata}
        seed=BASE.parent/'codex-skill'
    else:
        config=load(BASE/'bootstrap.json')
        claim_path=BASE/'.install-claim.json'
        if claim_path.exists():claim=load(claim_path)['claim_id']
        else:claim=secrets.token_hex(16);save(claim_path,{'claim_id':claim})
        request=Request(config['url'].rstrip('/')+'/api/codex/activate',method='POST',data=json.dumps({'activation':config['activation'],'claim_id':claim}).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=10) as response:result=json.load(response)
        config={key:value for key,value in config.items() if key not in {'activation','claim_id'}}
        config.update({'token':result['token'],'user':result['user']});seed=BASE/'seed'
    result=install(home,args.workspace,args.codex_home,config,seed)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:raise SystemExit(main())
    except Exception as error:
        print('初始化未完成：'+type(error).__name__+'，请检查连接或激活包有效期。',file=sys.stderr)
        raise SystemExit(1)
