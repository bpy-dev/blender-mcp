"""Opt-in, one-identity Tailcat session. No capability or upstream logs escape."""
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

TAILCAT = '/usr/local/bin/tailcat'


def key(value, prefix, nonzero=True):
    return (isinstance(value, str) and re.fullmatch(prefix + r':[0-9a-f]{64}', value)
            and (not nonzero or value.split(':')[1] != '0' * 64))


def admit(env):
    """Validate before launching anything; never put input values in errors."""
    try:
        allow = env['BPY_TAILCAT_ALLOW']
        if not key(allow, 'nodekey'):
            raise ValueError
        expires = env['BPY_TAILCAT_EXPIRES_AT']
        if not re.fullmatch(r'[0-9]{1,12}', expires):
            raise ValueError
        remaining = int(expires) - time.time()
        if not 0 < remaining <= 3600:
            raise ValueError
        raw = env['BPY_TAILCAT_SERVER_KEY']
        if len(raw) > 4096:
            raise ValueError
        identity = json.loads(raw)
        if set(identity) != {'Private', 'Public'} or not key(identity['Private'], 'privkey'):
            raise ValueError
        public = identity['Public']
        if set(public) != {'ServerPublic', 'ServerDiscoPublic', 'PresharedKey', 'RegionID'}:
            raise ValueError
        if not (key(public['ServerPublic'], 'nodekey')
                and key(public['ServerDiscoPublic'], 'discokey', nonzero=False)
                and key(public['PresharedKey'], 'psk')
                and type(public['RegionID']) is int and 0 < public['RegionID'] <= 65535):
            raise ValueError
        return allow, raw, remaining
    except (KeyError, TypeError, ValueError):
        raise ValueError('invalid Tailcat session configuration') from None


def run():
    allow, identity, remaining = admit(os.environ)
    deadline = time.monotonic() + remaining
    os.umask(0o077)
    # Adopt orphaned descendants so TTL cleanup does not leave zombies (Linux).
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('cannot enable session child reaping')
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    with tempfile.TemporaryDirectory(prefix='bpy-tailcat-') as directory:
        private = Path(directory) / 'server.private.json'
        private.write_text(identity)
        # Do not inherit launch credentials, provider keys, address callbacks or
        # Tailcat config overrides into the listener/MCP. GPU injection is host
        # runtime setup; these are the only application environment values needed.
        env = {name: os.environ[name] for name in (
            'PATH', 'BLENDER_MCP_CLI_BACKEND', 'BLENDER_MCP_BPY_PYTHON',
            'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED',
            'LD_LIBRARY_PATH', 'NVIDIA_VISIBLE_DEVICES', 'NVIDIA_DRIVER_CAPABILITIES',
        ) if name in os.environ}
        env.update(HOME=directory, XDG_CONFIG_HOME=directory, TMPDIR=directory)
        for name in ('BPY_TAILCAT_SERVER_KEY', 'BPY_TAILCAT_ALLOW', 'BPY_TAILCAT_EXPIRES_AT'):
            os.environ.pop(name, None)
        process = subprocess.Popen([
            TAILCAT, 'serve', '--key=' + str(private), '--allow=' + allow,
            'exec', '--', sys.executable, '/opt/runtime/entrypoint.py', '--transport', 'stdio',
        ], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, start_new_session=True)
        def diagnostics():
            # Positive allowlist, not fragile address-pattern redaction. Bound
            # each read; discard every byte except this fixed dispatch event.
            assert process.stderr is not None
            for line in iter(lambda: process.stderr.readline(4096), b''):
                if line == b'bpy: authenticated MCP session started\n':
                    print('bpy: authenticated MCP session started', file=sys.stderr, flush=True)
        reader = threading.Thread(target=diagnostics, daemon=True)
        reader.start()
        try:
            while not stopping and time.monotonic() < deadline:
                if process.poll() is not None:
                    return 1  # Never fall back to unauthenticated transport.
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            return 0
        finally:
            # MCP and normal bpy subprocesses inherit the listener process group.
            # Authorized Python can escape it: this is lifecycle, NOT a sandbox.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                if sig == signal.SIGTERM:
                    time.sleep(0.3)
            process.wait()
            until = time.monotonic() + 3
            while time.monotonic() < until:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not pid:
                    time.sleep(0.02)


def main():
    try:
        return run()
    except Exception:
        # Upstream CLI prints its secret capability address even without verbose.
        # Errors are deliberately constant, not exception reprs or child stderr.
        print('Tailcat session failed; check private launch configuration', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
