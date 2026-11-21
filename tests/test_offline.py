"""Exercise the offline policy through real subprocess transports."""

import tests

from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class OfflineSuiteTest(unittest.TestCase):
    def test_guard_rejects_external_python_sockets(self):
        with socket.socket() as connection:
            with self.assertRaisesRegex(AssertionError, "External connection forbidden"):
                connection.connect(("192.0.2.1", 443))
        with self.assertRaisesRegex(AssertionError, "External name resolution forbidden"):
            socket.getaddrinfo("example.invalid", 443)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with socket.create_connection(listener.getsockname(), timeout=1):
                connection, _ = listener.accept()
                connection.close()

    def test_subprocess_transports_fail_fast_and_local_git_still_works(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/repo"], cwd=root, check=True)
            probes = (
                (["git", "fetch", "origin"], False, "transport 'https' not allowed"),
                (["git", "fetch", "example.invalid:repo"], False, "transport 'ssh' not allowed"),
                ("git fetch origin", True, "transport 'https' not allowed"),
                (["/usr/bin/env", "git", "fetch", "origin"], False, "transport 'https' not allowed"),
                (["curl", "--silent", "--show-error", "https://example.invalid/repo"], False, "127.0.0.1"),
                ([sys.executable, "-I", "-c",
                  "import urllib.request; urllib.request.urlopen('https://example.invalid/repo')"],
                 False, "Connection refused"),
            )
            for command, shell, error in probes:
                with self.subTest(command=command):
                    result = subprocess.run(command, shell=shell, cwd=root, capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)
            source = root / "source"
            subprocess.run(["git", "init", "--quiet", str(source)], check=True)
            subprocess.run([
                "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "-c", "commit.gpgSign=false", "commit", "--quiet", "--allow-empty", "-m", "fixture",
            ], cwd=source, check=True)
            for remote in (str(source), source.as_uri()):
                subprocess.run(["git", "fetch", "--quiet", remote], cwd=root, check=True, timeout=5)
