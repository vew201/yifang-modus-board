import base64
from datetime import datetime, timedelta
import json
import sqlite3
import tempfile
import threading
import unittest

from server import WorkbenchServer, LOCAL_TZ, initialize_database
from test_backend import Client


class OpportunityWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = WorkbenchServer(('127.0.0.1', 0), cls.directory.name)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(f'http://127.0.0.1:{cls.server.server_port}')
        status, session, _ = cls.client.request('/api/setup', 'POST', {'accounts': [
            {'name':'甲','username':'a','password':'abcdefgh'},
            {'name':'乙','username':'b','password':'ijklmnop'},
        ]})
        assert status == 200
        cls.client.csrf = session['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.directory.cleanup()

    def request(self, path, method='GET', data=None, expected=200):
        status, body, _ = self.client.request(path, method, data)
        self.assertEqual(status, expected, body)
        return body

    def state(self):
        return self.request('/api/state')

    def opportunity(self, identifier):
        return next(x for x in self.state()['opportunities'] if x['id'] == identifier)

    def lead(self, owner=1):
        return self.request('/api/opportunities','POST',{'name':'三阶段验收意向','owner_id':owner,'channel':'线上帖子','company_details':'生物制药企业','service_type':'AI入企','post_text':'需要提升效果','description':'我的总结'})['id']

    def communication(self, identifier, **changes):
        data = {'opportunity_id':identifier,'owner_id':1,'text':'原始沟通内容','requirements':'已确认完整需求','occurred_at':'2026-01-01T10:30:00+08:00'}
        return self.request('/api/conversations','POST',{**data,**changes})['id']

    def upload(self, identifier, role, **changes):
        return self.request('/api/opportunity_files','POST',{'opportunity_id':identifier,'owner_id':1,'name':'方案.txt','role':role,'version':'最终版','content':base64.b64encode(b'original file').decode(),**changes})['id']

    def proposal(self, identifier=None, **changes):
        identifier = identifier or self.lead()
        solution = self.upload(identifier,'solution')
        quote = self.upload(identifier,'quote')
        self.request(f'/api/opportunities/{identifier}','PATCH',{'phase':'proposal','requirements':'已确认范围和需求','submitted_at':'2026-01-03T10:30:00+08:00','solution_file_id':solution,'quote_file_id':quote,**changes})
        return identifier

    def pending(self, identifier):
        return [x for x in self.state()['tasks'] if x['opportunity_id'] == identifier and x['auto_followup'] and not x['done']]

    def test_lead_fields_and_communication_24_hours(self):
        identifier = self.lead()
        self.assertEqual(self.opportunity(identifier)['channel'],'线上帖子')
        self.assertEqual(self.pending(identifier),[])
        self.communication(identifier)
        self.assertEqual(self.opportunity(identifier)['phase'],'communication')
        task = self.pending(identifier)[0]
        self.assertEqual((task['start_date'],task['time']),('2026-01-02','10:30'))
        self.assertEqual(len(self.pending(identifier)),1)

    def test_agreement_overrides_default_and_history_does_not_overwrite(self):
        identifier = self.lead(owner=2)
        self.communication(identifier,agreed_next_at='2026-01-06T14:00:00+08:00')
        self.communication(identifier,occurred_at='2025-12-01T10:30:00+08:00',requirements='旧需求')
        self.assertEqual(self.opportunity(identifier)['requirements'],'已确认完整需求')
        self.assertEqual(self.pending(identifier)[0]['start_date'],'2026-01-06')
        self.assertEqual(self.pending(identifier)[0]['owner_id'],2)
        self.communication(identifier,occurred_at='2026-01-07T11:00:00+08:00',agreed_next_at='2026-01-10T15:00:00+08:00')
        self.assertEqual(len(self.pending(identifier)),1)
        self.assertEqual(self.pending(identifier)[0]['time'],'15:00')

    def test_edit_and_retry_keep_count_and_revision_history(self):
        identifier = self.lead()
        record = self.communication(identifier,client_key='record-1')
        replay = self.communication(identifier,client_key='record-1')
        self.assertEqual(record,replay)
        self.request(f'/api/conversations/{record}','PATCH',{'summary':'修正摘要','revision':0})
        snapshot = self.state()
        self.assertEqual(len([x for x in snapshot['conversations'] if x['opportunity_id']==identifier]),1)
        self.assertEqual(len([x for x in snapshot['conversation_history'] if x['conversation_id']==record]),1)
        self.request(f'/api/conversations/{record}','PATCH',{'summary':'冲突','revision':0},409)

    def test_proposal_48_hours_and_reschedule_after_followup(self):
        identifier = self.proposal()
        self.assertEqual(self.pending(identifier)[0]['start_date'],'2026-01-05')
        self.communication(identifier,occurred_at='2026-01-06T11:00:00+08:00',agreed_next_at='2026-01-10T14:00:00+08:00')
        self.assertEqual(self.pending(identifier)[0]['start_date'],'2026-01-10')
        self.communication(identifier,occurred_at='2026-01-11T11:00:00+08:00')
        self.assertEqual(self.pending(identifier)[0]['start_date'],'2026-01-13')
        task = self.pending(identifier)[0]
        self.request(f'/api/tasks/{task["id"]}','PATCH',{'done':True,'revision':task['revision']})
        due = datetime.fromisoformat(self.opportunity(identifier)['next_followup_at'])
        self.assertLess(abs((due-datetime.now(LOCAL_TZ)-timedelta(hours=48)).total_seconds()),5)
        self.assertEqual(len(self.pending(identifier)),1)
        self.assertTrue(any(x['task_id']==task['id'] for x in self.state()['completions']))

    def test_proposal_requires_actual_files_and_historical_time(self):
        identifier = self.lead()
        self.request(f'/api/opportunities/{identifier}','PATCH',{'phase':'proposal'},400)
        other = self.proposal()
        final = self.opportunity(other)
        self.request(f'/api/opportunities/{identifier}','PATCH',{'phase':'proposal','requirements':'需求','submitted_at':'2026-01-03T10:00','solution_file_id':final['solution_file_id'],'quote_file_id':final['quote_file_id']},400)
        self.request(f'/api/opportunities/{identifier}','PATCH',{'phase':'communication'},400)
        self.request('/api/conversations','POST',{'opportunity_id':identifier,'owner_id':1,'text':'未来','occurred_at':'2099-01-01T10:00'},400)

    def test_failure_reason_and_followups_stop_and_reopen(self):
        identifier = self.proposal()
        self.request(f'/api/opportunities/{identifier}/fail','POST',{},400)
        self.request(f'/api/opportunities/{identifier}/fail','POST',{'failure_reason':'预算不匹配'})
        self.assertEqual(self.opportunity(identifier)['outcome'],'failed')
        self.assertEqual(self.pending(identifier),[])
        self.communication(identifier,occurred_at='2026-01-07T10:00')
        self.assertEqual(self.pending(identifier),[])
        self.request(f'/api/opportunities/{identifier}/reopen','POST',{})
        self.assertEqual(len(self.pending(identifier)),1)

    def test_conversion_and_success_requires_completion_and_final_payment(self):
        identifier = self.proposal()
        project = self.request(f'/api/opportunities/{identifier}/convert','POST',{})['project_id']
        self.assertEqual(self.opportunity(identifier)['outcome'],'converted')
        self.assertEqual(self.pending(identifier),[])
        self.request(f'/api/projects/{project}','PATCH',{'final_payment_at':'2026-01-08T10:00','payment_note':'尾款到账凭据已核对'})
        before = next(x for x in self.state()['projects'] if x['id']==project)
        self.assertFalse(before['completed_at'])
        tasks=[]
        for stage in [x for x in self.state()['stages'] if x['project_id']==project]:
            task=self.request('/api/tasks','POST',{'title':'实际工作','owner_id':1,'project_id':project,'stage_id':stage['id'],'start_date':'','end_date':''})['id']
            tasks.append(task)
            self.request(f'/api/tasks/{task}','PATCH',{'done':True})
        self.request(f'/api/projects/{project}','PATCH',{'development_done':True,'completion_note':'实际开发成果已核对完成'})
        after = next(x for x in self.state()['projects'] if x['id']==project)
        self.assertTrue(after['completed_at'] and after['final_payment_at'])
        self.request(f'/api/tasks/{tasks[0]}','PATCH',{'done':False})
        reopened = next(x for x in self.state()['projects'] if x['id']==project)
        self.assertTrue(reopened['completed_at'])
        self.request(f'/api/projects/{project}','PATCH',{'development_done':False})
        self.request(f'/api/projects/{project}','PATCH',{'final_payment_at':''})
        self.assertFalse(next(x for x in self.state()['projects'] if x['id']==project)['final_payment_at'])

    def test_files_roundtrip_parent_relationship_and_recycle(self):
        identifier = self.lead()
        file = self.upload(identifier,'attachment')
        self.assertEqual(self.request(f'/api/opportunity_files/{file}'),b'original file')
        record = self.communication(identifier)
        other = self.lead()
        self.request('/api/opportunity_files','POST',{'opportunity_id':other,'conversation_id':record,'owner_id':1,'name':'x','role':'attachment','content':'eA=='},400)
        self.request('/api/opportunity_files','POST',{'opportunity_id':identifier,'owner_id':1,'name':'bad.svg','role':'image','content':'PHN2Zz4='},400)
        self.request(f'/api/opportunity_files/{file}','DELETE',{'revision':0})
        self.assertFalse(any(x['id']==file for x in self.state()['opportunity_files']))
        self.request(f'/api/restore/opportunity_files/{file}','POST',{})
        self.assertEqual(self.request(f'/api/opportunity_files/{file}'),b'original file')

    def test_atomic_batch_failure_leaves_no_followup_or_file(self):
        identifier = self.lead()
        before_files = set(self.server.uploads.iterdir())
        self.request('/api/agent/submit','POST',{'request_id':'failed-opportunity-batch','operations':[
            {'method':'POST','path':'/api/conversations','data':{'opportunity_id':identifier,'owner_id':1,'text':'待回滚','occurred_at':'2026-01-01T10:00'}},
            {'method':'POST','path':'/api/opportunity_files','data':{'opportunity_id':identifier,'owner_id':1,'name':'rollback','role':'attachment','content':'eA=='}},
            {'method':'PATCH','path':f'/api/opportunities/{identifier}','data':{'phase':'invalid'}},
        ]},400)
        self.assertEqual(self.opportunity(identifier)['phase'],'lead')
        self.assertEqual(self.pending(identifier),[])
        self.assertEqual(set(self.server.uploads.iterdir()),before_files)

    def test_reassignment_preserves_due_and_completed_owner_check(self):
        identifier = self.proposal()
        before = self.opportunity(identifier)['next_followup_at']
        self.request(f'/api/opportunities/{identifier}','PATCH',{'owner_id':2})
        self.assertEqual(self.opportunity(identifier)['next_followup_at'],before)
        task=self.pending(identifier)[0]
        self.assertEqual(task['owner_id'],2)
        self.request(f'/api/tasks/{task["id"]}','PATCH',{'done':True},400)

    def test_migration_is_repeatable_and_preserves_accounts(self):
        before = self.state()
        initialize_database(self.server.database)
        initialize_database(self.server.database)
        self.assertEqual(before['users'],self.state()['users'])
        self.assertEqual(len(before['opportunities']),len(self.state()['opportunities']))

    def test_unknown_actual_time_does_not_invent_schedule(self):
        identifier = self.lead()
        self.request('/api/conversations','POST',{'opportunity_id':identifier,'owner_id':1,'text':'先存原文，发生时间待确认'})
        self.assertEqual(self.pending(identifier),[])
        self.assertEqual(self.opportunity(identifier)['phase'],'lead')

    def test_same_conversation_key_different_content_is_conflict(self):
        identifier = self.lead()
        self.communication(identifier,client_key='same-key')
        self.request('/api/conversations','POST',{'opportunity_id':identifier,'owner_id':1,'text':'不同原文','requirements':'已确认完整需求','occurred_at':'2026-01-01T10:30:00+08:00','client_key':'same-key'},409)

    def test_manual_followup_schedule_updates_card_and_undo_is_reversible(self):
        identifier=self.proposal()
        task=self.pending(identifier)[0]
        self.request(f'/api/tasks/{task["id"]}','PATCH',{'start_date':'2026-01-12','end_date':'2026-01-12','time':'14:00'})
        self.assertEqual(self.opportunity(identifier)['next_followup_at'],'2026-01-12T14:00:00+08:00')
        self.request(f'/api/tasks/{task["id"]}','PATCH',{'done':True})
        self.assertEqual(len(self.pending(identifier)),1)
        self.request(f'/api/tasks/{task["id"]}','PATCH',{'done':False})
        self.assertEqual(len(self.pending(identifier)),1)
        self.assertEqual(self.pending(identifier)[0]['id'],task['id'])

    def test_history_before_final_submission_does_not_replace_final_requirements(self):
        identifier=self.proposal()
        self.communication(identifier,occurred_at='2026-01-01T10:30:00+08:00',requirements='较早的需求')
        self.assertEqual(self.opportunity(identifier)['requirements'],'已确认范围和需求')
        self.assertEqual(self.pending(identifier)[0]['start_date'],'2026-01-05')

    def test_cli_downloads_exact_original_without_overwrite(self):
        import subprocess
        import sys
        from pathlib import Path
        identifier=self.lead()
        document=self.upload(identifier,'attachment')
        token=self.request('/api/agent-token','POST',{})['token']
        config=Path(self.directory.name)/'cli-connection.json'
        config.write_text(json.dumps({'url':f'http://127.0.0.1:{self.server.server_port}','token':token}),encoding='utf-8')
        destination=Path(self.directory.name)/'downloaded.txt'
        script=Path(__file__).resolve().parents[1]/'codex-skill/scripts/modus.py'
        args=[sys.executable,str(script),'--config',str(config),'download',f'/api/opportunity_files/{document}',str(destination)]
        result=subprocess.run(args,capture_output=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(destination.read_bytes(),b'original file')
        self.assertEqual(subprocess.run(args,capture_output=True).returncode,1)

    def test_moving_back_to_communication_or_lead_resets_pending_schedule(self):
        identifier=self.lead()
        self.communication(identifier)
        self.proposal(identifier)
        self.request(f'/api/opportunities/{identifier}','PATCH',{'phase':'communication'})
        self.assertEqual(self.pending(identifier)[0]['start_date'],'2026-01-02')
        self.request(f'/api/opportunities/{identifier}','PATCH',{'phase':'lead'})
        self.assertEqual(self.pending(identifier),[])
        self.assertFalse(self.opportunity(identifier)['next_followup_at'])


if __name__ == '__main__':
    unittest.main()
