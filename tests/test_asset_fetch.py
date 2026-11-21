import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('modus_assets_test',Path(__file__).resolve().parents[1]/'scripts/fetch_assets.py')
assets=importlib.util.module_from_spec(spec);spec.loader.exec_module(assets)


class AssetFetchTests(unittest.TestCase):
    def test_bad_checksum_keeps_existing_file_and_valid_download_is_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'static/assets/font.ttf';path.parent.mkdir(parents=True);path.write_bytes(b'old')
            asset={'path':'static/assets/font.ttf','url':'https://raw.githubusercontent.com/owner/repo/commit/file','size':3,'sha256':hashlib.sha256(b'new').hexdigest()}
            with patch.object(assets,'ROOT',root):
                with self.assertRaises(ValueError):assets.fetch(asset,lambda *args,**kwargs:io.BytesIO(b'bad'))
                self.assertEqual(path.read_bytes(),b'old')
                self.assertTrue(assets.fetch(asset,lambda *args,**kwargs:io.BytesIO(b'new')))
                self.assertFalse(assets.fetch(asset,lambda *args,**kwargs:self.fail('cached file must not download')))

    def test_download_cannot_escape_assets(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(assets,'ROOT',Path(temporary)):
            with self.assertRaises(ValueError):assets.fetch({'path':'../private','url':'https://raw.githubusercontent.com/x/y/file'})
