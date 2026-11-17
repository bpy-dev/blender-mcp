# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for the MCP server configuration (prompts, data files, tool metadata).

Does not require Blender. Run with::

    python -m unittest tests.test_mcp_server -v
"""

__all__ = ()

import ast
import asyncio
import contextlib
import functools
import importlib
import inspect
import os
import re
import sys
import tempfile
import types
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Root of the repository.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_DIR = os.path.join(_REPO_DIR, "mcp")

# Complete production roots that participate in MCP-to-Blender execution.
# ``chat_client`` is a separate UI client and does not import or call the
# MCP package's ``tools_helpers.connection.send_code`` function.
_PRODUCTION_SOURCE_ROOTS = (
    os.path.join(_REPO_DIR, "mcp", "blmcp"),
    os.path.join(_REPO_DIR, "addon", "blender_mcp_addon"),
)
_NON_PRODUCTION_DIR_NAMES = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "cache",
        "data",
        "dist",
        "generated",
        "site-packages",
        "test",
        "tests",
        "third_party",
        "vendor",
        "vendored",
        "venv",
    }
)


def _import_blmcp_module() -> Any:
    """
    Import and return the local ``blmcp`` package.
    """
    if _MCP_DIR not in sys.path:
        sys.path.insert(0, _MCP_DIR)
    return importlib.import_module("blmcp")


def _load_prompts() -> dict[str, object]:
    """
    Return the parsed ``prompts.yml`` file.
    """
    path = os.path.join(_REPO_DIR, "mcp", "blmcp", "data", "prompts.yml")
    with open(path, encoding="utf-8") as fh:
        prompts = yaml.safe_load(fh)
    assert isinstance(prompts, dict)
    return prompts


@functools.lru_cache(maxsize=1)
def _source_tool_definitions() -> dict[str, dict[str, object]]:
    """
    Return source-defined MCP tool signatures keyed by tool name.
    """
    tools_dir = os.path.join(_REPO_DIR, "mcp", "blmcp", "tools")
    tool_defs: dict[str, dict[str, object]] = {}
    for filename in sorted(os.listdir(tools_dir)):
        if (
            not filename.endswith(".py") or
            filename == "__init__.py" or
            filename.endswith("_toolcode.py") or
            filename.startswith("_template_")
        ):
            continue
        path = os.path.join(tools_dir, filename)
        with open(path, encoding="utf-8") as fh:
            module = ast.parse(fh.read(), filename=path)
        for node in ast.walk(module):
            if not isinstance(node, ast.FunctionDef):
                continue
            is_tool = any(
                isinstance(deco, ast.Call) and
                isinstance(deco.func, ast.Attribute) and
                deco.func.attr == "tool"
                for deco in node.decorator_list
            )
            if not is_tool:
                continue
            arg_names = [arg.arg for arg in node.args.args]
            required_count = len(arg_names) - len(node.args.defaults)
            required_names = arg_names[:required_count]
            tool_defs[node.name] = {
                "args": arg_names,
                "required": required_names,
                "module": filename,
            }
    return tool_defs


def _prompt_data_paths(instructions: str) -> list[str]:
    """
    Return ``data/...`` paths referenced in backticks in *instructions*.
    """
    return re.findall(r"`(data/[^`]+)`", instructions)


def _normalize_prompt_text(text: str) -> str:
    """Return case- and whitespace-normalized prompt text."""
    return " ".join(text.casefold().split())


def _backtick_tokens(text: str) -> set[str]:
    """Return exact backtick-delimited tokens from *text*."""
    return set(re.findall(r"`([^`]+)`", text))


def _prompt_passage(instructions: str, *concepts: str, sentence: bool = False) -> str:
    """Return a normalized prompt passage containing every requested concept."""
    if sentence:
        passages = re.split(r"(?<=[.!?])\s+", _normalize_prompt_text(instructions))
        passage_kind = "sentence"
    else:
        passages = [
            _normalize_prompt_text(paragraph)
            for paragraph in re.split(r"\n\s*\n", instructions)
        ]
        passage_kind = "paragraph"
    normalized_concepts = tuple(_normalize_prompt_text(item) for item in concepts)
    for passage in passages:
        if all(concept in passage for concept in normalized_concepts):
            return passage
    raise AssertionError(
        "No prompt {:s} contains all required concepts: {!r}".format(
            passage_kind, concepts
        )
    )


# This is a practical vocabulary audit, not a claim to understand every possible
# English contradiction. Keep the expressions table-driven as prompt vocabulary evolves.
_DOCS_LOOKUP_TARGET = (
    r"(?:runtime documentation|runtime docs|documentation lookup|docs lookup|"
    r"inspect (?:the )?(?:runtime )?(?:documentation|docs)|"
    r"look up (?:the )?(?:runtime )?(?:documentation|docs)|"
    r"consult (?:the )?(?:runtime )?(?:documentation|docs))"
)
_MANDATE = r"(?:always|(?:is|are) (?:required|mandatory|needed)|must|needs? to)"
_ROUTINE_PREFLIGHT = (
    r"(?:before|prior to) (?:(?:each|every|all) )?(?:operation|execution)s?"
)
_MANDATORY_DOCS_PREFLIGHT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"\b{_MANDATE}\b.{{0,100}}\b{_DOCS_LOOKUP_TARGET}\b.{{0,100}}"
        rf"\b{_ROUTINE_PREFLIGHT}\b",
        rf"\b{_DOCS_LOOKUP_TARGET}\b.{{0,100}}\b{_MANDATE}\b.{{0,100}}"
        rf"\b{_ROUTINE_PREFLIGHT}\b",
        rf"\b{_MANDATE}\b.{{0,100}}\b{_ROUTINE_PREFLIGHT}\b.{{0,100}}"
        rf"\b{_DOCS_LOOKUP_TARGET}\b",
    )
)


def _mandatory_docs_preflight_matches(text: str) -> list[str]:
    """Return routine mandatory docs-preflight phrases found anywhere in *text*."""
    sentences = re.split(r"(?<=[.!?])\s+", _normalize_prompt_text(text))
    return [
        match.group(0)
        for sentence in sentences
        for pattern in _MANDATORY_DOCS_PREFLIGHT_PATTERNS
        for match in pattern.finditer(sentence)
    ]


def _server_env() -> dict[str, str]:
    """
    Return an environment dict for the MCP server subprocess.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(_REPO_DIR, "mcp")
    return env


def _query_server() -> dict[str, Any]:
    """
    Launch the MCP server, initialize, fetch metadata, and return it all.
    """

    # Async is required because the MCP client SDK is async-only.
    async def _run() -> dict[str, Any]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "blmcp"],
            env=_server_env(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                tools_result = await session.list_tools()
                return {
                    "server_info": init_result.serverInfo,
                    "instructions": init_result.instructions or "",
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "inputSchema": t.inputSchema,
                        }
                        for t in tools_result.tools
                    ],
                }

    return asyncio.run(_run())


