import base64
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_backend import Client
from server import WorkbenchServer, connect_database, initialize_database
import trash_bin


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = WorkbenchServer(('127.0.0.1',0),self.temporary.name)
        self.server.trash_next_sweep = float('inf')
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = Client(self.url)
        status,body,_ = self.client.request('/api/setup','POST',{'accounts':[{'name':'甲','username':'a','password':'abcdefgh'},{'name':'乙','username':'b','password':'ijklmnop'}]})
        self.assertEqual(status,200,body);self.client.csrf=body['csrf']

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3);self.temporary.cleanup()

    def create(self, table, **values):
        defaults = {'tasks':{'title':'待办','owner_id':1,'start_date':'','end_date':''},'projects':{'name':'项目','owner_id':1,'start_date':'','end_date':''},'opportunities':{'name':'意向','owner_id':1}}
        status,body,_ = self.client.request('/api/'+table,'POST',{**defaults.get(table,{}),**values})
        self.assertEqual(status,200,body);return body['id']

    def recycle(self, table, identifier):
        status,body,_ = self.client.request(f'/api/{table}/{identifier}','DELETE',{})
        self.assertEqual(status,200,body)

    def preview(self, **scope):
        status,body,_=self.client.request('/api/trash/preview','POST',scope)
        self.assertEqual(status,200,body);return body

    def purge(self, scope, preview=None):
        preview = preview or self.preview(**scope)
        return self.client.request('/api/trash/purge','POST',{**scope,'confirm':True,'preview_token':preview['preview_token']})

    def age(self, table, identifier, days, now=None):
        now=now or datetime.now(trash_bin.TZ)
        with connect_database(self.server.database) as db:db.execute(f'UPDATE {table} SET deleted_at=? WHERE id=?',(trash_bin.stamp(now-timedelta(days=days)),identifier))

    def exists(self, table, identifier):
        with connect_database(self.server.database) as db:return db.execute(f'SELECT * FROM {table} WHERE id=?',(identifier,)).fetchone()

    def document(self, project):
        with connect_database(self.server.database) as db:section=db.execute('SELECT id FROM sections WHERE project_id=? LIMIT 1',(project,)).fetchone()[0]
        identifier=self.create('documents',section_id=section,owner_id=1,name='需求.txt',content=base64.b64encode(b'owned-file').decode())
        return identifier,self.server.uploads/self.exists('documents',identifier)['storage_name']

    def test_retention_restore_and_redelete(self):
        task=self.create('tasks');self.recycle('tasks',task)
        _,body,_=self.client.request('/api/trash')
        record=body['items']['tasks'][0]
        self.assertEqual(datetime.fromisoformat(record['expires_at'])-datetime.fromisoformat(record['deleted_at']),timedelta(days=30))
        self.age('tasks',task,35)
        self.assertEqual(self.client.request(f'/api/restore/tasks/{task}','POST',{})[0],200)
        self.assertEqual(self.exists('tasks',task)['deleted_at'],'')
        self.recycle('tasks',task)
        self.assertGreater(datetime.fromisoformat(self.exists('tasks',task)['deleted_at']),datetime.now(trash_bin.TZ)-timedelta(seconds=5))

    def test_legacy_deletes_get_full_window_idempotently(self):
        task=self.create('tasks');self.recycle('tasks',task)
        with connect_database(self.server.database) as db:db.execute("UPDATE tasks SET deleted_at='' WHERE id=?",(task,))
        initialize_database(self.server.database)
        first=self.exists('tasks',task)['deleted_at'];initialize_database(self.server.database)
        self.assertEqual(first,self.exists('tasks',task)['deleted_at'])
        self.assertGreater(datetime.fromisoformat(first),datetime.now(trash_bin.TZ)-timedelta(seconds=5))
        self.assertEqual(trash_bin.sweep(self.server)['roots_removed'],0)

    def test_exact_expiry_and_normal_archives_survive(self):
        now=datetime.now(trash_bin.TZ).replace(microsecond=0)
        task=self.create('tasks');self.recycle('tasks',task)
        with connect_database(self.server.database) as db:db.execute('UPDATE tasks SET deleted_at=? WHERE id=?',(trash_bin.stamp(now-timedelta(days=30)+timedelta(seconds=1)),task))
        self.assertEqual(trash_bin.sweep(self.server,now)['roots_removed'],0)
        project=self.create('projects');opportunity=self.create('opportunities')
        with connect_database(self.server.database) as db:
            db.execute('UPDATE projects SET completed_at=?,final_payment_at=? WHERE id=?',(trash_bin.stamp(now),trash_bin.stamp(now),project))
            db.execute('INSERT INTO project_archives VALUES(?,?,?)',(project,1,trash_bin.stamp(now)))
            db.execute("UPDATE opportunities SET outcome='failed',failure_reason='预算原因' WHERE id=?",(opportunity,))
        self.age('tasks',task,30,now)
        self.assertEqual(trash_bin.sweep(self.server,now)['roots_removed'],1)
        self.assertIsNone(self.exists('tasks',task));self.assertIsNotNone(self.exists('projects',project));self.assertIsNotNone(self.exists('opportunities',opportunity))
        with connect_database(self.server.database) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM project_archives').fetchone()[0],1)

    def test_project_cascade_history_disk_and_linked_opportunity(self):
        project=self.create('projects');other=self.create('projects',name='保留的项目')
        document,path=self.document(project)
        task=self.create('tasks',project_id=project)
        self.assertEqual(self.client.request(f'/api/tasks/{task}','PATCH',{'done':True})[0],200)
        conversation=self.create('conversations',project_id=project,owner_id=1,text='项目原文')
        opportunity=self.create('opportunities')
        with connect_database(self.server.database) as db:
            stage=db.execute('SELECT id FROM stages WHERE project_id=? LIMIT 1',(project,)).fetchone()[0]
            db.execute('INSERT INTO stage_history(stage_id,snapshot,saved_at,actor_id) VALUES(?,?,?,?)',(stage,'{}',trash_bin.stamp(),1))
            db.execute('INSERT INTO conversation_history(conversation_id,revision,snapshot,saved_at) VALUES(?,?,?,?)',(conversation,0,'{}',trash_bin.stamp()))
            db.execute("UPDATE opportunities SET linked_project_id=?,outcome='converted' WHERE id=?",(project,opportunity))
        self.recycle('projects',project)
        scope={'table':'projects','id':project};preview=self.preview(**scope)
        self.assertEqual(preview['counts']['tasks'],1);self.assertEqual(preview['file_count'],1)
        status,result,_=self.purge(scope,preview);self.assertEqual(status,200,result)
        for table,identifier in [('projects',project),('tasks',task),('documents',document),('conversations',conversation)]:self.assertIsNone(self.exists(table,identifier))
        self.assertFalse(path.exists());self.assertIsNotNone(self.exists('projects',other))
        self.assertIsNone(self.exists('opportunities',opportunity)['linked_project_id'])
        with connect_database(self.server.database) as db:
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stage_history').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM completions').fetchone()[0],0)

    def test_conversation_shared_with_normal_opportunity_is_retained(self):
        project=self.create('projects');opportunity=self.create('opportunities')
        conversation=self.create('conversations',project_id=project,owner_id=1,text='两者共用的记录')
        with connect_database(self.server.database) as db:db.execute('UPDATE conversations SET opportunity_id=? WHERE id=?',(opportunity,conversation))
        self.recycle('projects',project)
        status,result,_=self.purge({'table':'projects','id':project});self.assertEqual(status,200,result)
        record=self.exists('conversations',conversation)
        self.assertIsNotNone(record);self.assertIsNone(record['project_id']);self.assertEqual(record['opportunity_id'],opportunity)

    def test_opportunity_purge_keeps_its_live_project(self):
        project=self.create('projects');opportunity=self.create('opportunities')
        task=self.create('tasks',opportunity_id=opportunity)
        conversation=self.create('conversations',opportunity_id=opportunity,owner_id=1,text='实际沟通',occurred_at=trash_bin.stamp())
        attachment=self.create('opportunity_files',opportunity_id=opportunity,owner_id=1,name='报价.txt',role='quote',content=base64.b64encode(b'quote').decode())
        with connect_database(self.server.database) as db:db.execute("UPDATE opportunities SET linked_project_id=?,outcome='converted' WHERE id=?",(project,opportunity))
        path=self.server.uploads/self.exists('opportunity_files',attachment)['storage_name']
        self.recycle('opportunities',opportunity)
        status,result,_=self.purge({'table':'opportunities','id':opportunity});self.assertEqual(status,200,result)
        self.assertIsNotNone(self.exists('projects',project));self.assertIsNone(self.exists('tasks',task));self.assertIsNone(self.exists('conversations',conversation));self.assertFalse(path.exists())

    def test_clear_all_only_trash_and_shared_files_survive(self):
        project=self.create('projects');document,path=self.document(project)
        opportunity=self.create('opportunities')
        attachment=self.create('opportunity_files',opportunity_id=opportunity,owner_id=1,name='共用.txt',role='attachment',content=base64.b64encode(b'another').decode())
        old=self.server.uploads/self.exists('opportunity_files',attachment)['storage_name'];old.unlink()
        with connect_database(self.server.database) as db:db.execute('UPDATE opportunity_files SET storage_name=? WHERE id=?',(path.name,attachment))
        self.recycle('documents',document)
        live_task=self.create('tasks',title='正常工作记录')
        scope={'all':True};preview=self.preview(**scope)
        self.assertEqual(preview['shared_files_preserved'],1);self.assertEqual(preview['file_count'],0)
        status,body,_=self.purge(scope,preview);self.assertEqual(status,200,body)
        self.assertTrue(path.exists());self.assertIsNotNone(self.exists('tasks',live_task));self.assertIsNotNone(self.exists('opportunities',opportunity))
        self.recycle('opportunity_files',attachment)
        self.assertEqual(self.purge({'all':True})[0],200);self.assertFalse(path.exists())

    def test_confirmation_required_active_records_and_bad_types_rejected(self):
        task=self.create('tasks');scope={'table':'tasks','id':task}
        self.assertEqual(self.client.request('/api/trash/preview','POST',scope)[0],409)
        self.recycle('tasks',task)
        self.assertEqual(self.client.request('/api/trash/purge','POST',scope)[0],400)
        self.assertEqual(self.client.request('/api/trash/preview','POST',{'table':'users','id':1})[0],400)
        self.assertIsNotNone(self.exists('tasks',task))

    def test_changed_scope_and_restore_races_refuse_old_confirmation(self):
        project=self.create('projects');self.recycle('projects',project)
        scope={'table':'projects','id':project};preview=self.preview(**scope)
        # Another owner can have added a dependent record before the pending confirm.
        with connect_database(self.server.database) as db:
            db.execute("INSERT INTO tasks(title,owner_id,project_id,start_date,end_date,created_at) VALUES('新关联',1,?,'','','')",(project,))
        self.assertEqual(self.purge(scope,preview)[0],409);self.assertIsNotNone(self.exists('projects',project))
        task=self.create('tasks');self.recycle('tasks',task)
        scope={'table':'tasks','id':task};preview=self.preview(**scope)
        self.assertEqual(self.client.request(f'/api/restore/tasks/{task}','POST',{})[0],200)
        self.assertEqual(self.purge(scope,preview)[0],409);self.assertEqual(self.exists('tasks',task)['deleted'],0)

    def test_confirmation_bound_to_actor_and_timeout(self):
        task=self.create('tasks');self.recycle('tasks',task)
        scope={'table':'tasks','id':task};preview=self.preview(**scope)
        other=Client(self.url);status,body,_=other.request('/api/login','POST',{'username':'b','password':'ijklmnop'});other.csrf=body['csrf']
        self.assertEqual(other.request('/api/trash/purge','POST',{**scope,'confirm':True,'preview_token':preview['preview_token']})[0],409)
        with patch('trash_bin.time.time',return_value=time.time()+301):self.assertEqual(self.purge(scope,preview)[0],409)
        self.assertIsNotNone(self.exists('tasks',task))

    def test_recent_parent_keeps_full_recovery_window_and_restore_order(self):
        project=self.create('projects');document,path=self.document(project)
        self.recycle('documents',document);self.age('documents',document,45)
        self.recycle('projects',project)
        self.assertEqual(trash_bin.sweep(self.server)['roots_removed'],0)
        _,listing,_=self.client.request('/api/trash')
        self.assertEqual(listing['items']['documents'][0]['expires_at'],listing['items']['projects'][0]['expires_at'])
        self.assertIn('所属项目',listing['items']['documents'][0]['retention_basis'])
        self.assertTrue(path.exists())
        self.assertEqual(self.client.request(f'/api/restore/documents/{document}','POST',{})[0],400)
        self.assertEqual(self.client.request(f'/api/restore/projects/{project}','POST',{})[0],200)
        self.assertEqual(self.client.request(f'/api/restore/documents/{document}','POST',{})[0],200)

    def test_disk_lock_is_durably_retried_and_rollback_keeps_original(self):
        project=self.create('projects');document,path=self.document(project)
        self.recycle('documents',document);scope={'table':'documents','id':document};preview=self.preview(**scope)
        original_apply=trash_bin.apply
        def fail_after_database_changes(connection,value):original_apply(connection,value);raise ValueError('模拟提交失败')
        with patch('trash_bin.apply',side_effect=fail_after_database_changes):self.assertEqual(self.purge(scope,preview)[0],400)
        self.assertTrue(path.exists());self.assertIsNotNone(self.exists('documents',document))
        original_unlink=Path.unlink
        def locked(target,*args,**kwargs):
            if target==path:raise PermissionError('locked fixture')
            return original_unlink(target,*args,**kwargs)
        with patch.object(Path,'unlink',locked):
            status,body,_=self.purge(scope,preview);self.assertEqual(status,200,body);self.assertEqual(body['pending_files'],1)
        self.assertIsNone(self.exists('documents',document));self.assertTrue(path.exists())
        self.assertEqual(trash_bin.collect_files(self.server)['pending_files'],0);self.assertFalse(path.exists())

    def test_ids_are_not_reused_and_large_cascade_is_chunked(self):
        task=self.create('tasks');self.recycle('tasks',task);self.assertEqual(self.purge({'all':True})[0],200)
        new_task=self.create('tasks');self.assertGreater(new_task,task)
        self.assertEqual(self.client.request(f'/api/tasks/{task}','PATCH',{'title':'旧编号误操作'})[0],400)
        project=self.create('projects')
        with connect_database(self.server.database) as db:
            db.executemany("INSERT INTO tasks(title,owner_id,project_id,start_date,end_date,created_at) VALUES(?,1,?,'','','')",[(f'条目{i}',project) for i in range(510)])
        self.recycle('projects',project)
        scope={'table':'projects','id':project};preview=self.preview(**scope);self.assertEqual(preview['counts']['tasks'],510)
        self.assertEqual(self.purge(scope,preview)[0],200);self.assertIsNotNone(self.exists('tasks',new_task))

    def test_unsafe_file_path_refused_without_touching_disk(self):
        project=self.create('projects');document,path=self.document(project)
        self.recycle('documents',document)
        with connect_database(self.server.database) as db:db.execute('UPDATE documents SET storage_name=? WHERE id=?',('../escape',document))
        self.assertEqual(self.client.request('/api/trash/preview','POST',{'table':'documents','id':document})[0],400)
        self.assertTrue(path.exists());self.assertIsNotNone(self.exists('documents',document))

    def test_automatic_service_runs_without_user_request_and_is_throttled(self):
        task=self.create('tasks');self.recycle('tasks',task);self.age('tasks',task,31)
        self.server.trash_next_sweep=0
        trash_bin.service_actions(self.server)
        self.server.trash_worker.join(5)
        self.assertIsNone(self.exists('tasks',task))
        with patch('trash_bin.sweep') as sweep:
            for _ in range(100):trash_bin.service_actions(self.server)
            sweep.assert_not_called()

    def test_recent_parent_children_do_not_starve_other_expired_roots(self):
        project=self.create('projects')
        with connect_database(self.server.database) as db:
            old=trash_bin.stamp(datetime.now(trash_bin.TZ)-timedelta(days=40))
            db.executemany("INSERT INTO tasks(title,owner_id,project_id,start_date,end_date,created_at,deleted,deleted_at) VALUES(?,1,?,'','','',1,?)",[(f'等待父级{i}',project,old) for i in range(55)])
        self.recycle('projects',project)
        independent=self.create('tasks');self.recycle('tasks',independent);self.age('tasks',independent,35)
        self.assertEqual(trash_bin.sweep(self.server)['roots_removed'],1)
        self.assertIsNone(self.exists('tasks',independent));self.assertIsNotNone(self.exists('projects',project))


if __name__=='__main__':unittest.main(verbosity=2)
