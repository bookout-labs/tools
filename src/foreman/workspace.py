"""Local checkout operations used by delivery runs."""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, NewType, Protocol
from typing_extensions import NotRequired, TypedDict
from pydantic import BaseModel, BeforeValidator, ConfigDict, TypeAdapter, with_config
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

from foreman.credentials import CredentialFailure, COMMAND_TIMEOUT, credentials, failure, git_auth, validate_remote
from foreman.redaction import redact
from foreman.review_context import ReviewContext, ReviewSide

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
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


class PullRequestSize(TypedDict):
    head: str
    merge_base: str
    lines: int


class ImplementationEdits(TypedDict):
    disposition: Literal["edits"]
    fingerprint: str
    size: PullRequestSize


class AdoptedImplementation(TypedDict):
    disposition: Literal["adopted"]
    head: str
    tree: str
    fingerprint: str
    size: PullRequestSize


ImplementationCandidate = ImplementationEdits | AdoptedImplementation


class WorkState(BaseModel, frozen=True, extra="forbid"):
    branch: str = ""
    base: str = ""
    attempt_head: str = ""
    candidate: ImplementationCandidate | None = None
    partial_edits: str = ""
    head_sha: str | None = None
    repair_head: str | None = None


class WorktreeInspectionFailed(WorkspaceError):
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
    merge_base: str | None = None


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

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(line.strip() for line in self.command.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))

    def record(self) -> ReviewScopeRecord:
        return {"command": self.command, "inline": sorted(self.inline)}


def added_lines(patch: str) -> frozenset[tuple[str, int]]:
    return frozenset((file.path, line.target_line_no) for file in PatchSet(patch) if not file.is_removed_file
                     for hunk in file for line in hunk if line.is_added and line.target_line_no is not None)


def changed_paths(raw: bytes) -> tuple[frozenset[str], frozenset[str]]:
    """Read --name-status -z output as changed paths on base and head."""
    base: set[str] = set()
    head: set[str] = set()
    if not raw:
        return frozenset(), frozenset()
    if not raw.endswith(b"\0"):
        raise WorktreeInspectionFailed("Unterminated name-status record.")
    fields = iter(raw[:-1].split(b"\0"))
    try:
        for status in fields:
            if re.fullmatch(rb"(?:[ADMT]|[RC][0-9]+)", status) is None:
                raise WorktreeInspectionFailed(f"Unknown name-status status: {status!r}.")
            path = next(fields)
            target = next(fields) if status.startswith((b"R", b"C")) else path
            if not path or not target:
                raise WorktreeInspectionFailed("Empty name-status path.")
            if status[:1] in (b"D", b"M", b"T", b"R"):
                base.add(os.fsdecode(path))
            if status[:1] in (b"A", b"M", b"T", b"R", b"C"):
                head.add(os.fsdecode(target))
    except StopIteration as exc:
        raise WorktreeInspectionFailed("Incomplete name-status record.") from exc
    return frozenset(base), frozenset(head)


def line_counts(raw: bytes, revision: str) -> dict[str, int]:
    lines: dict[str, int] = {}
    prefix = os.fsencode(revision) + b":"
    records = iter(raw.split(b"\0"))
    path = next(records)
    for record in records:
        count, newline, next_path = record.partition(b"\n")
        if not newline or not path.startswith(prefix) or not count.isdigit():
            raise WorktreeInspectionFailed("Malformed git grep line-count record.")
        lines[os.fsdecode(path[len(prefix):])] = int(count)
        path = next_path
    if path:
        raise WorktreeInspectionFailed("Unterminated git grep line-count record.")
    return lines


class Workspace(Protocol):
    root: Path

    def run(self, command: str, *, log: CheckLog | None = None) -> int: ...
    def inspect(self) -> WorktreeState: ...
    def review_context(self, base: str, head: str) -> ReviewContext: ...
    def validate_origin(self, repo: str) -> None: ...
    def pin_base(self, base: str) -> str: ...
    def create_branch(self, branch: str, base: str, *, pinned: str = "") -> None: ...
    def refresh(self, branch: str, head: str) -> None: ...
    def commit(self, message: str) -> None: ...
    def committed(self, parent: str, message: str) -> bool: ...
    def reconcile(self, parent: str, branch: str, base_branch: str) -> ImplementationCandidate | None: ...
    def tree(self, head: str | None) -> str: ...
    def soft_reset(self, parent: str) -> None: ...
    def fingerprint(self) -> str: ...
    def push(self, branch: str, repo: str) -> None: ...
    def has_tracked_file(self, base: str, path: str) -> bool: ...
    def contains_commit(self, base: str, commit: str) -> bool: ...
    def pull_request_size(self, head: str | None, base_branch: str, *, merge_base: str | None = None) -> PullRequestSize: ...
    def review_diff(self, base: str, head: str) -> ReviewDiff: ...
    def review_scope(self, base: str, head: str) -> ReviewScope: ...


