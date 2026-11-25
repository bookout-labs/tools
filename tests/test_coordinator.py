"""Coordinator startup delivers the installed role to an interactive session."""

import tests

from configparser import ConfigParser
from contextlib import redirect_stderr, redirect_stdout
from importlib.resources import files
from io import StringIO
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from foreman.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CoordinatorTest(unittest.TestCase):
    def test_session_receives_role_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/coordinator.md").write_text("Distinctive coordinator instructions.\n")
            with patch("foreman.cli.files", return_value=root), patch("foreman.cli.os.execvp") as launch:
                launch.side_effect = SystemExit(7)
                with self.assertRaises(SystemExit) as stopped:
                    main(["coordinator"])
                self.assertEqual(stopped.exception.code, 7)
            launch.assert_called_once_with("claude", [
                "claude", "--append-system-prompt", "Distinctive coordinator instructions.\n",
            ])

    @unittest.skipUnless(os.name == "posix", "terminal process-group signals require POSIX")
    def test_session_survives_ctrl_c_and_preserves_exit_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "claude"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import signal, sys\n"
                "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
                "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})\n"
                "print('ready', flush=True)\n"
                "signal.sigwait({signal.SIGINT})\n"
                "print('interrupted', flush=True)\n"
                "sys.stdin.readline()\n"
                "sys.exit(7)\n"
            )
            executable.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
            with subprocess.Popen(
                [sys.executable, "-c", "from foreman.cli import main; raise SystemExit(main(['coordinator']))"],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True,
            ) as process:
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 10)[0], "session did not start")
                    self.assertEqual(process.stdout.readline(), "ready\n")
                    os.killpg(process.pid, signal.SIGINT)
                    self.assertTrue(select.select([process.stdout], [], [], 10)[0], "session did not handle Ctrl-C")
                    self.assertEqual(process.stdout.readline(), "interrupted\n")
                    stdout, stderr = process.communicate("done\n", timeout=10)
                    self.assertEqual((process.returncode, stdout, stderr), (7, "", ""))
                finally:
                    if process.poll() is None:
                        process.kill()

    def test_invalid_role_stops_before_session_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            role = root / "roles/coordinator.md"
            for content in (None, b"\xff\xfe"):
                with self.subTest(content=content):
                    if content is not None:
                        role.write_bytes(content)
                    with patch("foreman.cli.files", return_value=root), patch("foreman.cli.os.execvp") as launch:
                        stdout, stderr = StringIO(), StringIO()
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            self.assertEqual(main(["coordinator"]), 1)
                        launch.assert_not_called()
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
                        self.assertIn("reinstall the selected bookout-tools revision", stderr.getvalue())

    def test_unreadable_role_stops_before_session_start(self):
        with patch("foreman.cli.files", side_effect=PermissionError()), \
                patch("foreman.cli.os.execvp") as launch, redirect_stderr(StringIO()):
            self.assertEqual(main(["coordinator"]), 1)
        launch.assert_not_called()

    def test_empty_role_stops_before_session_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "roles").mkdir()
            (root / "roles/coordinator.md").write_text(" \n")
            with patch("foreman.cli.files", return_value=root), patch("foreman.cli.os.execvp") as launch, \
                    redirect_stderr(StringIO()):
                self.assertEqual(main(["coordinator"]), 1)
            launch.assert_not_called()

    def test_unavailable_claude_reports_startup_error(self):
        for error, detail in (
            (FileNotFoundError(2, "No such file or directory"), "No such file or directory"),
            (OSError("launch failed"), "launch failed"),
        ):
            with self.subTest(error=error):
                stderr = StringIO()
                with patch("foreman.cli.os.execvp", side_effect=error), redirect_stderr(stderr):
                    self.assertEqual(main(["coordinator"]), 1)
                self.assertEqual(stderr.getvalue(), f"cannot start coordinator: failed to run claude: {detail}\n")

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
                sys.executable, "-c",
                "from setuptools.build_meta import build_wheel; build_wheel('dist')",
            ], cwd=source, check=True, capture_output=True, text=True)
            wheel, = (source / "dist").glob("*.whl")
            with ZipFile(wheel) as archive:
                archive.extractall(installed)
            entry_points, = installed.glob("*.dist-info/entry_points.txt")
            config = ConfigParser()
            config.read(entry_points)
            entry_point = config["console_scripts"]["foreman"]
            self.assertEqual(entry_point, "foreman.cli:main")
            module, function = entry_point.split(":")
            executable = outside / "claude"
            executable.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            executable.chmod(0o755)
            env = {**os.environ, "PYTHONPATH": str(installed), "PATH": str(outside) + os.pathsep + os.environ["PATH"]}
            command = [sys.executable, "-c", f"from {module} import {function}; raise SystemExit({function}())", "coordinator"]
            process = subprocess.run(command, cwd=outside, env=env, capture_output=True, text=True, check=True)
            args = json.loads(process.stdout)
            self.assertEqual(args, ["--append-system-prompt", files("foreman").joinpath("roles/coordinator.md").read_text()])
            instruction = args[1]
            for required in (
                "- title: the outcome in plain words.", "- body: holds the decisions already made",
                "The Ready label starts the delivery graph.",
                "builds that issue as one pull request when it fits, or slices it",
                "When you agree on an outcome, file one parent issue.",
                "Add the Ready label after the person agrees to file it.",
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
