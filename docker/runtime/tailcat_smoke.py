"""Native image Tailcat acceptance. Client secrets exist ONLY on the host.

Run with host Python/Pillow and Docker; no provider credentials or paid pods.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any
from tailcat_prepare import prepare


class Wire:
    def __init__(self, tailcat, directory, address):
        home = str(directory / 'client')
        self.p = subprocess.Popen([tailcat, address, '80'], env={
            'PATH': '/usr/bin:/bin', 'HOME': home, 'XDG_CONFIG_HOME': home},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.q = queue.Queue(); self.n = 0
        def read():
            for line in self.p.stdout:
                self.q.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()

    def call(self, method, params, timeout=90):
        self.n += 1
        self.p.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': self.n,
                                      'method': method, 'params': params}) + '\n').encode())
        self.p.stdin.flush()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            response = self.q.get(timeout=max(.01, end - time.monotonic()))
            if response.get('id') == self.n:
                assert 'error' not in response, response
                result = response['result']; assert not result.get('isError'), result
                return result
        raise TimeoutError('MCP call deadline')

    def initialize(self):
        self.call('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                'clientInfo': {'name': 'runtime-tailcat-gate', 'version': '1'}}, 25)
        self.p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.p.stdin.flush()

    def tool(self, name, arguments):
        return self.call('tools/call', {'name': name, 'arguments': arguments})

    def close(self):
        self.p.terminate()
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill(); self.p.wait()
        self.p.stdin.close(); self.p.stdout.close()


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=True, **kwargs)


def run(args):
    from PIL import Image
    root = Path(__file__).resolve().parents[2]
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o777)  # Disposable render output only; never put identity files here.
    name = 'bpy-tailcat-test-' + uuid.uuid4().hex[:12]
    fixture = '/test/benchmarks/token_efficiency/fixtures/render_source.blend'
    report: dict[str, Any] = {'image': docker('image', 'inspect', args.image, '--format', '{{.Id}}').stdout.strip()}
    with tempfile.TemporaryDirectory(prefix='bpy-client-private-') as tmp:
        private = Path(tmp)
        # Copy the actual baked native binary; do not download at startup.
        cid = docker('create', args.image).stdout.strip()
        try:
            docker('cp', cid + ':/usr/local/bin/tailcat', str(private / 'tailcat'))
        finally:
            docker('rm', cid)
        tc = str(private / 'tailcat')
        report['tailcat_version'] = subprocess.check_output([tc, '--version'], text=True).strip()
        assert report['tailcat_version'] == 'v0.7.0'
        session = private / 'session'; bad = private / 'wrong'
        env = prepare(session, tc, 300); prepare(bad, tc, 300)
        address = (session / 'server.address').read_text()
        command = ['run', '-d', '--name', name, '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,nodev,mode=1777',
                   '--env-file', str(session / 'docker.env'),
                   '--mount', f'type=bind,src={root},dst=/test,readonly',
                   '--mount', f'type=bind,src={out},dst=/workspace']
        if args.gpu:
            command += ['--gpus', 'all']
        started = time.monotonic()
        docker(*command, args.image, '--transport', 'tailcat')
        clients = []
        try:
            time.sleep(3)
            denied = Wire(tc, bad, address); clients.append(denied)
            try:
                denied.initialize()
                raise AssertionError('wrong identity reached MCP')
            except queue.Empty:
                report['wrong_identity_correct_address_denied'] = True
            finally:
                denied.close(); clients.remove(denied)
            processes = docker('top', name, '-eo', 'pid,args').stdout
            assert '-m blmcp' not in processes
            denied_logs = docker('logs', name)
            assert 'bpy: authenticated MCP session started' not in denied_logs.stdout + denied_logs.stderr
            report['denied_mcp_dispatch_count'] = 0
            report['mcp_processes_after_denied'] = 0
            client = Wire(tc, session, address); clients.append(client); client.initialize()
            report['ready_seconds_including_denied_probe'] = time.monotonic() - started
            report['tool_count'] = len(client.call('tools/list', {})['tools'])
            result = client.tool('execute_blender_code_for_cli', {'blend_file': fixture,
                'code': "import bpy,os; result={'version':bpy.app.version_string,'uid':os.getuid(),'secret_env_present':any(k.startswith('BPY_TAILCAT_') for k in os.environ)}"})
            report['inspect'] = result
            text = json.loads(result['content'][0]['text'])
            assert text['version'].split()[0] == args.version and text['uid'] == 10001
            assert text['secret_env_present'] is False
            render = client.tool('get_local_render_as_image_for_cli', {'blend_file': fixture,
                'width': 512, 'height': 384, 'samples': 12, 'frame': 1,
                'frame_count': 1, 'timeout_seconds': 120})
            def image_receipt(response, label):
                image = next(c for c in response['content'] if c['type'] == 'image')
                data = base64.b64decode(image['data'])
                im = Image.open(io.BytesIO(data)); im.load(); assert im.size == (512, 384)
                digest = hashlib.sha256(data).hexdigest()
                if label == 'cpu':
                    assert image['_meta']['render_sha256'] == digest
                source = root / 'benchmarks/token_efficiency/fixtures/render_source.blend' if label == 'cpu' else out / 'gpu.blend'
                assert image['_meta']['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
                (out / (label + '.png')).write_bytes(data)
                report[label] = {'sha256': digest, 'size': list(im.size), 'meta': image['_meta']}
            image_receipt(render, 'cpu')
            if args.gpu:
                code = """import bpy