def branch_command(branch: str, base: str) -> list[str]:
    return ["git", "switch", "-c", redact(branch), base]


def commit_commands(message: str) -> list[list[str]]:
    return [["git", "add", "--all"], ["git", "commit", "-m", redact(message)]]


def push_command(remote: str, branch: str) -> list[str]:
    ref = f"refs/heads/{redact(branch)}"
    return ["git", "push", "--no-follow-tags", remote, f"{ref}:{ref}"]


class LiveWorkspace:
    def __init__(self, root: Path, repo: str = ""):
        self.root = root
        self.repo = repo
        self._urls: list[str] = []
        self._identity: dict[str, str] | None = None

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

    def inspect(self) -> WorktreeState:
        head = os.fsdecode(self._git("rev-parse", "HEAD")).strip()
        if commit_sha(head) is None:
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = os.fsdecode(self._git("branch", "--show-current")).strip()
        return WorktreeState(head, not status, branch or None)

    def has_tracked_file(self, base: str, path: str) -> bool:
        if not path or "\\" in path or "\0" in path or any(part in {"", ".", ".."} for part in path.split("/")):
            return False
        entry = self._git("--no-replace-objects", "--literal-pathspecs", "ls-tree", "--full-tree", "-z", base, "--", path)
        return entry.startswith((b"100644 blob ", b"100755 blob ")) and entry.endswith(b"\t" + os.fsencode(path) + b"\0")

    def contains_commit(self, base: str, commit: str) -> bool:
        if commit_sha(commit) is None:
            return False
        kind = self._git("--no-replace-objects", "cat-file", "--batch-check=%(objecttype)", input_paths=os.fsencode(commit + "\n"))
        return kind == b"commit\n" and self._git(
            "--no-replace-objects", "merge-base", base, commit, allowed_returncodes=(0, 1),
        ).strip() == os.fsencode(commit.lower())

    def validate_origin(self, repo: str) -> None:
        self._urls = self._validate_remote(repo)

    def pin_base(self, base: str) -> str:
        base_ref = f"refs/remotes/origin/{base}"
        self._git(
            "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin",
            f"+refs/heads/{base}:{base_ref}",
        )
        return os.fsdecode(self._git("rev-parse", "--verify", f"{base_ref}^{{commit}}")).strip()

    def create_branch(self, branch: str, base: str, *, pinned: str = "") -> None:
        branch = redact(branch)
        refs = self._git("for-each-ref", "--format=%(refname)", f"refs/heads/{branch}")
        exists = f"refs/heads/{branch}" in os.fsdecode(refs).splitlines()
        if exists and os.fsdecode(self._git("branch", "--show-current")).strip() == branch:
            return
        head = pinned or self.pin_base(base)
        if exists:
            existing = os.fsdecode(self._git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")).strip()
            if existing != head:
                raise WorktreeInspectionFailed(
                    f"Branch {branch} at {existing} differs from origin/{base} at {head}. Checkout preserved. "
                    f"Switch to {branch} to reuse it, or delete that branch before rerunning."
                )
            self._git("switch", branch)
        else:
            self._git(*branch_command(branch, head)[1:])

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

    def _bot_identity(self) -> dict[str, str]:
        from foreman.github import ForgeError, LiveForge

        if self._identity is None:
            try:
                self._identity = LiveForge().bot_identity(self.repo)
            except ForgeError as exc:
                raise WorktreeInspectionFailed(str(exc)) from None
        return self._identity

    def committed(self, parent: str, message: str) -> bool:
        if credentials.configured:
            identity = self._bot_identity()
            actual = os.fsdecode(self._git("show", "-s", "--format=%an%n%ae%n%cn%n%ce", "HEAD")).splitlines()
            if actual != [identity[f"GIT_{role}_{field}"] for role in ("AUTHOR", "COMMITTER") for field in ("NAME", "EMAIL")]:
                return False
        return (os.fsdecode(self._git("show", "-s", "--format=%P%n%B", "HEAD")).strip()
                == f"{parent}\n{redact(message)}".strip())

    def tree(self, head: str | None) -> str:
        """Resolve a revision, or snapshot the next commit tree through a private index."""
        if head is None:
            try:
                with TemporaryDirectory(prefix="foreman-candidate-") as directory:
                    index = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
                    source = Path(os.fsdecode(self._git("rev-parse", "--path-format=absolute", "--git-path", "index")).removesuffix("\n"))
                    if source.exists():
                        shutil.copyfile(source, index["GIT_INDEX_FILE"])
                    else:
                        self._git("read-tree", "--empty", environment=index)
                    self._git("add", "--all", environment=index)
                    return os.fsdecode(self._git("write-tree", environment=index)).strip()
            except OSError as exc:
                raise WorktreeInspectionFailed(f"Could not snapshot the candidate: {exc}.") from exc
        return os.fsdecode(self._git("--no-replace-objects", "rev-parse", f"{head}^{{tree}}")).strip()

    def _base_merge_base(self, base_branch: str, head: str) -> str:
        base_ref = f"refs/remotes/origin/{base_branch}"
        self._git(
            "fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin",
            f"+refs/heads/{base_branch}:{base_ref}",
        )
        ancestor = os.fsdecode(self._git(
            "--no-replace-objects", "merge-base", base_ref, head, allowed_returncodes=(0, 1),
        )).strip()
        if not ancestor:
            raise WorktreeInspectionFailed(
                f"Cannot establish ancestry with origin/{base_branch}. Checkout preserved. "
                "Fetch the needed history (git fetch --unshallow origin for a shallow clone), "
                "or restore related base history, then resume."
            )
        return ancestor

    def reconcile(self, parent: str, branch: str, base_branch: str) -> ImplementationCandidate | None:
        observed = self.inspect()
        recovery = (f" Checkout preserved. Restore branch {branch} at {parent}, keep the intended edits, then rerun. "
                    "If history is shallow, fetch the needed history with git fetch --unshallow origin before resuming.")
        if observed.branch != branch:
            raise ImplementationRejected(f"HEAD {observed.head} is not attached to delivery branch {branch}." + recovery)
        merge_base = self._base_merge_base(base_branch, parent)
        paths = set(self._inspection_git(
            merge_base, "diff", "--name-only", "--no-renames", "--diff-filter=d", "-z", parent, "--",
        ).split(b"\0"))
        status = iter(self._inspection_git(
            merge_base, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        ).split(b"\0"))
        for entry in status:
            if entry:
                if b"D" not in entry[:2]:
                    paths.add(entry[3:])
                if b"R" in entry[:2] or b"C" in entry[:2]:
                    next(status)
        paths.discard(b"")
        if rejected := self._ignored_at(merge_base, paths):
            names = ", ".join(repr(os.fsdecode(path)) for path in sorted(rejected))
            raise ImplementationRejected(f"Disposable paths rejected: {names}. Remove them, keep intended edits, then rerun. Checkout preserved.")
        if observed.head == parent:
            return None if observed.clean else {"disposition": "edits", "fingerprint": self.fingerprint(),
                "size": self.pull_request_size(None, base_branch, merge_base=merge_base)}
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
                "fingerprint": hashlib.sha256(diff).hexdigest(),
                "size": self.pull_request_size(tree, base_branch, merge_base=merge_base)}

    def soft_reset(self, parent: str) -> None:
        self._git("reset", "--soft", parent)

    def _validate_remote(self, repo: str) -> list[str]:
        try:
            return validate_remote(repo, self.root, app=credentials.configured)
        except CredentialFailure as exc:
            raise WorktreeInspectionFailed(str(exc)) from None
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise WorktreeInspectionFailed("Cannot inspect origin transport. Repair the repository and resume.") from None

    def push(self, branch: str, repo: str) -> None:
        if credentials.configured:
            if repo != self.repo:
                raise WorkspaceError("Push repository differs from the authorized run repository.")
        else:
            self._validate_remote(repo)
        ref = f"refs/heads/{redact(branch)}"
        observed = self._git("ls-remote", "origin", ref).split()
        head = self._git("rev-parse", ref).strip()
        if not observed or observed[0] != head:
            self._git(*push_command("origin", branch)[1:])

    def pull_request_size(self, head: str | None, base_branch: str, *, merge_base: str | None = None) -> PullRequestSize:
        merge_base = merge_base or self._base_merge_base(base_branch, head or "HEAD")
        head = self.tree(None) if head is None else head
        diff_options = (
            "--find-renames", "--text",
            "--no-ext-diff", "--no-textconv",
        )
        raw = self._inspection_git(
            merge_base, "diff", *diff_options, "--numstat", "-z", merge_base, head, "--",
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
        attrs = self._inspection_git(
            merge_base, "check-attr", "--stdin", "-z", "linguist-generated",
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
                patch = self._inspection_git(
                    merge_base, "diff", *diff_options, "--patch", "--unified=0", "--inter-hunk-context=0",
                    *(("--no-renames",) if crosses_exclusion else ()),
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

    def _fetch_review(self, base: str, head: str) -> None:
        if not (FULL_SHA.fullmatch(base) and FULL_SHA.fullmatch(head)):
            raise WorktreeInspectionFailed("Review requires full base and head SHAs.")
        self._git("fetch", "--no-tags", "origin", base, head, timeout=30)

    def review_diff(self, base: str, head: str) -> ReviewDiff:
        self._fetch_review(base, head)
        args = (
            "--no-replace-objects", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-relative",
            "--src-prefix=a/", "--dst-prefix=b/", "--find-renames", f"{base}...{head}", "--",
        )
        command = shlex.join(("git", *args))
        merge_base = os.fsdecode(self._git("--no-replace-objects", "merge-base", base, head)).strip()
        if not FULL_SHA.fullmatch(merge_base):
            raise WorktreeInspectionFailed("Git did not report a full merge-base SHA.")
        command = f"# base = PR merge-base {merge_base}\n# head = current verified head {head}\n# Full PR diff: old = merge-base (base); new = head\n{command}"
        return ReviewDiff(command, os.fsdecode(self._git(*args)), merge_base)

    def review_scope(self, base: str, head: str) -> ReviewScope:
        pr = self.review_diff(base, head)
        try:
            inline = added_lines(pr.patch)
        except UnidiffParseError:
            inline = frozenset[tuple[str, int]]()
        commands = [pr.command]
        merge_base = pr.merge_base
        assert merge_base is not None
        blobs: dict[str, list[str]] = {}
        for side, sha in (("base", merge_base), ("head", head)):
            args = ("--no-replace-objects", "ls-tree", "-rz", "--full-tree", sha, "--")
            commands.append(f"# {side} tree {sha}\n{shlex.join(('git', *args))}")
            for entry in self._git(*args).split(b"\0"):
                if not entry:
                    continue
                metadata, path = entry.split(b"\t", 1)
                _, kind, oid = metadata.split()
                if kind == b"blob":
                    blobs.setdefault(oid.decode("ascii"), []).append(f"{side} path {ascii(os.fsdecode(path))}")
        for oid, labels in blobs.items():
            # Object IDs avoid shell and permission-pattern syntax in repository paths.
            commands.append(f"# {'; '.join(labels)}\ngit --no-replace-objects cat-file blob {oid}")
        return ReviewScope("\n\n".join(commands), inline)

    def _ignored_at(self, commit: str, paths: set[bytes]) -> set[bytes]:
        names = self._inspection_git(commit, "ls-tree", "-rz", "--name-only", commit).split(b"\0")
        ignores = [os.fsdecode(name) for name in names if name and PurePosixPath(os.fsdecode(name)).name == ".gitignore"]
        if not paths or not ignores:
            return set()
        archive = self._inspection_git(commit, "archive", commit, "--", *ignores)
        with TemporaryDirectory(prefix="foreman-ignore-") as directory:
            policy = LiveWorkspace(Path(directory))
            policy._git("init", "--quiet", "--template=")
            with tarfile.open(fileobj=BytesIO(archive)) as tree:
                for entry in tree:
                    if entry.isfile():
                        content = tree.extractfile(entry)
                        assert content is not None
                        path = policy.root / entry.name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content.read())
            ignored = policy._git(
                "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", "-z", "--stdin",
                input_paths=b"\0".join(b"./" + path for path in sorted(paths)) + b"\0",
                allowed_returncodes=(0, 1),
            )
        return {path.removeprefix(b"./") for path in ignored.split(b"\0") if path}

    def review_context(self, base: str, head: str) -> ReviewContext:
        self._fetch_review(base, head)
        merge_base = os.fsdecode(self._git("--no-replace-objects", "merge-base", base, head)).strip()
        if not FULL_SHA.fullmatch(merge_base):
            raise WorktreeInspectionFailed("Git did not report a full merge-base SHA.")
        before, after = changed_paths(self._inspection_git(
            merge_base, "diff-tree", "-r", "-z", "--name-status", "-M", merge_base, head, "--",
        ))
        return ReviewContext(self._review_side(merge_base, before, merge_base), self._review_side(head, after, merge_base))

    def _review_side(self, sha: str, changed: frozenset[str], attribute_source: str) -> ReviewSide:
        files = frozenset(os.fsdecode(path) for path in self._inspection_git(
            attribute_source, "ls-tree", "-rz", "--name-only", "--full-tree", sha, "--",
        ).split(b"\0") if path)
        raw = self._inspection_git(
            attribute_source, "grep", "-c", "-I", "-z", "--no-textconv", "-e", "^", sha, "--",
            allowed_returncodes=(0, 1),
        )
        return ReviewSide(sha, files, line_counts(raw, sha), changed)

    def _inspection_git(self, attribute_source: str, command: str, *args: str, input_paths: bytes | None = None,
                        allowed_returncodes: tuple[int, ...] = (0,)) -> bytes:
        attributes = Path(os.fsdecode(self._git(
            "rev-parse", "--path-format=absolute", "--git-path", "info/attributes",
        )).removesuffix("\n"))
        try:
            if attributes.stat().st_size:
                raise WorktreeInspectionFailed(f"{attributes} must be empty to inspect a pull request.")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorktreeInspectionFailed(f"Could not inspect {attributes}: {exc}") from exc
        pattern_options = ("-G", "--no-color", "--no-recurse-submodules") if command == "grep" else ()
        return self._git(
            "--no-replace-objects", "--literal-pathspecs", "-c", "core.attributesFile=/dev/null",
            f"--attr-source={attribute_source}", command, *pattern_options, *args, input_paths=input_paths,
            allowed_returncodes=allowed_returncodes,
        )

    def _git(self, *args: str, input_paths: bytes | None = None, timeout: float | None = None,
             allowed_returncodes: tuple[int, ...] = (0,), environment: dict[str, str] | None = None) -> bytes:
        operation = f"git {args[0]}"
        if timeout is None:
            timeout = 30 if args[0] in {"fetch", "push", "ls-remote", "commit"} else 10
        env = {**os.environ, "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        if args[0] == "commit" and credentials.configured:
            env.update(self._bot_identity())
        env.update(environment or {})
        auth: tuple[str, ...] = ()
        if args[0] in {"fetch", "push", "ls-remote"}:
            if credentials.configured and not self._urls:
                self._urls = self._validate_remote(self.repo)
            try:
                app_env = credentials.environment(self.repo)
            except CredentialFailure as exc:
                raise WorktreeInspectionFailed(str(exc)) from None
            if app_env is not None:
                timeout = min(timeout, COMMAND_TIMEOUT)
                auth = git_auth(self._urls)
                env = app_env
        try:
            result = subprocess.run(
                ["git", *auth, *args], cwd=self.root, capture_output=True, input=input_paths,
                env=env,
                stdin=subprocess.DEVNULL if input_paths is None else None, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorktreeInspectionFailed(f"{operation} timed out after {timeout} seconds; rerun to resume.") from exc
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise WorktreeInspectionFailed(redact(f"{operation} failed ({exc}).", credentials_only=True)) from None
        if result.returncode not in allowed_returncodes:
            if auth:
                detail = os.fsdecode(result.stderr)
                status = re.search(r"(?:HTTP |error: )(\d{3})", detail)
                code = 401 if "Authentication failed" in detail else int(status[1]) if status else None
                if code == 401:
                    credentials.invalidate()
                if code is not None:
                    raise WorktreeInspectionFailed(failure(code, "rate limit" in detail.lower()))
            raise WorktreeInspectionFailed(
                redact(os.fsdecode(result.stderr).strip(), credentials_only=True) or f"{operation} exited with code {result.returncode}.",
            )
        return result.stdout
