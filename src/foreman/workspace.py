"""Local checkout operations used by delivery runs."""

from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import time
from typing import Annotated, Literal, NewType, Protocol, TextIO
from typing_extensions import TypedDict
from pydantic import BaseModel, BeforeValidator, ConfigDict, TypeAdapter, with_config
from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchSet

from foreman.check_output import CheckOutput
from foreman.container import (CheckLog as CheckLog, ContainerHost, ContainerSpec, ContainerUnavailable as ContainerUnavailable,
                               DockerHost, Mount, SETUP_SECONDS, SetupFailed as SetupFailed, TimedOut as TimedOut, ended as ended)
from foreman.contract import CONTRACT_PATH, Contract, parse_contract
from foreman.credentials import CredentialFailure, COMMAND_TIMEOUT, credentials, failure, git_auth, validate_remote
from foreman.redaction import redact, register_test_values
from foreman.review_context import ReviewContext, ReviewSide

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
HUNK = re.compile(rb"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", re.MULTILINE)
CommitSHA = NewType("CommitSHA", str)
# A command run in a container, allowed the seconds it is given.
RunIn = Callable[[str, float], int | TimedOut]


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


class UnrecognizedCheckout(WorkspaceError):
    """The checkout holds work Foreman does not recognize as its own, so it is preserved (dirty_worktree)."""


def dirty_report(checkout: Path, cause: str, paths: Collection[str]) -> str:
    """The dirty_worktree detail the status comment shows: what differs, the checkout's path and each path that differs."""
    named = f" Paths that differ: {', '.join(sorted(paths))}." if paths else ""
    return f"{cause} The checkout {checkout} is preserved.{named}"


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


class BaseMerge(BaseModel, frozen=True, extra="forbid"):
    """Merge confirmed a conflict with the base, so prepare merges the base head into the owned branch (merge B).

    Empty until the merge starts. Then it names the base head, which is the candidate's base, and the files Git could not merge."""

    head: str = ""
    unmerged: list[str] = []


class WorkState(BaseModel, frozen=True, extra="forbid"):
    branch: str = ""
    base: str = ""
    attempt_head: str = ""
    # The builder returned for this round. The fingerprint names the edits the attempt owns.
    built: bool = False
    owned: str = ""
    candidate: ImplementationCandidate | None = None
    head_sha: str | None = None
    published_head: str | None = None
    repair_head: str | None = None
    base_merge: BaseMerge | None = None

    def moved_to(self, head: str | None, *, conflict: bool = False) -> "WorkState":
        """The handoff to Implement for a repair: it fetches a moved PR head and rebuilds on it, or merges the base on a conflict."""
        return WorkState(branch=self.branch, base=self.base, repair_head=head, base_merge=BaseMerge() if conflict else None)

    def misplaced(self, root: Path, worktree: "WorktreeState") -> str | None:
        """The dirty_worktree report of a checkout that is not on the owned branch at the attempt's parent, or None when it is."""
        if (worktree.branch, worktree.head) == (self.branch, self.attempt_head):
            return None
        return dirty_report(root, f"The checkout is on {worktree.branch or 'no branch'} at {worktree.head}, not on the owned branch "
                            f"{self.branch} at the attempt's parent {self.attempt_head}. Put the checkout back on {self.branch} at "
                            f"{self.attempt_head}, keeping its files, then add Ready, which makes this test again before it calls any "
                            "worker.", ())


class RelatedCheckout(BaseModel, frozen=True, extra="forbid"):
    """A related repository the contract names, pinned at admission to its default-branch head and checked out read-only."""

    repository: str
    commit: str
    checkout: str


class WorktreeInspectionFailed(WorkspaceError):
    pass


class RelatedServiceFailed(ContainerUnavailable):
    """A related repository's start command that did not start its service in the container (item 4)."""

    def __init__(self, repository: str, detail: str):
        super().__init__(detail)
        self.repository = repository


@dataclass(frozen=True)
class WorktreeState:
    head: str
    clean: bool
    branch: str | None


@dataclass(frozen=True)
class ReviewDiff:
    command: str
    patch: str


ReviewLine = Annotated[tuple[str, int], BeforeValidator(TypeAdapter(tuple[object, ...]).validate_python)]


@with_config(ConfigDict(extra="forbid"))
class ReviewScopeRecord(TypedDict):
    command: str
    inline: list[ReviewLine] | None


