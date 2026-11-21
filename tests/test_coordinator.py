"""Coordinator startup delivers the installed role to an interactive session."""

import tests

from contextlib import redirect_stderr, redirect_stdout
from importlib.resources import files
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CoordinatorTest(unittest.TestCase):
    def test_session_receives_role_once_and_returns_session_exit_code(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/coordinator.md").write_text("Distinctive coordinator instructions.\n")
            with patch("foreman.cli.files", return_value=root), patch("foreman.cli.subprocess.run") as launch:
                launch.return_value.returncode = 7
                self.assertEqual(main(["coordinator"]), 7)
            launch.assert_called_once_with([
                "claude", "--append-system-prompt", "Distinctive coordinator instructions.\n",
            ])

    def test_invalid_role_stops_before_session_start(self):
        for error in (FileNotFoundError(), PermissionError(), UnicodeError(), ValueError()):
            with self.subTest(error=type(error).__name__), patch("foreman.cli.files", side_effect=error), \
                    patch("foreman.cli.subprocess.run") as launch:
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(["coordinator"]), 1)
                launch.assert_not_called()
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                self.assertIn("reinstall the selected bookout-tools revision", stderr.getvalue())

    def test_empty_role_stops_before_session_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/coordinator.md").write_text(" \n")
            with patch("foreman.cli.files", return_value=root), patch("foreman.cli.subprocess.run") as launch, \
                    redirect_stderr(StringIO()):
                self.assertEqual(main(["coordinator"]), 1)
            launch.assert_not_called()

    def test_unavailable_claude_reports_startup_error(self):
        stderr = StringIO()
        with patch("foreman.cli.subprocess.run", side_effect=FileNotFoundError(2, "No such file or directory")), \
                redirect_stderr(stderr):
            self.assertEqual(main(["coordinator"]), 1)
        self.assertEqual(stderr.getvalue(), "cannot start coordinator: No such file or directory\n")

    def test_installed_command_loads_packaged_role_outside_checkout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, installed, outside = root / "source", root / "installed", root / "outside"
            source.mkdir()
            outside.mkdir()
            shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            for name in ("pyproject.toml", "README.md"):
                shutil.copy(ROOT / name, source)
            subprocess.run([
                sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
                "--wheel-dir", str(root), str(source),
            ], check=True, capture_output=True, text=True)
            wheel, = root.glob("*.whl")
            subprocess.run([
                sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(installed), str(wheel),
            ], check=True, capture_output=True, text=True)
            executable = outside / "claude"
            executable.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            executable.chmod(0o755)
            env = {**os.environ, "PYTHONPATH": str(installed), "PATH": str(outside) + os.pathsep + os.environ["PATH"]}
            command = [sys.executable, str(installed / "bin/foreman"), "coordinator"]
            process = subprocess.run(command, cwd=outside, env=env, capture_output=True, text=True, check=True)
            args = json.loads(process.stdout)
            self.assertEqual(args, ["--append-system-prompt", files("foreman").joinpath("roles/coordinator.md").read_text()])
            instruction = args[1]
            for required in (
                "- title: the outcome in plain words.", "- body: holds the decisions already made",
                "The factory label triggers the delivery graph.",
                "builds that issue as one pull request when it fits, or slices it",
                "When you agree on an outcome, file one parent issue.",
                "Add the factory label after the person agrees to file it.",
                "arms the merge. This session does not.",
                "When the person asks, or when a run is waiting on a decision",
                "A suggestion stays in the conversation until the person says to file it.",
            ):
                self.assertIn(required, instruction)
            (installed / "foreman/roles/coordinator.md").unlink()
            process = subprocess.run(command, cwd=outside, env=env, capture_output=True, text=True)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stdout, "")
            self.assertEqual(len(process.stderr.splitlines()), 1)
            self.assertIn("cannot start coordinator", process.stderr)
