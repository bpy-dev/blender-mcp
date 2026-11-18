# Local CPU preview milestone

## Status and boundary

Merged in [PR #3](https://github.com/bpy-dev/blender-mcp/pull/3) at
`3d1a394096a273b2bd17e7db5378776b02c4c1d6`; no deployment. This is the first
bounded increment of the local agent loop, **not** completion of Fstack PR23's
entire local acceptance or the full Hermes plugin. This runtime increment changes
no Fstack, website, buildbpy, cloud or billing code.

Audit base: `bpy-dev/blender-mcp` `e8ac4088d4a3e0469d3eff9f9bb2dd717e899219`.
The existing registered surface supplies live-session tools and saved-file
inspection, arbitrary execution with explicit save, checkpoint hash receipts and
runtime API lookup. All pre-existing tools remain registered. In particular,
`get_render_as_image_for_cli` deliberately requires CUDA for historical benchmark
comparability; this change does not silently turn that benchmark into CPU work.

The missing small behavior was a usable CPU preview without CUDA. The additive
`get_local_render_as_image_for_cli` reuses `run_blender_cli` and its deadline and
cleanup behavior. Existing `execute_blender_code_for_cli` remains the edit/save/
fork interface; no session framework or scheduler was added. A saved output path
plus hash receipt is **not** yet an immutable checkpoint/dependency manifest.

## Launch from the public repository

No private demonstration harness is required. Install Git and
[uv](https://docs.astral.sh/uv/), then use the exact merged source:

```sh
git clone https://github.com/bpy-dev/blender-mcp.git
cd blender-mcp
git checkout --detach 3d1a394096a273b2bd17e7db5378776b02c4c1d6
uv sync --project mcp --frozen
uv run --project mcp --frozen blender-mcp --help
```

A working standalone bpy interpreter is a separate prerequisite; the MCP
package does not install Blender. Follow the platform-specific setup and wheel
provenance notes in [the backend guide](../readme_bpy_backend.rst). Verify the
interpreter you actually installed (replace the absolute path):

```sh
/absolute/path/to/bpy-python -c \
  'import bpy, sys; print(sys.version, bpy.app.version_string, bpy.app.build_hash)'
```

The backend guide's 5.2.1 macOS matrix is not a Linux wheel availability promise.
This CPU increment was also exercised on Linux aarch64 with native bpy 5.2.0 LTS;
use the real-runtime acceptance below to check your own installation.

Configure your MCP client's stdio server entry, substituting both absolute paths:

```json
{
  "mcpServers": {
    "blender": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/blender-mcp/mcp", "run", "--frozen", "blender-mcp", "--transport", "stdio"],
      "env": {
        "BLENDER_MCP_CLI_BACKEND": "bpy",
        "BLENDER_MCP_BPY_PYTHON": "/absolute/path/to/bpy-python"
      }
    }
  }
}
```

`mcpServers` is a common client configuration shape, not a universal config-file
format. Translate this command/argument/environment entry using your client's
MCP instructions; do not replace unrelated servers or profile settings. The client
starts the stdio process and owns its lifetime. Running it in a shell alone waits
for protocol messages; it is not a chat interface. No HTTP listener is needed.

First run the real-runtime acceptance below from the repository root. It creates
its own tiny saved scene and checks actual MCP edit/save/reopen/fork/preview calls,
so no model account, private scripts, or pre-existing scene is needed. To retain
its editable scenes and PNGs, set `BLMCP_LOCAL_EVIDENCE` to a fresh absolute output
directory. `BLENDER_MCP_BPY_PYTHON` must be set or the test **skips** rather than
proving rendering. If an agent host injects a `PYTHONPATH`, use a clean shell or
unset that variable for these commands so unrelated test packages cannot shadow
this repository's `tests` package.

For model-driven use, connect the client, list tools and choose the saved-file
`_for_cli` tools, not live-UI tools that require the Blender add-on. Start with a
trusted `.blend` containing a camera. Ask the agent to inspect it, make an edit
using `execute_blender_code_for_cli`, explicitly save to a distinct absolute
checkpoint path, call the CPU preview below, then save a revision to a second
new path. Preserve the original. Inspect returned PNGs and reopen saved files;
text claiming a save or render is not verification. Keep execution approval
on-request and review each generated-code operation; do not bypass client sandbox
or approval controls to make tools run. A refused operation needs the client's
normal approval flow.

Model access/authentication is configured through the client's supported login
flow. Rendering is local; model inference may still be hosted and billable.
The model-free acceptance is not an autonomous-agent test, and local rendering
is not proof of offline inference or a production network sandbox.

## Agent call

Call the existing MCP server over stdio with a trusted saved scene:

```json
{
  "name": "get_local_render_as_image_for_cli",
  "arguments": {
    "blend_file": "/absolute/path/to/checkpoint.blend",
    "width": 512,
    "height": 512,
    "samples": 16,
    "frame": 1,
    "frame_count": 1,
    "timeout_seconds": 60
  }
}
```

All limits are required integers, not coerced strings, booleans or floats.
Resolution is 4..2048 per side (Blender’s native minimum is 4), samples 1..64, deadline 1..120 seconds,
frame -1048574..1048574, frame count exactly one. Invalid limits fail before
opening the source or starting Blender. The selected backend must have Cycles
CPU support. No GPU detection, provider integration or cloud fallback occurs.

The child renders on CPU with two threads, explicit dimensions and samples,
100% scale, and border/crop, multiview, compositor, sequencer, adaptive sampling,
denoising and sample subsets disabled. Scene-level animation is evaluated at the selected frame
and then frozen in the subprocess copy so render-time re-evaluation cannot
restore an animated sample count above the requested cap; view-layer sample
overrides are reset to use the capped scene setting. These are **preview
overrides**, not fidelity to every
saved render configuration. No overrides are saved. The child decodes the PNG
and checks its dimensions. The MCP image response contains PNG bytes with
`_meta` source path/hash, render hash, requested limits and Blender runtime/build
provenance. The caller may save those bytes; the temporary renderer output is
removed. It is not a hosted download URL or an authorization token.

A preview deadline covers the Blender subprocess, including startup, not all
parent filesystem I/O. Timeout kills and reaps the direct child through the
existing subprocess implementation. Caller cancellation is not a durable cancel
operation. This is not an OS sandbox, a memory quota, descendant-process
containment, or protection against malicious scripts/drivers/add-ons. Trusted
local files and single-writer operation are required. Before/after source hashes
detect ordinary concurrent changes, not adversarial change-and-restore races.

## Reproduce

Portable contracts (also run by `.github/workflows/local-preview.yml`):

```sh
uv sync --project mcp --frozen
uv run --project mcp --frozen python -m unittest -v \
  tests.test_local_render tests.test_blender_cli \
  tests.test_tool_listing tests.test_documentation \
  tests.test_preview_compatibility tests.test_mcp_server \
  tests.integration.test_local_cpu_session
```

Real-runtime acceptance (requires a separately installed, working standalone
bpy interpreter; Pillow is used only by the independent test image decoder):

```sh
BLENDER_MCP_BPY_PYTHON=/absolute/path/to/bpy-python \
  uv run --project mcp --with pillow python -m unittest -v \
  tests.integration.test_local_cpu_session
```

Set `BLMCP_LOCAL_EVIDENCE` to a **fresh** directory to retain `source.blend`,
`checkpoint.blend`, `fork.blend`, `preview.png`, `fork.png`, `minimum.png`, `transcript.json` and
(on POSIX) `timeout.pid`. Without the environment variable, temporary outputs
are cleaned up. Without `BLENDER_MCP_BPY_PYTHON`, the integration case explicitly
skips; a portable CI pass does not certify real rendering.

The real acceptance creates a blue emissive plane, inspects it through MCP
stdio, edits/saves a red checkpoint, reopens it in another process, renders a
red preview, edits/saves a green fork and renders again. Pillow fully decodes
both images, verifies 64x48 dimensions and expected center-pixel colors, and
checks response hashes. It also rejects widths and heights 1, 2 and 3 through
MCP and decodes a 4x4 minimum-size preview. Original and checkpoint bytes remain unchanged after
fork/render. It also characterizes real script failure and a real sleeping bpy
child's timeout/reaping. This timeout check is of the existing runner, not proof
of a cancellation API.

## Evidence from this increment

- New-tool absence and later unimplemented rendering were observed RED through
  registered MCP calls; the latter used actual stdio and real bpy edit/reopen.
- An additional RED rejected implicit boolean/string coercion before source
  lookup; StrictInt validation made it GREEN.
- Linux aarch64, CPython 3.13.12, existing native bpy 5.2.0 LTS build
  `fbe6228777e7`: real CPU renders passed. This is not the previously documented
  macOS 5.2.1 matrix and does not certify this host's NVIDIA GB10 GPU.
- The real workflow also passed with inherited Linux seccomp denial of
  `connect(2)`; IPv4 and IPv6 negative controls returned EPERM before execution.
  Only a small runtime environment allowlist reached MCP/bpy, not cloud API-key
  environment variables. This evidence filter is not a product network sandbox.
- The host denied unprivileged network namespace creation; no host policy was
  changed. The seccomp proof added process-local restrictions instead.

## Next incomplete boundary

An independently reviewed/published CPU-preview increment is useful, but the
full local milestone still needs immutable dependency/runtime manifests,
owner-scoped artifacts, stable operation identity and changed-input retry
semantics, explicit cancellation and its child cleanup proof. Full enhanced
Hermes plugin packaging and live-UI parity remain separate. No product choice
or cloud credentials are needed to review this increment; no spending or
provider execution is authorized by it.
