# Security

This project lets MCP clients execute Blender Python. Treat that as powerful code execution.

## Execution boundaries

- Live tools run through the Blender add-on and can inspect or mutate the open Blender session.
- Saved-file tools whose names end in `_for_cli` run in a fresh subprocess using either a Blender executable or a standalone `bpy` Python.
- The subprocess boundary improves crash isolation and state cleanup. It is **not** an operating-system sandbox.
- The local CPU preview tool enforces a single frame and bounded resolution, samples and subprocess duration. It does not provide memory quotas, hostile-code isolation, durable cancellation, or artifact authorization; see [its boundary](docs/local-preview.md). Use stdio on trusted local files, not as a multi-tenant service.
- Model-generated Python can read, write, or delete files and may access the network with the permissions of the invoking user/process.

## Recommended use

- Run on disposable copies of `.blend` files.
- Use a dedicated working directory for agent outputs.
- Do not expose secrets, private source trees, credentials, or unrelated user files to agent-managed workspaces.
- Prefer explicit output paths and never ask agents to overwrite source `.blend` files.
- For benchmark trials, keep prompts, manifests, validators, raw captures, and answer/oracle files outside the model-controlled workspace.

## Configuration cautions

- `BLENDER_PATH` must point to a Blender executable, not Python.
- `BLENDER_MCP_BPY_PYTHON` must point to a Python interpreter that can import the intended standalone `bpy` build.
- Installing standalone `bpy` does not change the `bpy` module embedded in a running Blender application.

## Reporting issues

When reporting security-sensitive issues, include the smallest reproducible command/configuration and whether it used the live add-on path, Blender executable backend, or standalone `bpy` backend. Do not include credentials or private `.blend` assets.