s=bpy.context.scene
s.render.engine='CYCLES'
s.render.resolution_x=512
s.render.resolution_y=384
s.render.resolution_percentage=100
s.cycles.samples=12
s.cycles.use_denoising=False
s.cycles.use_sample_subset=False
if s.animation_data: s.animation_data_clear()
for layer in s.view_layers: layer.samples=0
s.render.use_border=False
s.render.use_crop_to_border=False
result={'prepared':True}
"""
                client.tool('execute_blender_code_for_cli', {'blend_file': fixture,
                    'code': code + "\nbpy.ops.wm.save_as_mainfile(filepath='/workspace/gpu.blend')\nimport os; os.chmod('/workspace/gpu.blend', 0o644)\n", 'expected_output_blend': '/workspace/gpu.blend'})
                image_receipt(client.tool('get_render_as_image_for_cli', {'blend_file': '/workspace/gpu.blend'}), 'gpu')
                # Independent second actual render includes POST-render telemetry;
                # do not relabel an enumeration before image-tool render as proof.
                telemetry = code + """
prefs=bpy.context.preferences.addons['cycles'].preferences
prefs.compute_device_type='CUDA'
prefs.get_devices()
for d in prefs.devices: d.use=d.type=='CUDA'
s.cycles.device='GPU'
s.render.filepath='/workspace/gpu-telemetry.png'
bpy.ops.render.render(write_still=True)
import os; os.chmod('/workspace/gpu-telemetry.png', 0o644)
result={'device':s.cycles.device,'compute':prefs.compute_device_type,'samples':s.cycles.samples,'devices':[{'name':d.name,'type':d.type,'use':bool(d.use)} for d in prefs.devices]}
assert s.cycles.device=='GPU'
assert any(d.type=='CUDA' and d.use for d in prefs.devices)
assert all(not d.use for d in prefs.devices if d.type!='CUDA')
"""
                report['post_render_cuda_only'] = client.tool('execute_blender_code_for_cli', {'blend_file': '/workspace/gpu.blend', 'code': telemetry})
            client.close(); clients.remove(client)
            reconnect = Wire(tc, session, address); clients.append(reconnect); reconnect.initialize()
            report['reconnect_tool_count'] = len(reconnect.call('tools/list', {})['tools'])
            reconnect.close(); clients.remove(reconnect)
            logs = docker('logs', name).stdout + docker('logs', name).stderr
            assert address not in logs and env['BPY_TAILCAT_SERVER_KEY'] not in logs
            assert logs.count('bpy: authenticated MCP session started') == 2
            report['allowed_mcp_dispatch_count'] = 2
            report['provider_log_bytes'] = len(logs)
        finally:
            for client in clients: client.close()
            state = docker('inspect', name, '--format', '{{json .State}}').stdout
            (out / 'server-state.json').write_text(state)
            # Only wrapper diagnostics can reach container logs; upstream is discarded.
            log = docker('logs', name)
            (out / 'server.log').write_text(log.stdout + log.stderr)
            docker('rm', '-f', name)
        # A fresh server with a short absolute expiry: no private client goes in it.
        short = private / 'short'; prepare(short, tc, 12)
        command[command.index(str(session / 'docker.env'))] = str(short / 'docker.env')
        docker(*command, args.image, '--transport', 'tailcat')
        client = None
        try:
            time.sleep(2)
            client = Wire(tc, short, (short / 'server.address').read_text()); client.initialize()
            # Leave a real MCP + bpy child running across expiry.
            client.p.stdin.write((json.dumps({'jsonrpc':'2.0','id':99,'method':'tools/call','params':{
                'name':'execute_blender_code_for_cli','arguments':{'blend_file':fixture,
                'code':'import time; time.sleep(120); result=True'}}})+'\n').encode());client.p.stdin.flush()
            time.sleep(1)
            rows = docker('top', name, '-eo', 'pid,args').stdout.splitlines()[1:]
            assert len(rows) >= 4, 'expected supervisor, listener, MCP and bpy before expiry'
            owned = [int(row.split()[0]) for row in rows]
            report['expiry_live_process_count'] = len(owned)
            report['expiry_exit_code'] = int(docker('wait', name, timeout=25).stdout.strip())
            assert report['expiry_exit_code'] == 0
            assert docker('inspect', name, '--format', '{{.State.Running}}').stdout.strip() == 'false'
            assert all(not Path(f'/proc/{pid}').exists() for pid in owned)
            expired = Wire(tc, short, (short / 'server.address').read_text())
            try:
                try:
                    expired.call('initialize', {'protocolVersion':'2024-11-05','capabilities':{},
                        'clientInfo':{'name':'expired','version':'1'}}, timeout=3)
                    raise AssertionError('expired session admitted')
                except queue.Empty:
                    report['expired_identity_denied'] = True
            finally:
                expired.close()
            report['expired_listener_and_children_stopped'] = True
        finally:
            if client: client.close()
            docker('rm', '-f', name)
        # Empty/malformed opt-in fails closed, emitting only the constant error.
        for value in ['', 'none', 'nodekey:invalid']:
            badenv = dict(env); badenv['BPY_TAILCAT_ALLOW'] = value
            path = private / 'invalid.env'
            path.write_text(''.join(k+'='+v+'\n' for k,v in badenv.items()))
            result = subprocess.run(['docker','run','--rm','--env-file',str(path),args.image,
                                     '--transport','tailcat'], capture_output=True, text=True, timeout=20)
            assert result.returncode == 1 and not result.stdout
            assert result.stderr.strip() == 'Tailcat session failed; check private launch configuration'
        report['malformed_startup_rejected'] = True
        for path in out.glob('*'):
            if path.is_file() and path.suffix in ('.json', '.log', '.md'):
                assert address.encode() not in path.read_bytes()
        report['status'] = 'PASS'
        client_identity = json.loads((session / 'client/tailcat/keys/client-default.private.json').read_text())
        server_identity = json.loads(env['BPY_TAILCAT_SERVER_KEY'])
        secrets = [address, client_identity['Private'], server_identity['Private'],
                   server_identity['Public']['PresharedKey']]
        assert client_identity['Private'] not in json.dumps(env)
        for path in session.rglob('*'):
            if path.is_file():
                assert path.stat().st_mode & 0o777 == 0o600
        for path in out.rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                assert all(secret.encode() not in data for secret in secrets), 'secret in evidence'
        payload = json.dumps(report, indent=2)
        assert all(secret not in payload for secret in secrets)
        (out / 'tailcat-receipt.json').write_text(payload)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', default='5.3.0')
    parser.add_argument('--gpu', action='store_true')
    run(parser.parse_args())
