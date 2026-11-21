import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from server import initialize_database


class LocalDeployTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.base=Path(self.directory.name).resolve()
        self.runtime=self.base/'runtime';self.source=self.base/'source';self.runtime.mkdir();self.source.mkdir()
        initialize_database(self.runtime/'data/modus.sqlite3')
        script=Path(__file__).resolve().parents[1]/'scripts/deploy_local.py'
        spec=importlib.util.spec_from_file_location('modus_deploy_test',script)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.module.ROOT=self.runtime;self.module.BASE=self.source
        self.module.FILES=['server.py','static/app.js']
        for name in self.module.FILES:
            for base,content in [(self.source,b'# new reviewed source\n'),(self.runtime,b'# previous source\n')]:
                target=base/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)

    def tearDown(self):self.directory.cleanup()

    def test_apply_and_rollback_preserve_data_without_real_process_management(self):
        for fail in (False,True):
            fake=Mock();fake.poll.return_value=None
            before=(self.runtime/'data/modus.sqlite3').read_bytes()
            with patch.dict('os.environ',{'MODUS_DEPLOY_REVISION':'a'*40}),patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(stdout='a'*40)),patch.object(self.module,'backend',side_effect=[42,None,None] if fail else [42]),patch.object(self.module,'stop_backend'),patch.object(self.module,'start_backend',return_value=fake),patch.object(self.module,'wait_ready',side_effect=RuntimeError('test failure') if fail else None),patch.object(self.module.Path,'home',return_value=self.base/'no-client'):
                if fail:
                    with self.assertRaises(RuntimeError):self.module.apply()
                else:
                    self.assertEqual(self.module.apply(),0)
                    self.assertEqual(json.loads((self.runtime/'data/deployment-state.json').read_text(encoding='utf-8'))['revision'],'a'*40)
            self.assertEqual(before,(self.runtime/'data/modus.sqlite3').read_bytes())

    def test_manifest_cannot_overwrite_private_runtime_data(self):
        self.module.FILES=['data/modus.sqlite3']
        with self.assertRaises(RuntimeError):self.module.verify()

    def test_an_unverified_revision_does_not_copy_source(self):
        with patch.dict('os.environ',{'MODUS_DEPLOY_REVISION':'a'*40}),patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(stdout='b'*40)):
            with self.assertRaises(RuntimeError):self.module.apply()
        self.assertEqual((self.runtime/'server.py').read_bytes(),b'# previous source\n')
