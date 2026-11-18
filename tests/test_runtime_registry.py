"""Registry boundary regressions; fixtures are not hosted publication evidence."""
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('registry', ROOT / 'docker/runtime/registry.py')
assert spec and spec.loader
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
SOURCE, FINGERPRINT = 'a' * 40, 'b' * 64


class RegistryTests(unittest.TestCase):
    def matrix(self):
        return {'include': [{'tag': 'v5.3.0', 'arch': arch, 'fingerprint': FINGERPRINT}
                            for arch in ('amd64', 'arm64')]}

    def test_input_bound_tag_changes_with_source_or_wheels(self):
        tag = registry.candidate_tag(SOURCE, FINGERPRINT)
        self.assertNotEqual(tag, registry.candidate_tag('c' * 40, FINGERPRINT))
        self.assertNotEqual(tag, registry.candidate_tag(SOURCE, 'd' * 64))
        self.assertLessEqual(len(tag + '-arm64'), 128)
        with self.assertRaises(ValueError):
            registry.candidate_tag('main', FINGERPRINT)

    def test_pr_does_not_skip_even_public_image(self):
        with patch.object(registry, 'verify') as verify:
            result = registry.plan(self.matrix(), SOURCE, False)
            verify.assert_not_called()
        self.assertEqual(len(result['build']['include']), 2)
        self.assertEqual(len(result['publish']['include']), 1)

    def test_only_live_anonymous_verified_index_skips(self):
        with patch.object(registry, 'verify', return_value={'anonymous': True}):
            self.assertEqual(registry.plan(self.matrix(), SOURCE, True)['build']['include'], [])
        for error in [urllib.error.URLError('missing or private'),
                      ValueError('wrong provenance'), KeyError('config')]:
            with self.subTest(error=error), patch.object(registry, 'verify', side_effect=error):
                self.assertEqual(len(registry.plan(self.matrix(), SOURCE, True)['build']['include']), 2)

    def test_both_legs_required(self):
        matrix = self.matrix()
        matrix['include'].pop()
        with self.assertRaises(ValueError):
            registry.plan(matrix, SOURCE, False)

    def fixture(self, wrong_label=False):
        responses, children = {}, []
        def store(kind, data):
            raw = json.dumps(data).encode()
            digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
            responses[f'https://ghcr.io/v2/bpy-dev/bpy-runtime/{kind}/{digest}'] = raw
            return digest
        for arch in ('amd64', 'arm64'):
            config = {'os': 'linux', 'architecture': arch, 'config': {'Labels': {
                'org.opencontainers.image.revision': 'bad' if wrong_label else SOURCE,
                'dev.bpy.release-fingerprint': FINGERPRINT}}}
            digest = store('blobs', config)
            child = store('manifests', {'config': {'digest': digest}})
            children.append({'platform': {'os': 'linux', 'architecture': arch}, 'digest': child})
        tag = registry.candidate_tag(SOURCE, FINGERPRINT)
        index_url = 'https://ghcr.io/v2/bpy-dev/bpy-runtime/manifests/' + tag
        responses[index_url] = json.dumps({'manifests': children}).encode()
        responses['https://ghcr.io/token?service=ghcr.io&scope=repository:bpy-dev/bpy-runtime:pull'] = b'{"token":"anonymous-test-token"}'
        return responses, tag, index_url

    def test_verify_reads_and_hashes_children_and_configs(self):
        responses, tag, url = self.fixture()
        with patch.object(registry, 'fetch', side_effect=lambda url, token=None: responses[url]):
            receipt = registry.verify(tag, SOURCE, FINGERPRINT)
        self.assertEqual(receipt['digest'], 'sha256:' + hashlib.sha256(responses[url]).hexdigest())
        self.assertTrue(receipt['anonymous'])
        config_url = next(key for key in responses if '/blobs/' in key)
        responses[config_url] = b'{}'
        with patch.object(registry, 'fetch', side_effect=lambda url, token=None: responses[url]):
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                registry.verify(tag, SOURCE, FINGERPRINT)

    def test_anonymous_layers_are_downloaded_and_hashed_without_docker_cache(self):
        data = b'compressed registry layer'
        descriptor = {'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'size': len(data)}
        for payload, valid in [(data, True), (data[:-1], False), (b'x' * len(data), False), (data + b'x', False)]:
            with self.subTest(payload=payload), patch.object(registry.urllib.request, 'urlopen', return_value=io.BytesIO(payload)) as request:
                if valid:
                    self.assertEqual(registry.download_layer(descriptor, 'anonymous-token'), descriptor)
                else:
                    with self.assertRaises(ValueError):
                        registry.download_layer(descriptor, 'anonymous-token')
                self.assertEqual(request.call_args.args[0].full_url, 'https://ghcr.io/v2/bpy-dev/bpy-runtime/blobs/' + descriptor['digest'])
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertIn('--download-layers', workflow)

    def test_verify_requires_layers_for_each_architecture(self):
        responses, tag, url = self.fixture()
        index = json.loads(responses[url])
        descriptor = {'digest': 'sha256:' + 'f' * 64, 'size': 20}
        for child in index['manifests']:
            child_url = 'https://ghcr.io/v2/bpy-dev/bpy-runtime/manifests/' + child['digest']
            manifest = json.loads(responses[child_url])
            manifest['layers'] = [descriptor]
            raw = json.dumps(manifest).encode()
            child['digest'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
            responses['https://ghcr.io/v2/bpy-dev/bpy-runtime/manifests/' + child['digest']] = raw
        responses[url] = json.dumps(index).encode()
        with patch.object(registry, 'fetch', side_effect=lambda url, token=None: responses[url]), patch.object(registry, 'download_layer', return_value=descriptor) as download:
            result = registry.verify(tag, SOURCE, FINGERPRINT, download_layers=True)
            self.assertEqual(download.call_count, 2)
            self.assertEqual({item['architecture'] for item in result['downloaded_layers']}, {'amd64', 'arm64'})
        with patch.object(registry, 'fetch', side_effect=lambda url, token=None: responses[url]), patch.object(registry, 'download_layer', side_effect=ValueError('layer unavailable')):
            with self.assertRaises(ValueError):
                registry.verify(tag, SOURCE, FINGERPRINT, download_layers=True)

    def test_reject_provenance_and_missing_or_duplicate_architecture(self):
        responses, tag, _ = self.fixture(wrong_label=True)
        with patch.object(registry, 'fetch', side_effect=lambda url, token=None: responses[url]):
            with self.assertRaisesRegex(ValueError, 'provenance'):
                registry.verify(tag, SOURCE, FINGERPRINT)
        child = {'platform': {'os': 'linux', 'architecture': 'amd64'}, 'digest': 'sha256:' + 'a' * 64}
        for children in ([], [child], [child, child]):
            with self.assertRaises(ValueError):
                registry.validate_index({'manifests': children})


if __name__ == '__main__':
    unittest.main()
