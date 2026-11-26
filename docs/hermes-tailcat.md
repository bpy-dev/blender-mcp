# Hermes over an existing Tailcat session

This recipe uses Hermes's native MCP client and the unchanged bpy.dev stdio
server. It does not require a custom Hermes plugin or a new transport adapter.
Local rendering requires no Fstack account; hosted model inference is separate.

First follow [local-preview.md](local-preview.md) for frozen MCP dependency
installation and a separately verified standalone bpy interpreter. For the
Tailcat runtime security/deployment contract see `docker/runtime/README.md`.
Use a trusted saved `.blend` seed; saved-file tools cannot open a nonexistent file.
Live UI tools still require the add-on and are not headless-compatible.

Generate distinct client and server Tailcat identities in private directories.
Keep the client private key on the client. Launch a fixed-command `tailcat serve
--key=new --allow=<client-public-key> exec -- <mcp-python> -m blmcp --transport
stdio` with `BLENDER_MCP_CLI_BACKEND=bpy` and the explicit
`BLENDER_MCP_BPY_PYTHON` interpreter. Use `TAILCAT_ADDR_FILE` in a mode-0700
directory rather than printing the capability address. Do not pass model/cloud
credentials into the runtime environment. Set a finite supervisor deadline and
terminate/reap only the session's owned process group on completion.

In a **new isolated** `HERMES_HOME` outside the default `.hermes` directory
ancestry, add this native configuration (replace placeholders; keep the real
capability address private). Validate the resolved home before launching a
gateway. A nested `.hermes/cache/scratch` home can still be treated as belonging
to the host gateway. Keep any temporary auth copy physically in private scratch,
with only a temporary symlink from the isolated home; remove both after exit.
Never stop or restart the default gateway to repair an acceptance fixture.

Configuration:

```yaml
model:
  provider: openai-codex
  default: YOUR_AUTHORIZED_MODEL
memory:
  memory_enabled: false
  user_profile_enabled: false
mcp_servers:
  bpy:
    command: /absolute/path/to/tailcat
    args: [PRIVATE_SESSION_ADDRESS, '80']
    env:
      HOME: /private/client-home
      XDG_CONFIG_HOME: /private/client-config
    connect_timeout: 60
    timeout: 150
    sampling:
      enabled: false
```

Use your supported Hermes model authentication. Do not print or place auth in
the prompt. Launch the isolated CLI with an allowlisted environment: inherited
messaging tokens can otherwise cause its gateway to attempt unrelated platform
connections. Do not stop an existing default-profile gateway to resolve such a
conflict. Use `hermes chat --cli -t bpy` (the configured server name, not `mcp`).
The installed gateway CLI may reject `--run-budget`; use its documented
`agent.run_budget_seconds` config field if a budget is required. Preserve normal
approval controls.

Example prompt:

> Inspect my trusted seed. Choose and build a colorful miniature lighthouse
> diorama with camera, lights and materials. Save a distinct checkpoint using
> execute_blender_code_for_cli and expected_output_blend. Reopen it, preview with
> get_local_render_as_image_for_cli at 768x576, 16 samples, frame 1, frame_count 1,
> timeout_seconds 120. Make a meaningful creative variation, save a distinct
> fork, reopen and preview. Preserve the original and checkpoint. Do not use
> external assets or provider APIs. Report actual artifacts and errors.

Hermes discovers all registered tools; resolve actual discovered names instead
of assuming a prefix. This runtime currently exposes no dedicated upload/download
tool. The existing trusted arbitrary-code execution surface can read an owned
saved file and return base64 plus SHA-256 for client-side decoding; this is not a
new owner-scoped artifact service. PNG bytes arrive as MCP ImageContent; Hermes
also persists them locally and exposes provenance through structuredContent.
Independently decode PNGs and hash-check and reopen retrieved `.blend` files.

Same-host Tailcat success is not cross-host NAT acceptance, forced DERP fallback,
live-UI parity, Claude/ChatGPT parity, or hosted billing acceptance. The process
is not an OS sandbox. Never expose arbitrary-code MCP to untrusted users.
