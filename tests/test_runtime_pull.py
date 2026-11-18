"""Execute the workflow's pull loop against a recording Docker boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ghcr.io/bpy-dev/bpy-runtime'
SOURCE = 'a' * 40
FINGERPRINT = 'b' * 64
DIGESTS = {'amd64': 'sha256:' + 'c' * 64, 'arm64': 'sha256:' + 'd' * 64}


def pull_loop():
    text = (ROOT / '.github/workflows/runtime-image.yml').read_text()
    section = text.index('name: Require public anonymous manifests and complete pulls')
    start = text.index('          for arch in amd64 arm64; do', section)
    end = text.index('          done', start) + len('          done')
    return textwrap.dedent(text[start:end])


@unittest.skipUnless(shutil.which('bash') and shutil.which('jq'), 'workflow shell needs bash/jq')
class RuntimePullTests(unittest.TestCase):
    def exercise(self, swap=False, wrong_source=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            receipt = {'manifests': [{'platform': {'os': 'linux', 'architecture': arch},
                                      'digest': DIGESTS['arm64' if arch == 'amd64' else 'amd64'] if swap else digest}
                                     for arch, digest in DIGESTS.items()]}
            (path / 'publication.json').write_text(json.dumps(receipt))
            fake = path / 'docker'
            fake.write_text('#!/usr/bin/env python3\n' + textwrap.dedent('''
                import json, os, sys
                from pathlib import Path
                args = sys.argv[1:]
                with Path('calls.jsonl').open('a') as output:
                    output.write(json.dumps(args) + '\\n')
                if args[:1] == ['--config']:
                    args = args[2:]
                if args[0] == 'pull':
                    sys.exit(0)
                if args[:2] == ['image', 'inspect']:
                    template = args[args.index('--format') + 1]
                    arch = 'amd64' if args[-1].endswith('c' * 64) else 'arm64'
                    if template == '{{.Architecture}}': print(arch)
                    elif 'org.opencontainers.image.revision' in template: print(os.environ['FAKE_SOURCE'])
                    elif 'dev.bpy.release-fingerprint' in template: print(os.environ['FINGERPRINT'])
                    else: sys.exit(2)
                else: sys.exit(2)
            '''))
            fake.chmod(0o755)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                       IMAGE=IMAGE, SOURCE_SHA=SOURCE, FINGERPRINT=FINGERPRINT,
                       FAKE_SOURCE='wrong' if wrong_source else SOURCE,
                       anonymous=str(path / 'empty-config'), digest='sha256:' + 'e' * 64)
            result = subprocess.run(['bash', '-e', '-c', pull_loop()], cwd=path, env=env,
                                    text=True, capture_output=True)
            calls = [json.loads(line) for line in (path / 'calls.jsonl').read_text().splitlines()]
            return result, calls

    def test_pulls_distinct_children_bound_to_each_architecture(self):
        result, calls = self.exercise()
        self.assertEqual(result.returncode, 0, result.stderr)
        pulls = [c for c in calls if 'pull' in c]
        self.assertEqual([c[-1] for c in pulls], [IMAGE + '@' + DIGESTS[a] for a in ('amd64', 'arm64')])
        self.assertEqual([c[-2] for c in pulls], ['linux/amd64', 'linux/arm64'])

    def test_swapped_architecture_mapping_is_rejected(self):
        result, calls = self.exercise(swap=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([c for c in calls if 'pull' in c]), 1)

    def test_wrong_source_provenance_is_rejected(self):
        result, calls = self.exercise(wrong_source=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([c for c in calls if 'pull' in c]), 1)


if __name__ == '__main__':
    unittest.main()
