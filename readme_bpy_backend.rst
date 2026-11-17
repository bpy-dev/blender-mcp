################################################
Command-line Blender and Standalone bpy Backends
################################################

The MCP server has two command-line backends for tools whose names end in
``_for_cli``. Both open a saved ``.blend`` file in a fresh subprocess. Backend
selection does not change the connected, live Blender tools.


Standalone bpy setup
====================

The compatible standalone configuration is CPython 3.13 with ``bpy`` 5.2.1.
Create a dedicated environment and install the wheel as follows. On Windows,
replace ``bin/python`` with ``Scripts/python.exe``.

.. code-block:: console

   uv venv --python 3.13 /absolute/path/to/bpy-venv
   uv pip install --python /absolute/path/to/bpy-venv/bin/python \
     --extra-index-url https://michaelgold.github.io/buildbpy/ "bpy==5.2.1"

Verify the interpreter before configuring the MCP client:

.. code-block:: console

   /absolute/path/to/bpy-venv/bin/python -c \
     'import bpy, platform, sys
   assert bpy.app.version == (5, 2, 1)
   assert sys.version_info[:2] == (3, 13)
   print(bpy.app.version_string, platform.python_version(), platform.machine())'

Then set both variables in the environment that launches ``blender-mcp``:

.. code-block:: console

   BLENDER_MCP_CLI_BACKEND=bpy
   BLENDER_MCP_BPY_PYTHON=/absolute/path/to/python-with-bpy

The native MCP client configuration in `readme.md <readme.md>`__ shows these
values in an environment map and launches ``blender-mcp`` with ``uv`` from the
repository's absolute ``mcp`` directory.

Wheel provenance
----------------

Blender itself and the Python module bundled inside a running Blender are
provided by the Blender project. The standalone 5.2.1 wheel used here is a
third-party/custom build published at
`michaelgold.github.io/buildbpy <https://michaelgold.github.io/buildbpy/>`__.
Its builder source is
`michaelgold/buildbpy <https://github.com/michaelgold/buildbpy>`__. It is not
an official Blender Foundation wheel, and this project does not imply Blender
Foundation support for it. Installing the wheel into another Python
environment does not replace or modify ``bpy`` embedded in a running Blender.


Blender executable backend
==========================

The default backend is the Blender executable. Either omit
``BLENDER_MCP_CLI_BACKEND`` or configure it explicitly:

.. code-block:: console

   BLENDER_MCP_CLI_BACKEND=blender
   BLENDER_PATH=/absolute/path/to/Blender

When ``BLENDER_PATH`` is omitted, the server runs ``blender`` from ``PATH``.
``BLENDER_PATH`` must identify the Blender application executable, not a
Python interpreter. On macOS this is commonly
``/Applications/Blender.app/Contents/MacOS/Blender``.

Code is passed to Blender through a temporary UTF-8 Python file, avoiding
Windows command-line length limits for runtime documentation and long scripts.
The file is closed before launching Blender and removed after execution,
including failures and timeouts. The child receives EOF on standard input
instead of inheriting the MCP server's protocol pipe. This prevents background
startup hangs observed on Windows; generated code cannot read interactive
input from that pipe. User preferences and enabled add-ons are still loaded.

You can inspect the selected Blender runtime with:

.. code-block:: console

   "$BLENDER_PATH" --background --python-expr \
     "import bpy, platform
   print(bpy.app.version_string, platform.python_version(), platform.machine(), bpy.app.build_hash.decode())"


Tested compatibility
====================

The following matrix was verified on 2026-09-07. A matching API version is
required; Python patch releases may differ between the embedded and
standalone runtimes.

.. list-table::
   :header-rows: 1

   * - Backend
     - Blender/bpy
     - Python
     - Platform
     - Build hash
   * - Blender executable
     - 5.2.1 LTS
     - 3.13.13
     - macOS arm64
     - 9e2066aef7ef
   * - Third-party standalone bpy
     - 5.2.1
     - CPython 3.13.15
     - macOS arm64
     - Not applicable


Runtime and capability boundaries
=================================

``execute_blender_code`` and ``get_runtime_python_api_docs`` execute through
the connected add-on. They inspect or change the currently open Blender,
including unsaved state and UI context. Blender must be open with the add-on
enabled and connected.

Tools ending in ``_for_cli``, including
``execute_blender_code_for_cli`` and
``get_runtime_python_api_docs_for_cli``, use the selected command-line backend
in a fresh subprocess. They are intended for saved-file batch and headless
work. Standalone ``bpy`` has no normal Blender UI session, so
operators that require windows, areas, regions, modes, selection, or another
UI context may be unavailable or behave differently. Use the Blender
executable backend or the live tool when UI context is required.

Runtime API documentation supplements the bundled static RST reference; it
does not replace it. Use ``search_api_docs`` for ranked full-text discovery
and ``get_python_api_docs`` for bundled explanations and examples. Request
``get_runtime_python_api_docs`` or
``get_runtime_python_api_docs_for_cli`` only for an exact identifier when a
signature, property, default, enum, or availability is uncertain, or after an
API-related execution error invalidates the known detail. Do not request
runtime documentation before every operation or repeat a lookup already
established in the same turn.


Saving and security
===================

Python supplied to execution tools may read or modify any filesystem content
available to that process. Run model-generated code only on non-sensitive
data, preferably in a disposable or otherwise isolated account, machine, or
container. The add-on's default weak sandbox reduces accidental access to
some operations but is not a security containment boundary.

The source ``.blend`` is not automatically saved with mutations made by a
command-line tool. Mutation code must save explicitly. Always write to a
distinct output path rather than overwriting the source; arbitrary generated
code can overwrite the source if instructed to do so.

Each ``_for_cli`` invocation uses a new subprocess, which isolates ordinary
interpreter failures and Blender crashes from the MCP server, and command-line
execution has a 120-second timeout. These controls are not an operating-system
sandbox and do not restrict filesystem or network access granted to the
process.

The live ``get_runtime_python_api_docs`` wrapper is fixed, repository-owned
code and uses a narrow, audited bypass of the weak sandbox. This is necessary
because the sandbox monkeypatches callable provenance that runtime
introspection must inspect. Ordinary model-generated live code remains on the
default weak-sandbox path. This distinction must not be treated as strong
security containment.


Local CPU previews
==================

``get_local_render_as_image_for_cli`` adds an explicit one-frame CPU preview
with resolution, sample and subprocess-deadline limits. It does not require
CUDA and never falls back to cloud. The historical
``get_render_as_image_for_cli`` CUDA benchmark path remains unchanged.
See `local preview acceptance and limitations <docs/local-preview.md>`__ for
the agent call, editable-file workflow, reproducible tests and remaining gates.


Troubleshooting
===============

``BLENDER_MCP_BPY_PYTHON is required when BLENDER_MCP_CLI_BACKEND=bpy``
   Set ``BLENDER_MCP_BPY_PYTHON`` to an absolute path to the Python
   interpreter in which the verification command above successfully imports
   ``bpy`` 5.2.1 under Python 3.13.

``Unknown BLENDER_MCP_CLI_BACKEND '<value>'; expected one of: blender, bpy``
   Use exactly ``blender`` or ``bpy``. Values are case-sensitive.

``bpy Python executable not found`` or an import error
   Confirm the path exists and run the verification command with that exact
   interpreter. A Python 3.13 executable is insufficient unless it can
   actually import ``bpy`` 5.2.1.

``Blender executable not found``
   Point ``BLENDER_PATH`` at the Blender executable, or ensure ``blender`` is
   on ``PATH``. Never point ``BLENDER_PATH`` at Python.
