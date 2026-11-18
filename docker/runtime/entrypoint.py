"""Offline entrypoint: verify baked interpreter and hand off to MCP stdio."""
import json
import os
from pathlib import Path
import sys

def main():
    import bpy
    expected = json.loads(Path('/opt/runtime/release.lock.json').read_text())['version']
    if bpy.app.version_string.split()[0] != expected:
        raise RuntimeError('baked bpy version differs from release lock')
    if os.environ.get('BLENDER_MCP_CLI_BACKEND') != 'bpy':
        raise RuntimeError('runtime image requires the baked bpy backend')
    if os.environ.get('BLENDER_MCP_BPY_PYTHON') != sys.executable:
        raise RuntimeError('runtime image requires the baked bpy interpreter')
    os.execv(sys.executable, [sys.executable, '-m', 'blmcp', *sys.argv[1:]])

if __name__ == '__main__':
    main()
