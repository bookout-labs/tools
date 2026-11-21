"""Local checkout operations used by delivery runs."""

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Protocol

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class WorkspaceError(RuntimeError):
    pass


class WorktreeInspectionFailed(WorkspaceError):
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
    def create_branch(self, branch: str, base: str) -> None: ...
    def commit(self, message: str) -> None: ...
    def push(self, branch: str) -> None: ...


def branch_command(branch: str, base: str) -> list[str]:
    return ["git", "switch", "-c", branch, f"refs/heads/{base}"]


def commit_commands(message: str) -> list[list[str]]:
    return [["git", "add", "--all"], ["git", "commit", "-m", message]]


def push_command(remote: str, branch: str) -> list[str]:
    ref = f"refs/heads/{branch}"
    return ["git", "push", "--no-follow-tags", remote, f"{ref}:{ref}"]


class LiveWorkspace:
    def __init__(self, root: Path, push_remote: str = "origin"):
        self.root = root
        self.push_remote = push_remote

    def run(self, command: str) -> int:
        return subprocess.run(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL).returncode

    def inspect(self) -> WorktreeState:
        try:
            head = self._git(["git", "rev-parse", "HEAD"]).strip()
            if not FULL_SHA.fullmatch(head):
                raise WorkspaceError("Git did not report a full 40-hex HEAD SHA.")
            status = self._git(["git", "status", "--porcelain", "--untracked-files=all"])
            branch = self._git(["git", "branch", "--show-current"]).strip()
        except WorkspaceError as exc:
            raise WorktreeInspectionFailed(str(exc)) from exc
        return WorktreeState(head, not status, branch or None)

    def create_branch(self, branch: str, base: str) -> None:
        self._git(branch_command(branch, base))

    def commit(self, message: str) -> None:
        for command in commit_commands(message):
            self._git(command)

    def push(self, branch: str) -> None:
        self._git(push_command(self.push_remote, branch))

    def _git(self, command: list[str]) -> str:
        operation = " ".join(command[:2])
        try:
            result = subprocess.run(
                command, cwd=self.root, capture_output=True, text=True, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorkspaceError(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or f"{operation} exited with code {result.returncode}.")
        return result.stdout
