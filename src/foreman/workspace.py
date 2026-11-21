"""Local checkout operations used by delivery runs."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import NewType, Protocol

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
CommitSHA = NewType("CommitSHA", str)
CheckLog = Callable[[str, str], None]


def commit_sha(value: str) -> CommitSHA | None:
    return CommitSHA(value) if FULL_SHA.fullmatch(value) else None


def head_mismatch_detail(observed: str, verified: str) -> str:
    return (
        f"HEAD is {observed}, verification checked {verified}. "
        "This run is finished and the merge was not armed."
    )


class WorktreeInspectionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeState:
    head: str
    clean: bool
    branch: str | None


class Workspace(Protocol):
    root: Path

    def run(self, command: str, *, log: CheckLog | None = None) -> int: ...
    def inspect(self) -> WorktreeState: ...


class LiveWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str, *, log: CheckLog | None = None) -> int:
        result = subprocess.run(
            command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace",
        )
        if log is not None:
            log(result.stdout, result.stderr)
        return result.returncode

    def inspect(self) -> WorktreeState:
        head = self._git("rev-parse", "HEAD").strip()
        if commit_sha(head) is None:
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = self._git("branch", "--show-current").strip()
        return WorktreeState(head, not status, branch or None)

    def _git(self, *args: str) -> str:
        operation = " ".join(("git", *args))
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise WorktreeInspectionFailed(result.stderr.strip() or f"{operation} exited with code {result.returncode}.")
        return result.stdout
