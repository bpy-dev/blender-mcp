"""Real MCP stdio CPU/CUDA render with an existing tiny fixture."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def run(args):
    env = dict(os.environ)
    env.update(BLENDER_MCP_CLI_BACKEND='bpy', BLENDER_MCP_BPY_PYTHON=sys.executable)
    command = args.command or [sys.executable, '-m', 'blmcp']
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    source_hash = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
    start = time.monotonic()
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        ready = time.monotonic() - start
        code = """
import bpy, platform, sys
assert bpy.app.version_string.split()[0] == VERSION
assert sys.version_info[:2] == (3, 13)
s = bpy.context.scene
s.render.engine = 'CYCLES'
s.render.resolution_x = 64
s.render.resolution_y = 48
s.render.resolution_percentage = 100
s.cycles.samples = 2
s.cycles.use_denoising = False
s.cycles.use_sample_subset = False
s.render.use_border = False
s.render.use_crop_to_border = False
if s.animation_data:
    s.animation_data_clear()
for layer in s.view_layers:
    layer.samples = 0
prefs = bpy.context.preferences.addons['cycles'].preferences
if GPU:
    prefs.compute_device_type = 'CUDA'
    prefs.get_devices()
    for device in prefs.devices:
        device.use = device.type == 'CUDA'
    assert any(d.type == 'CUDA' and d.use for d in prefs.devices)
    s.cycles.device = 'GPU'
else:
    s.cycles.device = 'CPU'
s.render.filepath = OUTPUT
bpy.ops.render.render(write_still=True)
result = {'version': bpy.app.version_string, 'machine': platform.machine(),
          'device': s.cycles.device, 'compute_device_type': prefs.compute_device_type,
          'devices': [{'name': d.name, 'type': d.type, 'use': bool(d.use)} for d in prefs.devices]}
""".replace('VERSION', repr(args.version)).replace('GPU:', repr(args.gpu) + ':').replace('OUTPUT', repr(str(args.output / 'render.png')))
        response = await session.call_tool('execute_blender_code_for_cli', {
            'blend_file': str(args.fixture.resolve()), 'code': code})
        if response.isError:
            raise RuntimeError(str(response))
        receipt = json.loads(response.content[0].text)
        receipt.update(ready_seconds=ready, elapsed_seconds=time.monotonic()-start)
        data = (args.output / 'render.png').read_bytes()
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('missing PNG output')
        from PIL import Image
        with Image.open(args.output / 'render.png') as image:
            image.load()
            if image.size != (64, 48):
                raise ValueError('render dimensions differ from requested bounds')
        if hashlib.sha256(args.fixture.read_bytes()).hexdigest() != source_hash:
            raise ValueError('source fixture changed')
        receipt['source_sha256'] = source_hash
        receipt['render_sha256'] = hashlib.sha256(data).hexdigest()
        (args.output / 'receipt.json').write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--version', default='5.3.0')
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args))
