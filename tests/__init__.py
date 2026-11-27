"""Install the unit suite's offline policy before test modules are imported."""

import atexit
import ipaddress
import os
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


with socket.socket() as proxy:
    proxy.bind(("127.0.0.1", 0))
    _proxy_url = f"http://127.0.0.1:{proxy.getsockname()[1]}"
os.environ["GIT_ALLOW_PROTOCOL"] = "file"
for _name in ("http_proxy", "https_proxy", "all_proxy"):
    os.environ[_name] = os.environ[_name.upper()] = _proxy_url
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
# Issue locks are per user. Each suite run gets its own runtime directory, shared by the processes it starts, so concurrent runs don't collide.
_RUNTIME_PREFIX = "foreman-test-runtime-"
if not os.path.basename(os.environ.get("XDG_RUNTIME_DIR", "")).startswith(_RUNTIME_PREFIX):
    os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp(prefix=_RUNTIME_PREFIX)
    atexit.register(shutil.rmtree, os.environ["XDG_RUNTIME_DIR"], True)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _audit(event, args):
    if event == "socket.connect":
        connection, address = args
        if connection.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address[0]):
            raise AssertionError(f"External connection forbidden: {address}")
    if event == "socket.getaddrinfo" and not _loopback(args[0]):
        raise AssertionError(f"External name resolution forbidden: {args[0]}")


sys.addaudithook(_audit)

# Every run in the suite mints this run id, so fixtures can name the branch it builds on. A test of two runs patches its own.
RUN_ID = "0a1b2c3d" * 4
patch("foreman.delivery.uuid4", return_value=SimpleNamespace(hex=RUN_ID)).start()
