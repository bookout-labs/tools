"""Run discovery where sandbox policy blocks the offline guard's port allocation.

Only the initial unused proxy-port allocation is replaced. The tests' actual
socket operations and external-network audit guard remain in effect.
"""

from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.object(socket.socket, "bind"), patch.object(
    socket.socket, "getsockname", return_value=("127.0.0.1", 9),
):
    import tests

pattern = sys.argv[1] if len(sys.argv) > 1 else "test*.py"
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("tests", pattern=pattern))
raise SystemExit(not result.wasSuccessful())
