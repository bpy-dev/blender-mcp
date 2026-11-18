"""Build-time only, checksummed upstream static Tailcat binary installation."""
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

VERSION = '0.7.0'
DIGESTS = {
    'amd64': '23c0b1887a5ec422f0d18a9c52b4f5357815febdaae738a1eb54036d10bd9ee6',
    'arm64': 'bbb1ab50f24f00effe1e1fd86d0501803fb80793a90785a2a16ff3428f03d8ef',
}


def install(arch, destination):
    expected = DIGESTS[arch]  # Fail closed for unsupported architectures.
    url = f'https://github.com/tailscale/tailcat/releases/download/v{VERSION}/tailcat_{VERSION}_linux_{arch}.tar.gz'
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError('Tailcat archive digest mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        for name, output in [('tailcat', destination), ('LICENSE', '/opt/runtime/TAILCAT-LICENSE')]:
            member = tar.getmember(name)
            if not member.isfile():
                raise ValueError('Tailcat archive member is not a file')
            source = tar.extractfile(member)
            assert source is not None
            with source:
                Path(output).write_bytes(source.read())
    Path(destination).chmod(0o755)
    actual = subprocess.check_output([destination, '--version'], text=True).strip()
    if actual != 'v' + VERSION:
        raise ValueError('Tailcat version mismatch')


if __name__ == '__main__':
    install(sys.argv[1], '/usr/local/bin/tailcat')
