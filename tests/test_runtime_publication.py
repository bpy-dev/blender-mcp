"""Fail-closed publication decisions; no real registry writes in these tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import urllib.error
import io
from email.message import Message

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publication_registry', ROOT / 'docker/runtime/registry.py')
assert spec and spec.loader
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
DIGEST = 'sha256:' + 'a' * 64
OTHER = 'sha256:' + 'b' * 64
TAG = registry.candidate_tag('a' * 40, 'b' * 64)


class PublicationTests(unittest.TestCase):
    def test_both_write_paths_fail_closed(self):
        for kind in ('child', 'index'):
            matching = {'config': {'digest': DIGEST}} if kind == 'child' else {'manifests': [{'digest': DIGEST}, {'digest': OTHER}]}
            mismatch = {'config': {'digest': OTHER}} if kind == 'child' else {'manifests': [{'digest': OTHER}]}
            for state in ('match', 'mismatch', 'absent', 'error', 'malformed'):
                with self.subTest(kind=kind, state=state):
                    result = {'match': matching, 'mismatch': mismatch, 'absent': None, 'malformed': {}}.get(state)
                    with patch.object(registry, 'authenticated_manifest', return_value=result,
                                      side_effect=urllib.error.URLError('503') if state == 'error' else None), patch.object(registry.subprocess, 'run') as run:
                        if state in ('mismatch', 'error', 'malformed'):
                            with self.assertRaises((ValueError, urllib.error.URLError)):
                                registry.publish(kind, TAG, DIGEST, [DIGEST, OTHER])
                        else:
                            registry.publish(kind, TAG, DIGEST, [DIGEST, OTHER])
                        self.assertEqual(run.call_count, 2 if state == 'absent' and kind == 'child' else 1 if state == 'absent' else 0)

    def test_authenticated_absence_is_strict(self):
        cases = [(404, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', True),
                 (404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}', False),
                 (404, b'not json', False), (404, b'{"errors":[]}', False),
                 (401, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}', False),
                 (403, b'{}', False), (503, b'{}', False)]
        for code, body, absent in cases:
            with self.subTest(code=code, body=body):
                error = urllib.error.HTTPError('https://ghcr.io', code, 'failure', Message(), io.BytesIO(body))
                with patch.dict(os.environ, GH_TOKEN='fixture', GH_ACTOR='fixture'), patch.object(registry.urllib.request, 'urlopen', side_effect=[io.BytesIO(b'{"token":"fixture-bearer"}'), error]) as request:
                    if absent:
                        self.assertIsNone(registry.authenticated_manifest(TAG))
                    else:
                        with self.assertRaises((ValueError, urllib.error.HTTPError)):
                            registry.authenticated_manifest(TAG)
                    self.assertTrue(request.call_args_list[0].args[0].get_header('Authorization').startswith('Basic '))
                    self.assertEqual(request.call_args_list[1].args[0].get_header('Authorization'), 'Bearer fixture-bearer')
                    self.assertEqual(request.call_args_list[1].args[0].get_header('Accept'), registry.ACCEPT)
                error.close()

    def test_network_and_bad_success_responses_do_not_authorize_writes(self):
        for reply in [urllib.error.URLError('timeout'), io.BytesIO(b'not json'), io.BytesIO(b'[]')]:
            with patch.dict(os.environ, GH_TOKEN='fixture', GH_ACTOR='fixture'), patch.object(registry.urllib.request, 'urlopen', side_effect=[io.BytesIO(b'{"token":"fixture-bearer"}'), reply]):
                with self.assertRaises((ValueError, urllib.error.URLError)):
                    registry.authenticated_manifest(TAG)

    def test_http_failure_reaches_both_write_guards_without_mutation(self):
        for kind in ('child', 'index'):
            for code in (401, 403, 404, 500, 503):
                with self.subTest(kind=kind, code=code):
                    error = urllib.error.HTTPError('https://ghcr.io', code, 'failure', Message(), io.BytesIO(b'{"errors":[{"code":"DENIED"}]}'))
                    with patch.dict(os.environ, GH_TOKEN='fixture', GH_ACTOR='fixture'), patch.object(registry.urllib.request, 'urlopen', side_effect=[io.BytesIO(b'{"token":"fixture-bearer"}'), error]), patch.object(registry.subprocess, 'run') as run:
                        with self.assertRaises((ValueError, urllib.error.HTTPError)):
                            registry.publish(kind, TAG, DIGEST, [DIGEST, OTHER])
                        run.assert_not_called()
                    error.close()

    def test_workflow_uses_guard_for_both_exact_write_paths(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertIn('--publish child', workflow)
        self.assertIn('--publish index', workflow)
        self.assertNotIn('if docker manifest inspect', workflow)
        self.assertNotIn('if ! docker manifest inspect', workflow)
        self.assertNotIn('docker push "$ref"', workflow)
        self.assertNotIn('imagetools create --tag', workflow)


if __name__ == '__main__':
    unittest.main()
