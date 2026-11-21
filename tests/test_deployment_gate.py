import copy
import importlib.util
from pathlib import Path
import sys
import unittest

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import watch_deploy as watcher


class DeploymentGateTests(unittest.TestCase):
    def setUp(self):
        self.revision='e'*40;self.head='a'*40;self.base='b'*40
        self.pr={'number':3,'state':'closed','merged':True,'merged_at':'2026-11-21T12:00:00Z','merge_commit_sha':self.revision,'draft':False,'user':{'login':'XinboChen-Jason'},'head':{'sha':self.head,'repo':{'full_name':watcher.REPOSITORY}},'base':{'ref':'main','repo':{'full_name':watcher.REPOSITORY}}}
        self.reviews=[{'id':1,'state':'APPROVED','commit_id':self.head,'user':{'login':'vew201'}}]
        self.checks=[{'id':1,'name':'Tests','head_sha':self.head,'app':{'slug':'github-actions'},'status':'completed','conclusion':'success'}]
        self.runs=[{'run_number':1,'path':'.github/workflows/ci.yml','event':'pull_request','head_sha':self.head,'status':'completed','conclusion':'success','pull_requests':[{'number':3,'head':{'sha':self.head},'base':{'sha':self.base}}]}]
        self.private=True;self.current=self.revision;self.associated=[self.pr]
        outer=self
        class API:
            def request(self,path):
                if path=='':return {'private':outer.private}
                if path=='/git/ref/heads/main':return {'object':{'sha':outer.current}}
                if path.startswith('/git/commits/'):return {'parents':[{'sha':outer.base}]}
                if path.startswith('/pulls/'):return copy.deepcopy(outer.pr)
                raise AssertionError(path)
            def pages(self,path,key=None):
                if path.endswith('/pulls'):return outer.associated
                if path.endswith('/reviews'):return outer.reviews
                if 'check-runs' in path:return outer.checks
                if 'runs?' in path:return outer.runs
                raise AssertionError(path)
        self.api=API()

    def test_a_reviewed_merged_revision_can_deploy(self):self.assertEqual(watcher.authorize(self.api,self.revision)['pr'],3)

    def test_a_direct_main_push_is_not_a_deployment_authorization(self):
        self.associated=[]
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision)

    def test_deployment_refuses_a_newer_main_or_public_repository(self):
        self.current='f'*40
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision)
        self.current=self.revision;self.private=False
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision)

    def test_old_approval_or_old_tested_base_cannot_deploy(self):
        self.reviews[0]['commit_id']='c'*40
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision)
        self.reviews[0]['commit_id']=self.head;self.runs[0]['pull_requests'][0]['base']['sha']='c'*40
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision)

    def test_first_deploy_requires_exact_pinned_bootstrap_and_ci(self):
        self.associated=[];self.checks[0]['head_sha']=self.revision
        self.assertTrue(watcher.authorize(self.api,self.revision,self.revision)['bootstrap'])
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision,'f'*40)
        self.checks[0]['conclusion']='failure'
        with self.assertRaises(watcher.GateError):watcher.authorize(self.api,self.revision,self.revision)

    def test_running_bootstrap_ci_is_pending_without_deploying(self):
        self.checks[0]['head_sha']=self.revision;self.checks[0]['status']='in_progress'
        with self.assertRaises(watcher.Pending):watcher.authorize(self.api,self.revision,self.revision)
