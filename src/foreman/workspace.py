"""Local checkout operations used by delivery runs."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
from typing import NewType, Protocol

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
CommitSHA = NewType("CommitSHA", str)
CheckLog = Callable[[str], None]


def commit_sha(value: str) -> CommitSHA | None:
    return CommitSHA(value) if FULL_SHA.fullmatch(value) else None


def head_mismatch_detail(observed: str, verified: str) -> str:
    return (
        f"HEAD is {observed}, verification checked {verified}. "
        "Reset the PR branch to the verified commit.\n"
        f"  git reset --keep {verified}\n"
        "Then rerun the same command. New commits need a new run."
    )


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

    def run(self, command: str, *, log: CheckLog | None = None) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def has_file(self, path: str) -> bool: ...
    def review_diff(self, base: str, head: str) -> ReviewDiff: ...


class LiveWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str, *, log: CheckLog | None = None) -> int:
        with subprocess.Popen(
            command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                if log is not None:
                    log(line)
            return process.wait()

    def has_file(self, path: str) -> bool:
        root = self.root.resolve()
        try:
            file = (root / path).resolve()
            return file.is_relative_to(root) and file.is_file()
        except (OSError, RuntimeError, ValueError):
            return False

    def inspect(self) -> WorktreeState:
        head = self._git("rev-parse", "HEAD").strip()
        if commit_sha(head) is None:
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = self._git("branch", "--show-current").strip()
        return WorktreeState(head, not status, branch or None)

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
