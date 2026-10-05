"""Project selection across native hosts, CLI calls, and legacy Claude sessions."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop_state
import models
import state_discovery as roots
from multiagent import cli, config

DISPATCHER = Path(__file__).resolve().parents[2] / "crew"


class ProjectRootTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.project = self.base / "project with spaces"
        self.legacy = self.base / "legacy"
        self.cwd = self.base / "elsewhere"
        for path in (self.project, self.legacy, self.cwd):
            path.mkdir()
        patch = mock.patch.dict(os.environ, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        previous = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        roots._warned.clear()
        self.addCleanup(roots._warned.clear)

    def test_precedence_and_empty_overrides(self) -> None:
        cases = (
            (
                {
                    "CREW_PROJECT_DIR": str(self.project),
                    "CLAUDE_PROJECT_DIR": str(self.legacy),
                },
                self.project,
            ),
            ({"CREW_PROJECT_DIR": str(self.project)}, self.project),
            (
                {"CREW_PROJECT_DIR": "", "CLAUDE_PROJECT_DIR": str(self.legacy)},
                self.legacy,
            ),
            ({"CLAUDE_PROJECT_DIR": str(self.legacy)}, self.legacy),
            ({"CREW_PROJECT_DIR": "", "CLAUDE_PROJECT_DIR": ""}, self.cwd),
            ({}, self.cwd),
        )
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(roots.crew_base(), expected)

    def test_payload_order_and_fail_closed_without_root(self) -> None:
        payload = {
            "workspace_roots": [str(self.base / "missing"), str(self.project)],
            "directory": str(self.legacy),
            "cwd": str(self.cwd),
        }
        self.assertEqual(roots.crew_base(payload, fallback_to_cwd=False), self.project)
        self.assertEqual(roots.crew_base({"directory": str(self.legacy)}), self.legacy)
        self.assertEqual(roots.crew_base({"cwd": str(self.project)}), self.project)
        self.assertIsNone(roots.crew_base({}, fallback_to_cwd=False))
        with mock.patch.dict(os.environ, {"CREW_PROJECT_DIR": str(self.legacy)}):
            self.assertEqual(roots.crew_base(payload), self.legacy)

    def test_state_config_hooks_and_artifacts_share_override(self) -> None:
        os.environ.update(
            CREW_PROJECT_DIR=str(self.project), CLAUDE_PROJECT_DIR=str(self.legacy)
        )
        self.assertEqual(config._project_dir(), self.project)
        self.assertEqual(
            Path(models._get_project_dir({"cwd": str(self.cwd)})), self.project
        )
        self.assertEqual(
            loop_state.resolve("mt", "project-root-test").parent, self.project / ".crew"
        )
        self.assertEqual(
            cli._reviews_subdir("project-root-test"),
            self.project / ".crew/reviews/project-root-test",
        )
        self.assertEqual(
            roots.anchor_path(".crew/plan.md"), str(self.project / ".crew/plan.md")
        )
        self.assertEqual(
            roots.anchor_path(str(self.cwd / "absolute.md")),
            str(self.cwd / "absolute.md"),
        )
        self.assertEqual(roots.anchor_path("-"), "-")
        self.assertFalse((self.legacy / ".crew").exists())
        self.assertFalse((self.cwd / ".crew").exists())

    def test_fallback_reanchoring_and_explicit_cleanup_guard(self) -> None:
        nested = self.project / ".crew/.crew"
        nested.mkdir(parents=True)
        os.chdir(nested)
        self.assertTrue(roots.cwd_reanchored())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(roots.crew_base(), self.project)
            self.assertEqual(roots.crew_base(), self.project)
        self.assertEqual(stderr.getvalue().count("ends in .crew"), 1)
        self.assertIn("set CREW_PROJECT_DIR", stderr.getvalue())
        os.environ["CREW_PROJECT_DIR"] = str(self.project)
        self.assertFalse(roots.cwd_reanchored())
        self.assertEqual(roots.crew_base(), self.project)
        # Explicit roots keep their literal semantics, even when named .crew.
        os.environ["CREW_PROJECT_DIR"] = str(nested)
        self.assertEqual(roots.crew_base(), nested)
        self.assertFalse(roots.cwd_reanchored())

    def test_payload_reanchoring_remains_shared(self) -> None:
        nested = self.project / ".crew/.crew"
        nested.mkdir(parents=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(roots.crew_base({"cwd": str(nested)}), self.project)

    def test_cli_is_absolute_side_effect_free_and_accepts_legacy(self) -> None:
        alias = self.base / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        for env, expected in (
            ({}, self.cwd),
            (
                {
                    "CREW_PROJECT_DIR": str(alias),
                    "CLAUDE_PROJECT_DIR": str(self.legacy),
                },
                self.project,
            ),
            ({"CLAUDE_PROJECT_DIR": str(self.legacy)}, self.legacy),
            ({"CREW_PROJECT_DIR": "../project with spaces"}, self.project),
        ):
            with self.subTest(env=env):
                result = subprocess.run(
                    [sys.executable, str(DISPATCHER), "project-root"],
                    env=env,
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, str(expected) + "\n")
                self.assertEqual(result.stderr, "")
        self.assertFalse(list(self.base.rglob(".crew")))


if __name__ == "__main__":
    unittest.main()
