# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Opt-in real standalone bpy + MCP stdio acceptance (no live UI required)."""

__all__ = ()

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blmcp.tools_helpers.blender_cli import run_blender_cli
from mcp.client.stdio import stdio_client

from mcp import ClientSession, StdioServerParameters


@unittest.skipUnless(os.environ.get('BLENDER_MCP_BPY_PYTHON'), 'requires real bpy interpreter')
class TestLocalCPUSession(unittest.TestCase):
    def test_inspect_edit_preview_fork(self):
        from PIL import Image

        with tempfile.TemporaryDirectory(prefix='local-session-') as temporary:
            directory = Path(os.environ.get('BLMCP_LOCAL_EVIDENCE', temporary)).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            source = directory / 'source.blend'
            checkpoint = directory / 'checkpoint.blend'
            fork = directory / 'fork.blend'
            self.assertFalse(source.exists(), 'use a fresh evidence directory')
            env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LD_LIBRARY_PATH')
                   if key in os.environ}
            env.update(BLENDER_MCP_CLI_BACKEND='bpy',
                       BLENDER_MCP_BPY_PYTHON=os.environ['BLENDER_MCP_BPY_PYTHON'])
            setup = """
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add(size=2)
plane = bpy.context.object
plane.name = 'PreviewPlane'
material = bpy.data.materials.new('Color')
material.use_nodes = True
nodes = material.node_tree.nodes
nodes.clear()
emission = nodes.new('ShaderNodeEmission')
emission.inputs['Color'].default_value = (0, 0, 1, 1)
output = nodes.new('ShaderNodeOutputMaterial')
material.node_tree.links.new(emission.outputs[0], output.inputs['Surface'])
plane.data.materials.append(material)
bpy.ops.object.camera_add(location=(0, 0, 5))
camera = bpy.context.object
camera.data.type = 'ORTHO'
camera.data.ortho_scale = 4
scene = bpy.context.scene
scene.camera = camera
scene.render.engine = 'CYCLES'
scene.render.resolution_x = 999
scene.render.resolution_y = 777
scene.render.resolution_percentage = 50
scene.cycles.use_sample_subset = True
scene.cycles.sample_offset = 3
scene.cycles.sample_subset_length = 12
scene.cycles.samples = 1000
scene.cycles.keyframe_insert(data_path='samples', frame=1)
bpy.context.view_layer.samples = 1000
scene.render.use_border = True
scene.render.use_crop_to_border = True
scene.view_settings.view_transform = 'Standard'
""" + '\nbpy.ops.wm.save_as_mainfile(filepath=' + repr(str(source)) + ')'
            subprocess.run([env['BLENDER_MCP_BPY_PYTHON'], '-c', setup],
                           env=env, check=True, capture_output=True, timeout=60)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            async def exercise():
                params = StdioServerParameters(command=sys.executable, args=['-m', 'blmcp'], env=env)
                async with (stdio_client(params) as (read, write),
                            ClientSession(read, write) as session):
                    await session.initialize()
                    transcript = []

                    async def call(name, arguments):
                        response = await session.call_tool(name, arguments)
                        transcript.append({'tool': name, 'result': response.model_dump(mode='json')})
                        self.assertFalse(response.isError, response)
                        return response

                    async def execute(path, code, output=None):
                        args = {'blend_file': str(path), 'code': code}
                        if output:
                            args['expected_output_blend'] = str(output)
                        result = await call('execute_blender_code_for_cli', args)
                        return json.loads(result.content[0].text)

                    failure = await session.call_tool('execute_blender_code_for_cli', {
                        'blend_file': str(source), 'code': "raise ValueError('deliberate failure')"})
                    self.assertTrue(failure.isError)
                    self.assertIn('deliberate failure', failure.content[0].text)
                    transcript.append({'script_failure': failure.model_dump(mode='json')})
                    initial = await execute(source, "import bpy; result={'objects': list(bpy.data.objects.keys())}")
                    self.assertIn('PreviewPlane', initial['objects'])
                    edited = await execute(source,
                        "import bpy\n"
                        "bpy.data.materials['Color'].node_tree.nodes['Emission'].inputs['Color'].default_value=(1,0,0,1)\n"
                        f"bpy.ops.wm.save_as_mainfile(filepath={str(checkpoint)!r})\n"
                        "result={'saved': True}", checkpoint)
                    checkpoint_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
                    self.assertEqual(edited['_checkpoint']['output_sha256'], checkpoint_hash)
                    reopened = await execute(checkpoint,
                        "import bpy; result={'color': list(bpy.data.materials['Color'].node_tree.nodes['Emission'].inputs['Color'].default_value)}")
                    self.assertEqual(reopened['color'], [1, 0, 0, 1])

                    for dimension in ('width', 'height'):
                        for value in (1, 2, 3):
                            args = {'blend_file': str(checkpoint), 'width': 4, 'height': 4,
                                    'samples': 2, 'frame': 1, 'frame_count': 1, 'timeout_seconds': 60}
                            args[dimension] = value
                            rejected = await session.call_tool('get_local_render_as_image_for_cli', args)
                            self.assertTrue(rejected.isError)
                            self.assertIn(f'{dimension} limit is 4..2048', rejected.content[0].text)
                            transcript.append({'invalid_dimension': dimension, 'value': value,
                                               'result': rejected.model_dump(mode='json')})

                    async def render(path, name, channel, width=64, height=48):
                        response = await call('get_local_render_as_image_for_cli', {
                            'blend_file': str(path), 'width': width, 'height': height,
                            'samples': 2, 'frame': 1, 'frame_count': 1, 'timeout_seconds': 60})
                        content = response.content[0]
                        data = base64.b64decode(content.data)
                        (directory / name).write_bytes(data)
                        image = Image.open(io.BytesIO(data))
                        image.load()
                        self.assertEqual(image.size, (width, height))
                        self.assertEqual((content.meta['width'], content.meta['height']), (width, height))
                        if channel is not None:
                            pixel = image.convert('RGB').getpixel((width // 2, height // 2))
                            self.assertGreater(pixel[channel], 200)
                            self.assertLess(pixel[(channel + 1) % 3], 20)
                        self.assertEqual(content.meta['runtime']['samples'], 2)
                        self.assertIs(content.meta['runtime']['use_sample_subset'], False)
                        self.assertEqual(content.meta['runtime']['view_layer_samples'], [0])
                        self.assertEqual(content.meta['source_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                        self.assertEqual(content.meta['render_sha256'], hashlib.sha256(data).hexdigest())

                    # At 4x4 the plane edges overlap the pixel filter; the existing
                    # 64x48 previews below retain the center-color assertions.
                    await render(checkpoint, 'minimum.png', None, width=4, height=4)
                    await render(checkpoint, 'preview.png', 0)
                    await execute(checkpoint,
                        "import bpy\n"
                        "bpy.data.materials['Color'].node_tree.nodes['Emission'].inputs['Color'].default_value=(0,1,0,1)\n"
                        f"bpy.ops.wm.save_as_mainfile(filepath={str(fork)!r})\n"
                        "result={'saved': True}", fork)
                    await render(fork, 'fork.png', 1)
                    self.assertEqual(hashlib.sha256(checkpoint.read_bytes()).hexdigest(), checkpoint_hash)
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
                    (directory / 'transcript.json').write_text(json.dumps(transcript, indent=2))
            asyncio.run(exercise())
            # Characterize the existing subprocess deadline: a real bpy child
            # publishes its PID, then stalls. It must be reaped before return.
            if os.name == 'posix':
                pid_path = directory / 'timeout.pid'
                with (mock.patch.dict(os.environ, env, clear=True),
                      self.assertRaisesRegex(RuntimeError, 'timed out after 2s')):
                    run_blender_cli(str(source),
                        "import os, time\nfrom pathlib import Path\n"
                        f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
                        "time.sleep(30)", timeout=2)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_path.read_text()), 0)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)


if __name__ == '__main__':
    unittest.main()
