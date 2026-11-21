"""Local checkout operations used by delivery runs."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Annotated, Literal, NewType, Protocol
from typing_extensions import NotRequired, TypedDict
from pydantic import BeforeValidator, ConfigDict, TypeAdapter, with_config
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

from foreman.redaction import redact

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


class WorkspaceError(RuntimeError):
    pass


class ImplementationRejected(WorkspaceError):
    """Recoverable implementation history mismatch; keep the checkout."""


class ImplementationEdits(TypedDict):
    disposition: Literal["edits"]
    fingerprint: str


class AdoptedImplementation(TypedDict):
    disposition: Literal["adopted"]
    head: str
    tree: str
    fingerprint: str


ImplementationCandidate = ImplementationEdits | AdoptedImplementation


class WorktreeInspectionFailed(WorkspaceError):
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


ReviewLine = Annotated[tuple[str, int], BeforeValidator(TypeAdapter(tuple[object, ...]).validate_python)]


@with_config(ConfigDict(extra="forbid"))
class ReviewScopeRecord(TypedDict):
    command: str
    inline: list[ReviewLine]
    eligible: NotRequired[list[ReviewLine] | None]


@dataclass(frozen=True)
class ReviewScope:
    command: str
    inline: frozenset[tuple[str, int]]

    def record(self) -> ReviewScopeRecord:
        return {"command": self.command, "inline": sorted(self.inline)}


def added_lines(patch: str) -> frozenset[tuple[str, int]]:
    return frozenset((file.path, line.target_line_no) for file in PatchSet(patch) if not file.is_removed_file
                     for hunk in file for line in hunk if line.is_added and line.target_line_no is not None)


class Workspace(Protocol):
    root: Path

    def run(self, command: str, *, log: CheckLog | None = None) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def create_branch(self, branch: str, base: str) -> None: ...
    def refresh(self, branch: str, head: str) -> None: ...
    def commit(self, message: str) -> None: ...
    def committed(self, parent: str, message: str) -> bool: ...
    def reconcile(self, parent: str, branch: str) -> ImplementationCandidate | None: ...
    def tree(self, head: str) -> str: ...
    def soft_reset(self, parent: str) -> None: ...
    def fingerprint(self) -> str: ...
    def push(self, branch: str, repo: str) -> None: ...
    def files_at(self, head: str, directory: str) -> set[str]: ...
    def read_file_at(self, head: str, path: str) -> str: ...
    def has_file(self, path: str) -> bool: ...
    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize: ...
    def review_diff(self, base: str, head: str, *, interdiff: bool = False) -> ReviewDiff: ...
    def review_scope(self, base: str, head: str, previous: str | None) -> ReviewScope: ...


def branch_command(branch: str, base: str) -> list[str]:
    return ["git", "switch", "-c", redact(branch), f"refs/heads/{base}"]


def commit_commands(message: str) -> list[list[str]]:
    return [["git", "add", "--all"], ["git", "commit", "-m", redact(message)]]


def push_command(remote: str, branch: str) -> list[str]:
    ref = f"refs/heads/{redact(branch)}"
    return ["git", "push", "--no-follow-tags", remote, f"{ref}:{ref}"]


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

    def create_branch(self, branch: str, base: str) -> None:
        branch = redact(branch)
        refs = self._git("for-each-ref", "--format=%(refname)", f"refs/heads/{branch}")
        if f"refs/heads/{branch}" in os.fsdecode(refs).splitlines():
            self._git("merge-base", "--is-ancestor", f"refs/heads/{base}", f"refs/heads/{branch}")
            self._git("switch", branch)
        else:
            self._git(*branch_command(branch, base)[1:])

    def refresh(self, branch: str, head: str) -> None:
        current = self.inspect()
        if not current.clean or current.branch != branch:
            raise WorkspaceError("Restore a clean PR branch before adopting its repaired head.")
        self._git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        fetched = os.fsdecode(self._git("rev-parse", "FETCH_HEAD")).strip()
        if fetched != head:
            raise WorkspaceError("PR head changed during fetch. Resume with a fresh observation.")
        self._git("merge", "--ff-only", fetched)

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self._git("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"))
        for name in self._git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
            if name:
                digest.update(name)
                path = self.root / os.fsdecode(name)
                digest.update(os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes())
        return digest.hexdigest()

    def commit(self, message: str) -> None:
        for command in commit_commands(message):
            self._git(*command[1:])

    def committed(self, parent: str, message: str) -> bool:
        return (os.fsdecode(self._git("show", "-s", "--format=%P%n%B", "HEAD")).strip()
                == f"{parent}\n{redact(message)}".strip())

    def tree(self, head: str) -> str:
        return os.fsdecode(self._git("--no-replace-objects", "rev-parse", f"{head}^{{tree}}")).strip()

    def reconcile(self, parent: str, branch: str) -> ImplementationCandidate | None:
        observed = self.inspect()
        recovery = f" Checkout preserved. Restore branch {branch} at {parent}, keep the intended edits, then rerun."
        if observed.branch != branch:
            raise ImplementationRejected(f"HEAD {observed.head} is not attached to delivery branch {branch}." + recovery)
        if observed.head == parent:
            return None if observed.clean else {"disposition": "edits", "fingerprint": self.fingerprint()}
        if not observed.clean:
            raise ImplementationRejected(f"Moved HEAD {observed.head} has uncommitted edits." + recovery)
        try:
            ancestry = subprocess.run(
                ["git", "--no-replace-objects", "merge-base", "--is-ancestor", parent, "HEAD"],
                cwd=self.root, capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(f"git merge-base failed ({exc}).") from exc
        if ancestry.returncode == 1:
            raise ImplementationRejected(f"Worker HEAD {observed.head} is a non-descendant of {parent}." + recovery)
        if ancestry.returncode:
            raise WorktreeInspectionFailed(
                os.fsdecode(ancestry.stderr).strip() or f"git merge-base exited with code {ancestry.returncode}.",
            )
        sha = observed.head
        while sha != parent:
            parents = self._git("--no-replace-objects", "show", "-s", "--format=%P", sha).split()
            if not parents:
                raise ImplementationRejected(f"Worker HEAD {observed.head} is a non-descendant of {parent}; chain ends at {sha}." + recovery)
            if len(parents) != 1:
                raise ImplementationRejected(f"Worker commit {sha} is a merge commit." + recovery)
            ancestor = os.fsdecode(parents[0])
            if self.tree(sha) == self.tree(ancestor):
                raise ImplementationRejected(f"Worker commit {sha} is empty; each commit must be nonempty." + recovery)
            sha = ancestor
        tree = self.tree(observed.head)
        if self.tree(parent) == tree:
            raise ImplementationRejected(f"Worker HEAD {observed.head} has no net tree change." + recovery)
        remote = self._git("ls-remote", "origin", f"refs/heads/{branch}").split()
        if remote and os.fsdecode(remote[0]) != parent:
            raise ImplementationRejected(f"Delivery branch is already published at {os.fsdecode(remote[0])}; cannot replace worker HEAD {observed.head}." + recovery)
        diff = self._git("diff", parent, observed.head, "--binary", "--no-ext-diff", "--no-textconv")
        return {"disposition": "adopted", "head": observed.head, "tree": tree,
                "fingerprint": hashlib.sha256(diff).hexdigest()}

    def soft_reset(self, parent: str) -> None:
        self._git("reset", "--soft", parent)

    def push(self, branch: str, repo: str) -> None:
        remote = os.fsdecode(self._git("remote", "get-url", "origin")).strip()
        expected = repo.lower()
        urls = (f"https://github.com/{expected}", f"git@github.com:{expected}", f"ssh://git@github.com/{expected}")
        push_urls = os.fsdecode(self._git("remote", "get-url", "--push", "--all", "origin")).splitlines()
        if not push_urls or any(url.lower().removesuffix(".git").rstrip("/") not in urls for url in [remote, *push_urls]):
            raise WorkspaceError("origin does not point at the selected GitHub repository.")
        ref = f"refs/heads/{redact(branch)}"
        observed = self._git("ls-remote", "origin", ref).split()
        head = self._git("rev-parse", ref).strip()
        if not observed or observed[0] != head:
            self._git(*push_command("origin", branch)[1:])

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
        raw = self._git(*diff_options, "--numstat", "-z", merge_base, head, "--")
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
            crosses_exclusion = (before in generated) != (after in generated)
            if added == b"-" or crosses_exclusion:
                patch = self._git(
                    "--literal-pathspecs", *diff_options, "--patch", "--unified=0", "--inter-hunk-context=0",
                    *(("--no-renames",) if crosses_exclusion else ()),
                    "--no-color", merge_base, head, "--", *map(os.fsdecode, (before, after)),
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
        return ReviewScope(diff.command, inline)

    def _git(self, *args: str, input_paths: bytes | None = None, timeout: float | None = None) -> bytes:
        operation = f"git {args[0]}"
        if timeout is None:
            timeout = 30 if args[0] in {"fetch", "push", "ls-remote", "commit"} else 10
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, input=input_paths,
                env={**os.environ, "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL if input_paths is None else None, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeInspectionFailed(f"{operation} timed out after {timeout} seconds; rerun to resume.") from exc
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(f"{operation} failed ({exc}).") from exc
        if result.returncode:
            raise WorktreeInspectionFailed(
                os.fsdecode(result.stderr).strip() or f"{operation} exited with code {result.returncode}.",
            )
        return result.stdout
