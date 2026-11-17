# SPDX-FileCopyrightText: 2026 Blender Authors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dependency-floor regressions for saved-file tools and optional acceptance."""

__all__ = ()

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from blmcp.tools import execute_blender_code


class TestPreviewCompatibility(unittest.TestCase):
    def test_saved_file_hashes_without_python311_file_digest(self):
        tools = {}

        class Registry:
            def tool(self, **kwargs):
                def register(function):
                    tools[function.__name__] = function
                    return function
                return register

        execute_blender_code.register(Registry())
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.blend'
            output = Path(temporary) / 'output.blend'
            source.write_bytes(b'source' * 200000)
            expected = hashlib.sha256(source.read_bytes()).hexdigest()

            def run(path, code, **kwargs):
                # Exercise host-side receipts without requiring Blender here.
                if 'render.filepath' in code:
                    import ast
                    for line in code.splitlines():
                        if '.render.filepath = ' in line:
                            Path(ast.literal_eval(line.split(' = ', 1)[1])).write_bytes(b'png')
                else:
                    output.write_bytes(b'checkpoint')
                return {}

            # Python 3.10 has no file_digest. Preserve the real sha256 API.
            with mock.patch.object(execute_blender_code, 'hashlib', wraps=hashlib) as legacy:
                del legacy.file_digest
                with mock.patch.object(execute_blender_code, 'run_blender_cli', side_effect=run):
                    receipt = tools['execute_blender_code_for_cli'](str(source), '', str(output))
                    self.assertEqual(receipt['_checkpoint']['source_sha256'], expected)
                    self.assertEqual(receipt['_checkpoint']['output_sha256'], hashlib.sha256(b'checkpoint').hexdigest())
                    for name in ('get_local_render_as_image_for_cli', 'get_render_as_image_for_cli'):
                        args = {} if name == 'get_render_as_image_for_cli' else dict(
                            width=4, height=4, samples=2, timeout_seconds=30, frame=1, frame_count=1)
                        result = tools[name](str(source), **args)
                        self.assertEqual(result.meta['source_sha256'], expected)

    def test_opt_in_acceptance_skips_without_pillow(self):
        code = '''
import importlib.abc
import sys
import unittest
class NoPillow(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PIL' or fullname.startswith('PIL.'):
            raise ModuleNotFoundError('Pillow deliberately unavailable')
sys.meta_path.insert(0, NoPillow())
suite = unittest.defaultTestLoader.loadTestsFromName('tests.integration.test_local_cpu_session')
result = unittest.TextTestRunner().run(suite)
assert result.wasSuccessful() and len(result.skipped) == 1
'''
        env = os.environ.copy()
        env.pop('BLENDER_MCP_BPY_PYTHON', None)
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
