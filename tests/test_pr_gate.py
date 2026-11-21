import copy
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('modus_pr_gate',ROOT/'scripts/pr_gate.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


class PRGateTests(unittest.TestCase):
    def setUp(self):
        self.pr={'number':5,'draft':False,'state':'open','merged':False,'user':{'login':'XinboChen-Jason'},'head':{'sha':'a'*40,'repo':{'full_name':gate.REPOSITORY}},'base':{'ref':'main','repo':{'full_name':gate.REPOSITORY}}}
        self.main='b'*40
        self.reviews=[{'id':1,'state':'APPROVED','commit_id':'a'*40,'user':{'login':'vew201'}}]
        self.checks=[{'id':3,'name':'Tests','head_sha':'a'*40,'status':'completed','conclusion':'success','app':{'slug':'github-actions'}}]
        self.runs=[{'run_number':1,'event':'pull_request','path':'.github/workflows/ci.yml','head_sha':'a'*40,'status':'completed','conclusion':'success','pull_requests':[{'number':5,'head':{'sha':'a'*40},'base':{'sha':self.main}}]}]

    def validate(self):return gate.validate_review(self.pr,self.reviews,self.checks,self.runs,self.main)

    def test_current_approved_tested_head_is_eligible(self):self.assertEqual(self.validate(),'a'*40)

    def test_no_findings_comment_does_not_authorize_merge(self):
        self.reviews[0]['state']='COMMENTED'
        with self.assertRaises(gate.GateError):self.validate()

    def test_author_or_unknown_approval_does_not_authorize_merge(self):
        for login in ['XinboChen-Jason','unknown-account']:
            self.reviews[0]['user']['login']=login
            with self.assertRaises(gate.GateError):self.validate()

    def test_pushing_a_new_commit_invalidates_previous_approval(self):
        self.reviews[0]['commit_id']='c'*40
        with self.assertRaises(gate.GateError):self.validate()

    def test_requested_changes_block_even_with_another_approval(self):
        self.pr['user']['login']='third-author'
        self.reviews.append({'id':2,'state':'CHANGES_REQUESTED','commit_id':'a'*40,'user':{'login':'XinboChen-Jason'}})
        with self.assertRaises(gate.GateError):self.validate()

    def test_latest_failed_check_supersedes_an_old_pass(self):
        self.checks.append({**self.checks[0],'id':4,'conclusion':'failure'})
        with self.assertRaises(gate.GateError):self.validate()

    def test_another_app_cannot_spoof_the_test_check(self):
        self.checks[0]['app']['slug']='other-app'
        with self.assertRaises(gate.GateError):self.validate()

    def test_changed_main_base_requires_retest(self):
        self.main='d'*40
        with self.assertRaises(gate.GateError):self.validate()

    def test_draft_or_wrong_branch_or_fork_never_merges(self):
        original=copy.deepcopy(self.pr)
        self.pr['draft']=True
        with self.assertRaises(gate.GateError):self.validate()
        self.pr=copy.deepcopy(original);self.pr['base']['ref']='another'
        with self.assertRaises(gate.GateError):self.validate()
        self.pr=copy.deepcopy(original);self.pr['head']['repo']['full_name']='other/fork'
        with self.assertRaises(gate.GateError):self.validate()

    def test_merge_uses_exact_sha_and_refuses_a_push_during_inspection(self):
        outer=self
        class API:
            changed=False
            def __init__(self):self.mutations=[];self.reads=0
            def request(self,path,method='GET',data=None):
                if method=='PUT':self.mutations.append(data);return {'merged':True,'sha':'e'*40}
                if path=='/git/ref/heads/main':return {'object':{'sha':outer.main}}
                self.reads+=1
                value=copy.deepcopy(outer.pr)
                if self.changed and self.reads>1:value['head']['sha']='f'*40
                return value
            def pages(self,path,key=None):
                if '/reviews' in path:return outer.reviews
                if 'check-runs' in path:return outer.checks
                return outer.runs
        api=API();self.assertTrue(gate.review_and_merge(api,5)['merged']);self.assertEqual(api.mutations[0]['sha'],'a'*40)
        api=API();api.changed=True
        with self.assertRaises(gate.GateError):gate.review_and_merge(api,5)
        self.assertEqual(api.mutations,[])
