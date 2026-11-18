"""Fail-closed session admission; portable tests never need a relay."""
import importlib.util
import json
from pathlib import Path
import time
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location('tailcat_session', ROOT / 'docker/runtime/tailcat_session.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config():
    # Synthetic values are format fixtures, not real identities.
    return {
        'BPY_TAILCAT_ALLOW': 'nodekey:' + '1' * 64,
        'BPY_TAILCAT_EXPIRES_AT': str(int(time.time()) + 60),
        'BPY_TAILCAT_SERVER_KEY': json.dumps({'Private': 'privkey:' + '2' * 64, 'Public': {
            'ServerPublic': 'nodekey:' + '3' * 64, 'ServerDiscoPublic': 'discokey:' + '0' * 64,
            'PresharedKey': 'psk:' + '4' * 64, 'RegionID': 302}}),
    }


class TailcatAdmissionTests(unittest.TestCase):
    def test_explicit_single_client_required_no_anonymous_fallback(self):
        m = load()
        self.assertEqual(m.admit(config())[0], config()['BPY_TAILCAT_ALLOW'])
        for value in ['', 'none', '*', 'nodekey:' + '0' * 64, 'nodekey:bad',
                      'nodekey:' + '1' * 64 + ',nodekey:' + '2' * 64, ' nodekey:' + '1' * 64]:
            with self.subTest(value=value):
                env = config(); env['BPY_TAILCAT_ALLOW'] = value
                with self.assertRaises(ValueError):
                    m.admit(env)

    def test_expired_or_unbounded_session_rejected(self):
        m = load()
        for value in ['', 'inf', '-1', str(int(time.time()) - 1), str(int(time.time()) + 3602)]:
            env = config(); env['BPY_TAILCAT_EXPIRES_AT'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.admit(env)

    def test_server_identity_requires_private_key_psk_and_fixed_region(self):
        m = load()
        for value in ['', '{}', 'not-json', '{"Private":"secret-must-not-echo"}']:
            env = config(); env['BPY_TAILCAT_SERVER_KEY'] = value
            with self.assertRaises(ValueError) as error:
                m.admit(env)
            self.assertNotIn(value, str(error.exception)) if value else None
        for field, value in [('PresharedKey', 'psk:' + '0' * 64), ('RegionID', -1), ('RegionID', True)]:
            env = config(); key = json.loads(env['BPY_TAILCAT_SERVER_KEY'])
            key['Public'][field] = value; env['BPY_TAILCAT_SERVER_KEY'] = json.dumps(key)
            with self.assertRaises(ValueError):
                m.admit(env)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux process lifecycle')
    def test_ttl_reaps_process_group_and_drops_untrusted_diagnostics(self):
        # Real process tree, no Docker PID namespace to mask wrapper cleanup.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / 'tailcat'
            pidfile = root / 'pids'
            child = 'import time; time.sleep(120)'
            launcher.write_text(f'#!{sys.executable}\nimport subprocess,sys,os,time\n'
                f'p=subprocess.Popen([sys.executable,"-c",{child!r}])\n'
                f'open({str(pidfile)!r},"w").write(str(os.getpid())+" "+str(p.pid))\n'
                'print("PRIVATE-CAPABILITY-SENTINEL",file=sys.stderr,flush=True)\n'
                'print("bpy: authenticated MCP session started",file=sys.stderr,flush=True)\n'
                'time.sleep(120)\n')
            launcher.chmod(0o700)
            env = dict(os.environ, **config())
            env['BPY_TAILCAT_EXPIRES_AT'] = str(int(time.time()) + 3)
            command = (f'import sys;sys.path.insert(0,{str(ROOT / "docker/runtime")!r});'
                       f'import tailcat_session as m;m.TAILCAT={str(launcher)!r};sys.exit(m.main())')
            process = subprocess.Popen([sys.executable, '-c', command], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(stdout, b'')
                self.assertEqual(stderr, b'bpy: authenticated MCP session started\n')
                pids = [int(p) for p in pidfile.read_text().split()]
                for pid in pids:
                    self.assertFalse(Path(f'/proc/{pid}').exists(), 'descendant not reaped')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
                if pidfile.exists():
                    for pid in map(int, pidfile.read_text().split()):
                        try: os.kill(pid, 9)
                        except ProcessLookupError: pass

    def test_native_gate_preserves_offline_smoke_and_adds_tailcat_both_arches(self):
        text = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertIn('/test/docker/runtime/smoke.py', text)
        self.assertIn('docker/runtime/tailcat_smoke.py --image bpy-runtime:candidate', text)
        self.assertIn('pillow==13.0.0', text)


if __name__ == '__main__':
    unittest.main()
