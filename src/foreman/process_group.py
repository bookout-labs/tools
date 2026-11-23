"""Run a child as the leader of its own process group, which ends with the child and with Foreman."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
import os
import signal
import subprocess
import time
from typing import Any


@contextmanager
def signals_end_foreman() -> Generator[None]:
    """Turn TERM and INT into SystemExit, so every owned group ends before Foreman does."""
    def stop(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def owned_group(command: Sequence[str], grace: float = 0, **popen: Any) -> Generator[subprocess.Popen[Any]]:
    """Start command in a new session. When the block exits, however it exits, end every process in the group.

    With a grace period the group gets TERM first. The block returns only after the group is gone, so nothing
    the child left behind keeps an inherited file descriptor open.
    """
    process = subprocess.Popen(command, start_new_session=True, **popen)
    try:
        yield process
    finally:
        if grace:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=grace)
        with suppress(ProcessLookupError):
            # Processes can ignore TERM even after their parent exits.
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        # Orphans are reaped by init shortly after the signal. macOS answers EPERM while only zombies remain.
        with suppress(ProcessLookupError, PermissionError):
            while True:
                os.killpg(process.pid, 0)
                time.sleep(0.01)
