"""Anonymous GHCR verification and release/source-bound candidate planning.

Anonymous verification/planning uses no local Docker credentials or GitHub token.
Publication checks separately exchange the trusted job's built-in token for a
registry bearer token, without printing either. A private, missing or damaged
image is never a successful build-suppression receipt.
"""
import argparse
import base64
import os
import subprocess
import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

IMAGE = 'ghcr.io/bpy-dev/bpy-runtime'
REPOSITORY = 'bpy-dev/bpy-runtime'
ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json'])


def candidate_tag(source, fingerprint):
    if not re.fullmatch('[0-9a-f]{40}', source) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
        raise ValueError('invalid source SHA or release fingerprint')
    return f'sha-{source}-{fingerprint}'


def validate_index(index):
    manifests = index.get('manifests', [])
    platforms = [(m.get('platform', {}).get('os'), m.get('platform', {}).get('architecture')) for m in manifests]
    if sorted(platforms) != [('linux', 'amd64'), ('linux', 'arm64')]:
        raise ValueError('exactly native Linux AMD64 and ARM64 required')
    for manifest in manifests:
        if not re.fullmatch('sha256:[0-9a-f]{64}', manifest.get('digest', '')):
            raise ValueError('invalid child digest')
    return manifests


def fetch(url, token=None):
    headers = {'Accept': ACCEPT, 'User-Agent': 'bpy-runtime-verifier'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read()


def download_layer(descriptor, token):
    """Stream every compressed blob from GHCR; never consult Docker's cache."""
    digest, expected_size = descriptor['digest'], descriptor['size']
    if (not re.fullmatch('sha256:[0-9a-f]{64}', digest) or
            type(expected_size) is not int or expected_size <= 0):
        raise ValueError('invalid layer descriptor')
    request = urllib.request.Request('https://ghcr.io/v2/' + REPOSITORY + '/blobs/' + digest,
                                     headers={'Authorization': 'Bearer ' + token})
    actual, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=120) as response:
        while data := response.read(1024 * 1024):
            size += len(data)
            if size > expected_size:
                raise ValueError('layer exceeds manifest size')
            actual.update(data)
    if size != expected_size or 'sha256:' + actual.hexdigest() != digest:
        raise ValueError('anonymous layer size/digest mismatch')
    return {'digest': digest, 'size': size}


def verify(tag, source, fingerprint, download_layers=False):
    expected_tag = candidate_tag(source, fingerprint)
    if tag != expected_tag:
        raise ValueError('candidate tag does not match inputs')
    token = json.loads(fetch('https://ghcr.io/token?service=ghcr.io&scope=repository:' + REPOSITORY + ':pull'))['token']
    base = 'https://ghcr.io/v2/' + REPOSITORY

    def document(kind, reference):
        raw = fetch(f'{base}/{kind}/{reference}', token)
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if reference.startswith('sha256:') and reference != digest:
            raise ValueError('registry content digest mismatch')
        return json.loads(raw), digest

    index, digest = document('manifests', tag)
    children = validate_index(index)
    downloaded = []
    for child in children:
        manifest, _ = document('manifests', child['digest'])
        config, _ = document('blobs', manifest['config']['digest'])
        if config['os'] != 'linux' or config['architecture'] != child['platform']['architecture']:
            raise ValueError('child architecture mismatch')
        labels = config.get('config', {}).get('Labels', {})
        if labels.get('org.opencontainers.image.revision') != source or labels.get('dev.bpy.release-fingerprint') != fingerprint:
            raise ValueError('child provenance mismatch')
        if download_layers:
            layers = manifest.get('layers')
            if not isinstance(layers, list) or not layers:
                raise ValueError('missing image layers')
            for layer in layers:
                downloaded.append(dict(download_layer(layer, token),
                                       architecture=child['platform']['architecture']))
    return {'image': IMAGE, 'tag': tag, 'digest': digest, 'source': source,
            'fingerprint': fingerprint, 'manifests': children, 'anonymous': True,
            'downloaded_layers': downloaded}