def _call_server_tool(name: str, arguments: dict[str, object]) -> dict[str, Any]:
    """
    Launch the MCP server, call tool *name* with *arguments*, return the
    JSON-decoded result payload from the first text content block.
    """
    import json

    async def _run() -> dict[str, Any]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "blmcp"],
            env=_server_env(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                call_result = await session.call_tool(name, arguments)
                if call_result.isError:
                    raise RuntimeError(
                        "Tool {:s} returned error: {!r}".format(name, call_result.content)
                    )
                # FastMCP serialises dict return values as a single JSON
                # text-content block.
                text = call_result.content[0].text  # type: ignore[attr-defined]
                payload = json.loads(text)
                assert isinstance(payload, dict)
                return payload

    return asyncio.run(_run())


def _production_python_paths(root: str) -> list[str]:
    """Return production Python files below *root* in deterministic order."""
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _NON_PRODUCTION_DIR_NAMES
        )
        paths.extend(
            os.path.join(dirpath, name)
            for name in sorted(filenames)
            if name.endswith(".py")
        )
    return paths


_APPROVED_SANDBOX_FALSE_CALLSITE = (
    "mcp/blmcp/tools/get_runtime_python_api_docs.py",
    "register.get_runtime_python_api_docs",
)


def _send_code_sandbox_audit(
    source: str, filename: str
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str, str]]]:
    """Return literal-false callsites and sandbox-policy violations."""
    tree = ast.parse(source, filename=filename)
    send_code_names = {"send_code"}

    # Account for direct import aliases and simple local aliases without
    # broadening the guard to unrelated functions that accept ``sandbox``.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name == "send_code":
                    send_code_names.add(imported.asname or imported.name)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            is_send_code = (
                isinstance(value, ast.Name) and value.id in send_code_names
            ) or (
                isinstance(value, ast.Attribute) and value.attr == "send_code"
            )
            if not is_send_code:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in send_code_names:
                    send_code_names.add(target.id)
                    changed = True

    false_callsites: list[tuple[str, int, str]] = []
    violations: list[tuple[str, int, str, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def _visit_scope(self, node: ast.AST, name: str) -> None:
            self.scope.append(name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            self._visit_scope(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._visit_scope(node, node.name)

        def visit_AsyncFunctionDef(  # noqa: N802
            self, node: ast.AsyncFunctionDef
        ) -> None:
            self._visit_scope(node, node.name)

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            is_send_code = (
                isinstance(node.func, ast.Name)
                and node.func.id in send_code_names
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_code"
            )
            if is_send_code:
                scope = ".".join(self.scope) or "<module>"
                location = (filename, node.lineno, scope)
                unpacked_keywords = [
                    keyword for keyword in node.keywords if keyword.arg is None
                ]
                sandbox_keywords = [
                    keyword
                    for keyword in node.keywords
                    if keyword.arg == "sandbox"
                ]

                if unpacked_keywords:
                    violations.append(
                        (*location, "send_code keyword unpacking (**kwargs) is forbidden")
                    )
                if len(sandbox_keywords) > 1:
                    violations.append(
                        (*location, "duplicate send_code sandbox keywords are forbidden")
                    )
                elif sandbox_keywords:
                    value = sandbox_keywords[0].value
                    literal_bool = (
                        value.value
                        if isinstance(value, ast.Constant)
                        and type(value.value) is bool
                        else None
                    )
                    if literal_bool is False:
                        false_callsites.append(location)
                        if (filename, scope) != _APPROVED_SANDBOX_FALSE_CALLSITE:
                            violations.append(
                                (
                                    *location,
                                    "literal sandbox=False is only allowed at the "
                                    "approved runtime-doc live wrapper",
                                )
                            )
                    elif literal_bool is not True:
                        violations.append(
                            (
                                *location,
                                "send_code sandbox must be omitted or the literal bool True",
                            )
                        )
            self.generic_visit(node)

    Visitor().visit(tree)
    return false_callsites, violations


# ---------------------------------------------------------------------------
# Data file tests (no server needed).


class TestDataFiles(unittest.TestCase):
    """
    Verify bundled data files are present and well-formed.
    """

    _data_dir: str
    _prompts: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        cls._data_dir = os.path.join(_REPO_DIR, "mcp", "blmcp", "data")
        cls._prompts = _load_prompts()

    def test_prompts_yml_valid_yaml(self) -> None:
        """
        Checks that ``prompts.yml`` loads as a dictionary.
        """
        self.assertIsInstance(self._prompts, dict)

    def test_prompts_yml_has_initial_instructions(self) -> None:
        """
        Checks that ``prompts.yml`` includes non-empty startup instructions.
        """
        self.assertIn("initial_instructions", self._prompts)
        self.assertIsInstance(self._prompts["initial_instructions"], str)
        self.assertTrue(len(self._prompts["initial_instructions"]) > 0)

    def test_prompt_has_exact_doc_tool_tokens_and_static_routing(self) -> None:
        """Checks exact tool names and bundled/static documentation routing."""
        instructions = str(self._prompts["initial_instructions"])
        tokens = _backtick_tokens(instructions)
        self.assertTrue(
            {
                "search_api_docs",
                "get_python_api_docs",
                "get_runtime_python_api_docs",
                "get_runtime_python_api_docs_for_cli",
                "data/api/",
                "data/manual/",
            }.issubset(tokens),
            "Prompt is missing one or more exact backtick-delimited doc tokens",
        )

        discovery = _prompt_passage(
            instructions, "`search_api_docs`", "ranked", "full-text", sentence=True
        )
        self.assertRegex(discovery, r"\b(?:discovery|search)\b")

        static_detail = _prompt_passage(
            instructions,
            "`get_python_api_docs`",
            "static",
            "explanation",
            "example",
            sentence=True,
        )
        self.assertIn("static", static_detail)

        bundled = _prompt_passage(instructions, "reference documentation", "`data/api/`")
        self.assertIn("when documentation is needed", bundled)
        self.assertIn("consult as needed", bundled)

    def test_prompt_lists_all_exact_runtime_detail_reasons(self) -> None:
        """Checks the five reasons for requesting exact runtime API details."""
        runtime_condition = _prompt_passage(
            str(self._prompts["initial_instructions"]),
            "`get_runtime_python_api_docs`",
            "only when",
            sentence=True,
        )
        for detail in ("signature", "property", "default", "enum", "availability"):
            with self.subTest(detail=detail):
                self.assertRegex(runtime_condition, rf"\b{detail}\b")

    def test_prompt_routes_error_rechecks_to_live_or_cli_runtime_docs(self) -> None:
        """Checks same-turn reuse, error precedence, and context-specific routing."""
        instructions = str(self._prompts["initial_instructions"])
        tokens = _backtick_tokens(instructions)
        self.assertIn("get_runtime_python_api_docs", tokens)
        self.assertIn("get_runtime_python_api_docs_for_cli", tokens)

        precedence = _prompt_passage(
            instructions,
            "already established",
            "api-related execution error",
            "invalidates",
            sentence=True,
        )
        self.assertIn("this turn", precedence)
        self.assertRegex(precedence, r"\b(?:unless|except)\b")

        error_action = _prompt_passage(instructions, "exact identifier", sentence=True)
        self.assertIn("before retrying", error_action)
        self.assertRegex(error_action, r"\bexception\b")
        self.assertRegex(error_action, r"\bruntime(?:-doc| documentation| docs)\b")

        routing = _prompt_passage(
            instructions,
            "saved-file cli context",
            "connected live scene",
            sentence=True,
        )
        self.assertRegex(
            routing,
            r"saved-file cli context[^.!?]{0,100}"
            r"`get_runtime_python_api_docs_for_cli`",
        )
        self.assertRegex(
            routing,
            r"connected live scene[^.!?]{0,100}"
            r"`get_runtime_python_api_docs`(?:[.,;:]|$)",
        )

    def test_prompt_docs_policy_is_selective_across_complete_prompt(self) -> None:
        """Checks conditional lookup and rejects practical routine-preflight mandates."""
        instructions = str(self._prompts["initial_instructions"])
        policy = _prompt_passage(instructions, "documentation by default", "this turn")
        self.assertRegex(policy, r"do not [^.!?]{0,80}documentation by default")
        self.assertRegex(
            policy,
            r"\b(?:avoid|skip|do not)\b[^.!?]{0,60}"
            r"\b(?:additional|unnecessary)\b[^.!?]{0,40}"
            r"\b(?:lookup|documentation|docs)\b",
        )
        self.assertEqual(
            _mandatory_docs_preflight_matches(instructions),
            [],
            "Prompt mandates routine documentation preflight",
        )

    def test_mandatory_docs_preflight_audit_examples(self) -> None:
        """Keeps the practical negative audit broad without blocking error recovery."""
        forbidden = (
            "runtime documentation is required prior to operations",
            "Always inspect runtime docs before execution",
            "You must look up docs before each operation",
            "Agents need to consult documentation prior to every execution",
            "Documentation lookup is needed before every operation",
        )
        for example in forbidden:
            with self.subTest(forbidden=example):
                text = "Harmless introduction. {:s}. Harmless ending.".format(example)
                self.assertTrue(
                    _mandatory_docs_preflight_matches(text),
                    "Mandatory-preflight variant was not detected",
                )

        allowed = (
            "Use runtime documentation only when an exact detail is uncertain.",
            "After an API-related execution error, you must inspect runtime docs "
            "before retrying.",
            "Do not look up documentation by default or before every operation.",
        )
        for example in allowed:
            with self.subTest(allowed=example):
                self.assertEqual(_mandatory_docs_preflight_matches(example), [])

    def test_prompt_prefers_narrow_tools_and_compact_results(self) -> None:
        """Checks economical execution and result-shaping guidance."""
        instructions = str(self._prompts["initial_instructions"])
        narrow = _prompt_passage(instructions, "narrow scene tools", "arbitrary code")
        self.assertRegex(narrow, r"narrow scene tools?[^.!?]{0,100}before arbitrary code")

        results = _prompt_passage(
            instructions, "compact structured results", "scene dumps", sentence=True
        )
        self.assertIn("avoid scene dumps", results)
        self.assertIn("print output", results)

    def test_api_directory_exists(self) -> None:
        """
        Checks that the bundled API docs folder exists and contains RST files.
        """
        api_dir = os.path.join(self._data_dir, "api")
        self.assertTrue(os.path.isdir(api_dir))
        rst_files = [f for f in os.listdir(api_dir) if f.endswith(".rst")]
        self.assertTrue(len(rst_files) > 0, "No RST files found in api/")

    def test_manual_directory_exists(self) -> None:
        """
        Checks that the bundled manual folder exists and contains RST files.
        """
        manual_dir = os.path.join(self._data_dir, "manual")
        if not os.path.isdir(manual_dir):
            self.skipTest("manual/ directory not present (run make update_manual)")
        has_rst = False
        for _dirpath, _dirnames, filenames in os.walk(manual_dir):
            if any(f.endswith(".rst") for f in filenames):
                has_rst = True
                break
        self.assertTrue(has_rst, "No RST files found in manual/")

    def test_api_index_exists(self) -> None:
        """
        Checks that the bundled API docs include an ``index.rst`` entry point.
        """
        self.assertTrue(
            os.path.isfile(os.path.join(self._data_dir, "api", "index.rst"))
        )

    def test_manual_index_exists(self) -> None:
        """
        Checks that the bundled manual docs include an ``index.rst`` entry point.
        """
        manual_index = os.path.join(self._data_dir, "manual", "index.rst")
        if not os.path.isdir(os.path.dirname(manual_index)):
            self.skipTest("manual/ directory not present (run make update_manual)")
        self.assertTrue(os.path.isfile(manual_index))

    def test_prompt_referenced_data_paths_exist(self) -> None:
        """
        Checks that every ``data/...`` path mentioned in the prompt actually exists.
        """
        instructions = str(self._prompts["initial_instructions"])
        prompt_paths = _prompt_data_paths(instructions)
        self.assertIn("data/api/", prompt_paths)
        self.assertIn("data/manual/", prompt_paths)
        for prompt_path in prompt_paths:
            if not prompt_path.startswith("data/"):
                continue
            fs_path = os.path.join(self._data_dir, prompt_path.removeprefix("data/"))
            if prompt_path == "data/manual/" and not os.path.exists(fs_path):
                self.skipTest("manual/ directory not present (run make update_manual)")
            self.assertTrue(
                os.path.exists(fs_path),
                "Prompt references missing path: {:s}".format(prompt_path),
            )


# ---------------------------------------------------------------------------
# Tool call tests.


class TestSearchApiDocs(unittest.TestCase):
    """
    Tests for the ``search_api_docs`` tool.
    """

    def test_basic_lookup(self) -> None:
        """
        Checks that the module page for a top-level API name appears in
        the hit list, and each hit carries the matched paragraph text.
        """
        # Match the canonical `.. module:: bpy.data` directive line,
        # which uniquely identifies `api/bpy.data.rst` regardless of
        # how many other files mention `bpy.data` in prose.
        payload = _call_server_tool(
            "search_api_docs",
            {"query": "module:: bpy.data", "max_results": 5},
        )
        hits = payload["hits"]
        paths = [hit["path"] for hit in hits]
        self.assertIn("api/bpy.data.rst", paths)
        for hit in hits:
            self.assertTrue(hit["text"].strip(), "hit has empty text")
            self.assertTrue(
                hit["path"].startswith("api/"),
                "API search returned non-API path: {:s}".format(hit["path"]),
            )


class TestSearchManualDocs(unittest.TestCase):
    """
    Tests for the ``search_manual_docs`` tool.
    """

    def test_basic_lookup(self) -> None:
        """
        Checks that a common manual term returns hits, all confined to
        the manual corpus.
        """
        payload = _call_server_tool(
            "search_manual_docs",
            {"query": "modeling", "max_results": 3},
        )
        hits = payload["hits"]
        self.assertTrue(hits, "expected at least one manual hit for 'modeling'")
        for hit in hits:
            self.assertTrue(hit["text"].strip(), "hit has empty text")
            self.assertTrue(
                hit["path"].startswith("manual/"),
                "manual search returned non-manual path: {:s}".format(hit["path"]),
            )


# ---------------------------------------------------------------------------
# Server metadata tests.


class TestMCPServer(unittest.TestCase):
    """
    Test the MCP server's metadata, instructions, and tool configuration.
    """

    _server_info: Any
    _instructions: str
    _prompts: dict[str, object]
    _tools: list[dict[str, Any]]
    _tools_by_name: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        data = _query_server()
        cls._prompts = _load_prompts()
        cls._server_info = data["server_info"]
        cls._instructions = data["instructions"]
        cls._tools = data["tools"]
        cls._tools_by_name = {tool["name"]: tool for tool in cls._tools}

    # -----------------------------------------------------------------
    # Server identity.

    def test_server_name(self) -> None:
        """
        Checks that the server reports the expected public name.
        """
        self.assertEqual(self._server_info.name, "blender-mcp")

    # -----------------------------------------------------------------
    # Instructions.

    def test_instructions_not_empty(self) -> None:
        """
        Checks that the server returns some startup instructions.
        """
        self.assertTrue(len(self._instructions) > 0)

    def test_instructions_match_prompts_yml_exactly(self) -> None:
        """
        Checks that the server returns exactly the instructions from ``prompts.yml``.
        """
        self.assertEqual(
            self._instructions,
            str(self._prompts["initial_instructions"]),
        )

    def test_instructions_contains_key_sections(self) -> None:
        """
        Checks that the instructions still include the main guidance sections.
        """
        for section in (
            "Executing Code",
            "Scene & Data Structure",
            "Object Types & Creation",
            "Transformations & Coordinate Spaces",
        ):
            self.assertIn(
                section,
                self._instructions,
                "Missing section: {:s}".format(section),
            )

    def test_instructions_is_ascii(self) -> None:
        """
        Checks that the instructions stay ASCII-only.
        """
        try:
            self._instructions.encode("ascii")
        except UnicodeEncodeError as ex:
            self.fail("Non-ASCII character in instructions: {:s}".format(str(ex)))

    # -----------------------------------------------------------------
    # Tool metadata.

    def test_tool_count(self) -> None:
        """
        Checks that the server exposes at least one tool.
        """
        self.assertTrue(len(self._tools) > 0)

    def test_tool_names_unique(self) -> None:
        """
        Checks that every exposed tool has a unique name.
        """
        names = [t["name"] for t in self._tools]
        self.assertEqual(len(names), len(set(names)))

    def test_all_tools_have_descriptions(self) -> None:
        """
        Checks that every exposed tool has a non-empty description.
        """
        for tool in self._tools:
            self.assertTrue(
                len(tool["description"].strip()) > 0,
                "Tool {:s} has no description".format(tool["name"]),
            )

    def test_all_tools_have_input_schemas(self) -> None:
        """
        Checks that every exposed tool includes an input schema with a type.
        """
        for tool in self._tools:
            schema = tool["inputSchema"]
            self.assertIsInstance(
                schema,
                dict,
                "Tool {:s} has no inputSchema".format(tool["name"]),
            )
            self.assertIn(
                "type",
                schema,
                "Tool {:s} inputSchema missing 'type'".format(tool["name"]),
            )

    def test_server_tools_match_public_source_tool_functions(self) -> None:
        """
        Checks that the live server exposes exactly the public ``@mcp.tool()`` wrappers.
        """
        self.assertEqual(
            set(self._tools_by_name),
            set(_source_tool_definitions()),
        )

    def test_all_tool_schemas_are_object_schemas(self) -> None:
        """
        Checks that every tool schema looks like a normal object schema.
        """
        for tool in self._tools:
            schema = tool["inputSchema"]
            self.assertEqual(
                schema.get("type"),
                "object",
                "Tool {:s} inputSchema type is not 'object'".format(tool["name"]),
            )
            properties = schema.get("properties", {})
            self.assertIsInstance(
                properties,
                dict,
                "Tool {:s} inputSchema properties are not a dict".format(tool["name"]),
            )
            required = schema.get("required", [])
            self.assertIsInstance(
                required,
                list,
                "Tool {:s} inputSchema required is not a list".format(tool["name"]),
            )
            for key in required:
                self.assertIn(
                    key,
                    properties,
                    "Tool {:s} requires unknown property {:s}".format(
                        tool["name"], key
                    ),
                )

    def test_tool_schemas_match_source_signatures(self) -> None:
        """
        Checks that each schema matches the Python function signature that defines it.
        """
        tool_defs = _source_tool_definitions()
        for name, tool_def in tool_defs.items():
            schema = self._tools_by_name[name]["inputSchema"]
            self.assertEqual(
                set(schema.get("properties", {})),
                set(tool_def["args"]),
                "Tool {:s} properties do not match function arguments in {:s}".format(
                    name, tool_def["module"]
                ),
            )
            self.assertEqual(
                set(schema.get("required", [])),
                set(tool_def["required"]),
                "Tool {:s} required properties do not match function signature in {:s}".format(
                    name, tool_def["module"]
                ),
            )

    def test_cli_tools_require_blend_file(self) -> None:
        """
        Checks that every ``_for_cli`` tool requires a ``blend_file`` argument.
        """
        checked = 0
        for tool in self._tools:
            if not tool["name"].endswith("_for_cli"):
                continue
            schema = tool["inputSchema"]
            required = schema.get("required", [])
            self.assertIn(
                "blend_file",
                required,
                "Tool {:s} does not require 'blend_file'".format(tool["name"]),
            )
            checked += 1
        self.assertGreater(checked, 0, "No _for_cli tools found")

    def test_non_cli_tools_do_not_require_blend_file(self) -> None:
        """
        Checks that non-CLI tools do not require a ``blend_file`` argument.
        """
        checked = 0
        for tool in self._tools:
            if tool["name"].endswith("_for_cli"):
                continue
            self.assertNotIn(
                "blend_file",
                tool["inputSchema"].get("required", []),
                "Tool {:s} unexpectedly requires 'blend_file'".format(tool["name"]),
            )
            checked += 1
        self.assertGreater(checked, 0, "No non-CLI tools found")

    def test_cli_tools_have_non_cli_twins(self) -> None:
        """
        Checks CLI twins except the intentionally saved-file-only CPU preview.
        """
        cli_only = {"get_local_render_as_image_for_cli"}
        self.assertTrue(cli_only <= self._tools_by_name.keys())
        checked = 0
        for name in self._tools_by_name:
            if not name.endswith("_for_cli") or name in cli_only:
                continue
            self.assertIn(
                name[:-8],
                self._tools_by_name,
                "Tool {:s} has no non-CLI twin".format(name),
            )
            checked += 1
        self.assertGreater(checked, 0, "No _for_cli tools found")

    def test_runtime_docs_tools_are_paired_once_and_static_docs_remain(self) -> None:
        """
        Checks that runtime introspection adds one live/CLI pair without replacing
        the existing static documentation and discovery tools.
        """
        names = [tool["name"] for tool in self._tools]
        for name in (
            "get_runtime_python_api_docs",
            "get_runtime_python_api_docs_for_cli",
            "get_python_api_docs",
            "search_api_docs",
        ):
            self.assertEqual(names.count(name), 1, name)


class TestGetRuntimePythonAPIDocsDispatch(unittest.TestCase):
    """Test runtime-doc wrapper dispatch without requiring Blender."""

    @staticmethod
    def _registered_tools() -> tuple[
        Any, dict[str, Callable[..., object]], dict[str, dict[str, object]]
    ]:
        _import_blmcp_module()
        module = importlib.import_module(
            "blmcp.tools.get_runtime_python_api_docs"
        )
        registered: dict[str, Callable[..., object]] = {}
        metadata: dict[str, dict[str, object]] = {}

        class FakeMCP:
            def tool(
                self, **kwargs: object
            ) -> Callable[[Callable[..., object]], Callable[..., object]]:
                def decorator(
                    function: Callable[..., object],
                ) -> Callable[..., object]:
                    registered[function.__name__] = function
                    metadata[function.__name__] = kwargs
                    return function

                return decorator

        module.register(FakeMCP())
        return module, registered, metadata

    def test_live_dispatch_sends_shared_toolcode_as_strict_json(self) -> None:
        module, registered, _metadata = self._registered_tools()
        expected_code = module.toolcode_format_call(
            module._TOOL_CALL, {"identifier": "bpy.types.Object.location"}
        )
        with mock.patch.object(
            module,
            "send_code",
            return_value={"found": True, "kind": "rna_property"},
        ) as send:
            result = registered["get_runtime_python_api_docs"](
                "bpy.types.Object.location"
            )

        self.assertEqual(result, {"found": True, "kind": "rna_property"})
        send.assert_called_once_with(
            expected_code, strict_json=True, sandbox=False
        )

    def test_cli_dispatch_uses_synced_file_shared_toolcode_and_strict_runner(self) -> None:
        module, registered, _metadata = self._registered_tools()
        expected_code = module.toolcode_format_call(
            module._TOOL_CALL,
            {"identifier": "bpy.ops.mesh.primitive_cube_add"},
        )
        with (
            mock.patch.object(
                module,
                "synced_blend_for_cli",
                return_value=contextlib.nullcontext("synced.blend"),
            ) as sync,
            mock.patch.object(
                module,
                "run_blender_cli",
                return_value={"found": True, "kind": "operator"},
            ) as run,
        ):
            result = registered["get_runtime_python_api_docs_for_cli"](
                "scene.blend", "bpy.ops.mesh.primitive_cube_add"
            )

        self.assertEqual(result, {"found": True, "kind": "operator"})
        sync.assert_called_once_with("scene.blend")
        run.assert_called_once_with("synced.blend", expected_code)

    def test_cli_wrapper_has_no_sandbox_argument(self) -> None:
        _module, registered, _metadata = self._registered_tools()

        parameters = inspect.signature(
            registered["get_runtime_python_api_docs_for_cli"]
        ).parameters

        self.assertNotIn("sandbox", parameters)

    def test_runtime_docs_live_wrapper_is_only_sandbox_false_callsite(self) -> None:
        """Guard every MCP and add-on production package recursively."""
        callsites: list[tuple[str, int, str]] = []
        violations: list[tuple[str, int, str, str]] = []
        for root in _PRODUCTION_SOURCE_ROOTS:
            for path in _production_python_paths(root):
                relative_path = os.path.relpath(path, _REPO_DIR).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    file_callsites, file_violations = _send_code_sandbox_audit(
                        fh.read(), relative_path
                    )
                callsites.extend(file_callsites)
                violations.extend(file_violations)

        self.assertEqual(violations, [], violations)
        self.assertEqual(
            [(path, scope) for path, _line, scope in callsites],
            [_APPROVED_SANDBOX_FALSE_CALLSITE],
            callsites,
        )
        self.assertGreater(callsites[0][1], 0)

    def test_sandbox_false_scan_finds_nested_aliased_helper_callsite(self) -> None:
        """Prove recursive helpers and direct ``send_code`` aliases cannot evade it."""
        with tempfile.TemporaryDirectory() as root:
            helper_dir = os.path.join(root, "nested", "helpers")
            os.makedirs(helper_dir)
            path = os.path.join(helper_dir, "connection_wrapper.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "from pkg.connection import send_code as dispatch\n"
                    "\n"
                    "def helper():\n"
                    "    return dispatch('code', strict_json=True, sandbox=False)\n"
                    "\n"
                    "def unrelated():\n"
                    "    return other_api(sandbox=False)\n"
                )

            paths = _production_python_paths(root)
            self.assertEqual(paths, [path])
            with open(path, encoding="utf-8") as fh:
                callsites, violations = _send_code_sandbox_audit(fh.read(), path)
            self.assertEqual(callsites, [(path, 4, "helper")])
            self.assertEqual(
                violations,
                [
                    (
                        path,
                        4,
                        "helper",
                        "literal sandbox=False is only allowed at the approved "
                        "runtime-doc live wrapper",
                    )
                ],
            )

    def test_send_code_sandbox_scan_rejects_indirection_and_unpacking(self) -> None:
        source = (
            "from pkg.connection import send_code as dispatch\n"
            "import pkg.connection as connection\n"
            "UNSANDBOXED = False\n"
            "enabled = True\n"
            "alias = connection.send_code\n"
            "def helper():\n"
            "    dispatch('name', sandbox=UNSANDBOXED)\n"
            "    send_code('not', sandbox=not enabled)\n"
            "    send_code('conditional', sandbox=False if enabled else True)\n"
            "    connection.send_code('unpacked', **{'sandbox': False})\n"
            "    alias('constant', sandbox=0)\n"
            "    connection.send_code('false', sandbox=False)\n"
            "    unrelated(sandbox=UNSANDBOXED)\n"
        )

        callsites, violations = _send_code_sandbox_audit(source, "pkg/helper.py")

        self.assertEqual(callsites, [("pkg/helper.py", 12, "helper")])
        self.assertEqual(
            violations,
            [
                (
                    "pkg/helper.py",
                    7,
                    "helper",
                    "send_code sandbox must be omitted or the literal bool True",
                ),
                (
                    "pkg/helper.py",
                    8,
                    "helper",
                    "send_code sandbox must be omitted or the literal bool True",
                ),
                (
                    "pkg/helper.py",
                    9,
                    "helper",
                    "send_code sandbox must be omitted or the literal bool True",
                ),
                (
                    "pkg/helper.py",
                    10,
                    "helper",
                    "send_code keyword unpacking (**kwargs) is forbidden",
                ),
                (
                    "pkg/helper.py",
                    11,
                    "helper",
                    "send_code sandbox must be omitted or the literal bool True",
                ),
                (
                    "pkg/helper.py",
                    12,
                    "helper",
                    "literal sandbox=False is only allowed at the approved "
                    "runtime-doc live wrapper",
                ),
            ],
        )

    def test_send_code_sandbox_scan_rejects_duplicate_keywords(self) -> None:
        self.assertEqual(
            _send_code_sandbox_audit(
                "send_code('code', sandbox=True, sandbox=False)\n",
                "pkg/helper.py",
            ),
            (
                [],
                [
                    (
                        "pkg/helper.py",
                        1,
                        "<module>",
                        "duplicate send_code sandbox keywords are forbidden",
                    )
                ],
            ),
        )

    def test_send_code_sandbox_scan_allows_default_and_literal_true(self) -> None:
        source = (
            "from pkg.connection import send_code as dispatch\n"
            "import pkg.connection as connection\n"
            "alias = connection.send_code\n"
            "def helper():\n"
            "    send_code('default')\n"
            "    dispatch('true', sandbox=True)\n"
            "    connection.send_code('attribute default')\n"
            "    alias('alias true', sandbox=True)\n"
            "    unrelated(sandbox=False)\n"
        )

        self.assertEqual(
            _send_code_sandbox_audit(source, "pkg/helper.py"), ([], [])
        )

    def test_send_code_sandbox_scan_allows_only_approved_literal_false(self) -> None:
        source = (
            "def register():\n"
            "    def get_runtime_python_api_docs():\n"
            "        return send_code('code', sandbox=False)\n"
        )

        self.assertEqual(
            _send_code_sandbox_audit(
                source, "mcp/blmcp/tools/get_runtime_python_api_docs.py"
            ),
            (
                [
                    (
                        "mcp/blmcp/tools/get_runtime_python_api_docs.py",
                        3,
                        "register.get_runtime_python_api_docs",
                    )
                ],
                [],
            ),
        )

    def test_runtime_docs_annotations_are_read_only(self) -> None:
        _module, _registered, metadata = self._registered_tools()
        for name in (
            "get_runtime_python_api_docs",
            "get_runtime_python_api_docs_for_cli",
        ):
            annotations: Any = metadata[name]["annotations"]
            self.assertTrue(annotations.readOnlyHint)


class TestMainConfiguration(unittest.TestCase):
    """
    Test server startup configuration without launching Blender.
    """

    _prompts: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        cls._prompts = _load_prompts()

    def test_main_uses_stdio_transport_by_default(self) -> None:
        """
        Checks that ``main()`` starts the server in ``stdio`` mode by default.
        """
        blmcp = _import_blmcp_module()
        mcp_instance = mock.Mock()
        with (
            mock.patch.object(sys, "argv", ["blmcp"]),
            mock.patch.object(
                blmcp, "FastMCP", return_value=mcp_instance
            ) as fastmcp_cls,
            mock.patch.object(blmcp.pkgutil, "iter_modules", return_value=[]),
        ):
            result = blmcp.main()
        self.assertEqual(result, 0)
        fastmcp_cls.assert_called_once_with(
            "blender-mcp",
            instructions=str(self._prompts["initial_instructions"]),
        )
        mcp_instance.run.assert_called_once_with(transport="stdio")

    def test_main_discovers_and_registers_only_public_tool_modules(self) -> None:
        """
        Checks that startup imports only public tool modules and registers them.
        """
        blmcp = _import_blmcp_module()
        mcp_instance = mock.Mock()
        gamma_mod = mock.Mock()
        no_register_mod = object()

        with (
            mock.patch.object(sys, "argv", ["blmcp"]),
            mock.patch.object(blmcp, "FastMCP", return_value=mcp_instance),
            mock.patch.object(
                blmcp.pkgutil,
                "iter_modules",
                return_value=[
                    (None, "alpha", False),
                    (None, "beta_toolcode", False),
                    (None, "_template_hidden", False),
                    (None, "gamma", False),
                ],
            ),
            mock.patch.object(
                blmcp.importlib,
                "import_module",
                side_effect=[no_register_mod, gamma_mod],
            ) as import_module,
        ):
            result = blmcp.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            import_module.call_args_list,
            [
                mock.call("blmcp.tools.alpha"),
                mock.call("blmcp.tools.gamma"),
            ],
        )
        gamma_mod.register.assert_called_once_with(mcp_instance)
        mcp_instance.run.assert_called_once_with(transport="stdio")

    def test_main_skips_modules_without_register(self) -> None:
        """
        Checks that startup ignores tool modules that do not define ``register()``.
        """
        blmcp = _import_blmcp_module()
        mcp_instance = mock.Mock()
        no_register_mod = object()

        with (
            mock.patch.object(sys, "argv", ["blmcp"]),
            mock.patch.object(blmcp, "FastMCP", return_value=mcp_instance),
            mock.patch.object(
                blmcp.pkgutil,
                "iter_modules",
                return_value=[(None, "alpha", False)],
            ),
            mock.patch.object(
                blmcp.importlib,
                "import_module",
                return_value=no_register_mod,
            ) as import_module,
        ):
            result = blmcp.main()

        self.assertEqual(result, 0)
        import_module.assert_called_once_with("blmcp.tools.alpha")
        mcp_instance.run.assert_called_once_with(transport="stdio")

    def test_main_configures_http_transport_settings_and_cors(self) -> None:
        """
        Checks that HTTP mode sets the expected server settings and CORS wrapper.
        """
        # NOTE: this is fairly closely tied with HTTP which only tests the few things
        # we have supported for LLAMA.C++.

        blmcp = _import_blmcp_module()

        class FakeTransportSecuritySettings:
            def __init__(self, **kwargs: object):
                self.kwargs = kwargs

        class FakeCORSMiddleware:
            pass

        class FakeApp:
            def __init__(self) -> None:
                self.middleware_calls: list[tuple[object, dict[str, object]]] = []

            def add_middleware(self, middleware: object, **kwargs: object) -> None:
                self.middleware_calls.append((middleware, kwargs))

        fake_app = FakeApp()
        mcp_instance = mock.Mock()
        mcp_instance.settings = types.SimpleNamespace()
        mcp_instance.streamable_http_app = mock.Mock(return_value=fake_app)

        fastmcp_server_mod = types.ModuleType("mcp.server.fastmcp.server")
        fastmcp_server_mod.TransportSecuritySettings = FakeTransportSecuritySettings
        starlette_apps_mod = types.ModuleType("starlette.applications")
        starlette_apps_mod.Starlette = object
        starlette_cors_mod = types.ModuleType("starlette.middleware.cors")
        starlette_cors_mod.CORSMiddleware = FakeCORSMiddleware

        with (
            mock.patch.object(
                sys,
                "argv",
                ["blmcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8123"],
            ),
            mock.patch.object(blmcp, "FastMCP", return_value=mcp_instance),
            mock.patch.object(blmcp.pkgutil, "iter_modules", return_value=[]),
            mock.patch.dict(
                sys.modules,
                {
                    "mcp.server.fastmcp.server": fastmcp_server_mod,
                    "starlette.applications": starlette_apps_mod,
                    "starlette.middleware.cors": starlette_cors_mod,
                },
            ),
        ):
            result = blmcp.main()

        self.assertEqual(result, 0)
        self.assertEqual(mcp_instance.settings.host, "0.0.0.0")
        self.assertEqual(mcp_instance.settings.port, 8123)
        self.assertEqual(mcp_instance.settings.streamable_http_path, "/")
        self.assertTrue(mcp_instance.settings.stateless_http)
        self.assertIsInstance(
            mcp_instance.settings.transport_security,
            FakeTransportSecuritySettings,
        )
        self.assertEqual(
            mcp_instance.settings.transport_security.kwargs,
            {"enable_dns_rebinding_protection": False},
        )
        mcp_instance.run.assert_called_once_with(transport="streamable-http")

        app = mcp_instance.streamable_http_app()
        self.assertIs(app, fake_app)
        self.assertEqual(
            fake_app.middleware_calls,
            [
                (
                    FakeCORSMiddleware,
                    {
                        "allow_origins": ["*"],
                        "allow_methods": ["*"],
                        "allow_headers": ["*"],
                    },
                ),
            ],
        )


class TestGetPythonAPIDocs(unittest.TestCase):
    """
    Exercise the ``get_python_api_docs`` MCP tool end-to-end via a
    subprocess server. Does not require Blender.
    """

    def test_returns_content_for_bpy(self) -> None:
        """
        Checks that calling the tool with ``bpy.app`` returns an ``exact``
        payload with the RST content.

        (There is no standalone ``bpy.rst`` in the bundled docs; the top-level
        namespace is split into ``bpy.app``, ``bpy.context``, ``bpy.data`` etc.)
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.app"},
        )
        self.assertEqual(payload["kind"], "exact")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["identifier"], "bpy.app")
        content = payload["content"]
        self.assertIsInstance(content, str)
        self.assertTrue(len(content) > 0)

    def test_star_lists_top_level_modules(self) -> None:
        """
        ``*`` is a discovery sentinel - it returns a ``namespace``
        payload enumerating every top-level API module, with doc-meta
        pages (``index``, ``info_overview``, ``change_log``) filtered
        out.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "*"},
        )
        self.assertEqual(payload["kind"], "namespace")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["identifier"], "*")
        submodules = payload["submodules"]
        self.assertIsInstance(submodules, list)
        # Modules reached via either rule (child-file or `.. module::`).
        for expected in ("bpy", "bmesh", "mathutils", "gpu", "blf", "bl_math"):
            self.assertIn(expected, submodules)
        # Doc-meta pages must not leak into the listing.
        for excluded in ("index", "change_log", "info_overview"):
            self.assertNotIn(excluded, submodules)

    def test_dot_star_lists_direct_children_of_namespace(self) -> None:
        """
        ``bpy.*`` forces a ``namespace`` response listing the direct
        children of ``bpy`` regardless of whether a root ``bpy.rst``
        exists. Same shape as plain ``bpy``, but explicit.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.*"},
        )
        self.assertEqual(payload["kind"], "namespace")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["identifier"], "bpy.*")
        submodules = payload["submodules"]
        self.assertIsInstance(submodules, list)
        for expected in ("bpy.app", "bpy.data", "bpy.props", "bpy.types"):
            self.assertIn(expected, submodules)
        # Must be direct children only; `bpy.app.handlers` is a
        # grandchild and must not appear.
        self.assertNotIn("bpy.app.handlers", submodules)

    def test_dot_star_forces_namespace_even_when_exact_exists(self) -> None:
        """
        ``bmesh.rst`` exists and has a ``.. module::`` directive, so
        ``bmesh`` alone resolves to ``kind: exact``. ``bmesh.*`` must
        bypass the exact-match and return the child namespace listing.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bmesh.*"},
        )
        self.assertEqual(payload["kind"], "namespace")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["identifier"], "bmesh.*")
        submodules = payload["submodules"]
        self.assertIsInstance(submodules, list)
        for expected in ("bmesh.ops", "bmesh.types", "bmesh.utils"):
            self.assertIn(expected, submodules)

    def test_generates_listing_when_identifier_has_no_rst_file(self) -> None:
        """
        Checks the namespace-listing path: ``bpy`` has no ``bpy.rst``
        on disk, so the tool must list direct-child identifiers under
        a ``submodules`` key.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy"},
        )
        self.assertEqual(payload["kind"], "namespace")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["identifier"], "bpy")
        submodules = payload["submodules"]
        self.assertIsInstance(submodules, list)
        self.assertTrue(submodules, "Expected at least one submodule")
        for entry in submodules:
            self.assertIsInstance(entry, str)
            self.assertTrue(entry.startswith("bpy."))
        for expected in ("bpy.app", "bpy.data", "bpy.props"):
            self.assertIn(expected, submodules)

    def test_generates_suggestions_when_identifier_appears_as_component_elsewhere(self) -> None:
        """
        Checks the ``suggestions`` path: ``app`` is not itself a
        documented identifier, but is a component of several paths
        (``bpy.app``, ``bpy.app.handlers``, ...), so the tool should
        return them as suggestions with ``found: False``.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "app"},
        )
        self.assertEqual(payload["kind"], "suggestions")
        self.assertFalse(payload["found"])
        self.assertEqual(payload["identifier"], "app")
        suggestions = payload["suggestions"]
        self.assertIsInstance(suggestions, list)
        self.assertIn("bpy.app", suggestions)
        # Descendants of a shorter match are pruned - `bpy.app` is
        # already listed, so `bpy.app.handlers` (and siblings) must
        # not appear.
        for descendant in ("bpy.app.handlers", "bpy.app.icons", "bpy.app.timers"):
            self.assertNotIn(descendant, suggestions)

    def test_resolves_identifier_defined_within_rst(self) -> None:
        """
        ``bpy.props.IntProperty`` has no file of its own; the definition
        lives inside ``bpy.props.rst`` as ``.. function:: IntProperty``.
        The tool should find and return that block under ``kind: definition``.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.props.IntProperty"},
        )
        self.assertEqual(payload["kind"], "definition")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["identifier"], "bpy.props.IntProperty")
        content = payload["content"]
        self.assertIsInstance(content, str)
        # Rendered block includes the RST signature line.
        self.assertIn(".. function:: IntProperty(", content)

    def test_returns_not_found_for_missing_identifier(self) -> None:
        """
        Checks that an unknown identifier yields ``kind: missing`` with
        ``found: False`` and no content.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "this.module.doesnt.exist"},
        )
        self.assertEqual(payload["kind"], "missing")
        self.assertFalse(payload["found"])
        self.assertEqual(payload["identifier"], "this.module.doesnt.exist")
        self.assertNotIn("content", payload)

    def test_partial_when_parent_exists_but_tail_missing(self) -> None:
        """
        ``bpy.props.IntegerProperty`` is a plausible typo for
        ``bpy.props.IntProperty``. The tool should locate the parent
        ``bpy.props.rst``, fail to match the tail, and return a
        ``partial`` payload listing the real definitions in that RST
        so the agent can retry.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.props.IntegerProperty"},
        )
        self.assertEqual(payload["kind"], "partial")
        self.assertFalse(payload["found"])
        self.assertEqual(payload["identifier"], "bpy.props.IntegerProperty")
        self.assertEqual(payload["parent"], "bpy.props")
        available = payload["available"]
        self.assertIsInstance(available, list)
        self.assertIn("IntProperty", available)
        self.assertIn("BoolProperty", available)
        # Module pseudo-definition is filtered out of `available`.
        self.assertNotIn("bpy.props", available)
        # Bogus typo shouldn't leak in.
        self.assertNotIn("IntegerProperty", available)
        # `bpy.props` is a self-contained module (no sibling
        # `bpy.props.<child>.rst` files), so submodules is empty.
        self.assertEqual(payload["submodules"], [])

    def test_summarizes_oversized_exact_match(self) -> None:
        """
        ``bpy.ops.object.rst`` is ~150 KB - far above the 32 KB inline
        cap. The tool should return a bullet-list summary with a
        too-large header instead of the full file, and empty examples.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.ops.object"},
        )
        self.assertEqual(payload["kind"], "exact")
        self.assertTrue(payload["found"])
        self.assertEqual(payload["examples"], [])
        content = payload["content"]
        # Header must flag the truncation so agents don't treat the
        # bullet list as if it were the full RST.
        self.assertIn("File too large", content)
        self.assertIn("32 KB", content)
        lines = content.splitlines()
        bullet_lines = [line for line in lines if line.startswith("- ")]
        self.assertGreater(
            len(bullet_lines), 10,
            "Expected many top-level operators in the summary",
        )
        # Spot-check a well-known bpy.ops.object operator.
        self.assertTrue(
            any(line == "- delete" for line in bullet_lines),
            "Expected `- delete` bullet in summary",
        )

    def test_partial_toctree_parent_yields_submodules(self) -> None:
        """
        ``bpy.types.rst`` is a toctree landing page - its doctree has
        no API definitions of its own, but `bpy.types.<X>.rst` sibling
        files hold the per-class docs. The tool should surface those
        siblings via ``submodules``, filtered to names containing every
        character of the missing tail so an agent that typoed a class
        name still gets a focused near-miss list.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.types.Scne"},
        )
        self.assertEqual(payload["kind"], "partial")
        self.assertFalse(payload["found"])
        self.assertEqual(payload["parent"], "bpy.types")
        # Landing page contributes nothing addressable (the module
        # directive is filtered out).
        self.assertEqual(payload["available"], [])
        submodules = payload["submodules"]
        self.assertIsInstance(submodules, list)
        # Closest typo-match must come first.
        self.assertEqual(submodules[0], "bpy.types.Scene")
        # `Object` is missing both `S` and `n` -> filtered out.
        self.assertNotIn("bpy.types.Object", submodules)
        # Filter must never keep a sibling whose last component lacks
        # any of the tail chars; assert the invariant directly.
        tail_chars = set("Scne")
        for entry in submodules:
            last = entry.rsplit(".", 1)[-1]
            self.assertTrue(
                tail_chars.issubset(last),
                "Filter kept {!r} missing chars {!r}".format(
                    entry, tail_chars - set(last),
                ),
            )

    def test_collects_literalinclude_examples(self) -> None:
        """
        ``bpy.app.handlers.rst`` pulls in three ``.. literalinclude::``
        examples. The tool should surface each referenced file's content
        in an ``examples`` list, deduplicated and with stable ordering.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.app.handlers"},
        )
        self.assertEqual(payload["kind"], "exact")
        self.assertTrue(payload["found"])
        examples = payload["examples"]
        self.assertIsInstance(examples, list)
        paths = [e["path"] for e in examples]
        self.assertEqual(
            paths,
            [
                "examples/bpy.app.handlers.0.py",
                "examples/bpy.app.handlers.1.py",
                "examples/bpy.app.handlers.2.py",
            ],
        )
        for entry in examples:
            self.assertIsInstance(entry["content"], str)
            self.assertTrue(
                entry["content"].strip(),
                "Empty example content for {!r}".format(entry["path"]),
            )
            if entry["path"].endswith(".py"):
                # `ast.parse` surfaces a clear SyntaxError on truncated
                # or corrupted content.
                ast.parse(entry["content"], filename=entry["path"])
        # Each of these RST directives specifies `:lines: N-` to drop
        # the leading module docstring. The first example's file has
        # the docstring on lines 1-7 and `import bpy` on line 8, so
        # the extracted content must start at `import bpy` (not the
        # triple-quoted docstring) and must not contain the docstring
        # title.
        self.assertTrue(examples[0]["content"].startswith("import bpy"))
        self.assertNotIn("Basic Handler Example", examples[0]["content"])

    def test_examples_empty_when_no_literalincludes(self) -> None:
        """
        ``bpy.app.rst`` has no ``.. literalinclude::`` directives, so
        the tool should return an empty ``examples`` list for shape
        stability.
        """
        payload = _call_server_tool(
            "get_python_api_docs", {"identifier": "bpy.app"},
        )
        self.assertEqual(payload["kind"], "exact")
        self.assertEqual(payload["examples"], [])


if __name__ == "__main__":
    unittest.main()
