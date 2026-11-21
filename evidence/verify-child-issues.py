"""Run the unmodified tests despite this sandbox denying their setup bind.

Only tests/__init__.py's proxy-port selection is mocked. All sockets and all
application operations are restored before discovery. Tests requiring actual
loopback servers will still fail under the sandbox and must run in normal CI.
"""

from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.object(socket.socket, "bind"), patch.object(socket.socket, "getsockname", return_value=("127.0.0.1", 9)):
    import tests

pattern = sys.argv[1] if len(sys.argv) > 1 else "test_*.py"
print("Only offline-policy import setup uses a bind workaround; tests run unchanged.", flush=True)
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests", pattern=pattern))
raise SystemExit(not result.wasSuccessful())