def plan(matrix, source, skip_published):
    builds, publications = [], []
    keys = sorted({(row['tag'], row['fingerprint']) for row in matrix['include']})
    for release, fingerprint in keys:
        tag = candidate_tag(source, fingerprint)
        if skip_published:
            try:
                verify(tag, source, fingerprint)
                continue
            except (urllib.error.URLError, ValueError, KeyError, TypeError):
                pass  # Fail closed: never skip on a stale cache or a failed read.
        rows = [dict(row, image_tag=tag) for row in matrix['include'] if (row['tag'], row['fingerprint']) == (release, fingerprint)]
        if sorted(row['arch'] for row in rows) != ['amd64', 'arm64']:
            raise ValueError('both native build legs required')
        builds.extend(rows)
        publications.append({'tag': release, 'fingerprint': fingerprint, 'image_tag': tag})
    return {'build': {'include': builds}, 'publish': {'include': publications}}


def authenticated_manifest(tag):
    """Return None ONLY on authenticated 404/MANIFEST_UNKNOWN, else fail closed."""
    if not re.fullmatch(r'sha-[0-9a-f]{40}-[0-9a-f]{64}(?:-amd64|-arm64)?', tag):
        raise ValueError('invalid publication tag')
    basic = base64.b64encode((os.environ['GH_ACTOR'] + ':' + os.environ['GH_TOKEN']).encode()).decode()
    request = urllib.request.Request(
        'https://ghcr.io/token?service=ghcr.io&scope=repository:' + REPOSITORY + ':pull,push',
        headers={'Authorization': 'Basic ' + basic})
    with urllib.request.urlopen(request, timeout=60) as response:
        token = json.load(response)['token']
    if not isinstance(token, str) or not token:
        raise ValueError('missing registry bearer token')
    request = urllib.request.Request('https://ghcr.io/v2/' + REPOSITORY + '/manifests/' + tag,
                                     headers={'Authorization': 'Bearer ' + token, 'Accept': ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            manifest = json.load(response)
        if not isinstance(manifest, dict):
            raise ValueError('invalid manifest response')
        return manifest
    except urllib.error.HTTPError as error:
        try:
            if error.code != 404:
                raise
            body = json.load(error)
            errors = body.get('errors') if isinstance(body, dict) else None
            if (not isinstance(errors, list) or len(errors) != 1 or
                    not isinstance(errors[0], dict) or errors[0].get('code') != 'MANIFEST_UNKNOWN'):
                raise ValueError('404 is not confirmed manifest absence') from None
            return None
        finally:
            error.close()


def publish(kind, tag, expected, children):
    """Guard both mutation paths. Serialized workflow writers, NOT registry CAS."""
    ref = IMAGE + ':' + tag
    if kind not in ('child', 'index'):
        raise ValueError('unknown publication kind')
    digests = [expected] if kind == 'child' else children
    if (not digests or any(not re.fullmatch('sha256:[0-9a-f]{64}', d) for d in digests)
            or (kind == 'index' and (len(digests) != 2 or len(set(digests)) != 2))):
        raise ValueError('invalid expected image digests')
    manifest = authenticated_manifest(tag)
    if manifest is not None:
        if kind == 'child':
            valid = manifest.get('config', {}).get('digest') == expected
        else:
            actual = manifest.get('manifests', [])
            valid = len(actual) == 2 and {m.get('digest') for m in actual} == set(children)
        if not valid:
            raise ValueError('existing tag differs from tested image; refusing replacement')
        return
    if kind == 'child':
        subprocess.run(['docker', 'tag', 'bpy-runtime:candidate', ref], check=True)
        subprocess.run(['docker', 'push', ref], check=True)
    else:
        subprocess.run(['docker', 'buildx', 'imagetools', 'create', '--tag', ref,
                        *[IMAGE + '@' + digest for digest in children]], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', choices=['child', 'index'])
    parser.add_argument('--expected')
    parser.add_argument('--children', nargs=2)
    parser.add_argument('--download-layers', action='store_true')
    parser.add_argument('--source', required=True)
    parser.add_argument('--matrix', type=Path)
    parser.add_argument('--skip-published', action='store_true')
    parser.add_argument('--fingerprint')
    parser.add_argument('--tag')
    args = parser.parse_args()
    if args.publish:
        publish(args.publish, args.tag, args.expected, args.children)
        return
    if args.matrix:
        result = plan(json.loads(args.matrix.read_text()), args.source, args.skip_published)
    else:
        result = verify(args.tag, args.source, args.fingerprint, args.download_layers)
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
