# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks for command-line backend documentation examples and links."""

__all__ = ()

import copy
import io
import json
import os
import re
import shlex
import unittest

import yaml

import docutils.core  # type: ignore[import-untyped]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_README = os.path.join(_REPO_ROOT, "readme.md")
_BACKEND_DOC = os.path.join(_REPO_ROOT, "readme_bpy_backend.rst")


class TestBackendDocumentation(unittest.TestCase):
    """Keep published backend configuration executable and complete."""

    def test_native_mcp_config_is_valid_json(self) -> None:
        with open(_README, "r", encoding="utf-8") as fh:
            readme = fh.read()
        matches = re.findall(r"```json\n(.*?)\n```", readme, flags=re.DOTALL)
        self.assertEqual(len(matches), 1)
        config = json.loads(matches[0])
        server = config["mcpServers"]["blender"]
        self.assertEqual(server["command"], "uv")
        self.assertEqual(
            server["args"],
            [
                "--directory",
                "/absolute/path/to/blender_mcp/mcp",
                "run",
                "blender-mcp",
            ],
        )
        self.assertEqual(server["env"]["BLENDER_MCP_CLI_BACKEND"], "bpy")
        self.assertEqual(
            server["env"]["BLENDER_MCP_BPY_PYTHON"],
            "/absolute/path/to/python-with-bpy",
        )

    def test_backend_rst_parses_without_warnings(self) -> None:
        warning_stream = io.StringIO()
        with open(_BACKEND_DOC, "r", encoding="utf-8") as fh:
            source = fh.read()
        docutils.core.publish_doctree(
            source,
            source_path=_BACKEND_DOC,
            settings_overrides={
                "halt_level": 2,
                "report_level": 2,
                "warning_stream": warning_stream,
            },
        )
        self.assertEqual(warning_stream.getvalue(), "")

    def test_documentation_is_ascii_and_wrapped(self) -> None:
        with open(_BACKEND_DOC, "r", encoding="utf-8") as fh:
            source = fh.read()
        source.encode("ascii")
        long_lines = [
            (line_number, len(line))
            for line_number, line in enumerate(source.splitlines(), 1)
            if len(line) > 120
        ]
        self.assertEqual(long_lines, [])

    def test_readme_local_links_exist(self) -> None:
        with open(_README, "r", encoding="utf-8") as fh:
            readme = fh.read()
        local_links = [
            target
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", readme)
            if "://" not in target and not target.startswith("#")
        ]
        self.assertTrue(local_links)
        for target in local_links:
            with self.subTest(target=target):
                self.assertTrue(os.path.exists(os.path.join(_REPO_ROOT, target)))


class TestRequiredContractsWorkflow(unittest.TestCase):
    """Documentation-only PRs must execute the real required contracts check."""

    def _workflow(self):
        path = os.path.join(_REPO_ROOT, ".github", "workflows", "local-preview.yml")
        with open(path, encoding="utf-8") as stream:
            # BaseLoader preserves GitHub's `on` key rather than YAML 1.1's bool.
            return yaml.load(stream, Loader=yaml.BaseLoader)

    def _assert_contract(self, workflow):
        for event in ("push", "pull_request"):
            self.assertIn(event, workflow["on"])
            self.assertIn(workflow["on"][event], (None, "", {}))
        job = workflow["jobs"]["contracts"]
        self.assertEqual(job.get("name", "contracts"), "contracts")
        for scope in [job, *job["steps"]]:
            self.assertNotIn("if", scope)
            self.assertNotIn("continue-on-error", scope)
        expected = shlex.split(
            "uv run --project mcp --frozen python -m unittest -v "
            "tests.test_local_render tests.test_blender_cli "
            "tests.test_tool_listing tests.test_documentation "
            "tests.test_preview_compatibility tests.test_mcp_server "
            "tests.integration.test_local_cpu_session"
        )
        commands = [shlex.split(step["run"]) for step in job["steps"] if "run" in step]
        self.assertIn(expected, commands)

    def test_docs_pr_runs_required_contracts(self):
        self._assert_contract(self._workflow())

    def test_regression_controls_reject_filtered_or_fake_checks(self):
        workflow = self._workflow()
        self._assert_contract(workflow)
        for fault in ("paths", "renamed", "conditional", "ignored_failure", "fake_command"):
            changed = copy.deepcopy(workflow)
            job = changed["jobs"]["contracts"]
            if fault == "paths":
                changed["on"]["pull_request"] = {"paths": ["mcp/**"]}
            elif fault == "renamed":
                job["name"] = "not-required"
            elif fault == "conditional":
                job["steps"][-1]["if"] = "false"
            elif fault == "ignored_failure":
                job["continue-on-error"] = "true"
            else:
                job["steps"][-1]["run"] = "echo passed"
            with self.subTest(fault=fault), self.assertRaises(AssertionError):
                self._assert_contract(changed)


if __name__ == "__main__":
    unittest.main()