@dataclass(frozen=True)
class ReviewScope:
    command: str
    # None when the review patch cannot be parsed, so no finding has an inline anchor.
    inline: frozenset[tuple[str, int]] | None

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(line.strip() for line in self.command.splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))

    def record(self) -> ReviewScopeRecord:
        return {"command": self.command, "inline": None if self.inline is None else sorted(self.inline)}


def added_lines(patch: str) -> frozenset[tuple[str, int]] | None:
    try:
        files = PatchSet(patch)
    except UnidiffParseError:
        return None
    return frozenset((file.path, line.target_line_no) for file in files if not file.is_removed_file
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


PathKind = Literal["absent", "file", "directory", "symlink", "submodule"]
TREE_MODES: dict[bytes, PathKind] = {b"100644": "file", b"100755": "file", b"040000": "directory", b"120000": "symlink",
                                     b"160000": "submodule"}


def repository_parts(path: str) -> list[str] | None:
    """A repository-relative path's components, or None for a path that is absolute, empty or has '.' or '..'."""
    parts = path.split("/")
    return None if "\\" in path or "\0" in path or any(part in {"", ".", ".."} for part in parts) else parts


class Workspace(Protocol):
    root: Path

    def prepare_container(self, *, base: str, log: CheckLog | None) -> None: ...
    def container_route(self, base: str) -> tuple[str, float]: ...
    def remove_container(self, *, unless: Callable[[], bool] = lambda: False) -> None: ...
    def remove_leftover_containers(self) -> None: ...
    def run(self, command: str, *, base: str, log: CheckLog | None) -> int | TimedOut: ...
    def demonstrate(self, command: str, files: Mapping[str, str], *, base: str, log: CheckLog | None) -> int | TimedOut: ...
    @property
    def demonstrations(self) -> Path | None: ...
    def run_at(self, commit: str, command: str, *, base: str, log: CheckLog | None) -> int | TimedOut: ...
    def container_at(self, commit: str, *, base: str, log: CheckLog | None) -> AbstractContextManager[RunIn]: ...
    def inspect(self) -> WorktreeState: ...
    def review_context(self, base: str, head: str) -> ReviewContext: ...
    def validate_origin(self, repo: str) -> None: ...
    def pin_base(self, base: str) -> str: ...
    def check_out_related(self, repo: str, branch: str) -> RelatedCheckout: ...
    def create_branch(self, branch: str, base: str, *, pinned: str = "") -> None: ...
    def refresh(self, branch: str, head: str) -> None: ...
    def base_head(self, base: str) -> str: ...
    def merge(self, head: str) -> list[str]: ...
    def abandon_merge(self, branch: str, parent: str, base_branch: str) -> None: ...
    def commit(self, message: str) -> None: ...
    def committed(self, parents: Sequence[str], message: str) -> bool: ...
    def reconcile(self, parent: str, branch: str, base_branch: str, *, base: str | None = None) -> ImplementationCandidate | None: ...
    def tree(self, head: str | None) -> str: ...
    def soft_reset(self, parent: str) -> None: ...
    def uncommit(self, parent: str) -> None: ...
    def fingerprint(self) -> str: ...
    def release(self, heads: Collection[str]) -> bool: ...
    def remote_head(self, branch: str) -> str | None: ...
    def push(self, branch: str, repo: str) -> None: ...
    def tracked_path(self, base: str, path: str) -> PathKind | None: ...
    def tracked_file(self, base: str, path: str) -> bytes | None: ...
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


def lock_file(*key: str) -> Path:
    """One lock file per key for this user, in the XDG runtime directory, which neither TMPDIR nor XDG_STATE_HOME moves."""
    locks = Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache") / "foreman-locks"
    locks.mkdir(parents=True, exist_ok=True)
    return locks / hashlib.sha256(json.dumps(list(key), separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# Git commands that write the shared clone's objects, refs or worktree list.
SHARED_WRITES = frozenset({"fetch", "push", "worktree", "init", "config"})
# Git's global options that take their value as the next argument, before the command.
VALUED_OPTIONS = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source"})
# Git commands that write a checkout's index, so each holds the index lock while it runs.
INDEX_WRITES = frozenset({"add", "checkout", "commit", "merge", "read-tree", "reset", "switch", "write-tree"})
# Git's network commands get 30 seconds, and no more than an App token's command limit.
NETWORK_TIMEOUTS: dict[str, float] = {"fetch": 30, "push": 30, "ls-remote": 30}
# Git's reads get 10 seconds. A merge, and merge-tree making the same merge, run with no limit, as the check at the base does.
GIT_TIMEOUTS: dict[str, float | None] = {"commit": 30, "merge": None, "merge-tree": None}
# The base merge, the proof that a checkout holds exactly that merge and its abort read none of the host's Git configuration,
# so each sees the same merge on every host. Rerere would rewrite hunks that merge-tree leaves conflicted.
MERGE_ENVIRONMENT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_COUNT": "2",
                     "GIT_CONFIG_KEY_0": "rerere.enabled", "GIT_CONFIG_VALUE_0": "false",
                     "GIT_CONFIG_KEY_1": "merge.conflictStyle", "GIT_CONFIG_VALUE_1": "merge"}


def subcommand(args: Sequence[str]) -> str:
    """The Git command args run, after Git's global options."""
    options = iter(args)
    for arg in options:
        if arg in VALUED_OPTIONS:
            next(options, None)
        elif not arg.startswith("-"):
            return arg
    return ""


class LiveWorkspace:
    def __init__(self, root: Path, repo: str = "", *, clone: Path | None = None, clones: Path | None = None,
                 related: Path | None = None, scratch: Path | None = None, containers: ContainerHost | None = None):
        self.root = root
        self.repo = repo
        self.clone = clone
        # A workspace that runs delivery names the shared clones, the run's own directory of related checkouts and the run's
        # scratch directory, which its attempt container mounts and where each worker call makes its attempt scratch.
        self.clones = clones
        self.related = related
        self.scratch = scratch
        self.containers: ContainerHost = containers or DockerHost()
        # The run's attempt container, built at its first use in this process.
        self._container: str | None = None
        self._urls: list[str] = []
        self._identity: dict[str, str] | None = None
        self._lock: TextIO | None = None

    @classmethod
    def managed(cls, state_home: Path, repo: str, issue: int) -> "LiveWorkspace":
        """The issue's own worktree of one shared clone per repository. Admission creates the worktree at the pinned base."""
        owner, name = repo.lower().split("/")
        home = (state_home / "foreman").resolve()
        runs = home / "runs" / owner / name
        return cls(runs / str(issue), repo, clone=home / "checkouts" / owner / name, clones=home / "checkouts",
                   related=runs / f"{issue}.related", scratch=runs / f"{issue}.scratch")

    @property
    def _home(self) -> Path:
        # Before admission and after release, a managed workspace reads its repository through the shared clone.
        return self.clone if self.clone is not None and not (self.root / ".git").exists() else self.root

    @contextmanager
    def _shared(self) -> Generator[None]:
        """Hold the repository lock while writing the shared clone. It is short, blocking and never passed to workers."""
        if self.clone is None or self._lock is not None:
            yield
            return
        with lock_file("repo", self.repo.lower()).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._lock = lock
            try:
                yield
            finally:
                self._lock = None

    @property
    def _label(self) -> str:
        # The run's containers carry its checkout's path, which one issue's run holds at a time under the issue lock.
        return str(self.root.resolve())

    def _contract(self, base: str) -> Contract:
        """The contract at the run's pinned base, which admission validated."""
        text = self.tracked_file(base, CONTRACT_PATH)
        if text is None:
            raise WorktreeInspectionFailed(f"The pinned base {base} tracks no {CONTRACT_PATH}.")
        return parse_contract(text)

    def _build(self, checkout: Path, contract: Contract, log: CheckLog | None) -> str:
        """A fresh container for the checkout, from the contract's image, with each setup command run in order in the checkout.
        It has thirty minutes to be ready, from the start of the image pull to the end of the last setup command."""
        deadline = time.monotonic() + SETUP_SECONDS
        values = {name: os.environ.get(f"FOREMAN_TEST_{name}", "") for name in contract.secrets}
        if missing := [f"FOREMAN_TEST_{name}" for name, value in values.items() if not value.strip()]:
            raise SetupFailed(f"{', '.join(missing)} is not set on the Foreman host. Set each to the secret's test value "
                                       "in the environment Foreman runs in, then add Ready.")
        # Registered before anything runs in the container, so no text Foreman writes from now on holds a value.
        register_test_values(values.values())
        # Each related repository's checkout, pinned at admission, is mounted read-only with its Git directory (item 4).
        related = [(item.repository, self._related_root(item.repository), item.start) for item in contract.related_repositories]
        mounts = (Mount(checkout), Mount(self._common_dir(checkout), read_only=True), *((Mount(self.scratch),) if self.scratch else ()),
                  *(mount for _, root, _ in related for mount in (Mount(root, read_only=True), Mount(self._common_dir(root), read_only=True))))
        if self.scratch:
            self.scratch.mkdir(parents=True, exist_ok=True)
        name = self.containers.create(ContainerSpec(self._label, contract.image, checkout, mounts, values), deadline - time.monotonic())
        try:
            # The container host shares only some host paths, such as the home directory under colima. Docker refuses a path
            # it cannot see, or mounts it empty.
            if self.containers.run(name, "test -e .git", cwd=checkout, log=None, timeout=max(1.0, deadline - time.monotonic())) != 0:
                raise ContainerUnavailable(f"The checkout {checkout} is empty inside the container: its .git is not there. "
                                           "Keep Foreman's state under a path the container host shares.")
            for command in contract.setup:
                output = CheckOutput(log)
                result = self.containers.run(name, command, cwd=checkout, log=output, timeout=max(0.0, deadline - time.monotonic()))
                if isinstance(result, TimedOut):
                    raise SetupFailed(f"Setup command {command!r} was still running when the thirty minutes a container "
                                               f"has to be ready ran out.\n\n{output.text}")
                if result:
                    raise SetupFailed(f"Setup command {command!r} exited with {result}.\n\n{output.text}")
            if contract.network == "setup":
                self.containers.isolate(name)
            # Each related service starts from its checkout and keeps running for the workers that use it.
            for repository, root, start in related:
                launched = self.containers.run(name, f"({start}) >/dev/null 2>&1 &", cwd=root, log=None, timeout=max(1.0, deadline - time.monotonic()))
                if launched != 0:
                    raise RelatedServiceFailed(repository, f"The start command {start!r} of the related repository {repository} {ended(launched)} "
                                                           f"in its checkout {root}")
        except BaseException:
            self.containers.stop([name])
            raise
        return name

    def _common_dir(self, checkout: Path) -> Path:
        return Path(os.fsdecode(self._git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout)).removesuffix("\n"))

    def _related_root(self, repo: str) -> Path:
        """Where the run checks out a related repository, in the run's related directory."""
        assert self.related is not None
        owner, name = repo.lower().split("/")
        return self.related / owner / name

    def prepare_container(self, *, base: str, log: CheckLog | None) -> None:
        """Build the run's container when this process has none, or the one it had is gone, first removing any container a
        cut-off process left with the run's label."""
        if self._container is not None and self.containers.running(self._container):
            return
        self._container = None
        self.remove_leftover_containers()
        self._container = self._build(self.root, self._contract(base), log)

    def container_route(self, base: str) -> tuple[str, float]:
        """The run's container, built when this process has none, and the seconds each command in it is allowed."""
        self.prepare_container(base=base, log=None)
        assert self._container is not None
        return self._container, self._contract(base).check_timeout_seconds

    def remove_container(self, *, unless: Callable[[], bool] = lambda: False) -> None:
        """Remove the run's container, when this process built one, unless the condition holds then."""
        if self._container is not None and not unless():
            self.containers.stop([self._container])
            self._container = None

    def remove_leftover_containers(self) -> None:
        """Remove every container with the run's label, which only a cut-off process can have left."""
        self._container = None
        self.containers.stop(self.containers.leftovers(self._label))

    def _exec(self, name: str, checkout: Path, command: str, seconds: float, log: CheckLog | None) -> int | TimedOut:
        result = self.containers.run(name, command, cwd=checkout, log=log, timeout=seconds)
        if result != 0 and not isinstance(result, TimedOut) and not self.containers.running(name):
            raise ContainerUnavailable(f"The container that ran {command!r} is gone, so its exit is no check result. "
                                       "Add Ready to build a new one.")
        return result

    def run(self, command: str, *, base: str, log: CheckLog | None) -> int | TimedOut:
        """Run the command in the run's container, allowed the contract's check_timeout_seconds. A command ended at its limit
        does not end what it started, so its container is removed and the next use builds a new one."""
        self.prepare_container(base=base, log=log)
        assert self._container is not None
        result = self._exec(self._container, self.root, command, self._contract(base).check_timeout_seconds, log)
        if isinstance(result, TimedOut):
            self.remove_container()
        return result

    @property
    def demonstrations(self) -> Path | None:
        """Where a review demonstration's test files land: the run's scratch, which its container mounts, outside the checkout."""
        return self.scratch / "demonstrations" if self.scratch else None

    def demonstrate(self, command: str, files: Mapping[str, str], *, base: str, log: CheckLog | None) -> int | TimedOut:
        """Write a review demonstration's test files under the demonstration directory, so nothing commits them, then run its
        command in the run's container at the checked-out head."""
        if files and self.demonstrations is None:
            raise ContainerUnavailable("The run has no scratch directory for a demonstration's test file.")
        for path, content in files.items():
            assert self.demonstrations is not None
            target = self.demonstrations / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return self.run(command, base=base, log=log)

    @contextmanager
    def container_at(self, commit: str, *, base: str, log: CheckLog | None) -> Generator[RunIn]:
        """A separate detached checkout of the commit, in a container of its own built as the run's is, removed with it when
        the block ends. Each command runs in that checkout, allowed the seconds it is given. The checkout sits beside the
        run's, outside Git's directory, which containers mount read-only."""
        contract = self._contract(base)
        with TemporaryDirectory(prefix=f"{self.root.name}.base-", dir=self.root.parent) as directory:
            checkout = Path(directory) / "checkout"
            self._git("worktree", "add", "--quiet", "--detach", str(checkout), commit)
            try:
                name = self._build(checkout, contract, log)
                try:
                    yield lambda command, seconds: self._exec(name, checkout, command, seconds, log)
                finally:
                    self.containers.stop([name])
            finally:
                self._git("worktree", "remove", "--force", str(checkout))

    def run_at(self, commit: str, command: str, *, base: str, log: CheckLog | None) -> int | TimedOut:
        """Run the command in a container of its own at the commit, allowed the contract's check_timeout_seconds."""
        with self.container_at(commit, base=base, log=log) as run:
            return run(command, self._contract(base).check_timeout_seconds)

    def inspect(self) -> WorktreeState:
        head = os.fsdecode(self._git("rev-parse", "HEAD")).strip()
        if commit_sha(head) is None:
            raise WorktreeInspectionFailed("Git did not report a full 40-hex HEAD SHA.")
        status = self._git("status", "--porcelain")
        branch = os.fsdecode(self._git("branch", "--show-current")).strip()
        return WorktreeState(head, not status, branch or None)

    def tracked_path(self, base: str, path: str) -> PathKind | None:
        """What the commit tracks at the path, or None when repository_parts refuses it. A path under a tracked symlink or
        submodule is that symlink or submodule, since the path resolves through it."""
        if (parts := repository_parts(path)) is None:
            return None
        prefixes = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
        listing = self._git("--no-replace-objects", "--literal-pathspecs", "ls-tree", "-t", "--full-tree", "-z", base, "--", *prefixes)
        modes = {os.fsdecode(name): meta.split(b" ", 1)[0] for meta, _, name in (entry.partition(b"\t") for entry in listing.split(b"\0") if entry)}
        for prefix in prefixes[:-1]:
            if (kind := TREE_MODES.get(modes.get(prefix, b""))) in ("symlink", "submodule"):
                return kind
            if kind != "directory":
                return "absent"
        return TREE_MODES[modes[path]] if path in modes else "absent"

    def tracked_file(self, base: str, path: str) -> bytes | None:
        """The content of a Git-tracked regular file at the commit, or None when the commit tracks no such file."""
        if self.tracked_path(base, path) != "file":
            return None
        return self._git("--no-replace-objects", "cat-file", "blob", f"{base}:{path}")

    def contains_commit(self, base: str, commit: str) -> bool:
        if commit_sha(commit) is None:
            return False
        kind = self._git("--no-replace-objects", "cat-file", "--batch-check=%(objecttype)", input_paths=os.fsencode(commit + "\n"))
        return kind == b"commit\n" and self._git(
            "--no-replace-objects", "merge-base", base, commit, allowed_returncodes=(0, 1),
        ).strip() == os.fsencode(commit.lower())

    def validate_origin(self, repo: str) -> None:
        if self.clone is not None:
            # Foreman owns the shared clone. Init and configuration are idempotent, so an interrupted creation completes.
            with self._shared():
                self.clone.mkdir(parents=True, exist_ok=True)
                self._git("init", "--quiet", "--template=", cwd=self.clone)
                self._git("config", "remote.origin.url", f"https://github.com/{repo}", cwd=self.clone)
                self._git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=self.clone)
        self._urls = self._validate_remote(repo)

    def base_head(self, base: str) -> str:
        """Fetch the base branch and return its head."""
        base_ref = f"refs/remotes/origin/{base}"
        self._git("fetch", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin", f"+refs/heads/{base}:{base_ref}", cwd=self._home)
        return os.fsdecode(self._git("rev-parse", "--verify", f"{base_ref}^{{commit}}", cwd=self._home)).strip()

    def pin_base(self, base: str) -> str:
        with self._shared():
            pinned = self.base_head(base)
            if self.clone is not None:
                # Only admission pins a base, so this allocates the issue's checkout exactly once.
                listed = self._git("worktree", "list", "--porcelain", "-z", cwd=self.clone).split(b"\0")
                if self.root.exists() and os.fsencode(f"worktree {self.root}") in listed:
                    # A crash before the admission checkpoint left the worktree. Nothing has edited it yet.
                    self._git("checkout", "--quiet", "--detach", pinned)
                else:
                    # Git adds into a missing or empty directory and refuses anything else, which is preserved.
                    self._git("worktree", "prune", cwd=self.clone)
                    self._git("worktree", "add", "--quiet", "--detach", str(self.root), pinned, cwd=self.clone)
        return pinned

    def check_out_related(self, repo: str, branch: str) -> RelatedCheckout:
        """Pin a related repository at its branch head in a worktree of that repository's shared clone, in the run's related directory."""
        assert self.clones is not None
        owner, name = repo.lower().split("/")
        related = LiveWorkspace(self._related_root(repo), repo, clone=self.clones / owner / name)
        related.validate_origin(repo)
        return RelatedCheckout(repository=repo, commit=related.pin_base(branch), checkout=str(related.root))

    def release(self, heads: Collection[str]) -> bool:
        """Remove a finished run's managed worktree, with its scratch directory, when it is clean at a head the run recorded."""
        if self.clone is None or not self.root.exists():
            return False
        observed = self.inspect()
        if not observed.clean or observed.head not in heads:
            return False
        with self._shared():
            self._git("worktree", "remove", str(self.root), cwd=self.clone)
        if self.scratch is not None and self.scratch.exists():
            shutil.rmtree(self.scratch)
        return True

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

    def merge(self, head: str) -> list[str]:
        """Start merging head into the branch without committing, and name the files Git could not merge.

        Foreman's commit completes the merge, so the builder never writes Git's own files."""
        self._git("merge", "--no-commit", "--no-ff", "--no-edit", "--quiet", head, allowed_returncodes=(0, 1),
                  environment=MERGE_ENVIRONMENT)
        if self._merging() != head:
            raise WorktreeInspectionFailed(f"Git could not start merging the base head {head}. Checkout preserved.")
        return sorted(os.fsdecode(name) for name in self._git("diff-files", "--name-only", "--diff-filter=U", "-z").split(b"\0") if name)

    def _merging(self) -> str:
        return os.fsdecode(self._git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", allowed_returncodes=(0, 1))).strip()

    def abandon_merge(self, branch: str, parent: str, base_branch: str) -> None:
        """Abandon the unfinished base merge a prepare that stopped inside it left, which saved nothing of it.

        Foreman recognizes its own merge only when the checkout is on branch at parent, merging a commit this clone fetched
        as the base branch's head, and holds exactly what that merge produces, so the abort throws away no one's edit. Until
        the abort it only reads. Any other change raises UnrecognizedCheckout."""
        def read(*args: str, index: dict[str, str] | None = None) -> set[bytes]:
            return {name for name in self._git(*args, environment={**MERGE_ENVIRONMENT, **(index or {})}).split(b"\0") if name}

        def unrecognized(cause: str, paths: Collection[bytes]) -> UnrecognizedCheckout:
            return UnrecognizedCheckout(dirty_report(self.root, cause, {os.fsdecode(path) for path in paths}))

        changed = {entry[3:] for entry in read("status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")}
        merging = self._merging()
        if not merging:
            if changed:
                raise unrecognized("No merge is under way, and the checkout has changes.", changed)
            return
        head, on = (os.fsdecode(self._git(*args)).strip() for args in (("rev-parse", "HEAD"), ("branch", "--show-current")))
        if (on, head) != (branch, parent):
            raise unrecognized(f"A merge is under way on {on or 'a detached HEAD'} at {head}, not on {branch} at {parent}.", changed)
        tracking = f"refs/remotes/origin/{base_branch}"
        known = self._git("rev-parse", "--verify", "--quiet", tracking, allowed_returncodes=(0, 1), cwd=self._home)
        fetched = os.fsdecode(self._git("reflog", "show", "--format=%H", tracking, "--", cwd=self._home)).split() if known else []
        if merging not in fetched:
            raise unrecognized(f"A merge of {merging} is under way, which this clone never fetched as the head of {base_branch}.", changed)
        # Merge-tree makes the same merge without a checkout. HEAD is named as merge named it, so the conflict labels match.
        tree, *conflicts = self._git("merge-tree", "--write-tree", "-z", "HEAD", merging, allowed_returncodes=(0, 1),
                                     environment=MERGE_ENVIRONMENT).split(b"\0\0", 1)[0].split(b"\0")
        produced = {entry for entry in conflicts if entry}
        conflicted = {entry.split(b"\t", 1)[1] for entry in produced}
        differs = {entry.split(b"\t", 1)[1] for entry in produced ^ read("ls-files", "-u", "-z")}
        with self._private_index() as index:
            differs |= read("diff", "--name-only", "--no-renames", "-z", os.fsdecode(tree), index=index)
        differs |= read("diff", "--cached", "--name-only", "--no-renames", "-z", os.fsdecode(tree)) - conflicted
        differs |= read("ls-files", "--others", "--exclude-standard", "-z")
        if differs:
            raise unrecognized(f"The checkout differs from Foreman's unfinished merge of {merging}.", differs)
        try:
            self._git("merge", "--abort", environment=MERGE_ENVIRONMENT)
        except WorkspaceError as exc:
            raise WorktreeInspectionFailed(f"Git could not abandon Foreman's unfinished merge of {merging} in {self.root}: {exc} "
                                           "The checkout is preserved.") from None

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self._git("diff-index", "--patch", "--find-renames", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"))
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

    def committed(self, parents: Sequence[str], message: str) -> bool:
        if credentials.configured:
            identity = self._bot_identity()
            actual = os.fsdecode(self._git("show", "-s", "--format=%an%n%ae%n%cn%n%ce", "HEAD")).splitlines()
            if actual != [identity[f"GIT_{role}_{field}"] for role in ("AUTHOR", "COMMITTER") for field in ("NAME", "EMAIL")]:
                return False
        return (os.fsdecode(self._git("show", "-s", "--format=%P%n%B", "HEAD")).strip()
                == f"{' '.join(parents)}\n{redact(message)}".strip())

    def tree(self, head: str | None) -> str:
        """Resolve a revision, or snapshot the next commit tree through a private index."""
        if head is None:
            try:
                with self._private_index() as index:
                    self._git("add", "--all", environment=index)
                    return os.fsdecode(self._git("write-tree", environment=index)).strip()
            except OSError as exc:
                raise WorktreeInspectionFailed(f"Could not snapshot the candidate: {exc}.") from exc
        return os.fsdecode(self._git("--no-replace-objects", "rev-parse", f"{head}^{{tree}}")).strip()

    @contextmanager
    def _private_index(self) -> Generator[dict[str, str], None, None]:
        """A copy of the checkout's index for Git to refresh or write, so the checkout's index lock is never taken."""
        with TemporaryDirectory(prefix="foreman-index-") as directory:
            index = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            source = Path(os.fsdecode(self._git("rev-parse", "--path-format=absolute", "--git-path", "index")).removesuffix("\n"))
            if source.exists():
                # The copy keeps the index's time, by which Git tells which entries it must compare by content.
                shutil.copy2(source, index["GIT_INDEX_FILE"])
            else:
                self._git("read-tree", "--empty", environment=index)
            yield index

    def _base_merge_base(self, base_branch: str, head: str) -> str:
        ancestor = os.fsdecode(self._git(
            "--no-replace-objects", "merge-base", self.base_head(base_branch), head, allowed_returncodes=(0, 1),
        )).strip()
        if not ancestor:
            raise WorktreeInspectionFailed(
                f"Cannot establish ancestry with origin/{base_branch}. Checkout preserved. "
                "Fetch the needed history (git fetch --unshallow origin for a shallow clone), "
                "or restore related base history, then resume."
            )
        return ancestor

    def reconcile(self, parent: str, branch: str, base_branch: str, *, base: str | None = None) -> ImplementationCandidate | None:
        """The candidate the builder left on parent, compared with the candidate's base.

        After a base merge the candidate's base is the merged base head, and the history is Foreman's, so no worker commit is
        adopted: commit stops if the head moved under it."""
        observed = self.inspect()
        recovery = (f" Checkout preserved. Restore branch {branch} at {parent}, keep the intended edits, then rerun. "
                    "If history is shallow, fetch the needed history with git fetch --unshallow origin before resuming.")
        if observed.branch != branch:
            raise ImplementationRejected(f"HEAD {observed.head} is not attached to delivery branch {branch}." + recovery)
        merge_base = base or self._base_merge_base(base_branch, parent)
        with self._private_index() as index:
            paths = set(self._inspection_git(
                merge_base, "diff", "--name-only", "--no-renames", "--diff-filter=d", "-z", parent, "--", environment=index,
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
        if observed.head == parent or base:
            return None if observed.clean and observed.head == parent else {"disposition": "edits", "fingerprint": self.fingerprint(),
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
        diff = self._git("diff-tree", "--patch", "--find-renames", "--binary", "--no-ext-diff", "--no-textconv", parent, observed.head)
        return {"disposition": "adopted", "head": observed.head, "tree": tree,
                "fingerprint": hashlib.sha256(diff).hexdigest(),
                "size": self.pull_request_size(tree, base_branch, merge_base=merge_base)}

    def soft_reset(self, parent: str) -> None:
        self._git("reset", "--soft", parent)

    def uncommit(self, parent: str) -> None:
        """Move the clean branch back to parent, dropping unpublished commits."""
        self._git("reset", "--keep", parent)

    def _validate_remote(self, repo: str) -> list[str]:
        try:
            return validate_remote(repo, self._home, app=credentials.configured)
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
        self._git(*push_command("origin", branch)[1:])

    def remote_head(self, branch: str) -> str | None:
        observed = self._git("ls-remote", "origin", f"refs/heads/{redact(branch)}").split()
        return os.fsdecode(observed[0]) if observed else None

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
        return ReviewDiff(command, os.fsdecode(self._git(*args)))

    def review_scope(self, base: str, head: str) -> ReviewScope:
        pr = self.review_diff(base, head)
        return ReviewScope(pr.command, added_lines(pr.patch))

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
                        allowed_returncodes: tuple[int, ...] = (0,), environment: dict[str, str] | None = None) -> bytes:
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
            allowed_returncodes=allowed_returncodes, environment=environment,
        )

    def _git(self, *args: str, input_paths: bytes | None = None, timeout: float | None = None,
             allowed_returncodes: tuple[int, ...] = (0,), environment: dict[str, str] | None = None,
             cwd: Path | None = None) -> bytes:
        command = subcommand(args)
        operation = f"git {command}"
        # Optional locks off, so status refreshes the index only in memory and never takes the index lock.
        env = {**os.environ, "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        if command == "commit" and credentials.configured:
            env.update(self._bot_identity())
        env.update(environment or {})
        auth: tuple[str, ...] = ()
        if command in NETWORK_TIMEOUTS:
            timeout = NETWORK_TIMEOUTS[command] if timeout is None else timeout
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
        elif timeout is None:
            timeout = GIT_TIMEOUTS.get(command, 10)
        started = time.time_ns()
        try:
            with self._shared() if command in SHARED_WRITES else nullcontext():
                result = subprocess.run(
                    ["git", *auth, *args], cwd=cwd or self.root, capture_output=True, input=input_paths,
                    env=env,
                    stdin=subprocess.DEVNULL if input_paths is None else None, timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            if command in INDEX_WRITES:
                # Run killed the command and waited for it to end, so an index lock made since it started is one it left.
                self._release_index_lock(cwd or self.root, env, started)
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

    @staticmethod
    def _release_index_lock(cwd: Path, env: dict[str, str], since: int) -> None:
        try:
            index = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-path", "index"], cwd=cwd, env=env,
                                   capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
            lock = Path(os.fsdecode(index.stdout).removesuffix("\n") + ".lock")
            if not index.returncode and lock.stat().st_mtime_ns >= since:
                lock.unlink()
        except (OSError, subprocess.SubprocessError):
            # Without the lock's path or its file there is nothing Foreman knows it left.
            pass


def related_path(related: RelatedCheckout, path: str) -> PathKind | None:
    """What a related repository checkout tracks at the path at its pinned commit (rules.paths), as tracked_path reads it."""
    return LiveWorkspace(Path(related.checkout), related.repository).tracked_path(related.commit, path)
