# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local CPU preview contract, through the registered MCP tool."""

__all__ = ()

import asyncio
import unittest
from unittest import mock

from blmcp.tools import execute_blender_code
from mcp.server.fastmcp import FastMCP


class TestLocalRender(unittest.TestCase):
    def test_dimensions_below_blender_minimum_rejected_before_execution(self):
        async def check():
            server = FastMCP('test')
            execute_blender_code.register(server)
            for dimension in ('width', 'height'):
                for value in (1, 2, 3):
                    with self.subTest(dimension=dimension, value=value):
                        args = {'blend_file': '/missing.blend', 'width': 4, 'height': 4,
                                'samples': 2, 'timeout_seconds': 30, 'frame': 1, 'frame_count': 1}
                        args[dimension] = value
                        with (mock.patch.object(execute_blender_code.Path, 'resolve') as resolve,
                              mock.patch.object(execute_blender_code, 'run_blender_cli') as run):
                            resolve.side_effect = AssertionError('source lookup reached')
                            with self.assertRaisesRegex(Exception, f'{dimension} limit is 4..2048'):
                                await server.call_tool('get_local_render_as_image_for_cli', args)
                            resolve.assert_not_called()
                            run.assert_not_called()
        asyncio.run(check())

    def test_invalid_limits_are_rejected_before_opening_source(self):
        async def check():
            server = FastMCP('test')
            execute_blender_code.register(server)
            tools = {tool.name for tool in await server.list_tools()}
            self.assertIn('get_local_render_as_image_for_cli', tools)
            for change in ({'width': 0}, {'height': 2049}, {'samples': 65},
                           {'timeout_seconds': 121}, {'timeout_seconds': 0},
                           {'frame_count': 2}, {'width': True}, {'width': '64'}, {'samples': 1.5}):
                args = {'blend_file': '/missing.blend', 'width': 64, 'height': 64,
                            'samples': 2, 'timeout_seconds': 30, 'frame': 1, 'frame_count': 1}
                args.update(change)
                with self.assertRaisesRegex(Exception, 'limit|frame_count|valid integer'):
                    await server.call_tool('get_local_render_as_image_for_cli', args)
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
