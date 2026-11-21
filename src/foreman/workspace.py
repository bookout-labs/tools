"""Local checkout operations used by delivery runs."""

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Protocol

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class WorktreeInspectionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeState:
    head: str
    clean: bool
    branch: str | None


class Workspace(Protocol):
    root: Path

    def run(self, command: str) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def has_file(self, path: str) -> bool: ...


class LiveWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str) -> int:
        return subprocess.run(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL).returncode

    def has_file(self, path: str) -> bool:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        root = self.root.resolve()
        try:
            file = (root / relative).resolve()
            return file.is_relative_to(root) and file.is_file()
        except (OSError, RuntimeError, ValueError):
            return False

    def inspect(self) -> WorktreeState:
        head = self._git("rev-parse", "HEAD").strip()
        if not FULL_SHA.fullmatch(head):
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
