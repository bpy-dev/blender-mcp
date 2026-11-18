"""Originating-agent bootstrap. Outputs are PRIVATE, never evidence/artifacts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def prepare(directory, tailcat, ttl=900, region=302):
    if not 5 <= ttl <= 3600 or not 1 <= region <= 65535:
        raise ValueError('invalid session lifetime or region')
    os.umask(0o077)
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700)  # Never reuse an identity directory.
    try:
        for role in ('client', 'server'):
            home = directory / role
            home.mkdir(mode=0o700)
            env = {'HOME': str(home), 'XDG_CONFIG_HOME': str(home), 'PATH': '/usr/bin:/bin'}
            args = [tailcat, 'genkey', '--key=' + ('client-default' if role == 'client' else 'default')]
            args += ['--client'] if role == 'client' else ['--region=' + str(region)]
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError('private session key generation failed')
            (directory / (role + '.address')).write_text(result.stdout.strip())
        public = (directory / 'client.address').read_text()
        server_key = json.loads((directory / 'server/tailcat/keys/default.private.json').read_text())
        env = {'BPY_TAILCAT_ALLOW': public,
               'BPY_TAILCAT_EXPIRES_AT': str(int(time.time()) + ttl),
               'BPY_TAILCAT_SERVER_KEY': json.dumps(server_key, separators=(',', ':'))}
        (directory / 'provider-env.json').write_text(json.dumps(env))
        (directory / 'docker.env').write_text(''.join(k + '=' + v + '\n' for k, v in env.items()))
        for path in directory.rglob('*'):
            if path.is_file():
                path.chmod(0o600)
        return env
    except BaseException:
        import shutil
        shutil.rmtree(directory)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--tailcat', default='tailcat')
    parser.add_argument('--ttl', type=int, default=900)
    parser.add_argument('--region', type=int, default=302)
    args = parser.parse_args()
    prepare(args.directory, args.tailcat, args.ttl, args.region)
    print('Private session files created; do not upload or print them.')
