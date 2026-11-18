# Blender MCP

## Enhanced MCP developer preview

This branch packages an enhanced Blender MCP workflow for agentic Blender work. It keeps the upstream live Blender add-on/server model and adds saved-file/headless execution, selectable command-line backends, runtime Blender Python API lookup, subprocess/capture hardening, and benchmark tooling.

This is a modified distribution of Blender Lab's Blender MCP project. See [NOTICE.md](NOTICE.md) for provenance and [SECURITY.md](SECURITY.md) for the execution-risk model. Public `bpy.dev` website assets will live in a separate repository.

### BlenderBench result

[![BlenderBench result](https://bpy.dev/assets/blenderbench-result.webp)](https://huggingface.co/datasets/michaelgold/blenderbench-direct-results/resolve/7c2c43be4517aee4d76d2b445578988de186970c/video/blenderbench-complete-compilation.mp4)

The compilation covers **27 tasks / 270 rounds**. Its on-video “CLIP accuracy” label means
**CLIP image-embedding cosine similarity, not literal accuracy**. The linked immutable
H.264 video is 848×464, 134.9 seconds, and 10,161,884 bytes; SHA-256:
`04966320de680203baed6c01c3e211059350454139dc8a802c557cce24815c17`.

### What is enhanced

- **Saved-file tools:** `_for_cli` tools operate on `.blend` files in fresh subprocesses without requiring a live Blender UI session.
- **Local CPU previews:** the additive capped preview tool supports saved-file agent edit/save/fork workflows without CUDA; see [local milestone status and acceptance](docs/local-preview.md). Existing CUDA benchmark rendering is unchanged.
- **Backend selection:** use a Blender executable backend or a standalone `bpy` Python backend with `BLENDER_MCP_CLI_BACKEND`.
- **Runtime API lookup:** agents can query exact signatures, enums, defaults, and availability from the active Blender/`bpy` runtime instead of relying only on bundled docs.
- **Process isolation:** command-line executions are bounded by explicit timeouts, output caps, compact JSON framing, and source-file overwrite checks. This is crash/process-state isolation, not an OS sandbox.
- **Benchmark tooling:** deterministic token-efficiency tasks, exploratory creative comparison harnesses, and a [portable model-neutral full-27 BlenderBench runner](benchmarks/blenderbench_direct/README.md) with pinned inputs and post-generation scoring.

### Current publication status

Developer-preview cleanup is in progress. Before publishing, complete [PUBLICATION_CHECKLIST.md](PUBLICATION_CHECKLIST.md), especially package naming, fresh-clone install smoke tests, and tracked-file secret scanning.

## Overview

A lightweight MCP (Model Context Protocol) server for Blender.
It offers a natural language interface with Blender's Python API,
improving access to documentation, and allowing users to explore
and understand complex setups.

Read the documentation at [blender.org/lab/mcp-server](https://www.blender.org/lab/mcp-server/)

----

The project is deliberately small, maintainable, and does no more than
necessary. It has two components that communicate over a TCP socket:

- A **Blender add-on** that runs inside Blender and executes live-scene requests.
- An **MCP server** that runs as a separate process, launched by the
  MCP client (e.g. [Llama.cpp](https://projects.blender.org/lab/blender_mcp/wiki/Llama.cpp)).

The data flow is:
```
MCP Client  ⇐ MCP/stdio ⇒  blender-mcp  ⇐ TCP socket ⇒  Blender Add-on
```


## Blender Add-on

Located in ``addon/blender_mcp_addon/``.

A Blender extension that allows the MCP server to communicate with a
running Blender instance. It must be installed and enabled for connected
live-scene tools; saved-file ``_for_cli`` tools run without it.

The add-on provides a preferences panel for configuring the host, port,
and an optional auto-start setting.

### Functionality Overview

Note that this is intended to be a fairly minimal add-on.

Connectivity
   - Auto-start (optional), is non-blocking any issues can be viewed from the preferences.
   - Configurable polling intervals (active and idle rates) from preferences to avoid excessive overhead.
   - Client timeout protection - stalled connections are evicted.
   - Start/stop operators accessible from the preferences panel.
   - Deferred responses are supported only by the interactive add-on server;
     background mode requires requests to complete synchronously and rejects deferred results.




## MCP Server

Located in ``mcp/blmcp/``, installed as a Python package with the
entry point ``blender-mcp``.

An MCP client launches this process and communicates with it over
stdio. The server connects to the add-on's TCP socket to relay
requests to Blender.

``mcp/blmcp/data/``
   Data files bundled with the package.

   - ``prompts.yml`` provides instructions sent to the LLM at
     connection time.
   - ``api/`` contains Blender Python API reference in RST format.
   - ``manual/`` contains Blender user manual excerpts in RST format.

``mcp/blmcp/tools/``
   Each tool is a single module, auto-discovered at startup.
   Modules ending in ``_toolcode`` contain code that runs inside
   Blender (sent to the add-on for execution) and are skipped during
   discovery.

``mcp/blmcp/tools_helpers/``
   Shared utilities used by tools. Tools should not import from each
   other; shared logic lives here instead.


### Command-line Backends

Tools whose names end in ``_for_cli`` open a saved ``.blend`` file in a fresh
subprocess. They use the Blender executable by default, or a standalone
``bpy`` Python when selected with environment variables. Live tools such as
``execute_blender_code`` still use the add-on and the open Blender session.

This native MCP client configuration launches the server with the standalone
backend. Replace both absolute placeholders for your installation:

```json
{
  "mcpServers": {
    "blender": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/blender_mcp/mcp",
        "run",
        "blender-mcp"
      ],
      "env": {
        "BLENDER_MCP_CLI_BACKEND": "bpy",
        "BLENDER_MCP_BPY_PYTHON": "/absolute/path/to/python-with-bpy"
      }
    }
  }
}
```

See [Command-line Blender and standalone bpy backends](readme_bpy_backend.rst)
for installation, compatibility, runtime boundaries, troubleshooting, and
security guidance.


### Tools

See [readme_tools.rst](readme_tools.rst) for the tools the MCP server exposes.
