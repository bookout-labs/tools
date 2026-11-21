"""Local checkout operations used by delivery runs."""

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
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


@dataclass(frozen=True)
class ReviewDiff:
    command: str
    patch: str


class Workspace(Protocol):
    root: Path

    def run(self, command: str) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def files_at(self, head: str, directory: str) -> set[str]: ...
    def read_file_at(self, head: str, path: str) -> str: ...
    def review_diff(self, base: str, head: str) -> ReviewDiff: ...


class LiveWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str) -> int:
        return subprocess.run(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL).returncode

    def inspect(self) -> WorktreeState:
        head = self._git("rev-parse", "HEAD").strip()
        if not FULL_SHA.fullmatch(head):
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = self._git("branch", "--show-current").strip()
        return WorktreeState(head, not status, branch or None)

    def files_at(self, head: str, directory: str) -> set[str]:
        tree = self._git("--no-replace-objects", "ls-tree", "-rz", "--full-tree", head, "--", directory)
        files: set[str] = set()
        for entry in tree.split("\0"):
            metadata, _, path = entry.partition("\t")
            if metadata.startswith(("100644 blob ", "100755 blob ")):
                files.add(path)
        return files

    def read_file_at(self, head: str, path: str) -> str:
        return self._git("--no-replace-objects", "show", f"{head}:{path}")

    def review_diff(self, base: str, head: str) -> ReviewDiff:
        if not (FULL_SHA.fullmatch(base) and FULL_SHA.fullmatch(head)):
            raise WorktreeInspectionFailed("Review requires full base and head SHAs.")
        self._git("fetch", "--no-tags", "origin", base, head, timeout=30)
        args = (
            "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-relative",
            "--src-prefix=a/", "--dst-prefix=b/", "--find-renames", f"{base}...{head}", "--",
        )
        return ReviewDiff(shlex.join(("git", *args)), self._git(*args))

    def _git(self, *args: str, timeout: float | None = None) -> str:
        operation = " ".join(("git", *args))
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise WorktreeInspectionFailed(result.stderr.strip() or f"{operation} exited with code {result.returncode}.")
        return result.stdout
