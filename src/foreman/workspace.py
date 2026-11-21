"""Local checkout operations used by delivery runs."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Protocol, TypedDict

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
ATTRIBUTE_CONFIG = ("-c", "core.attributesFile=/dev/null")
HUNK = re.compile(rb"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", re.MULTILINE)


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

@dataclass(frozen=True)
class ReviewDiff:
    command: str
    patch: str


class Workspace(Protocol):
    root: Path

    def run(self, command: str) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize: ...

    def review_diff(self, base: str, head: str) -> ReviewDiff: ...


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

    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize:
        attributes = Path(os.fsdecode(self._git(
            "rev-parse", "--path-format=absolute", "--git-path", "info/attributes",
        )).removesuffix("\n"))
        try:
            if attributes.stat().st_size:
                raise WorktreeInspectionFailed(f"{attributes} must be empty to measure a pull request.")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorktreeInspectionFailed(f"Could not inspect {attributes}: {exc}") from exc

        base_ref = f"refs/remotes/origin/{base_branch}"
        self._git(
            "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin",
            f"+refs/heads/{base_branch}:{base_ref}",
        )
        merge_base = os.fsdecode(self._git("merge-base", base_ref, head)).strip()
        diff_options = (
            *ATTRIBUTE_CONFIG, f"--attr-source={merge_base}", "diff", "--find-renames", "--text",
            "--no-ext-diff", "--no-textconv",
        )
        raw = self._git(
            *diff_options, "--numstat", "-z", merge_base, head, "--", "src/", "tests/",
        )
        if not raw:
            return {"head": head, "merge_base": merge_base, "lines": 0}
        entries: list[tuple[bytes, bytes, bytes, bytes]] = []
        paths: set[bytes] = set()
        records = iter(raw.removesuffix(b"\0").split(b"\0"))
        for record in records:
            added, removed, path = record.split(b"\t", 2)
            before, after = (path, path) if path else (next(records), next(records))
            entries.append((before, after, removed, added))
            paths.update((before, after))
        attrs = self._git(
            *ATTRIBUTE_CONFIG, "check-attr", f"--source={merge_base}", "--stdin", "-z", "linguist-generated",
            input_paths=b"\0".join(paths) + b"\0",
        )
        fields = attrs.removesuffix(b"\0").split(b"\0")
        generated: set[bytes] = set()
        for index in range(0, len(fields), 3):
            path, _, value = fields[index:index + 3]
            if value in (b"set", b"true"):
                generated.add(path)
        lines = 0
        for before, after, removed, added in entries:
            if before in generated and after in generated:
                continue
            if added == b"-":
                # Numstat can report binary even when Git emits a text patch.
                patch = self._git(
                    "--literal-pathspecs", *diff_options, "--patch", "--unified=0", "--inter-hunk-context=0",
                    "--no-color", merge_base, head, "--", os.fsdecode(before), os.fsdecode(after),
                )
                hunks = HUNK.findall(patch)
                removed_lines = sum(int(old or b"1") for old, _ in hunks)
                added_lines = sum(int(new or b"1") for _, new in hunks)
            else:
                removed_lines, added_lines = int(removed), int(added)
            if before not in generated:
                lines += removed_lines
            if after not in generated:
                lines += added_lines
        return {"head": head, "merge_base": merge_base, "lines": lines}

    def review_diff(self, base: str, head: str) -> ReviewDiff:
        if not (FULL_SHA.fullmatch(base) and FULL_SHA.fullmatch(head)):
            raise WorktreeInspectionFailed("Review requires full base and head SHAs.")
        self._git("fetch", "--no-tags", "origin", base, head, timeout=30)
        args = (
            "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-relative",
            "--src-prefix=a/", "--dst-prefix=b/", "--find-renames", f"{base}...{head}", "--",
        )
        return ReviewDiff(shlex.join(("git", *args)), os.fsdecode(self._git(*args)))

    def _git(self, *args: str, input_paths: bytes | None = None, timeout: float | None = None) -> bytes:
        operation = " ".join(("git", *args))
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, input=input_paths,
                env={**os.environ, "GIT_ATTR_NOSYSTEM": "1"},
                stdin=subprocess.DEVNULL if input_paths is None else None, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise WorktreeInspectionFailed(
                os.fsdecode(result.stderr).strip() or f"{operation} exited with code {result.returncode}.",
            )
        return result.stdout
