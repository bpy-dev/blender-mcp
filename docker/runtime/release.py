"""Admit buildbpy releases; download only allowlisted, digest-bound wheels.

Only Blender 5.3 / CPython 3.13 is qualified. New minor versions require review.
GitHub API metadata is the trust root, not user-supplied download URLs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

REPO = 'michaelgold/buildbpy'
PLATFORMS = {'arm64': 'manylinux_2_39_aarch64', 'amd64': 'manylinux_2_39_x86_64'}

def admit(release):
    tag = release.get('tag_name', '')
    if not re.fullmatch(r'v5\.3\.(0|[1-9][0-9]*)', tag):
        raise ValueError('unqualified release series or invalid tag')
    if release.get('draft') is not False or release.get('prerelease') is not False:
        raise ValueError('release must be published and stable')
    version = tag[1:]
    assets = release['assets']
    names = [a['name'] for a in assets]
    if len(set(names)) != len(names):
        raise ValueError('duplicate asset')
    required = [f'bpy-{version}-cp313-cp313-{p}.whl' for p in
                [*PLATFORMS.values(), 'macosx_13_0_arm64', 'win_amd64']]
    selected = {}
    for name in required:
        matches = [a for a in assets if a['name'] == name]
        if len(matches) != 1:
            raise ValueError('incomplete platform publication: ' + name)
        asset = matches[0]
        if (asset.get('state') != 'uploaded' or asset.get('size', 0) <= 0 or
                not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or '')):
            raise ValueError('asset missing uploaded state, size or SHA256')
        selected[name] = asset
    wheels = {}
    for arch, platform in PLATFORMS.items():
        name = f'bpy-{version}-cp313-cp313-{platform}.whl'
        wheels[arch] = {'name': name, 'sha256': selected[name]['digest'][7:],
                        'size': selected[name]['size']}
    return {'repository': REPO, 'tag': tag, 'version': version, 'wheels': wheels}

def api(path):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'bpy-runtime-release'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + path, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

def resolve(tag):
    if not re.fullmatch(r'v5\.3\.(0|[1-9][0-9]*)', tag):
        raise ValueError('invalid or unqualified release tag')
    return admit(api('/releases/tags/' + tag))

def download(lock, arch, directory):
    # Revalidate serialized lock: no arbitrary repository, path or URL inputs.
    if lock['repository'] != REPO or arch not in PLATFORMS:
        raise ValueError('invalid repository or architecture')
    current = resolve(lock['tag'])
    if current != lock:
        raise ValueError('release metadata changed since admission')
    wheel = lock['wheels'][arch]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / wheel['name']
    partial = target.with_suffix('.partial')
    url = f'https://github.com/{REPO}/releases/download/{lock["tag"]}/{wheel["name"]}'
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open('wb') as output:
            while data := response.read(1024 * 1024):
                size += len(data)
                if size > wheel['size']:
                    raise ValueError('wheel exceeds admitted size')
                digest.update(data)
                output.write(data)
        if size != wheel['size'] or digest.hexdigest() != wheel['sha256']:
            raise ValueError('wheel size/SHA256 mismatch')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag')
    parser.add_argument('--lock', type=Path)
    parser.add_argument('--matrix', action='store_true')
    parser.add_argument('--download', choices=PLATFORMS)
    parser.add_argument('--directory', type=Path, default=Path('wheels'))
    args = parser.parse_args()
    if args.matrix:
        locks = []
        if args.tag:
            locks = [resolve(args.tag)]
        else:
            page = 1
            while True:
                releases = api(f'/releases?per_page=100&page={page}')
                for release in releases:
                    if not re.fullmatch(r'v5\.3\.(0|[1-9][0-9]*)', release['tag_name']):
                        continue
                    try:
                        locks.append(admit(release))
                    except ValueError:
                        continue  # Incomplete uploads are retried next hour.
                if len(releases) < 100:
                    break
                page += 1
        include = [{'tag': lock['tag'], 'arch': arch, 'runner': runner,
                    'fingerprint': hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()}
                   for lock in locks for arch, runner in
                   [('arm64', 'ubuntu-24.04-arm'), ('amd64', 'ubuntu-24.04')]]
        print(json.dumps({'include': include}, separators=(',', ':')))
        return
    if args.lock is None:
        parser.error('--lock is required outside --matrix mode')
    if args.download:
        print(download(json.loads(args.lock.read_text()), args.download, args.directory))
        return
    if args.tag:
        lock = resolve(args.tag)
    else:
        # Polling avoids GITHUB_TOKEN-generated release event suppression and
        # retries partial platform publication on the next scheduled run.
        releases = api('/releases?per_page=100')
        candidates = [r for r in releases if re.fullmatch(r'v5\.3\.(0|[1-9][0-9]*)', r['tag_name'])]
        candidates.sort(key=lambda r: int(r['tag_name'].split('.')[-1]), reverse=True)
        lock = None
        for release in candidates:
            try:
                lock = admit(release)
                break
            except ValueError:
                continue
        if lock is None:
            raise ValueError('no complete qualified release')
    args.lock.write_text(json.dumps(lock, indent=2) + '\n')
    print(lock['tag'])

if __name__ == '__main__':
    main()
