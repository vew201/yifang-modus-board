import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('modus_source_guard',Path(__file__).resolve().parents[1]/'scripts/check_repository.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


class SourceBoundaryTests(unittest.TestCase):
    def test_data_and_connection_files_are_rejected_without_printing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);files=[]
            for name in ['data/customer.txt','connection.json','.env','project.sqlite3']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture-private-value',encoding='utf-8');files.append(path)
            with patch.object(guard,'ROOT',root):problems=guard.inspect(files)
            self.assertEqual(len(problems),4);self.assertNotIn('fixture-private-value',''.join(problems))

    def test_detected_api_secret_is_not_echoed_and_source_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);safe=root/'server.py';unsafe=root/'notes.txt'
            safe.write_text("token = generate_token()",encoding='utf-8');value='ghp_'+'x'*36;unsafe.write_text(value,encoding='utf-8')
            with patch.object(guard,'ROOT',root):problems=guard.inspect([safe,unsafe])
            self.assertEqual(len(problems),1);self.assertNotIn(value,problems[0])
