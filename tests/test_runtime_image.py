"""Offline admission, entrypoint, and workflow contracts."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]

class RuntimeImageTests(unittest.TestCase):
    def test_automatic_rebuild_is_native_and_publishing_is_trusted(self):
        workflow = ROOT / '.github/workflows/runtime-image.yml'
        self.assertTrue(workflow.exists(), 'missing automatic runtime image rebuild')
        text = workflow.read_text()
        self.assertIn('schedule:', text)
        release = (ROOT / 'docker/runtime/release.py').read_text()
        self.assertIn('ubuntu-24.04-arm', release)
        self.assertIn('runs-on: ${{ matrix.runner }}', text)
        self.assertIn('ubuntu-24.04', text)
        self.assertIn('push: false', text)
        self.assertIn('pull_request:', text)
        self.assertIn('packages: write', text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("github.event_name != 'pull_request'", text)
        self.assertIn('needs: [discover, build]', text)
        self.assertNotIn('.runtime-success', text)
        self.assertIn('docker --config "$anonymous" pull', text)
        self.assertIn('org.opencontainers.image.revision', text)
        self.assertNotIn('repository_dispatch:', text)

    def test_entrypoint_refuses_wrong_runtime_and_execs_without_installing(self):
        spec = importlib.util.spec_from_file_location('entrypoint', ROOT / 'docker/runtime/entrypoint.py')
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fake = types.SimpleNamespace(app=types.SimpleNamespace(version_string='5.3.0'))
        env = {'BLENDER_MCP_CLI_BACKEND': 'bpy', 'BLENDER_MCP_BPY_PYTHON': sys.executable}
        with patch.dict(sys.modules, bpy=fake), patch.object(pathlib.Path, 'read_text', return_value='{"version":"5.3.0"}'), patch.dict(module.os.environ, env, clear=True), patch.object(module.os, 'execv') as execute, patch.object(sys, 'argv', ['entrypoint', '--transport', 'stdio']):
            module.main()
            execute.assert_called_once_with(sys.executable, [sys.executable, '-m', 'blmcp', '--transport', 'stdio'])
            fake.app.version_string = '5.2.3'
            with self.assertRaises(RuntimeError):
                module.main()
            execute.assert_called_once()

if __name__ == '__main__':
    unittest.main()
