from datetime import datetime, timedelta
from pathlib import Path
import base64
import tempfile
import threading
import unittest

from server import WorkbenchServer, LOCAL_TZ, connect_database, initialize_database
from test_backend import Client
from project_model import aging


class ActivityAndExtraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory()
        cls.server=WorkbenchServer(('127.0.0.1',0),cls.directory.name)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.client=Client(f'http://127.0.0.1:{cls.server.server_port}')
        _,session,_=cls.client.request('/api/setup','POST',{'accounts':[{'name':'甲','username':'a','password':'abcdefgh'},{'name':'乙','username':'b','password':'ijklmnop'}]})
        cls.client.csrf=session['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(3);cls.directory.cleanup()

    def request(self,path,method='GET',data=None,expected=200):
        status,body,_=self.client.request(path,method,data)
        self.assertEqual(status,expected,body)
        return body

    def state(self):return self.request('/api/state')
    def lead(self):return self.request('/api/opportunities','POST',{'name':'停留时间验收','owner_id':1})['id']
    def item(self,identifier):return next(x for x in self.state()['opportunities'] if x['id']==identifier)
    def old(self,identifier,days=7):
        stamp=(datetime.now(LOCAL_TZ)-timedelta(days=days)).isoformat(timespec='seconds')
        with connect_database(self.server.database) as connection:connection.execute('UPDATE opportunities SET last_activity_at=? WHERE id=?',(stamp,identifier))
        return stamp

    def test_exact_aging_boundaries_and_timezones(self):
        now=datetime(2026,11,20,12,tzinfo=LOCAL_TZ)
        for seconds,band in [(0,'white'),(3*86400-1,'white'),(3*86400,'gray'),(6*86400-1,'gray'),(6*86400,'black'),(10*86400,'black')]:
            stamp=(now-timedelta(seconds=seconds)).isoformat()
            self.assertEqual(aging({'last_activity_at':stamp,'created_at':stamp},now)['age_band'],band)
        utc=(now-timedelta(days=6)).astimezone(__import__('datetime').timezone.utc).isoformat()
        self.assertEqual(aging({'last_activity_at':utc,'created_at':utc},now)['age_band'],'black')

    def test_read_refresh_and_unchanged_edit_do_not_reset(self):
        identifier=self.lead();stamp=self.old(identifier)
        self.assertEqual(self.item(identifier)['age_band'],'black')
        self.request(f'/api/opportunities/{identifier}','PATCH',{'description':''})
        self.assertEqual(self.item(identifier)['last_activity_at'],stamp)
        self.assertEqual(self.item(identifier)['age_band'],'black')

    def test_real_edit_upload_communication_and_task_action_reset(self):
        identifier=self.lead()
        self.old(identifier)
        self.request(f'/api/opportunities/{identifier}','PATCH',{'description':'最新需求有变化'})
        self.assertEqual(self.item(identifier)['age_band'],'white')
        self.old(identifier)
        self.request('/api/opportunity_files','POST',{'opportunity_id':identifier,'owner_id':1,'role':'attachment','name':'记录.txt','content':'eA=='})
        self.assertEqual(self.item(identifier)['age_band'],'white')
        self.old(identifier)
        self.request('/api/conversations','POST',{'opportunity_id':identifier,'owner_id':1,'text':'新沟通原文','occurred_at':'2026-01-01T10:00'})
        self.assertEqual(self.item(identifier)['age_band'],'white')
        task=self.request('/api/tasks','POST',{'title':'单独行动','owner_id':1,'opportunity_id':identifier,'start_date':'','end_date':''})['id']
        self.old(identifier)
        self.request(f'/api/tasks/{task}','PATCH',{'done':True})
        self.assertEqual(self.item(identifier)['age_band'],'white')

    def test_replay_does_not_reset_activity(self):
        identifier=self.lead()
        payload={'request_id':'activity-replay','operations':[{'method':'POST','path':'/api/conversations','data':{'opportunity_id':identifier,'owner_id':1,'text':'原文','client_key':'fixed-key','occurred_at':'2026-01-01T10:00'}}]}
        self.request('/api/agent/submit','POST',payload)
        stamp=self.old(identifier)
        self.request('/api/agent/submit','POST',payload)
        self.assertEqual(self.item(identifier)['last_activity_at'],stamp)
        self.request('/api/conversations','POST',payload['operations'][0]['data'])
        self.assertEqual(self.item(identifier)['last_activity_at'],stamp)

    def test_activity_rolls_back_when_batch_fails(self):
        identifier=self.lead();stamp=self.old(identifier)
        self.request('/api/agent/submit','POST',{'request_id':'bad-activity','operations':[{'method':'PATCH','path':f'/api/opportunities/{identifier}','data':{'description':'将回滚'}},{'method':'POST','path':'/api/tasks','data':{'title':'错误','owner_id':999}}]},400)
        self.assertEqual(self.item(identifier)['last_activity_at'],stamp)

    def test_project_extra_and_opportunity_tasks_remain_distinct(self):
        today=self.state()['today']
        task=self.request('/api/tasks','POST',{'title':'明天日常提醒','owner_id':1,'start_date':today,'end_date':today,'time':'14:00'})['id']
        record=next(x for x in self.state()['tasks'] if x['id']==task)
        self.assertIsNone(record['project_id']);self.assertIsNone(record['opportunity_id']);self.assertIsNone(record['stage_id'])
        self.request(f'/api/tasks/{task}','PATCH',{'done':True})
        self.assertTrue(any(x['task_id']==task for x in self.state()['completions']))
        self.request(f'/api/tasks/{task}','PATCH',{'done':False})
        self.assertFalse(any(x['task_id']==task for x in self.state()['completions']))

    def test_file_and_conversation_phase_classification(self):
        project=self.request('/api/projects','POST',{'name':'分类验收','owner_id':1,'start_date':'','end_date':''})['id']
        state=self.state();section=next(x for x in state['sections'] if x['project_id']==project)
        stage=next(x for x in state['stages'] if x['project_id']==project and x['name']=='需求澄清')
        file=self.request('/api/documents','POST',{'section_id':section['id'],'owner_id':1,'name':'客户需求书.txt','content':'eA=='})['id']
        self.assertEqual(next(x for x in self.state()['documents'] if x['id']==file)['stage_id'],stage['id'])
        self.request('/api/conversations','POST',{'project_id':project,'stage_id':stage['id'],'owner_id':1,'text':'已确认需求边界','summary':'需求结论'})
        self.assertTrue(any(x['stage_id']==stage['id'] for x in self.state()['conversations']))
        self.assertIsNone(next(x for x in self.state()['stages'] if x['id']==stage['id'])['completed_at'])

    def test_restart_never_resets_known_activity_time(self):
        identifier=self.lead();stamp=self.old(identifier)
        initialize_database(self.server.database)
        self.assertEqual(self.item(identifier)['last_activity_at'],stamp)


if __name__=='__main__':unittest.main()
