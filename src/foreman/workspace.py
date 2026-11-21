"""Local checkout operations used by delivery runs."""

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Protocol


@dataclass(frozen=True)
class WorktreeState:
    head: str
    clean: bool
    branch: str | None


class Workspace(Protocol):
    def run(self, command: str) -> int: ...
    def inspect(self) -> WorktreeState: ...


class LiveWorkspace:
    def __init__(self, cwd: Path):
        self.cwd = cwd

    def run(self, command: str) -> int:
        return subprocess.run(command, shell=True, cwd=self.cwd, stdin=subprocess.DEVNULL).returncode

    def inspect(self) -> WorktreeState:
        head = self._git("rev-parse", "HEAD")
        status = self._git("status", "--porcelain")
        branch = self._git("branch", "--show-current")
        clean = head.returncode == status.returncode == branch.returncode == 0 and not status.stdout
        return WorktreeState(head.stdout.strip(), clean, branch.stdout.strip() or None)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
