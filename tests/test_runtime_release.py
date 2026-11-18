"""Runtime release admission must fail before downloading untrusted input."""
import copy
import importlib.util
import io
import hashlib
import pathlib
import tempfile
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().parents[1] / 'docker/runtime/release.py'

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PATH.exists(), 'missing runtime release admission')
        spec = importlib.util.spec_from_file_location('runtime_release', PATH)
        assert spec and spec.loader
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def release(self):
        tag = 'v5.3.0'
        names = ['manylinux_2_39_aarch64', 'manylinux_2_39_x86_64', 'macosx_13_0_arm64', 'win_amd64']
        return {'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': [
            {'name': f'bpy-5.3.0-cp313-cp313-{p}.whl', 'digest': 'sha256:' + 'a'*64,
             'state': 'uploaded', 'size': 10} for p in names]}

    def test_complete_release_and_fail_closed(self):
        release = self.release()
        lock = self.module.admit(release)
        self.assertEqual(lock['version'], '5.3.0')
        self.assertEqual(set(lock['wheels']), {'arm64', 'amd64'})
        mutations = [lambda r: r.update(tag_name='v5.3.0;echo BAD'),
                     lambda r: r.update(tag_name='v5.4.0'),
                     lambda r: r.update(draft=True),
                     lambda r: r.update(prerelease=True),
                     lambda r: r['assets'].pop(),
                     lambda r: r['assets'][0].update(digest=None),
                     lambda r: r['assets'][0].update(state='new'),
                     lambda r: r['assets'].append(r['assets'][0])]
        for mutate in mutations:
            bad = copy.deepcopy(release)
            mutate(bad)
            with self.assertRaises(ValueError):
                self.module.admit(bad)

    def test_bad_tag_never_requests_api(self):
        with patch.object(self.module, 'api') as api:
            for tag in ('v5.3.0;echo BAD', '../../other', 'v5.4.0', 'main', 'v5.3.01'):
                with self.assertRaises(ValueError):
                    self.module.resolve(tag)
            api.assert_not_called()

    def test_download_checks_bytes_and_cleans_partial_files(self):
        lock = self.module.admit(self.release())
        data = b'real test payload'
        lock['wheels']['arm64'].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            with patch.object(self.module, 'resolve', return_value=lock):
                with patch.object(self.module.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                    result = self.module.download(lock, 'arm64', directory)
                    self.assertEqual(result.read_bytes(), data)
                    result.unlink()
                for corrupt in (b'x'*len(data), b'short', data+b'excess'):
                    with patch.object(self.module.urllib.request, 'urlopen', return_value=io.BytesIO(corrupt)):
                        with self.assertRaises(ValueError):
                            self.module.download(lock, 'arm64', directory)
                        self.assertEqual(list(directory.iterdir()), [])

    def test_changed_release_never_downloads(self):
        lock = self.module.admit(self.release())
        changed = copy.deepcopy(lock)
        changed['wheels']['arm64']['sha256'] = 'b'*64
        with patch.object(self.module, 'resolve', return_value=changed), patch.object(self.module.urllib.request, 'urlopen') as download:
            with self.assertRaises(ValueError):
                self.module.download(lock, 'arm64', pathlib.Path('unused'))
            download.assert_not_called()

if __name__ == '__main__':
    unittest.main()
