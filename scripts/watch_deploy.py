"""Local-only deployment controller. Install outside the contributor checkout.

Polls one GitHub ref conditionally; does not poll Codex skills or accept web requests.
The installed review gate is not auto-replaced by repository changes.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

from pr_gate import GateError,REPOSITORY,validate_review


class Pending(Exception):pass


def clean_env():
    env=os.environ.copy()
    for key in ('GH_DEBUG','GH_TOKEN','GITHUB_TOKEN','OPENAI_API_KEY','PYTHONPATH'):env.pop(key,None)
    env.update({'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'Never','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'})
    return env


class CLI:
    def __init__(self,gh):self.gh=str(gh)

    def request(self,path,method='GET',data=None,etag=None,metadata=False):
        args=[self.gh,'api','--include','--method',method,'repos/'+REPOSITORY+path]
        if etag:args+=['-H','If-None-Match: '+etag]
        if data is not None:args+=['--input','-']
        result=subprocess.run(args,input=json.dumps(data) if data is not None else None,capture_output=True,text=True,encoding='utf-8',env=clean_env(),timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        raw=result.stdout.replace('\r\n','\n')
        header,_,body=raw.partition('\n\n')
        match=re.match(r'HTTP/\S+\s+(\d+)',header)
        status=int(match.group(1)) if match else 0
        headers={line.partition(':')[0].lower():line.partition(':')[2].strip() for line in header.splitlines()[1:] if ':' in line}
        if status==304:return (None,headers.get('etag',etag)) if metadata else None
        if result.returncode or status<200 or status>=300:raise GateError('GitHub request failed ('+str(status)+'); check the owner CLI login')
        value=json.loads(body) if body.strip() else {}
        return (value,headers.get('etag')) if metadata else value

    def pages(self,path,key=None):
        result=[]
        for page in range(1,21):
            value=self.request(path+('&' if '?' in path else '?')+urlencode({'page':page,'per_page':100}))
            rows=value[key] if key else value;result.extend(rows)
            if len(rows)<100:return result
        raise GateError('Too many results to validate safely')


def authorize(api,revision,bootstrap_sha=None):
    if not re.fullmatch('[a-f0-9]{40}',revision):raise GateError('Invalid deployment revision')
    repository=api.request('')
    if not repository.get('private'):raise GateError('This deployment is restricted to the private repository')
    if api.request('/git/ref/heads/main')['object']['sha']!=revision:raise GateError('A newer main exists; do not deploy a stale revision')
    if bootstrap_sha==revision:
        checks=api.pages(f'/commits/{revision}/check-runs?filter=latest','check_runs')
        tests=[item for item in checks if item.get('name')=='Tests' and item.get('head_sha')==revision and item.get('app',{}).get('slug')=='github-actions']
        if not tests or max(tests,key=lambda row:row['id']).get('status')!='completed':raise Pending('Bootstrap CI is still running')
        if max(tests,key=lambda row:row['id']).get('conclusion')!='success':raise GateError('Bootstrap CI did not pass')
        return {'bootstrap':True,'revision':revision}
    associated=api.pages(f'/commits/{revision}/pulls')
    candidates=[item for item in associated if item.get('merged_at') and item.get('merge_commit_sha')==revision and item.get('base',{}).get('ref')=='main']
    if not candidates:raise GateError('main has no matching reviewed merged PR; deployment refused')
    pr=api.request('/pulls/'+str(candidates[0]['number']))
    parents=api.request('/git/commits/'+revision).get('parents',[])
    if len(parents)!=1:raise GateError('Only a squash merge is accepted by this deployment controller')
    head=pr['head']['sha'];number=pr['number']
    reviews=api.pages(f'/pulls/{number}/reviews')
    checks=api.pages(f'/commits/{head}/check-runs?filter=latest','check_runs')
    runs=api.pages('/actions/workflows/ci.yml/runs?event=pull_request&head_sha='+head,'workflow_runs')
    candidate={**pr,'state':'open','merged':False}
    validate_review(candidate,reviews,checks,runs,parents[0]['sha'])
    return {'bootstrap':False,'revision':revision,'pr':number}


def checkout(config,revision):
    base=Path(config['cache']).resolve();target=base/revision
    if not target.resolve().is_relative_to(base) or not re.fullmatch('[a-f0-9]{40}',revision):raise GateError('Unsafe cache directory')
    target.mkdir(parents=True,exist_ok=True)
    gh=str(Path(config['gh']).resolve()).replace('\\','/')
    if any(value in gh for value in ('"','`','\n','\r')):raise GateError('Unexpected CLI path')
    commands=[['git','init'],['git','config','core.autocrlf','false'],['git','config','credential.helper','!"'+gh+'" auth git-credential'],['git','fetch','--depth','1','https://github.com/'+REPOSITORY+'.git',revision],['git','checkout','--detach',revision]]
    for args in commands:
        result=subprocess.run(args,cwd=target,env=clean_env(),capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise GateError('Source fetch or checkout did not complete')
    actual=subprocess.run(['git','rev-parse','HEAD'],cwd=target,capture_output=True,text=True,check=True).stdout.strip()
    if actual!=revision:raise GateError('Fetched source revision does not match')
    # Reuse only checksum-matching public font assets, never customer uploads.
    manifest=json.loads((target/'asset-manifest.json').read_text(encoding='utf-8'))
    runtime=Path(config['runtime']).resolve()
    for asset in manifest.get('assets',[]):
        relative=Path(asset['path'])
        if not relative.as_posix().startswith('static/assets/') or relative.suffix not in {'.ttf','.woff','.woff2'} or '..' in relative.parts:raise GateError('Invalid shared asset path')
        existing=runtime/relative;destination=target/relative
        if existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest()==asset['sha256']:
            destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(existing.read_bytes())
    result=subprocess.run([sys.executable,str(target/'scripts/fetch_assets.py')],cwd=target,env=clean_env(),capture_output=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise GateError('Asset preparation did not pass')
    return target


def tick(config,state):
    api=CLI(config['gh'])
    ref,etag=api.request('/git/ref/heads/main',etag=state.get('etag'),metadata=True)
    if etag:state['etag']=etag
    if ref:state['head']=ref['object']['sha']
    revision=state.get('head')
    runtime=Path(config['runtime'])
    installed_path=runtime/'data/deployment-state.json'
    installed=json.loads(installed_path.read_text(encoding='utf-8')) if installed_path.exists() else {}
    if not revision or installed.get('revision')==revision:return
    if state.get('blocked_revision')==revision:return
    bootstrap=config.get('bootstrap_sha') if not installed.get('revision') else None
    try:permit=authorize(api,revision,bootstrap)
    except Pending:
        state['message']='Waiting for CI';return
    except GateError as error:
        state.update({'blocked_revision':revision,'message':str(error)});return
    source=checkout(config,revision)
    # Recheck the authorization after downloading; a new main must not be overwritten.
    authorize(api,revision,bootstrap)
    env=clean_env();env.update({'MODUS_RUNTIME_DIR':str(runtime),'MODUS_DEPLOY_REVISION':revision})
    result=subprocess.run([sys.executable,str(source/'scripts/deploy_local.py')],cwd=source,env=env,capture_output=True,text=True,encoding='utf-8',timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (Path(config['cache']).parent/'last-deploy.log').write_text((result.stdout+'\n'+result.stderr)[-20000:],encoding='utf-8')
    if result.returncode:raise GateError('Deployment failed and attempted code rollback; check local backend logs')
    state.update({'deployed_revision':revision,'message':'Deployed '+revision[:12],'last_deployed_at':datetime.now().astimezone().isoformat(),'last_pr':permit.get('pr')})
    state.pop('blocked_revision',None)


def run(config_file,once=False):
    config_file=Path(config_file).resolve();config=json.loads(config_file.read_text(encoding='utf-8'))
    lock=(config_file.parent/'watcher.lock').open('a+b')
    lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close();return {'message':'Controller is already running'}
    state_file=config_file.parent/'deployment-status.json'
    state={}
    while True:
        try:tick(config,state)
        except Exception as error:state['message']=type(error).__name__+': deployment connection or service action needs attention'
        state['checked_at']=datetime.now().astimezone().isoformat()
        pending=state_file.with_suffix('.pending');pending.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');pending.replace(state_file)
        if once:return state
        time.sleep(60)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    run(args.config,args.once)
