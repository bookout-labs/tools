"""Run the unit suite with external connections forbidden, including Git fetches."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


GUARD = '''
import ipaddress
import socket
import sys
from urllib.parse import urlsplit


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def audit(event, args):
    if event == "socket.connect":
        connection, address = args
        if connection.family in (socket.AF_INET, socket.AF_INET6) and not loopback(address[0]):
            raise AssertionError(f"External connection forbidden: {address}")
    if event == "subprocess.Popen":
        executable, command, cwd, env = args
        if isinstance(command, (list, tuple)) and str(command[0]).split("/")[-1] == "git":
            for arg in command[1:]:
                value = str(arg)
                url = urlsplit(value)
                if url.scheme in ("http", "https", "ssh", "git") and not loopback(url.hostname or ""):
                    raise AssertionError(f"External Git transport forbidden: {value}")

sys.addaudithook(audit)
'''


class OfflineSuiteTest(unittest.TestCase):
    @unittest.skipIf(os.environ.get("FOREMAN_OFFLINE_GUARD"), "already inside the offline suite")
    def test_suite_without_external_connections(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory(prefix="foreman-offline-") as directory:
            Path(directory, "sitecustomize.py").write_text(GUARD)
            env = {
                **os.environ,
                "FOREMAN_OFFLINE_GUARD": "1",
                "PYTHONPATH": os.pathsep.join((directory, str(root / "tests"), os.environ.get("PYTHONPATH", ""))),
            }
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=root, env=env, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
