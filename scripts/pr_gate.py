"""Merge only an explicitly approved, currently tested head. Standard library."""
import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen

REPOSITORY='vew201/yifang-modus-board'
REVIEWERS={'vew201','XinboChen-Jason'}


class GateError(Exception):pass


def validate_review(pr,reviews,checks,runs,main_sha):
    head=pr['head']['sha'];number=pr['number']
    if pr.get('draft') or pr.get('state')!='open' or pr.get('merged'):raise GateError('PR is not an open, ready change')
    if pr['base']['ref']!='main' or pr['base']['repo']['full_name']!=REPOSITORY:raise GateError('Unexpected target repository or branch')
    if pr['head']['repo']['full_name']!=REPOSITORY:raise GateError('External forks require an explicit maintainer review workflow')
    latest={}
    for review in sorted(reviews,key=lambda item:item.get('id',0)):
        login=review['user']['login']
        if login not in REVIEWERS or review['state'] in {'COMMENTED','PENDING'}:continue
        latest[login]=review
    if any(item['state']=='CHANGES_REQUESTED' for item in latest.values()):raise GateError('A requested change has not been cleared')
    approvals=[item for login,item in latest.items() if login!=pr['user']['login'] and item['state']=='APPROVED' and item.get('commit_id')==head]
    if not approvals:raise GateError('A real approval on the current head from another authorized reviewer is required')
    matching=[item for item in checks if item.get('name')=='Tests' and item.get('head_sha')==head and item.get('app',{}).get('slug')=='github-actions']
    if not matching:raise GateError('Tests check is missing for the reviewed head')
    check=max(matching,key=lambda item:item.get('id',0))
    if check.get('status')!='completed' or check.get('conclusion')!='success':raise GateError('Latest Tests check did not pass')
    run_matches=[]
    for run in runs:
        if run.get('event')!='pull_request' or run.get('head_sha')!=head or run.get('path')!='.github/workflows/ci.yml':continue
        for associated in run.get('pull_requests',[]):
            if associated.get('number')==number and associated.get('head',{}).get('sha')==head and associated.get('base',{}).get('sha')==main_sha:
                run_matches.append(run)
    if not run_matches:raise GateError('CI must test this head against the current main; update the branch and rerun CI')
    run=max(run_matches,key=lambda item:item.get('run_number',0))
    if run.get('status')!='completed' or run.get('conclusion')!='success':raise GateError('The current-base CI run did not pass')
    return head


class GitHub:
    def __init__(self,token):
        if not token:raise GateError('GitHub credential is missing')
        self.token=token

    def request(self,path,method='GET',data=None):
        request=Request('https://api.github.com/repos/'+REPOSITORY+path,method=method,data=json.dumps(data).encode() if data is not None else None,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2026-03-10','User-Agent':'MODUS-PR-gate','Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=20) as response:return json.load(response)
        except HTTPError as error:raise GateError(f'GitHub returned {error.code}; no merge was assumed') from None

    def pages(self,path,key=None):
        rows=[]
        for page in range(1,21):
            result=self.request(path+('&' if '?' in path else '?')+urlencode({'per_page':100,'page':page}))
            items=result[key] if key else result
            rows.extend(items)
            if len(items)<100:return rows
        raise GateError('Response exceeds the review gate page limit')


def review_and_merge(api,number):
    pr=api.request(f'/pulls/{number}')
    main=api.request('/git/ref/heads/main')['object']['sha']
    head=pr['head']['sha']
    reviews=api.pages(f'/pulls/{number}/reviews')
    checks=api.pages(f'/commits/{head}/check-runs?filter=latest','check_runs')
    runs=api.pages('/actions/workflows/ci.yml/runs?event=pull_request&head_sha='+head,'workflow_runs')
    validate_review(pr,reviews,checks,runs,main)
    # Refuse stale authorization if another push or merge happened during inspection.
    if api.request('/git/ref/heads/main')['object']['sha']!=main:raise GateError('main changed; inspect and retest the new base')
    if api.request(f'/pulls/{number}')['head']['sha']!=head:raise GateError('PR head changed; a new review is required')
    result=api.request(f'/pulls/{number}/merge','PUT',{'sha':head,'merge_method':'squash'})
    if not result.get('merged'):raise GateError('GitHub did not merge the PR')
    return {'merged':True,'reviewed_head':head,'merge_sha':result['sha'],'pr':number}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--pr',type=int,required=True);args=parser.parse_args()
    if os.environ.get('GITHUB_REPOSITORY',REPOSITORY)!=REPOSITORY:raise GateError('Unexpected repository')
    if args.pr<1:raise GateError('Invalid PR number')
    result=review_and_merge(GitHub(os.environ.get('GH_TOKEN','')),args.pr)
    output=os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output,'a',encoding='utf-8') as handle:handle.write('merge_sha='+result['merge_sha']+'\n')
    print(json.dumps(result));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except GateError as error:print(str(error),file=sys.stderr);raise SystemExit(1)
