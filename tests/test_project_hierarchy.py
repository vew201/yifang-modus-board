import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from test_backend import Client
from server import DEFAULT_STAGE_NAMES, WorkbenchServer, initialize_database


class ProjectHierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = WorkbenchServer(("127.0.0.1", 0), cls.directory.name)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.client = Client(f"http://127.0.0.1:{cls.server.server_port}")
        status, session, _ = cls.client.request("/api/setup", "POST", {"accounts": [
            {"name": "甲", "username": "a", "password": "abcdefgh"},
            {"name": "乙", "username": "b", "password": "ijklmnop"},
        ]})
        assert status == 200
        cls.client.csrf = session["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)
        cls.directory.cleanup()

    def snapshot(self):
        return self.client.request("/api/state")[1]

    def create_project(self):
        status, result, _ = self.client.request("/api/projects", "POST", {"name": "目录测试项目", "owner_id": 1, "start_date": "2026-10-01", "end_date": "2027-05-01"})
        self.assertEqual(status, 200, result)
        stages = [stage for stage in self.snapshot()["stages"] if stage["project_id"] == result["id"]]
        self.assertEqual([stage["name"] for stage in stages], DEFAULT_STAGE_NAMES)
        return result["id"], stages

    def task(self, project, stage=None, title="子任务"):
        status, result, _ = self.client.request("/api/tasks", "POST", {"title": title, "owner_id": 1, "project_id": project, "stage_id": stage, "start_date": "", "end_date": ""})
        self.assertEqual(status, 200, result)
        return result["id"]

    def complete(self, identifier, done=True):
        task = next(task for task in self.snapshot()["tasks"] if task["id"] == identifier)
        self.assertEqual(self.client.request(f"/api/tasks/{identifier}", "PATCH", {"done": done, "revision": task["revision"]})[0], 200)

    def project(self, identifier):
        return next(project for project in self.snapshot()["projects"] if project["id"] == identifier)

    def stage(self, identifier):
        return next(stage for stage in self.snapshot()["stages"] if stage["id"]==identifier)

    def completed_project(self):
        project,stages=self.create_project()
        tasks=[self.task(project,stage["id"]) for stage in stages]
        status,body,_=self.client.request(f"/api/projects/{project}","PATCH",{"development_done":True,"completion_note":"实际开发成果已经核对"})
        self.assertEqual(status,200,body)
        self.assertTrue(self.project(project)["completed_at"])
        return project,stages,tasks

    def test_tasks_do_not_complete_stage_or_project(self):
        project,stages=self.create_project()
        tasks=[self.task(project,stage["id"]) for stage in stages]
        for task in tasks:self.complete(task)
        self.assertTrue(all(not self.stage(x["id"])["completed_at"] for x in stages))
        self.assertIsNone(self.project(project)["completed_at"])

    def test_stage_complete_with_pending_tasks_and_requires_evidence(self):
        project,stages=self.create_project()
        task=self.task(project,stages[2]["id"])
        path=f"/api/stages/{stages[2]['id']}"
        self.assertEqual(self.client.request(path,"PATCH",{"status":"completed"})[0],400)
        status,body,_=self.client.request(path,"PATCH",{"status":"completed","completion_note":"客户已确认需求范围","work_summary":"范围和验收标准已明确"})
        self.assertEqual(status,200,body)
        self.assertTrue(self.stage(stages[2]["id"])["completed_at"])
        self.assertFalse(next(x for x in self.snapshot()["tasks"] if x["id"]==task)["done"])
        self.complete(task);self.complete(task,False)
        self.assertEqual(self.stage(stages[2]["id"])["status"],"completed")
        self.assertIsNone(self.project(project)["completed_at"])

    def test_fixed_stage_names_order_count_cannot_change(self):
        project,stages=self.create_project()
        self.assertEqual(len(stages),8)
        self.assertEqual(self.client.request('/api/stages','POST',{"project_id":project,"name":"额外阶段"})[0],400)
        for data in [{"name":"改名"},{"position":99},{"project_id":999}]:
            self.assertEqual(self.client.request(f"/api/stages/{stages[0]['id']}",'PATCH',data)[0],400)
        self.assertEqual(self.client.request(f"/api/stages/{stages[0]['id']}",'DELETE',{})[0],400)

    def test_project_completion_is_explicit_and_reversible(self):
        project,stages,tasks=self.completed_project()
        self.assertTrue(any(x['project_id']==project for x in self.snapshot()['project_archives']))
        self.task(project,stages[0]['id'],'新待办')
        self.complete(tasks[0]);self.complete(tasks[0],False)
        self.assertTrue(self.project(project)['completed_at'])
        self.assertEqual(self.client.request(f'/api/projects/{project}','PATCH',{'development_done':False})[0],200)
        self.assertIsNone(self.project(project)['completed_at'])
        self.assertFalse(any(x['project_id']==project for x in self.snapshot()['project_archives']))

    def test_stage_must_belong_to_project(self):
        project,_=self.create_project();_,other=self.create_project()
        self.assertEqual(self.client.request('/api/tasks','POST',{'title':'错误关联','owner_id':1,'project_id':project,'stage_id':other[0]['id'],'start_date':'','end_date':''})[0],400)
        self.task(project,None,'无需关联阶段也能保存')

    def test_stage_history_conflict_and_reopen(self):
        project,stages=self.create_project();identifier=stages[2]['id']
        path=f'/api/stages/{identifier}'
        self.assertEqual(self.client.request(path,'PATCH',{'status':'completed','completion_note':'需求已确认','revision':0})[0],200)
        self.assertEqual(self.client.request(path,'PATCH',{'status':'pending','revision':0})[0],409)
        self.assertEqual(self.client.request(path,'PATCH',{'status':'in_progress','completion_note':'需求变更需重新澄清','revision':1})[0],200)
        self.assertIsNone(self.stage(identifier)['completed_at'])
        self.assertEqual(len([x for x in self.snapshot()['stage_history'] if x['stage_id']==identifier]),2)

    def test_recycle_restore_task_does_not_change_completion(self):
        project,stages,tasks=self.completed_project()
        self.assertEqual(self.client.request(f'/api/tasks/{tasks[0]}','DELETE',{})[0],200)
        self.assertEqual(self.client.request(f'/api/restore/tasks/{tasks[0]}','POST',{})[0],200)
        self.assertTrue(self.project(project)['completed_at'])

    def test_restart_preserves_explicit_states(self):
        project,stages,_=self.completed_project()
        path=f"/api/stages/{stages[3]['id']}"
        self.assertEqual(self.client.request(path,'PATCH',{'status':'completed','completion_note':'方案与排期已对齐'})[0],200)
        before=self.project(project)['completed_at']
        initialize_database(self.server.database)
        self.assertEqual(self.project(project)['completed_at'],before)
        self.assertEqual(len([x for x in self.snapshot()['stages'] if x['project_id']==project]),8)
        self.assertEqual(self.stage(stages[3]['id'])['status'],'completed')

    def test_legacy_database_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,name TEXT,password_hash TEXT);
                    CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT,client TEXT,description TEXT,owner_id INTEGER,start_date TEXT,end_date TEXT,color TEXT,created_at TEXT);
                    CREATE TABLE stages(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT,start_date TEXT,end_date TEXT,position INTEGER);
                    CREATE TABLE tasks(id INTEGER PRIMARY KEY,title TEXT,description TEXT,owner_id INTEGER,project_id INTEGER,opportunity_id INTEGER,kind TEXT,start_date TEXT,end_date TEXT,time TEXT,done INTEGER,created_at TEXT);
                    INSERT INTO users VALUES(1,'demo','demo','test');
                    INSERT INTO projects VALUES(1,'示例项目','','',1,'2026-01-01','2027-01-01','#7864A5','2026-01-01');
                    INSERT INTO stages VALUES(1,1,'需求与方案','2026-01-01','2026-04-01',0);
                    INSERT INTO stages VALUES(2,1,'开发与测试','2026-04-02','2026-08-01',1);
                    INSERT INTO stages VALUES(3,1,'交付与培训','2026-08-02','2027-01-01',2);
                    INSERT INTO tasks VALUES(1,'制作项目需求书','',1,1,NULL,'daily','2026-01-01','2026-01-02','',1,'2026-01-01');
                """)
            connection.close()
            initialize_database(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT s.name,t.done FROM tasks t JOIN stages s ON s.id=t.stage_id WHERE t.id=1").fetchone(), ("需求澄清", 1))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM stages WHERE deleted=0").fetchone()[0], 8)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_model_migrations").fetchone()[0], 1)
                self.assertIsNone(connection.execute("SELECT completed_at FROM projects WHERE id=1").fetchone()[0])
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
