"""Local checkout operations used by delivery runs."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Protocol, TypedDict

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class WorktreeInspectionFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeState:
    head: str
    clean: bool
    branch: str | None


class PullRequestSize(TypedDict):
    head: str
    merge_base: str
    lines: int


class Workspace(Protocol):
    root: Path

    def run(self, command: str) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def pull_request_size(self, head: str) -> PullRequestSize: ...


class LiveWorkspace:
    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str) -> int:
        return subprocess.run(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL).returncode

    def inspect(self) -> WorktreeState:
        head = os.fsdecode(self._git("rev-parse", "HEAD")).strip()
        if not FULL_SHA.fullmatch(head):
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = os.fsdecode(self._git("branch", "--show-current")).strip()
        return WorktreeState(head, not status, branch or None)

    def pull_request_size(self, head: str) -> PullRequestSize:
        merge_base = os.fsdecode(self._git("merge-base", "refs/remotes/origin/main", head)).strip()
        raw = self._git(
            f"--attr-source={merge_base}", "diff", "--numstat", "-z", "--find-renames",
            "--no-ext-diff", "--no-textconv", merge_base, head, "--", "src/", "tests/",
        )
        lines_by_path: dict[bytes, int] = {}
        if raw:
            records = iter(raw.removesuffix(b"\0").split(b"\0"))
            for record in records:
                added, removed, path = record.split(b"\t", 2)
                before, after = (path, path) if path else (next(records), next(records))
                if added != b"-":
                    lines_by_path[before] = lines_by_path.get(before, 0) + int(removed)
                    lines_by_path[after] = lines_by_path.get(after, 0) + int(added)
        lines = 0
        if lines_by_path:
            attrs = self._git(
                "check-attr", f"--source={merge_base}", "--stdin", "-z", "linguist-generated",
                input_paths=b"\0".join(lines_by_path) + b"\0",
            )
            fields = attrs.removesuffix(b"\0").split(b"\0")
            for index in range(0, len(fields), 3):
                path, _, value = fields[index:index + 3]
                if value != b"set":
                    lines += lines_by_path[path]
        return {"head": head, "merge_base": merge_base, "lines": lines}

    def _git(self, *args: str, input_paths: bytes | None = None) -> bytes:
        operation = " ".join(("git", *args))
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, input=input_paths,
                stdin=subprocess.DEVNULL if input_paths is None else None,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise WorktreeInspectionFailed(
                os.fsdecode(result.stderr).strip() or f"{operation} exited with code {result.returncode}.",
            )
        return result.stdout
