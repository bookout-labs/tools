import tests

import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from foreman.process_group import owned_group
from foreman.worker import run_worker_command


def gone(pid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class OwnedGroupTests(unittest.TestCase):
    def test_a_leftover_holding_stdout_neither_blocks_the_worker_nor_outlives_it(self):
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / "leftover.pid"
            started = time.monotonic()
            done = run_worker_command(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; echo hi"], Path(directory), {})
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual((done.returncode, done.stdout), (0, "hi\n"))
            self.assertTrue(gone(int(pidfile.read_text())))

    def test_the_failure_that_ended_the_block_is_the_one_raised(self):
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / "child.pid"
            with self.assertRaisesRegex(RuntimeError, "synthetic"), \
                 owned_group(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], stdout=subprocess.DEVNULL):
                while not pidfile.is_file() or not pidfile.read_text().strip():
                    time.sleep(0.01)
                raise RuntimeError("synthetic")
            self.assertTrue(gone(int(pidfile.read_text())))

    def test_the_worker_group_ends_when_foreman_is_terminated(self):
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / "worker.pid"
            script = f"""
import sys
from pathlib import Path
from foreman.process_group import signals_end_foreman
from foreman.worker import run_worker_command
with signals_end_foreman():
    run_worker_command(["sh", "-c", "sleep 30 & echo $! > {pidfile}; wait"], Path({directory!r}), {{}})
"""
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            foreman = subprocess.Popen([sys.executable, "-c", script], env=env)
            try:
                deadline = time.monotonic() + 10
                while not (pidfile.is_file() and pidfile.read_text().strip()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                worker = int(pidfile.read_text())
                foreman.send_signal(signal.SIGTERM)
                self.assertEqual(foreman.wait(timeout=10), 128 + signal.SIGTERM)
                self.assertTrue(gone(worker))
            finally:
                if foreman.poll() is None:
                    foreman.kill()
                    foreman.wait()


if __name__ == "__main__":
    unittest.main()
