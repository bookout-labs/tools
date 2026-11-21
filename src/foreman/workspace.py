"""Local checkout operations used by delivery runs."""

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import NewType, Protocol
from typing_extensions import TypedDict
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
ATTRIBUTE_CONFIG = ("-c", "core.attributesFile=/dev/null")
HUNK = re.compile(rb"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", re.MULTILINE)
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


class PullRequestSize(TypedDict):
    head: str
    merge_base: str
    lines: int


@dataclass(frozen=True)
class ReviewDiff:
    command: str
    patch: str


@dataclass(frozen=True)
class ReviewScope:
    command: str
    inline: frozenset[tuple[str, int]]
    eligible: frozenset[tuple[str, int]] | None = None


def added_lines(patch: str) -> frozenset[tuple[str, int]]:
    return frozenset((file.path, line.target_line_no) for file in PatchSet(patch) if not file.is_removed_file
                     for hunk in file for line in hunk if line.is_added and line.target_line_no is not None)


class Workspace(Protocol):
    root: Path

    def run(self, command: str, *, log: CheckLog | None = None) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def files_at(self, head: str, directory: str) -> set[str]: ...
    def read_file_at(self, head: str, path: str) -> str: ...
    def has_file(self, path: str) -> bool: ...
    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize: ...
    def review_diff(self, base: str, head: str, *, interdiff: bool = False) -> ReviewDiff: ...
    def review_scope(self, base: str, head: str, previous: str | None) -> ReviewScope: ...


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
        head = os.fsdecode(self._git("rev-parse", "HEAD")).strip()
        if commit_sha(head) is None:
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = os.fsdecode(self._git("branch", "--show-current")).strip()
        return WorktreeState(head, not status, branch or None)

    def files_at(self, head: str, directory: str) -> set[str]:
        tree = os.fsdecode(self._git("--no-replace-objects", "ls-tree", "-rz", "--full-tree", head, "--", directory))
        files: set[str] = set()
        for entry in tree.split("\0"):
            metadata, _, path = entry.partition("\t")
            if metadata.startswith(("100644 blob ", "100755 blob ")):
                files.add(path)
        return files

    def read_file_at(self, head: str, path: str) -> str:
        try:
            return self._git("--no-replace-objects", "show", f"{head}:{path}").decode("utf-8")
        except UnicodeError as exc:
            raise WorktreeInspectionFailed(f"Could not decode {path}: {exc}") from exc
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

    def review_diff(self, base: str, head: str, *, interdiff: bool = False) -> ReviewDiff:
        if not (FULL_SHA.fullmatch(base) and FULL_SHA.fullmatch(head)):
            raise WorktreeInspectionFailed("Review requires full base and head SHAs.")
        self._git("fetch", "--no-tags", "origin", base, head, timeout=30)
        args = (
            "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-relative",
            "--src-prefix=a/", "--dst-prefix=b/", "--find-renames", f"{base}..{head}" if interdiff else f"{base}...{head}", "--",
        )
        return ReviewDiff(shlex.join(("git", *args)), os.fsdecode(self._git(*args)))

    def review_scope(self, base: str, head: str, previous: str | None) -> ReviewScope:
        pr = self.review_diff(base, head)
        try:
            inline = added_lines(pr.patch)
        except UnidiffParseError as exc:
            if previous is not None:
                raise WorktreeInspectionFailed("PR diff could not be parsed for scoping.") from exc
            inline = frozenset[tuple[str, int]]()
        if previous is None:
            return ReviewScope(pr.command, inline)
        diff = self.review_diff(previous, head, interdiff=True)
        try:
            eligible = added_lines(diff.patch) & inline
        except UnidiffParseError as exc:
            raise WorktreeInspectionFailed("Review interdiff could not be parsed.") from exc
        return ReviewScope(diff.command, inline, eligible)

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
