"""Live runner behavior with offline workers and forge doubles."""

import tests
from foreman.stages import STAGES

from collections.abc import Callable
import contextlib
import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from inspect import signature
import io
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import shlex
import shutil
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.pregel import Pregel

from publication_fixture import BRANCH, EMPTY_SLICE, SLICE, SUBSCRIBED, VERIFY_SKILL_PATH, contract_text, slice_body
from test_dry_run import transcript
from foreman.readiness import MergeState
from foreman.adapters import Outcome, Usage
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.credentials import command_environment
from foreman.cli import _print_run, _write_check_log, main, run_exit_code
from foreman.contract import CONTRACT_PATH
from foreman.config import ConfigError, ModelSpec, NodeConfig, load_config
from foreman.outcomes import Halt
from foreman.delivery import compile_delivery, LiveRun, halted, run_delivery, shape, stage_text
from foreman.dispatch import InvalidOutput
from foreman.dry_run import CLEAR, ScriptHandler, SCRIPTED_USAGE
from foreman.review_context import SCRIPTED_REVIEW_CONTEXT
from foreman.redaction import redact
from foreman.github import marked, BOT_LOGIN, CommandTimedOut, ForgeError, Issue, IssueBlocker, IssueComment, LinkedIssue, LiveForge, Permission, VariablesUnreadable, MergeNotArmed, HandoffChanged, QueueRejected, PullRequest, PublishedPullRequest, render_slice_body, status_body, RUN_LABELS
from foreman.status import STATUS_MARKER, Status, running, shows, write_status
from foreman.steps import CANCELLATION, TOO_LATE, StageRun, implement, review, verify

from foreman.receiver import listener
from foreman.readiness import MergeReceipt, Observation, PullSnapshot, QueuePolicy
from foreman.accept import MergeRun
from foreman.runner import AnswerError, CheckpointRuns, RunPresentation, forget_run, issue_thread, open_run, ready_resumes, released_issues, run_live_delivery, runs_in_merge, saved_run, saved_stage, saved_workspace, snapshot_result
from foreman.state import ReviewState, PlanState, ChecksState, RunState, StartSettings, checkpoint_serializer, CheckpointVersionError, SCHEMA_VERSION
from foreman.skill_rules import SkillMissing, export_skills, git
from foreman.stage_output import GateReply, HumanPrompt, ImplementOutput, ReviewOutput, Slice, SpecOutput, StoredSlice, parse_output, load_stored_slice as _load_slice
from foreman.worker import CliWorker, run_worker_command, verify_attempts
from foreman.workspace import WorkState, LiveWorkspace, PullRequestSize, RelatedCheckout, ReviewDiff, Workspace, WorkspaceError, WorktreeInspectionFailed, WorktreeState
from skill_fixture import FixtureSkills, fixture_pin, fixture_skills, skill_fixture, checker_reply as spec_check_reply
from test_adapters import codex_events, fixture_process, worker_process
from test_spec_candidate import CandidateTests
from owner_contract import assert_channels


HEAD = "a" * 40
SYNTHETIC_QUESTION_TOKEN = "ghp_" + "a" * 36

BASE = "b" * 40
# Verify's drive and a Claude build confine Bash with the host's OS sandbox. These tests fake that host check, which
# test_claude_check and test_permissions cover, so they run alike on macOS and on a Linux host without bwrap or socat.
CONFINABLE_HOST = "FOREMAN_TEST_CONFINABLE_HOST"


ONE_TOKEN = Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.0)


def confine_host(case: unittest.TestCase) -> None:
    case.enterContext(patch("foreman.adapters.claude.sandboxed"))
    # A script the test starts imports this module, which fakes the same host check there.
    case.enterContext(patch.dict("os.environ", {CONFINABLE_HOST: "1"}))


if os.environ.get(CONFINABLE_HOST):
    patch("foreman.adapters.claude.sandboxed").start()

MERGE_COMMIT = "e" * 40

CLEAN_WORKTREE = WorktreeState(HEAD, True, BRANCH)
BUILDER = SLICE
CONSUMER = {**EMPTY_SLICE, "title": "Finish items", "depends_on": [SLICE["title"]]}
APPROVE = {"findings": [], "human_question": None}
FINDING = {
    "id": "f1", "kind": "correctness", "severity": "P1", "ruling_id": None,
    "location": {"side": "head", "path": "items.txt", "line": 2},
    "summary": "Add a check.", "evidence": "A duplicate item bypasses validation.",
    "impact": "The duplicate is counted twice.",
}
DIFF = Path(__file__).with_name("fixtures").joinpath("review.diff").read_text()


class FakeWorkspace(LiveWorkspace):
    """A checkout whose pinned base is its own HEAD commit and tracks a contract file for `check` and the skill it names."""

    def __init__(self, cwd, snapshots=(), check="exit 0"):
        super().__init__(cwd)
        # Doctor and Verify read these base files through the same reader, as they read one commit in a real run.
        self.base_files = {CONTRACT_PATH: contract_text(check), VERIFY_SKILL_PATH: b"# Verify the change\n"}
        if not (Path(cwd) / ".git").exists():
            # Stages read the target repository's own skills from Git at HEAD, as in a real checkout.
            subprocess.run(["git", "init", "-q", str(cwd)], check=True)
            subprocess.run(["git", "-c", "user.name=Foreman Test", "-c", "user.email=test@example.invalid", "-C", str(cwd),
                            "commit", "-q", "--allow-empty", "-m", "Empty checkout"], check=True)
        self.snapshots = iter(snapshots)
        self.current = CLEAN_WORKTREE
        self.committed = False
        self.remote: dict[str, str] = {}

    def inspect(self):
        snapshot = (next(self.snapshots, self.current) or self.current) if self.committed else self.current
        if isinstance(snapshot, WorktreeInspectionFailed):
            raise snapshot
        return snapshot

    def fingerprint(self):
        return "synthetic-edits"

    def reconcile(self, parent, branch, base_branch, *, base=None):
        return None if self.current.clean else {"disposition": "edits", "fingerprint": self.fingerprint(),
            "size": self.pull_request_size(self.tree(None), base_branch)}

    def tree(self, head):
        return HEAD

    def validate_origin(self, repo):
        pass

    def pin_base(self, base):
        return self.current.head

    def base_head(self, base):
        return "c" * 40

    def run_at(self, commit, command, *, log=None):
        return 0

    def merge(self, head):
        # Git leaves the slice's file conflicted, so the builder resolves it.
        return ["items.txt"]

    def tracked_path(self, base, path):
        # A file the test commits or writes in the checkout stands for a tracked base file, as do the base files.
        kind = LiveWorkspace.tracked_path(self, "HEAD", path)
        file = self.root / path
        if kind != "absent":
            return kind
        if file.is_symlink():
            return "symlink"
        if path in self.base_files or file.is_file():
            return "file"
        return "directory" if file.is_dir() else "absent"

    def tracked_file(self, base, path):
        file = self.root / path
        if path != CONTRACT_PATH and LiveWorkspace.tracked_path(self, "HEAD", path) == "file":
            return self._git("cat-file", "blob", f"HEAD:{path}")
        if path != CONTRACT_PATH and file.is_file() and not file.is_symlink():
            return file.read_bytes()
        return self.base_files.get(path)

    def contains_commit(self, base, commit):
        return base == commit or (base, commit) == (HEAD, BASE)

    def create_branch(self, branch, base, *, pinned=""):
        self.current = WorktreeState(self.current.head, True, branch)

    def commit(self, message):
        head = HEAD
        self.current = WorktreeState(head, True, self.current.branch)
        self.snapshots = iter((None, *self.snapshots))
        self.committed = True

    def push(self, branch, repo):
        self.remote[branch] = self.current.head

    def remote_head(self, branch):
        return self.remote.get(branch)

    def reply(self, args, reply):
        if "implementation" in reply:
            self.committed = False
            self.current = WorktreeState(self.current.head, False, self.current.branch)
        return worker_reply(args, reply)

    def pull_request_size(self, head: str | None, base_branch: str, *, merge_base: str | None = None) -> PullRequestSize:
        return {"head": head or self.tree(None), "merge_base": merge_base or "b" * 40, "lines": 1}

    def review_context(self, base, head):
        return SCRIPTED_REVIEW_CONTEXT

    def review_diff(self, base, head):
        return ReviewDiff(f"git diff {base}...{head}", DIFF)


class RelatedWorkspace(FakeWorkspace):
    """A target checkout whose contract names a related repository, pinned at whatever head its default branch has now."""

    def __init__(self, cwd):
        super().__init__(cwd)
        self.related_head = "1" * 40
        self.related_checkout = (Path(cwd).parent / "api").resolve()
        self.pins: list[tuple[str, str]] = []

    def tracked_file(self, base, path):
        text = super().tracked_file(base, path)
        if path == CONTRACT_PATH and text is not None:
            return text + b"related_repositories: [{repository: Example/Api, start: 'true'}]\n"
        return text

    def check_out_related(self, repo, branch):
        self.pins.append((repo, branch))
        return RelatedCheckout(repository=repo, commit=self.related_head, checkout=str(self.related_checkout))


def quota_with_usage(args):
    process = fixture_process(args, "quota", 1)
    if args[0] == "codex":
        events = fixture_process(args, "success", 0).stdout.splitlines()
        receipt = next(line for line in events if '"turn.completed"' in line)
        return subprocess.CompletedProcess(args, 1, process.stdout + "\n" + receipt, "")
    return process


def priced_quota(args):
    """A quota reply whose Claude cost is known. Every run has a dollar cap, and an unknown Claude cost is charged the whole allowance."""
    process = quota_with_usage(args)
    if args[0] != "claude":
        return process
    envelope = json.loads(process.stdout)
    envelope["total_cost_usd"] = sum(usage["costUSD"] for usage in envelope["modelUsage"].values())
    return subprocess.CompletedProcess(args, process.returncode, json.dumps(envelope), process.stderr)


def planning_answer_env(detail, answer):
    env = dict(os.environ)
    if match := re.search(r"foreman answer ([0-9a-f-]{71})", detail):
        env["FOREMAN_FIXTURE_RULING"] = f"foreman answer {match[1]}\n{answer}"
    return env


def verify_reply(args):
    """Verify's default answer: one criterion passed at the published head the prompt's state names."""
    if not args[-1].startswith(stage_text("verify")):
        return None
    state = json.loads(args[-1].split("Delivery state:\n")[1])
    return {"tested_head": state["work"]["published_head"], "criteria": [{"criterion": "The change works.", "result": "passed",
            "observation": "The scripted drive used the change.", "evidence": "captures/scripted.txt"}]}


def checker_reply(args):
    """The default answer of a stage no scripted reply list names: Spec check finds nothing and Verify passes."""
    return spec_check_reply(args) or verify_reply(args)


def is_verify_answer(reply):
    return isinstance(reply, dict) and "criteria" in reply


def worker_reply(args, reply):
    # A scripted reply shaped as Verify's answer stays Verify's; any other reply stands for another stage.
    reply = spec_check_reply(args) or (None if is_verify_answer(reply) else verify_reply(args)) or reply
    if isinstance(reply, dict):
        if "triage" in reply:
            reply = {"work_kind": "feature", "slice": None, "human_question": None, **reply}
        if "spec" in reply:
            reply = {"slices": None, "questions": [], **reply}
        if "implementation" in reply:
            reply = {"no_change": None, **reply}
        if "implementation" in reply or "research" in reply:
            reply = {"human_question": None, **reply}
    if args[0] == "codex":
        events = codex_events()
        events[-2]["item"]["text"] = json.dumps(reply) if isinstance(reply, dict) else reply
        return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")
    recorded = fixture_process(args, "success", 0)
    envelope = json.loads(recorded.stdout)
    field = "structured_output" if args[0] == "claude" else "structuredOutput"
    envelope[field] = reply
    return subprocess.CompletedProcess(args, recorded.returncode, json.dumps(envelope), recorded.stderr)


class FixtureWorkspace(LiveWorkspace):
    validate_origin = FakeWorkspace.validate_origin
    review_scope = FakeWorkspace.review_scope

    def __init__(self, *args, remote: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.remote = {} if remote is None else remote

    def push(self, branch, repo):
        self.remote[branch] = self.inspect().head

    def remote_head(self, branch):
        return self.remote.get(branch)

    def pull_request_size(self, head: str | None, base_branch: str, *, merge_base: str | None = None) -> PullRequestSize:
        return {"head": head or self.tree(None), "merge_base": merge_base or BASE, "lines": 1}

    def review_context(self, base, head):
        return SCRIPTED_REVIEW_CONTEXT

    def review_diff(self, base, head):
        return ReviewDiff(f"git diff {base}...{head}", DIFF)


class FakeGitHub:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else ["slice", "Ready"]
        self.body = slice_body(EMPTY_SLICE) if "slice" in self.labels else "Issue body"
        self.created: list[tuple[str, str, list[str]]] = []
        self.comments: dict[tuple[str, int], list[str]] = {}
        self.rulings: list[IssueComment] = []
        self.closed = set()
        self.merged = set()
        self.label_calls = []
        self.created_labels: list[str] = []
        self.status: dict[tuple[str, int], str] = {}
        self.status_writes: list[tuple[str, int, str]] = []
        # The labels each issue shows once Foreman has edited them. Other issues show the fixture's labels.
        self.edited: dict[int, list[str]] = {}
        self.issue_labels: dict[int, list[str]] = {}
        self.issue_repos: dict[int, str] = {}
        self.bodies: dict[tuple[str, int], str] = {}
        self.parents: dict[tuple[str, int], int] = {}
        self.failures: dict[tuple[str, bool], list[BaseException]] = {}
        self.merge_calls = []
        self.dependencies = {}
        self.base_branch = "main"
        self.reviews = []
        self.base = BASE
        self.hold: str | None = None
        # The repository's run settings, which each start reads once. A test's subprocess passes its own.
        self.settings = {"FOREMAN_HUMAN_LOGIN": "fixture-owner", **json.loads(os.environ.get("FOREMAN_FIXTURE_SETTINGS", "{}"))}
        self.variable_reads = []
        # The quick requirements GitHub answers for the repository: the App install, its labels and its merge queue rule.
        self.installation, self.repository_labels, self.queue_rule = "installed", {"Ready"}, True
        self.subscription = SUBSCRIBED
        # Each login's base role on the repository, None for a login GitHub does not know. Any other login can write.
        self.permissions: dict[str, Permission | None] = {}
        self.opened: dict[str, PublishedPullRequest] = {}
        self.branches: dict[str, str] = {}
        self.queue_merges = True
        self.foreign_heads: set[str] = set()
        self.pr_closes: list[tuple[str, int, str]] = []
        # GitHub's clock: each human comment and Ready label event takes the next second. The issue starts Ready.
        self.seconds = 0
        self.ready_events = [self.now()]

    def now(self):
        self.seconds += 1
        return datetime(2026, 11, 24, tzinfo=UTC) + timedelta(seconds=self.seconds)

    def default_branch(self, repo):
        return "main"

    def open_pull_request(self, repo, head, base, title, body):
        self.opened[head] = PublishedPullRequest.model_validate({
            "number": 12, "state": "OPEN", "headRefOid": HEAD, "headRefName": head,
            "baseRefName": base, "baseRefOid": self.base, "autoMergeRequest": None, "body": body,
        })

    def pull_request_for(self, repo, branch):
        opened = self.opened.get(branch)
        return opened.model_copy(update={"headRefOid": self.branches.get(branch, HEAD)}) if opened else None

    def close_pull_request(self, repo, number, comment):
        self._fail("close_pull_request", after_write=False)
        self.pr_closes.append((repo, number, comment))
        for branch, opened in self.opened.items():
            if opened.number == number:
                self.opened[branch] = opened.model_copy(update={"state": "CLOSED"})

    def review_target(self, repo, issue, branch, sha):
        return PullRequest.model_validate({
            "number": 12, "state": "OPEN", "headRefOid": sha, "headRefName": branch,
            "baseRefName": "main", "baseRefOid": self.base, "autoMergeRequest": None,
        })

    def post_review(self, repo, target, body, comments, *, marker):
        self.reviews.append((repo, target.number, target.headRefOid, body, comments))
        return {"id": len(self.reviews), "state": "COMMENTED", "commit_id": target.headRefOid}
    def readiness(self, repo, issue, branch, sha):
        # The queue merges an armed head unless a test holds it in the queue.
        merged = self.queue_merges and any(call[3] == sha for call in self.merge_calls)
        return Observation(repo=repo, pr=PullSnapshot(number=12, state="closed" if merged else "open", draft=False, merged=merged,
            head={"sha": sha, "ref": branch}, base={"sha": self.base, "ref": "main"},
            mergeable=True, merge_commit_sha=MERGE_COMMIT if merged else None, mergeable_state="clean"),
            checks=[], outcome="ready", recovery="Eligible.", queue_policy={"merge_method": "SQUASH"})

    def descends(self, repo, ancestor, head):
        return head not in self.foreign_heads

    def reconcile_merge(self, receipt):
        # Like GitHub's queue readback, a receipt binds the base it was armed or last reconciled at.
        if receipt.base != self.base:
            raise HandoffChanged(f"PR #{receipt.pr} base changed from {receipt.base} to {self.base}")
        return receipt

    def variables(self, repo):
        self.variable_reads.append(repo)
        return {**self.settings, **({} if self.hold is None else {"FOREMAN_MERGE_HOLD": self.hold})}

    def app_installation(self, repo):
        return self.installation

    def app_subscription(self, repo):
        return self.subscription

    def permission(self, repo, login):
        return self.permissions.get(login, "write")

    def has_label(self, repo, name):
        return name in self.repository_labels

    def merge_queue(self, repo, branch):
        return QueuePolicy(merge_method="SQUASH") if self.queue_rule else None

    def arm_auto_merge(self, repo, issue, branch, sha, observation):
        self.merge_calls.append((repo, issue, branch, sha))
        return MergeReceipt(repo=repo, pr=12, source_head=sha, base=self.base,
                            base_ref="main", method="SQUASH", queue_policy={"merge_method": "SQUASH"}, identity="queue-test", status="QUEUED")

    def load_issue(self, repo, number) -> Issue:
        if number in self.issue_labels:
            title, body, _ = self.created[number - 101]
            return {"repo": repo, "number": number, "title": title, "body": self.bodies.get((repo, number), body), "labels": self.issue_labels[number]}
        return {"repo": repo, "number": number, "title": "Issue title", "body": self.bodies.get((repo, number), self.body),
                "labels": list(self.edited.get(number, self.labels))}

    def marked_issues(self, repo, marker) -> list[int]:
        numbers = dict.fromkeys([number for number, issue_repo in self.issue_repos.items() if issue_repo == repo]
                                + [number for issue_repo, number in self.bodies if issue_repo == repo])
        return [number for number in numbers if marker in self.load_issue(repo, number)["body"]]

    def _linked_issue(self, repo, number) -> LinkedIssue:
        return LinkedIssue(id=number + 10000, number=number, title=self.load_issue(repo, number)["title"],
                           repository_url=f"https://api.github.com/repos/{repo}",
                           html_url=f"https://github.com/{repo}/issues/{number}",
                           state="closed" if number in self.closed or number in self.merged else "open")

    def sub_issues(self, repo, number) -> list[LinkedIssue]:
        return [self._linked_issue(repo, child) for (issue_repo, child), parent in self.parents.items()
                if issue_repo == repo and parent == number]

    def parent_issue(self, repo, number) -> LinkedIssue | None:
        parent = self.parents.get((repo, number))
        return self._linked_issue(repo, parent) if parent is not None else None

    def preset_sub_issues(self, repo, number, count):
        start = max(19999, max((child for issue_repo, child in self.parents if issue_repo == repo), default=0)) + 1
        for child in range(start, start + count):
            self.parents[repo, child] = number

    def add_sub_issue(self, repo, number, child):
        self._fail("add_sub_issue", after_write=False)
        if (repo, child) in self.parents or len(self.sub_issues(repo, number)) >= 100:
            raise ForgeError("Child already has a parent or parent is full.")
        self.parents[repo, child] = number
        self._fail("add_sub_issue", after_write=True)

    def update_issue_body(self, repo, number, body):
        self._fail("update_issue_body", after_write=False)
        self.bodies[repo, number] = body
        self._fail("update_issue_body", after_write=True)

    def fail_next(self, operation, *, after_write=False, error=None):
        self.failures.setdefault((operation, after_write), []).append(
            error if error is not None else ForgeError("Injected write failure."))

    def _fail(self, operation, *, after_write):
        failures = self.failures.get((operation, after_write), [])
        if failures:
            raise failures.pop(0)

    def issue_comment_records(self, repo, number):
        body = os.environ.get("FOREMAN_FIXTURE_RULING")
        relayed = [IssueComment.model_validate({"user": {"login": "fixture-owner"}, "body": body})] if body else []
        posted = [IssueComment.model_validate({"user": {"login": BOT_LOGIN}, "body": text}) for text in self.comments.get((repo, number), [])]
        status = [IssueComment.model_validate({"id": 1, "user": {"login": BOT_LOGIN}, "body": self.status[repo, number]})] if (repo, number) in self.status else []
        return [*relayed, *self.rulings, *posted, *status]

    def upsert_comment(self, repo, number, text, facts, *, marker):
        body = status_body(text, facts, marker)  # As LiveForge writes it.
        self._fail("upsert_comment", after_write=False)
        if self.status.get((repo, number)) != body:
            self.status[repo, number] = body
            self.status_writes.append((repo, number, body))
        self._fail("upsert_comment", after_write=True)

    def ruling(self, body, login="fixture-owner"):
        self.rulings.append(IssueComment.model_validate({"user": {"login": login}, "body": body, "created_at": self.now()}))

    def ready_labeled_at(self, repo, number):
        return max(self.ready_events)

    def answer(self, detail, answer, login="fixture-owner"):
        """Post the human's reply to the paused question, as the coordinator relays it under their identity."""
        marker = re.search(r"foreman answer [0-9a-f-]{71}", detail)
        assert marker is not None, detail
        self.ruling(f"{marker[0]}\n{answer}", login)

    def comment_issue(self, repo, number, body, *, marker):
        body = marked(redact(body), marker)  # As LiveForge writes it.
        self._fail("comment_issue", after_write=False)
        self.comments.setdefault((repo, number), []).append(body)
        self._fail("comment_issue", after_write=True)

    def create_issue(self, repo, title, body, labels, *, marker, child=None):
        body = marked(redact(body), marker)  # As LiveForge writes it.
        self._fail("create_issue", after_write=False)
        if child is not None:
            body = render_slice_body(body, child)
        self.created.append((title, body, labels))
        number = 100 + len(self.created)
        self.issue_labels[number] = list(labels)
        self.issue_repos[number] = repo
        self.bodies[repo, number] = body
        self._fail("create_issue", after_write=True)
        return number

    def is_merged(self, repo, number, *, contains=None):
        return number in self.merged and (contains is None or contains(self.base))

    def add_blocker(self, repo, number, blocker):
        self._fail("add_blocker", after_write=False)
        self.dependencies.setdefault(number, []).append(blocker)
        self._fail("add_blocker", after_write=True)

    def blockers(self, repo, number):
        return [
            IssueBlocker(number=blocker, title=self.load_issue(repo, blocker)["title"],
                         repository_url=f"https://api.github.com/repos/{repo}",
                         html_url=f"https://github.com/{repo}/issues/{blocker}",
                         state="closed" if blocker in self.closed or blocker in self.merged else "open")
            for blocker in self.dependencies.get(number, ())
        ]

    def blocking(self, repo, number):
        return [
            IssueBlocker(number=dependent, title=self.load_issue(repo, dependent)["title"],
                         repository_url=f"https://api.github.com/repos/{repo}",
                         html_url=f"https://github.com/{repo}/issues/{dependent}",
                         state="closed" if dependent in self.closed or dependent in self.merged else "open")
            for dependent, blockers in self.dependencies.items() if number in blockers
        ]

    def ensure_label(self, repo, name, color):
        if name not in self.created_labels:
            self.created_labels.append(name)

    def edit_labels(self, repo, number, add, remove):
        self._fail("edit_labels", after_write=False)
        assert all(label in self.created_labels for label in add), "A label is created before it is added."
        self.label_calls.append((repo, number, add, remove))
        labels = self.issue_labels[number] if number in self.issue_labels else self.edited.setdefault(number, list(self.labels))
        labels[:] = [label for label in labels if label not in remove] + [label for label in add if label not in labels]
        self._fail("edit_labels", after_write=True)

    def release_labels(self, run_issue=8):
        """Label writes on issues other than the run's own, whose labels the status mirror writes."""
        return [call for call in self.label_calls if call[1] != run_issue]

    def ready_again(self, number):
        """A human puts Ready on an issue a run labelled, which starts a fresh run."""
        labels = self.issue_labels.get(number, self.edited.get(number))
        if labels is not None and any(label in RUN_LABELS for label in labels):
            labels[:] = [label for label in labels if label not in RUN_LABELS] + ["Ready"]
            self.ready_events.append(self.now())


class CliWorkerTest(unittest.TestCase):
    def test_implement_instruction_names_the_exact_granted_check_only(self):
        check, preflight = "npm run check && ./scripts/smoke.py 9", "npm ci"
        state = {"checks": ChecksState(check=check, preflight=preflight)}
        instruction, target, scope = STAGES["implement"].prepare(state, FakeWorkspace(Path(".")), None)
        self.assertEqual((target, scope), (None, None))
        self.assertTrue(instruction.endswith(f"granted exactly as written. Run it unchanged, with no wrapper, prefix, suffix or "
                                             f"chained command, and never run one segment of it alone:\n{check}"))
        self.assertNotIn(preflight, instruction.replace(check, ""))
        self.assertEqual(STAGES["implement"].prepare({"checks": ChecksState()}, FakeWorkspace(Path(".")), None), ("", None, None))

    def test_spec_worker_receives_paired_questions_once(self):
        qa = [{"stage": "triage", "question": "Which scope?", "answer": "Items"},
              {"stage": "spec", "question": "Which policy?", "answer": "Sort"}]
        state = {"plan": PlanState(human_qa=qa), "run": RunState(repo="a/b", base_sha=BASE)}
        instruction, _, _ = STAGES["spec"].prepare(state, FakeWorkspace(Path(".")), None)

        def run(args, cwd, env):
            prompt = args[-1]
            delivered = json.loads(prompt.split("Delivery state:\n")[1])
            self.assertEqual(delivered["plan"]["human_qa"], qa)
            for pair in qa:
                self.assertEqual(prompt.count(pair["question"]), 1)
                self.assertEqual(prompt.count(pair["answer"]), 1)
            return worker_reply(args, {"spec": "Sort items", "spec_gate": "ready", "slices": [], "questions": []})

        result = CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory())))("spec", ModelSpec.parse("gpt-6.1-sol-high"), state, instruction)
        self.assertEqual(result.kind, "ran")

    def test_commands_use_the_configured_models_and_parse_the_final_result(self):
        models = {
            "gpt-6.1-sol-high": ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high"],
            "claude-opus-5-5-high": ["claude", "--model", "claude-opus-5-5", "--effort", "high"],
        }
        for model, prefix in models.items():
            with self.subTest(model=model):
                schema_paths = []

                def run(args, cwd, env):
                    self.assertEqual(args[:len(prefix)], prefix)
                    self.assertEqual(cwd, Path("engine"))
                    self.assertEqual(json.loads(args[-1].split("Delivery state:\n")[1])["run"],
                             RunState(issue=12).model_dump(mode="json"))
                    expected_env = ClaudeAdapter.env if args[0] == "claude" else CodexAdapter.env
                    self.assertEqual(env, {**expected_env, "TMPDIR": env["TMPDIR"]})
                    self.assertTrue(Path(env["TMPDIR"]).is_dir())
                    self.assertNotIn("Output schema:", args[-1])
                    if args[0] == "codex":
                        self.assertIn("--json", args)
                        self.assertNotIn("--ephemeral", args)
                        schema_path = Path(args[args.index("--output-schema") + 1])
                        schema_paths.append(schema_path)
                        schema = json.loads(schema_path.read_text())
                    else:
                        schema = json.loads(args[args.index("--json-schema") + 1])
                    self.assertEqual(schema, STAGES["triage"].output.model_json_schema())
                    return worker_reply(args, {"triage": "supplied_slice"})

                result = CliWorker(Path("engine"), run, scratch_root=Path(self.enterContext(TemporaryDirectory())))(
                    "triage", ModelSpec.parse(model), {"review": ReviewState(), "run": RunState(issue=12), "plan": PlanState()}, "Read the issue.",
                )
                self.assertEqual(result.kind, "ran")
                self.assertEqual(result.output["triage"], "supplied_slice")
                for path in schema_paths:
                    self.assertFalse(path.exists())

    def test_each_family_can_use_scratch_outside_checkout(self):
        with TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            checkout.mkdir()
            paths = []
            for model in ("gpt-6.1-sol-high", "claude-opus-5-5-high"):
                with self.subTest(model=model):
                    def run(args, cwd, env):
                        scratch = Path(env["TMPDIR"])
                        paths.append(scratch)
                        self.assertTrue(scratch.is_absolute())
                        self.assertFalse(scratch.resolve().is_relative_to(checkout.resolve()))
                        self.assertEqual(list(scratch.iterdir()), [])
                        if args[0] == "codex":
                            schema_path = Path(args[args.index("--output-schema") + 1])
                            self.assertFalse(schema_path.resolve().is_relative_to(scratch.resolve()))
                            self.assertTrue(schema_path.is_file())
                        self.assertNotIn(str(scratch), args[-1])
                        process = run_worker_command([
                            sys.executable, "-c",
                            "import os, pathlib; "
                            "p = pathlib.Path(os.environ['TMPDIR']) / 'scratch.txt'; "
                            "p.write_text('worker scratch'); print(p.read_text())",
                        ], cwd, env)
                        self.assertEqual(process.returncode, 0, process.stderr)
                        self.assertEqual(process.stdout.strip(), "worker scratch")
                        return worker_reply(args, {"triage": "supplied_slice"})

                    result = CliWorker(checkout, run, scratch_root=Path(root))("triage", ModelSpec.parse(model), {}, "Read the issue.")
                    self.assertEqual(result.kind, "ran")
                    self.assertFalse(paths[-1].exists())
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(list(checkout.iterdir()), [])

    def test_inherited_checkout_tmpdir_is_overridden_without_changing_environment(self):
        script = """
import os
from pathlib import Path
import subprocess
import tempfile
from foreman.config import ModelSpec
from foreman.worker import CliWorker

checkout = Path.cwd().resolve()
assert Path(tempfile.gettempdir()).resolve().is_relative_to(checkout)
inherited = dict(os.environ)
paths = []
def run(args, cwd, env):
    scratch = Path(env["TMPDIR"])
    assert not scratch.resolve().is_relative_to(checkout)
    assert list(scratch.iterdir()) == []
    paths.append(scratch)
    return subprocess.CompletedProcess(args, 1, "", "")
CliWorker(checkout, run, scratch_root=checkout.parent)("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "Read.")
assert paths and all(not path.parent.exists() for path in paths)
assert dict(os.environ) == inherited
"""
        with TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            checkout.mkdir()
            inherited_temp = checkout / "temp"
            inherited_temp.mkdir()
            env = {**os.environ, "TMPDIR": str(inherited_temp),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            process = subprocess.run([sys.executable, "-c", script], cwd=checkout,
                                     env=env, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(list(checkout.iterdir()), [inherited_temp])

    def test_a_worker_inherits_nothing_but_the_environment_foreman_builds(self):
        inherited = {"PATH": "/bin", "HOME": "/home/operator", "CLAUDE_CODE_NO_MODEL_FALLBACK": "0", "SSH_AUTH_SOCK": "/agent",
                     "GH_TOKEN": "inherited", "XDG_STATE_HOME": "/state", "GIT_DIR": "/elsewhere"}
        adapter_env = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1", "TMPDIR": "/scratch"}
        with patch.dict("os.environ", inherited, clear=True), worker_process() as popen:
            run_worker_command(["claude"], Path("engine"), adapter_env)
            built = command_environment()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["claude"])
        # Foreman's own command environment with no token, the adapter's variables, then TMPDIR; no other inherited name.
        self.assertEqual(popen.call_args.kwargs["env"], {**built, **adapter_env})
        self.assertEqual({name: popen.call_args.kwargs["env"][name] for name in ("PATH", "HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
                                                                                "GIT_ATTR_NOSYSTEM")},
                         {"PATH": "/bin", "HOME": "/home/operator", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                          "GIT_ATTR_NOSYSTEM": "1"})
        self.assertFalse({"SSH_AUTH_SOCK", "GH_TOKEN", "XDG_STATE_HOME", "GIT_DIR"} & set(popen.call_args.kwargs["env"]))
        self.assertEqual((popen.call_args.kwargs["cwd"], popen.call_args.kwargs["pass_fds"]), (Path("engine"), ()))
        self.assertIsNone(popen.prompt)

    def test_worker_without_a_model_raises_a_config_error(self):
        worker = CliWorker(Path("."), lambda *_: self.fail("CLI must not run"), scratch_root=Path(self.enterContext(TemporaryDirectory())))
        with self.assertRaisesRegex(ConfigError, "triage has no model configured"):
            worker("triage", None, {}, "")

    def test_non_object_replies_are_invalid(self):
        replies = (
            ('Here is the result: {"triage": "supplied_slice"}', "did not return valid JSON"),
            ('["implement"]', "JSON object"),
        )
        for reply, detail in replies:
            with self.subTest(reply=reply):
                worker = CliWorker(Path("."), lambda args, cwd, env: worker_reply(args, reply), scratch_root=Path(self.enterContext(TemporaryDirectory())))
                outcome = worker("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "")
                self.assertEqual(outcome.kind, "failed")
                self.assertIn(detail, outcome.detail)

    def walk_triage(self, models, run):
        config = load_config()
        config.nodes["triage"] = NodeConfig("coordinator", tuple(ModelSpec.parse(model) for model in models))
        with skill_fixture(config) as (fixture_config, scratch):
            return run_delivery(CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory()))), repo="Example/Engine",
                                request="List items", config=fixture_config, slice=EMPTY_SLICE, scratch=scratch)

    def test_grok_capability_failure_falls_through_without_launching_it(self):
        calls = []
        def run(args, cwd, env):
            calls.append(args[0])
            return worker_reply(args, {"triage": "needs_human", "human_question": "Which items?"})
        result = self.walk_triage(("grok-4.7-high", "gpt-6.1-sol-high"), run)
        self.assertEqual(result["stop"], "needs_human")
        self.assertEqual(calls, ["codex"])
        self.assertEqual([a["outcome"] for a in result["attempts"]], ["cli_missing", "ran"])

    def test_delivery_falls_through_only_for_quota_or_missing_cli(self):
        cases = [("quota", "needs_human"), ("cli_missing", "needs_human"),
                 ("is_error", "invalid_output"), ("missing_model_usage", "invalid_output")]
        cases.append(("wrong_model", "invalid_output"))
        for case, stop in cases:
            with self.subTest(case=case):
                calls = []

                def run(args, cwd, env):
                    calls.append(args[0])
                    if args[0] == "codex":
                        return worker_reply(args, {"triage": "needs_human", "human_question": "Which items?"})
                    if case == "cli_missing":
                        raise FileNotFoundError(args[0])
                    if case == "quota":
                        return priced_quota(args)
                    return fixture_process(args, case, 1 if case == "is_error" else 0)

                result = self.walk_triage(("claude-opus-5-5-high", "gpt-6.1-sol-high"), run)
                self.assertEqual(result["stop"], stop)
                self.assertEqual(calls, ["claude", "codex"] if stop == "needs_human" else ["claude"])
                expected = [case, "ran"] if stop == "needs_human" else ["failed"]
                self.assertEqual([item["outcome"] for item in result["attempts"]], expected)
                if case in {"is_error", "missing_model_usage"}:
                    # Invalid output stays stopped and is charged at the remaining allowance.
                    self.assertIn("work_charge", result["attempts"][-1])

    def test_delivery_stops_for_an_incomplete_native_stage_reply(self):
        def run(args, cwd, env):
            self.assertEqual(args[0], "claude")
            recorded = fixture_process(args, "success", 0)
            envelope = json.loads(recorded.stdout)
            envelope["structured_output"] = {"triage": "question"}
            return subprocess.CompletedProcess(args, 0, json.dumps(envelope), recorded.stderr)

        result = self.walk_triage(("claude-opus-5-5-high", "gpt-6.1-sol-high"), run)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("Field required", result["halt_detail"])

    def test_delivery_stops_when_every_cli_is_missing(self):
        calls = []

        def run(args, cwd, env):
            calls.append(args[0])
            raise FileNotFoundError(args[0])

        result = self.walk_triage(("claude-test", "grok-test", "gpt-test"), run)
        self.assertEqual(calls, ["claude", "codex"])
        # No qualified CLI is installed: dispatch halts worker_unavailable, which Ready resumes.
        self.assertEqual(result["stop"], "worker_unavailable")
        self.assertIn("codex executable is unavailable", result["halt_detail"])

    def test_codex_cli_missing_removes_the_schema_file(self):
        paths = []

        def run(args, cwd, env):
            path = Path(args[args.index("--output-schema") + 1])
            self.assertTrue(path.is_file())
            paths.append(path)
            raise FileNotFoundError(args[0])

        outcome = CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory())))("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "")
        self.assertEqual((outcome.kind, outcome.detail), ("cli_missing", "codex executable is unavailable"))
        self.assertFalse(paths[0].exists())


class StageOutputTest(unittest.TestCase):
    def test_worker_schemas_deliver_the_shared_edit_contract(self):
        for node in ("triage", "spec"):
            def run(args, cwd, env):
                schema = json.loads(args[args.index("--json-schema") + 1])
                encoded = json.dumps(schema)
                self.assertIn("List the files you expect this slice to touch, as paths relative to the repository root.", encoded)
                self.assertIn("List the titles of the other slices in this plan that must merge before this one", encoded)
                return worker_reply(args, {})
            CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory())))(node, ModelSpec.parse("claude-opus-5-5-high"), {}, "Read.")

    def test_schema_snapshot(self):
        snapshot = Path(__file__).with_name("stage-schemas.json").read_text()
        schemas = {name: model.model_json_schema() for name, stage in STAGES.items() if (model := stage.output) is not None}
        self.assertEqual(schemas, json.loads(snapshot))

    def test_every_object_in_the_schema_is_strict(self):
        def check(schema):
            if schema.get("type") == "object":
                self.assertCountEqual(schema["required"], schema["properties"])
                self.assertIs(schema["additionalProperties"], False)
            return schema

        for stage in STAGES.values():
            if (model := stage.output) is None:
                continue
            schema = model.model_json_schema()
            self.assertEqual(schema["type"], "object")
            self.assertNotIn("anyOf", schema)
            self.assertNotIn("oneOf", schema)
            json.loads(json.dumps(schema), object_hook=check)

    def test_screen_rejects_control_characters_and_subdirectories(self):
        for screen in ("docs/screens/na\x01me.md", "docs/screens/a/b.md"):
            with self.subTest(screen=screen), self.assertRaisesRegex(InvalidOutput, r"^screen:"):
                parse_output(Slice, {**SLICE, "screen": screen})

    def test_implementation_rejects_worker_head(self):
        with self.assertRaisesRegex(InvalidOutput, r"^head_sha:"):
            parse_output(ImplementOutput, {"implementation": "Done", "no_change": None, "human_question": None, "head_sha": HEAD})

    def test_hedged_approval_is_invalid(self):
        with self.assertRaisesRegex(InvalidOutput, "^review:"):
            parse_output(ReviewOutput, {**APPROVE, "review": "approve, but please fix X first"}, context=SCRIPTED_REVIEW_CONTEXT)

    def test_review_requires_an_exact_verdict(self):
        for verdict in ("Approve.", "approve.", "changes.", "changes. Add a check."):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(InvalidOutput, "^review:"):
                parse_output(ReviewOutput, {**APPROVE, "review": verdict}, context=SCRIPTED_REVIEW_CONTEXT)

    def test_review_summary_is_limited_to_200_characters(self):
        self.assertEqual(ReviewOutput.model_json_schema()["$defs"]["ReviewFinding"]["properties"]["summary"]["maxLength"], 200)
        raw = {**APPROVE, "findings": [{**FINDING, "summary": "a" * 201}]}
        with self.assertRaisesRegex(InvalidOutput, r"^findings\.0\.summary:"):
            parse_output(ReviewOutput, raw, context=SCRIPTED_REVIEW_CONTEXT)


class LiveRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.pin = fixture_pin(root / "fixture")

    def setUp(self):
        confine_host(self)
        # The checkpoint sits beside the checkout, so the run's scratch stays in the test's own directory.
        self.cwd = Path(self.enterContext(TemporaryDirectory())) / "checkout"
        self.cwd.mkdir()
        self.checkpoint = self.cwd.parent / "runs.sqlite3"
        self.calls = []
        self.argvs = []
        self.prompts = []
        self.github = FakeGitHub()
        self.question = ""
        self.enterContext(patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=self.pin, matt_skills=self.pin.model_copy(update={"directory": "skills"}))))

    def walk(self, replies, *, issue=8, check="exit 0", preflight="", answer=None, snapshots=(), workspace: Workspace | None = None, skills=export_skills, presentation=None, wake=False):
        if saved_workspace(self.checkpoint, issue_thread("Example/Engine", issue)) is None:
            self.github.ready_again(issue)  # A fresh run starts from Ready.
        if answer is not None and "foreman answer " in self.question:
            # The coordinator relays the human's answer to the paused question under their identity.
            self.github.answer(self.question, answer)
        outputs = list(replies)
        active = workspace if workspace is not None else FakeWorkspace(self.cwd, snapshots)
        if isinstance(active, FakeWorkspace):
            active.remote = self.github.branches
            active.base_files[CONTRACT_PATH] = contract_text(check)

        def run(args, cwd, env):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            self.argvs.append(args)
            self.prompts.append(args[-1])
            # A test about Verify's answer lists it; otherwise Verify takes the default and the list keeps the other stages' replies.
            listed = not verify_reply(args) or bool(outputs) and is_verify_answer(outputs[0])
            reply = spec_check_reply(args) or (outputs.pop(0) if listed else verify_reply(args))
            return active.reply(args, reply) if isinstance(active, FakeWorkspace) else worker_reply(args, reply)

        self.github.settings["FOREMAN_PREFLIGHT"] = preflight
        result = run_live_delivery(
            repo="Example/Engine", issue=issue, answer=answer,
            checkpoint=self.checkpoint, dispatcher=CliWorker(self.cwd, run, scratch_root=self.checkpoint.parent), presentation=presentation,
            run=LiveRun(self.github, active, skills=skills), wake=wake,
        )
        if result.get("stop") == "needs_human":
            self.question = result.get("halt_detail", "")
        return result

    def test_verify_feedback_reopens_and_fails_over_without_resetting_cap(self):
        canary = "syntheticlowentropy"
        token = "ghp_" + "A" * 36
        sensitive = [canary, token, "bearercanary", "YmFzaWNjYW5hcnk=", "KEYBODY"]
        secret_lines = ("Authorization: Bearer bearercanary\nAuthorization: Basic YmFzaWNjYW5hcnk=\n"
                        "-----BEGIN RSA PRIVATE KEY-----\nKEYBODY\n-----END RSA PRIVATE KEY-----")
        script = self.cwd / "check.py"
        script.write_text("import sys\nprint('noise\\n' * 300)\n"
                          f"print({canary!r})\nprint({token!r})\nprint({secret_lines!r})\n"
                          "print('AssertionError: expected short id', file=sys.stderr)\n"
                          "print('- actual full id\\n+ expected short id', file=sys.stderr)\nsys.exit(1)\n")
        active = FakeWorkspace(self.cwd, check=f"{sys.executable} {script}")
        logs = []
        crash = False
        replies = iter([{"triage": "supplied_slice"}])
        def worker(args, cwd, env):
            prompt = args[-1]
            state = json.loads(prompt.split("Delivery state:\n")[1])
            if checker_reply(args):
                return worker_reply(args, {})
            if state.get("checks", {}).get("verify_failures"):
                if crash:
                    raise RuntimeError("synthetic pending dispatch crash")
                self.assertIn("untrusted diagnostic data", prompt)
                self.assertIn("previous attempt", prompt)
                self.assertIn("authoritative", prompt)
                for secret in sensitive:
                    self.assertNotIn(secret, prompt)
                self.assertEqual(state["checks"]["check_exit_code"], 1)
                self.assertIn("+ expected short id", state["checks"]["failure_output"])
                self.assertFalse(state["checks"]["verified_head"])
                if args[0] == "codex":
                    self.assertIn("Network and database connections are unavailable", prompt)
                    return priced_quota(args)
                self.assertIn("Claude's Bash runs sandboxed", prompt)
                with worker_process(worker_reply(args, {"implementation": "retry"})) as popen:
                    run_worker_command(args, cwd, env)
                self.assertEqual(popen.prompt, prompt)
                return active.reply(args, {"implementation": "retry"})
            return active.reply(args, checker_reply(args) or next(replies, {"implementation": "first"}))
        def invoke(stop=None):
            def compile(*args, **kwargs):
                graph = compile_delivery(*args, **kwargs)
                graph.interrupt_after_nodes = [stop] if stop else []
                return graph
            with patch("foreman.runner.compile_delivery", side_effect=compile):
                return run_live_delivery(repo="Example/Engine", issue=8,
                    checkpoint=self.checkpoint, dispatcher=CliWorker(self.cwd, worker, scratch_root=self.checkpoint.parent),
                    run=LiveRun(self.github, active, check_log=logs.append))
        with patch.dict(os.environ, {"GH_TOKEN": canary}):
            planned = invoke("spec")
            self.assertEqual(planned["path"][-1:], ["spec_check"])
            backup = self.checkpoint.read_bytes()
            crash = True
            # The first check fails, and the repair dispatch crashes after saving its reservation.
            with self.assertRaisesRegex(RuntimeError, "pending dispatch crash"):
                invoke()
            crash = False
            interrupted = invoke()
            self.assertEqual(interrupted["stop"], "budget_exceeded")
            self.assertIn("usage_unknown", interrupted["halt_detail"])
            evidence = interrupted["checks"].failure_output
            self.assertIn("+ expected short id", evidence)
            self.assertEqual((interrupted["checks"].check_exit_code, interrupted["checks"].verify), (1, "fail"))
            self.checkpoint.write_bytes(backup)
            active.current = CLEAN_WORKTREE  # The restored checkpoint predates the attempt's edits.
            result = invoke()
        self.assertEqual(result["stop"], "verify_cap", result.get("halt_detail"))
        self.assertEqual(result["checks"].verify_failures, 3)
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertNotIn("review", result["path"])
        self.assertEqual(result["checks"].failure_output, evidence)
        self.assertIn(canary, "".join(logs))
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            checkpoints = list(saver.list({"configurable": {"thread_id": "example/engine#8"}}))
            # Dispatch reservations are saved in the nested graph's namespace with the caller's state.
            pending = [c for c in checkpoints if c.checkpoint["channel_values"].get("reservation")
                       and c.checkpoint["channel_values"]["caller"]["checks"].verify_failures]
            self.assertTrue(pending)
            for saved in checkpoints:
                for secret in sensitive:
                    self.assertNotIn(secret, str(saved.checkpoint))
            for saved in pending:
                self.assertEqual(saved.checkpoint["channel_values"]["caller"]["checks"].failure_output, evidence)
        self.assertEqual(ChecksState().failure_output, "")
        self.assertEqual(ChecksState(verify="pass", verified_head=HEAD, failure_output=evidence).for_attempt(), ChecksState())

    def test_next_failure_replaces_diagnostics_and_pass_clears_them(self):
        script = self.cwd / "check.py"
        script.write_text("from pathlib import Path\nimport sys\np = Path('count')\n"
                          "n = int(p.read_text()) + 1 if p.exists() else 1\np.write_text(str(n))\n"
                          "print('failure attempt', n)\nsys.exit(1 if n < 3 else 0)\n")
        result = self.walk([{"triage": "supplied_slice"}, *[{"implementation": "built"}] * 3, APPROVE],
                           check=f"{sys.executable} {script}")
        states = [json.loads(prompt.split("Delivery state:\n")[1]) for prompt in self.prompts]
        self.assertIn("failure attempt 1", states[3]["checks"]["failure_output"])
        self.assertIn("failure attempt 2", states[4]["checks"]["failure_output"])
        self.assertNotIn("failure attempt 1", states[4]["checks"]["failure_output"])
        self.assertEqual(result["checks"].verify, "pass")
        self.assertEqual(result["checks"].failure_output, "")
        self.assertEqual(result["path"].count("publish"), 1)
        self.assertEqual(result["path"][-5:], ["commit", "publish", "verify", "review", "merge"])
        self.assertEqual(self.github.reviews[-1][2], result["checks"].verified_head)

    def test_preflight_output_never_replaces_failed_check_context(self):
        active = FakeWorkspace(self.cwd)
        probes = iter([0, 1])
        def run(command, *, log=None):
            if log:
                log("preflight private context\n" if command == "probe" else "synthetic check failure\n")
            return next(probes) if command == "probe" else 1
        with patch.object(active, "run", side_effect=run):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}],
                               workspace=active, preflight="probe")
        self.assertEqual(result["stop"], "env_unavailable")
        self.assertEqual(result["checks"].failure_output, "synthetic check failure\n")
        self.assertEqual(result["checks"].verify_failures, 0)

    def test_arm_waits_for_checks_and_resumes_without_repeating_workers(self):
        pending = self.github.readiness("Example/Engine", 8, BRANCH, HEAD).model_copy(update={"outcome": "pending", "recovery": "Wait for required checks, then resume."})
        with patch.object(self.github, "readiness", return_value=pending):
            waiting = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            self.assertEqual((waiting["stop"], run_exit_code(waiting)), ("merge_pending", 75))
            self.assertIn("PR #12 head=", waiting["halt_detail"])
            self.assertEqual(waiting["path"][-2:], ["review", "merge"])
            count = len(self.calls)
            # A duplicate wake reads GitHub again and keeps waiting.
            self.assertEqual(self.walk([])["stop"], "merge_pending")
        resumed = self.walk([])
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual(len(self.calls), count)
        self.assertEqual(resumed["review"].review_receipt, waiting["review"].review_receipt)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_finished_run_releases_its_checkout_and_a_halted_run_keeps_it(self):
        released: list[set[str]] = []

        class Releasing(FakeWorkspace):
            def release(self, heads):
                released.append(set(heads))
                return True

        # Verifier captures go with the checkout: a finished run removes its own, a halted run keeps them as evidence.
        captures = {issue: verify_attempts(self.checkpoint.parent, "Example/Engine", issue) / "attempt"
                    for issue in (8, 9)}
        for capture in captures.values():
            capture.mkdir(parents=True)
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE],
                                   workspace=Releasing(self.cwd))["stop"], "merged")
        self.assertEqual(len(released), 1)
        self.assertIn(HEAD, released[0])
        self.assertFalse(captures[8].parent.exists())
        # The invalid answer and its one repair turn both fail, so the run halts.
        halted = self.walk([{"triage": "not-a-route"}] * 2, issue=9, workspace=Releasing(self.cwd))
        self.assertIsNotNone(halted.get("halt"))
        self.assertEqual(len(released), 1)
        self.assertTrue(captures[9].is_dir())

    def test_a_waiting_run_rechecks_its_origin_before_resuming(self):
        pending = self.github.readiness("Example/Engine", 8, BRANCH, HEAD).model_copy(update={"outcome": "pending", "recovery": "Wait."})
        with patch.object(self.github, "readiness", return_value=pending):
            self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_pending")

        class Moved(FakeWorkspace):
            def validate_origin(self, repo):
                raise WorktreeInspectionFailed("origin moved")

        self.assertEqual(self.walk([], workspace=Moved(self.cwd)).get("halt"), "git_failed")

    def test_failed_checks_or_conflict_route_to_repair_with_github_evidence(self):
        original = self.github.readiness
        for outcome, step in [("red", "arm"), ("conflict", "arm"), ("red", "observe")]:
            with self.subTest(outcome=outcome, step=step), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.prompts, self.github.merge_calls, self.github.reviews = Path(directory) / "runs.sqlite3", [], [], []
                failing = original("Example/Engine", 8, BRANCH, HEAD).model_copy(update={"outcome": outcome, "recovery": "Repair, then resume."})
                readings = iter([failing] if step == "arm" else [])
                self.github.queue_merges = step == "arm"
                def observe(*args):
                    reading = next(readings, None)
                    if reading is None and step == "observe" and self.github.merge_calls and not self.github.queue_merges:
                        # The queue drops the armed head after its checks fail, and the repaired head merges.
                        self.github.queue_merges = True
                        return failing
                    return reading or original(*args)
                with patch.object(self.github, "readiness", side_effect=observe):
                    result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE, {"implementation": "built"}, APPROVE])
                self.assertEqual(result["stop"], "merged")
                # The repair Implement prompt comes before the new head's Verify and Review.
                repair = json.loads(self.prompts[-3].split("Delivery state:\n")[1])
                self.assertTrue(repair["checks"]["failure_output"].startswith(f"GitHub: PR #12 head={HEAD}"))
                self.assertIsNone(repair["review"]["review_receipt"])
                self.assertEqual(repair["work"]["repair_head"], None)
                self.assertEqual(len(self.github.reviews), 2)
                self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.github.queue_merges = True

    def test_resume_rejects_historical_schema_before_missing_usage(self):
        self.github.hold = "Maintenance"
        result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["stop"], "merge_held")
        config = {"configurable": {"thread_id": "example/engine#8"}}
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            historical = next(item for item in saver.list(config)
                              if item.checkpoint["channel_values"].get("path", [])[-1:] == ["review"])
            historical.checkpoint["channel_values"].pop("schema_version")
            saver.put(historical.config, historical.checkpoint, historical.metadata, {})
        with self.assertRaises(CheckpointVersionError) as raised:
            self.walk([])
        self.assertEqual(str(raised.exception), refusal("example/engine#8", None))

    def test_old_checkpoint_refusal_leaves_it_untouched_and_affects_no_other_issue(self):
        self.github.labels = ["Ready"]
        config = {"configurable": {"thread_id": "example/engine#8"}}
        for version in (None, SCHEMA_VERSION - 1):
            with self.subTest(version=version):
                state_home = Path(f"old-{version}")
                self.checkpoint = self.cwd.parent / state_home / "foreman" / "checkpoints.sqlite3"
                paused = self.walk([{"triage": "needs_human", "human_question": "Which items?"}])
                self.assertEqual(paused["stop"], "needs_human")
                with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                    saver.serde = checkpoint_serializer()
                    saved = saver.get_tuple(config)
                    channels = saved.checkpoint["channel_values"]
                    if version is None:
                        channels.pop("schema_version")
                    else:
                        channels["schema_version"] = version
                    saver.put(saved.config, saved.checkpoint, saved.metadata, {})
                    before = list(saver.list(config))
                old_bytes = self.checkpoint.read_bytes()
                command = ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"]
                worker = Mock(side_effect=AssertionError("old checkpoint dispatched a worker"))
                live_run = LiveRun(self.github, FakeWorkspace(self.cwd))
                with (contextlib.chdir(self.cwd.parent),
                      patch.dict(os.environ, XDG_STATE_HOME=str(state_home)),
                      patch("foreman.cli.Path.home", return_value=self.cwd / "unused-home"),
                      contextlib.redirect_stderr(io.StringIO()) as stderr):
                    code = main(command + ["--answer", "Option A"], live_run=live_run, worker_runner=worker)
                self.assertEqual(code, 1)
                message = stderr.getvalue()
                self.assertIn(refusal("example/engine#8", version), message)
                worker.assert_not_called()
                self.assertEqual(self.checkpoint.read_bytes(), old_bytes)
                other_command = ["run", "delivery", "--repo", "Example/Engine", "--issue", "9"]
                replies = iter([{"triage": "question"}, {"research": "Other answer"}])
                self.github.ready_again(9)
                with (contextlib.chdir(self.cwd.parent),
                      patch.dict(os.environ, XDG_STATE_HOME=str(state_home)),
                      contextlib.redirect_stdout(io.StringIO()) as stdout):
                    code = main(other_command, live_run=live_run,
                                worker_runner=lambda args, cwd, env: worker_reply(args, checker_reply(args) or next(replies)))
                self.assertEqual(code, 0)
                self.assertIn("done answered", stdout.getvalue())
                with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                    saver.serde = checkpoint_serializer()
                    self.assertEqual(list(saver.list(config)), before)
                    other = saver.get({"configurable": {"thread_id": "example/engine#9"}})
                    self.assertEqual(other["channel_values"]["run"].issue, 9)
                old_bytes = self.checkpoint.read_bytes()
                fresh_home = self.cwd.parent / f"fresh-{version}"
                fresh_checkpoint = fresh_home / "foreman" / "checkpoints.sqlite3"
                replies = iter([{"triage": "question"}, {"research": "Fresh answer"}])
                self.github.ready_again(8)
                with (patch.dict(os.environ, XDG_STATE_HOME=str(fresh_home)),
                      contextlib.redirect_stdout(io.StringIO()) as stdout):
                    code = main(command, live_run=live_run,
                                worker_runner=lambda args, cwd, env: worker_reply(args, checker_reply(args) or next(replies)))
                self.assertEqual(code, 0)
                self.assertIn("done answered", stdout.getvalue())
                self.assertTrue(fresh_checkpoint.is_file())
                with SqliteSaver.from_conn_string(str(fresh_checkpoint)) as saver:
                    saver.serde = checkpoint_serializer()
                    fresh = saver.get(config)["channel_values"]
                    self.assertEqual(fresh["schema_version"], SCHEMA_VERSION)
                    self.assertEqual(fresh["run"].issue, 8)
                self.assertEqual(self.checkpoint.read_bytes(), old_bytes)

    def test_queue_receipt_is_persisted_and_completion_does_not_reobserve(self):
        result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        receipt = result["merge"].receipt
        self.assertEqual((receipt["source_head"], receipt["identity"]), (HEAD, "queue-test"))
        with patch.object(self.github, "readiness", side_effect=AssertionError("completed run reobserved")):
            resumed = self.walk([])
        assert_channels(self, resumed, result)

    def test_saved_receipt_reconciles_before_readiness_without_reenqueue(self):
        for status in ["missing", "merged"]:
            self.checkpoint = self.cwd.parent / (status + ".sqlite3")
            self.github.merge_calls, self.github.queue_merges = [], status == "merged"
            ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
            with patch.object(self.github, "readiness", side_effect=[ready, ForgeError("interrupted readback")]):
                first = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            self.assertEqual(first["stop"], "readiness_unavailable")
            count = len(self.calls)
            receipt = MergeReceipt.model_validate(first["merge"].receipt)
            if status == "merged":
                with patch.object(self.github, "arm_auto_merge", side_effect=AssertionError("re-enqueued")), patch.object(
                        self.github, "reconcile_merge", return_value=receipt.model_copy(update={"status": "MERGED"})) as read:
                    result = self.walk([])
                read.assert_called_once_with(receipt)
                self.assertEqual((result["stop"], len(self.calls)), ("merged", count))
                continue
            # A vanished queue entry is a rejection: the run repairs, then arms the repaired head once.
            rejected = QueueRejected(f"Queue receipt {receipt.identity} disappeared")
            with patch.object(self.github, "reconcile_merge", side_effect=[rejected, receipt]) as read:
                result = self.walk([{"implementation": "built"}, APPROVE])
            self.assertEqual(read.call_args_list[0].args, (receipt,))
            # The repair calls Implement, Verify for the repaired head, and Review.
            self.assertEqual((result["stop"], len(self.calls)), ("merge_pending", count + 3))
            self.assertIn(receipt.identity, json.loads(self.prompts[-3].split("Delivery state:\n")[1])["checks"]["failure_output"])
            self.assertEqual(len(self.github.merge_calls), 2)
        self.github.queue_merges = True

    def test_changed_saved_handoff_reads_github_again_before_arming(self):
        self.github.queue_merges = False
        ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        with patch.object(self.github, "readiness", side_effect=[ready, ForgeError("interrupted readback")]):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        with patch.object(self.github, "reconcile_merge", side_effect=HandoffChanged("PR head changed; resume to renew review")):
            result = self.walk([])
        # The stale receipt is dropped, and a fresh observation arms through GitHub's existing-entry check.
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual(len(self.github.merge_calls), 2)
        self.assertIn("PR head changed", result["halt_detail"])
        self.github.queue_merges = True
        self.assertEqual(self.walk([])["stop"], "merged")

    def test_fresh_process_reconciles_crash_after_write_or_merge(self):
        script = r"""
import json, os, sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from foreman.cli import main
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun
from foreman.github import LiveForge
from foreman.readiness import QueueSnapshot
from test_runner import APPROVE, BASE, BRANCH, HEAD, FakeGitHub, FakeWorkspace, worker_reply

class Forge(FakeGitHub):
    arm_auto_merge = LiveForge.arm_auto_merge
    _arm_auto_merge = LiveForge._arm_auto_merge
    reconcile_merge = LiveForge.reconcile_merge
    def readiness(self, *args):
        value = super().readiness(*args)
        if Path("merges.log").exists() and sys.argv[3] == "merged":
            value.pr.state, value.pr.merged = "closed", True
        return value
    def _queue_snapshot(self, repo, number):
        written = Path("merges.log").exists()
        return QueueSnapshot(number=12, state="MERGED" if written and sys.argv[3] == "merged" else "OPEN",
            headRefOid=HEAD, baseRefOid=BASE, baseRefName="main", isDraft=False, autoMergeRequest=None,
            mergeQueueEntry={"id": "queue-crash", "state": "QUEUED"} if written else None)
    def _run_command(self, repo, command):
        assert command[1:3] == ["pr", "merge"]
        with Path("merges.log").open("a") as log:
            log.write("merge\n")
        os._exit(17)

workspace = FakeWorkspace(Path.cwd())
def worker(args, cwd, env):
    schema = json.loads(Path(args[args.index("--output-schema")+1]).read_text()) if args[0] == "codex" else json.loads(args[args.index("--json-schema")+1])
    with Path("workers.log").open("a") as log:
        log.write(schema["title"] + "\n")
    if schema["title"] == "TriageOutput":
        return worker_reply(args, {"triage": "supplied_slice"})
    if schema["title"] == "ImplementOutput":
        return workspace.reply(args, {"implementation": "built"})
    return worker_reply(args, APPROVE)
pin = SkillPin.model_validate_json(sys.argv[2])
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
    sys.exit(main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                  checkpoint=Path(sys.argv[1]), worker_runner=worker, live_run=LiveRun(Forge(), workspace)))
"""
        bootstrap = f"import sys; sys.path[:0] = {[str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]!r}\n"
        for mode in ["queued", "merged"]:
            directory = self.cwd / mode
            directory.mkdir()
            command = [sys.executable, "-c", bootstrap + script, str(self.cwd.parent / f"{mode}.sqlite3"), self.pin.model_dump_json(), mode]
            first = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            self.assertEqual(first.returncode, 17, first.stderr)
            resumed = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            # The adopted arm stays pending until GitHub reports the merge.
            self.assertEqual(resumed.returncode, 0 if mode == "merged" else 75, resumed.stderr)
            self.assertIn("done merged" if mode == "merged" else "paused merge_pending", resumed.stdout)
            self.assertEqual((directory / "merges.log").read_text(), "merge\n")
            self.assertEqual((directory / "workers.log").read_text().splitlines(),
                             ["TriageOutput", "SpecCheckOutput", "ImplementOutput", "VerifyOutput", "ReviewOutput"])

    def test_queued_pull_request_is_pending_until_github_reports_the_merge(self):
        self.github.queue_merges = False
        result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        # A queue receipt alone is not delivery: the run waits at observe without credit.
        self.assertEqual((result["stop"], result["merge"].status, run_exit_code(result)), ("merge_pending", "armed", 75))
        self.assertIn("QUEUED", result["halt_detail"])
        self.github.base = "c" * 40
        # The queue tests the head on top of the newer base, so the moved base does not reopen review.
        self.assertEqual(self.walk([])["stop"], "merge_pending")
        self.github.queue_merges = True
        merged = self.walk([])
        self.assertEqual((merged["stop"], merged["merge"].status, merged["merge"].merge_commit_sha), ("merged", "merged", MERGE_COMMIT))
        self.assertEqual(len(self.github.reviews), 1)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(run_exit_code(merged), 0)

    def test_merge_of_an_unreviewed_head_earns_no_credit_or_release(self):
        self.github.parents[("Example/Engine", 8)] = 7
        merged = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        merged.pr.state, merged.pr.merged, merged.pr.head.sha = "closed", True, "c" * 40
        readings = iter([self.github.readiness("Example/Engine", 8, BRANCH, HEAD)])
        with patch.object(self.github, "readiness", side_effect=lambda *args: next(readings, merged)):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual((result["stop"], result["merge"].observed_head), ("merged_unreviewed", "c" * 40))
        self.assertIn("No delivery credit or dependent release", result["halt_detail"])
        self.assertEqual(self.github.release_labels(), [])
        # The merge cannot be undone by a retry, so the run ends. It files no issue and writes only to its own issue and pull request.
        self.assertEqual(run_exit_code(result), 1)
        self.assertEqual((self.github.created, self.github.comments), ([], {}))
        self.assertEqual({(repo, number) for repo, number, *_ in self.github.label_calls}, {("Example/Engine", 8)})
        self.assertEqual(set(self.github.status), {("Example/Engine", 8), ("Example/Engine", 12)})
        # The comment main 1bb930c6 wrote for this halt, less its Watch line (#449), with the fixture's skill pin.
        before = (Path(__file__).parent / "fixtures/status/halted-merged-unreviewed.md").read_text()
        expected = before.replace("Watch: bookout-labs/tools#101\n\n", "").replace("<pin12>", self.pin.commit[:12]).replace("<pin>", self.pin.commit)
        self.assertEqual(self.github.status["Example/Engine", 8], expected)

    def test_open_dependents_are_released_once_after_rejection_repair_and_matching_merge(self):
        # ME3: this issue blocks a Ready dependent, one also blocked by an open sibling, and one already closed.
        other = self.github.create_issue("Example/Engine", "Other", "Still open.", ["slice"], marker="")
        titles = {8: EMPTY_SLICE["title"], other: "Other"}
        planned = lambda title, *numbers: slice_body({**EMPTY_SLICE, "title": title, "depends_on": [titles[number] for number in numbers],
                                                      "blocker_numbers": list(numbers)})
        dependent = self.github.create_issue("Example/Engine", "Dependent", planned("Dependent", 8), ["slice", "Ready"], marker="")
        waiting = self.github.create_issue("Example/Engine", "Waiting", planned("Waiting", 8, other), ["slice", "Ready"], marker="")
        done = self.github.create_issue("Example/Engine", "Done", planned("Done", 8), ["slice", "Ready"], marker="")
        self.github.closed.add(done)
        self.github.dependencies = {dependent: [8], waiting: [8, other], done: [8]}
        self.github.queue_merges = False
        rejected = []

        def reconcile(receipt):
            # The queue removes the entry once, on the wake after the duplicate.
            if rejected == [True]:
                rejected.append(False)
                raise QueueRejected(f"PR #12 head={receipt.source_head}: the merge queue removed the entry.")
            return receipt
        with patch.object(self.github, "reconcile_merge", side_effect=reconcile):
            queued = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            self.assertEqual((queued["stop"], queued["merge"].status), ("merge_pending", "armed"))
            # A duplicate wake re-reads GitHub, keeps waiting and releases nothing early.
            waited = self.walk([])
            self.assertEqual(waited["stop"], "merge_pending")
            self.assertNotIn("released", waited)
            rejected.append(True)
            repaired = self.walk([{"implementation": "built"}, APPROVE])
        self.assertEqual(repaired["stop"], "merge_pending")
        self.assertNotIn("released", repaired)
        # The repair Implement prompt comes before the new head's Verify and Review.
        repair = json.loads(self.prompts[-3].split("Delivery state:\n")[1])
        self.assertIn("merge queue removed the entry", repair["checks"]["failure_output"])
        self.assertEqual(len(self.github.reviews), 2)
        self.assertEqual(len(self.github.merge_calls), 2)
        self.github.queue_merges = True
        merged = self.walk([])
        self.assertEqual((merged["stop"], merged["merge"].merge_commit_sha), ("merged", MERGE_COMMIT))
        # Release names every open dependent and writes no label. Admission decides each one from its own prerequisites.
        names = [f"Example/Engine#{dependent}", f"Example/Engine#{waiting}"]
        self.assertEqual(merged["released"], names)
        # A replayed wake after the merge names the same dependents again.
        replayed = self.walk([])
        self.assertEqual((replayed["stop"], replayed["released"]), ("merged", names))
        # The host starts each named issue. Admission admits the dependent once and keeps the waiting one blocked.
        self.github.merged.add(8)
        fake = FakeWorkspace(self.cwd)
        fake.remote = self.github.branches
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.enterContext(patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(self.cwd / "runtime"), "FOREMAN_APP_ID": "1"}))
        self.enterContext(patch("foreman.cli.credentials.environment", return_value={"GH_TOKEN": "synthetic"}))

        # The dependent's run reads its own repository's settings at its own start and inherits nothing from this run.
        self.github.settings = {"FOREMAN_HUMAN_LOGIN": "fixture-owner", "FOREMAN_MAX_TOKENS": "900000", "FOREMAN_MAX_BUDGET_USD": "7"}
        results = []

        def start(name):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(["start", "--repo", "Example/Engine", "--issue", name.split("#")[1]], checkpoint=self.checkpoint,
                            live_run=LiveRun(self.github, fake), report=results.append,
                            worker_runner=lambda args, cwd, env: fake.reply(args, checker_reply(args) or next(replies)))
            return code, out.getvalue()
        admitted, blocked = start(names[0]), start(names[1])
        self.assertEqual(admitted[0], 0, admitted[1])
        self.assertEqual((merged["start"].max_tokens, merged["start"].max_budget_usd), (20_000_000, "3"))
        self.assertEqual((results[0]["run"].issue, results[0]["start"].max_tokens, results[0]["start"].max_budget_usd), (dependent, 900000, "7"))
        self.assertIn("done merged", admitted[1])
        self.assertEqual(blocked[0], 1)
        self.assertIn(f"done blocked\nOpen blockers: https://github.com/Example/Engine/issues/{other}", blocked[1])
        self.assertEqual(start(names[0]), (0, f"already started example/engine#{dependent}\n"))
        self.assertEqual(next(replies, None), None)
        # Each run labels only its own issue, and release labels none: the blocked dependent keeps Ready.
        self.assertEqual({number for _, number, _, _ in self.github.label_calls}, {8, dependent})
        self.assertEqual(self.github.load_issue("Example/Engine", waiting)["labels"], ["slice", "Ready"])

    def test_a_merge_wake_reads_no_setting_and_goes_on_to_confirm(self):
        """ME10: a run waiting on the merge queue, the operator sets a cap that won't parse, and GitHub then reports the
        merge. The wake reads no login or cap variable, and the run goes on to confirm."""
        self.github.queue_merges = False
        queued = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual((queued["stop"], queued["merge"].status), ("merge_pending", "armed"))
        reads = list(self.github.variable_reads)
        self.github.settings["FOREMAN_MAX_BUDGET_USD"] = "4.5o"
        self.github.queue_merges = True
        merged = self.walk([])
        self.assertEqual((merged["stop"], merged["merge"].merge_commit_sha), ("merged", MERGE_COMMIT))
        self.assertEqual(self.github.variable_reads, reads)
        self.assertEqual(merged["start"].max_budget_usd, "3")

    def test_each_merge_wake_reads_github_once(self):
        # A wake restarts the waiting stage, so it never replays the reads of earlier wakes.
        for step in ("arm", "observe"):
            with self.subTest(step=step), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.github.merge_calls = Path(directory) / "runs.sqlite3", []
                self.github.queue_merges = False
                observe = self.github.readiness
                pending = (lambda *args: observe(*args).model_copy(update={"outcome": "pending"})) if step == "arm" else observe
                with patch.object(self.github, "readiness", side_effect=pending) as read:
                    first = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
                    self.assertEqual((first["stop"], first.get("merge", MergeState()).status), ("merge_pending", None if step == "arm" else "armed"))
                    reads = []
                    for _ in range(4):
                        before = read.call_count
                        self.assertEqual(self.walk([])["stop"], "merge_pending")
                        reads.append(read.call_count - before)
                self.assertEqual(reads, [1] * 4)
                self.assertEqual(len(self.github.merge_calls), 0 if step == "arm" else 1)
        self.github.queue_merges = True

    def test_unverified_head_answer_is_kept_when_the_head_is_restored(self):
        observe, foreign, restored = self.github.readiness, "c" * 40, []
        self.github.foreign_heads.add(foreign)

        def read(*args):
            reading = observe(*args)
            if restored:
                return reading
            head = reading.pr.head.model_copy(update={"sha": foreign})
            return reading.model_copy(update={"outcome": "head_changed", "pr": reading.pr.model_copy(update={"head": head})})
        with patch.object(self.github, "readiness", side_effect=read):
            self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "needs_human")
            restored.append(True)
            result = self.walk([], answer="I restored the head.")
        # The answer is saved before GitHub is read again, so restoring the head keeps the evidence.
        self.assertEqual(result["stop"], "merged")
        self.assertEqual([(saved["stage"], saved["answer"]) for saved in result["plan"].human_qa], [("merge", "I restored the head.")])
        self.assertEqual(len(self.github.reviews), 1)

    def test_conflict_on_a_moved_base_repairs_without_an_extra_review(self):
        observe = self.github.readiness

        def conflict(*args):
            reading = observe(*args)
            moved = reading.pr.model_copy(update={"base": reading.pr.base.model_copy(update={"sha": "c" * 40})})
            return reading.model_copy(update={"outcome": "conflict", "pr": moved})
        readings = iter([conflict])
        with patch.object(self.github, "readiness", side_effect=lambda *args: next(readings, observe)(*args)):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["stop"], "merged")
        # The repair Implement prompt comes before the new head's Verify and Review.
        self.assertIn(": conflict;", json.loads(self.prompts[-3].split("Delivery state:\n")[1])["checks"]["failure_output"])
        self.assertEqual(len(self.github.reviews), 2)

    def test_release_names_dependents_in_other_repositories_and_admission_decides(self):
        local = self.github.create_issue("Example/Engine", "Local", "No slice record.", ["slice"], marker="")
        self.github.dependencies = {local: [8]}
        listed = self.github.blocking
        foreign = IssueBlocker(number=3, title="Dependent", repository_url="https://api.github.com/repos/Example/Other",
                               html_url="https://github.com/Example/Other/issues/3", state="open")
        with patch.object(self.github, "blocking", side_effect=lambda repo, number: [*listed(repo, number), foreign]) as read:
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["stop"], "merged")
        # One read of the issue's blocking list. A broken slice is still named, because admission halts it with evidence.
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result["released"], [f"Example/Engine#{local}", "Example/Other#3"])
        self.assertEqual(self.github.release_labels(), [])

    def test_release_read_failure_resumes_and_still_ends_merged(self):
        self.github.dependencies = {101: [8]}
        self.github.create_issue("Example/Engine", "Dependent", "Body", ["slice"], marker="")
        listed = self.github.blocking
        with patch.object(self.github, "blocking", side_effect=[ForgeError("HTTP 503"), *[listed("Example/Engine", 8)] * 2]):
            failed = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            self.assertEqual((failed["stop"], run_exit_code(failed)), ("readiness_unavailable", 75))
            self.assertIn("Dependent release could not read GitHub", failed["halt_detail"])
            resumed = self.walk([])
        self.assertEqual((resumed["stop"], resumed["released"], run_exit_code(resumed)), ("merged", ["Example/Engine#101"], 0))
        self.assertEqual(len(self.github.merge_calls), 1)
        self.assertEqual(len(self.github.reviews), 1)

    def test_prerequisites_merged_together_both_count_before_their_issues_close(self):
        first, second = (self.github.create_issue("Example/Engine", title, slice_body({**EMPTY_SLICE, "title": title}), ["slice"], marker="")
                         for title in ("First", "Second"))
        self.github.bodies[("Example/Engine", 8)] = slice_body(
            {**EMPTY_SLICE, "depends_on": ["First", "Second"], "blocker_numbers": [first, second]})
        self.github.dependencies = {8: [first, second]}
        merged_pulls = set()
        is_merged = self.github.is_merged
        # GitHub reports each closing pull request merged while its issue is still open.
        with patch.object(self.github, "is_merged", side_effect=lambda repo, number, contains=None:
                          number in merged_pulls or is_merged(repo, number, contains=contains)):
            merged_pulls.add(first)
            blocked = self.walk([])
            self.assertEqual(blocked["stop"], "blocked")
            self.assertEqual(blocked["halt_detail"], f"Open blockers: https://github.com/Example/Engine/issues/{second}")
            merged_pulls.add(second)
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["stop"], "merged")

    def test_unverified_head_answer_is_saved_and_never_answers_the_next_question(self):
        observe, foreign = self.github.readiness, "c" * 40
        self.github.foreign_heads.add(foreign)

        def moved(*args):
            reading = observe(*args)
            head = reading.pr.head.model_copy(update={"sha": foreign})
            return reading.model_copy(update={"outcome": "head_changed", "pr": reading.pr.model_copy(update={"head": head})})
        with patch.object(self.github, "readiness", side_effect=moved) as read:
            asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            self.assertEqual(asked["stop"], "needs_human")
            answered = self.walk([], answer="The revision is mine.")
            # Ancestry is still unverified, so the gate stays open with a new question.
            self.assertEqual(answered["stop"], "needs_human")
            self.assertNotEqual(re.search(r"foreman answer \S+", answered["halt_detail"])[0], re.search(r"foreman answer \S+", asked["halt_detail"])[0])
            before = read.call_count
            # The earlier reply never answers the new question, so a later run neither resumes nor reads GitHub.
            self.assertEqual((self.walk([])["stop"], read.call_count), ("needs_human", before))
            cancelled = self.walk([], answer="cancel")
        self.assertEqual(cancelled["stop"], "cancelled")
        self.assertEqual([(saved["stage"], saved["answer"]) for saved in cancelled["plan"].human_qa],
                         [("merge", "The revision is mine."), ("merge", "cancel")])
        self.assertFalse(self.github.merge_calls)
        self.assertEqual(self.github.pr_closes, [("Example/Engine", 12, CANCELLATION)])

    def pause_after(self, stage, replies):
        """Walk the run until the stage ends, so the run waits between stages."""
        def compile(*args, **kwargs):
            graph = compile_delivery(*args, **kwargs)
            graph.interrupt_after_nodes = [stage]
            return graph
        with patch("foreman.runner.compile_delivery", side_effect=compile):
            return self.walk(replies)

    def test_a_stop_from_the_human_ends_the_run_cancelled_at_the_next_stage_entry(self):
        """TR6: the next stage entry reads the human's stop and ends the run before any paid call or write. Another login's
        comment and a first line other than foreman stop change nothing."""
        built = [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]
        for labels, stage, entry, replies in (
                (["Ready"], "triage", "research", [{"triage": "question"}]), (["Ready"], "triage", "spec", [{"triage": "plan"}]),
                (None, "triage", "spec", built), (None, "spec", "prepare", built), (None, "implement", "verify", built),
                (None, "merge", "merge", built)):
            with self.subTest(labels=labels, after=stage), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.github = Path(directory) / "runs.sqlite3", FakeGitHub(labels)
                if stage == "merge":
                    # The queue holds the armed PR, so the run waits for GitHub's merge.
                    self.github.queue_merges = False
                    self.assertEqual(self.walk(replies)["stop"], "merge_pending")
                else:
                    self.pause_after(stage, replies)
                calls, published = len(self.calls), bool(self.github.opened)
                self.github.ruling("foreman stop", login="someone-else")
                self.github.ruling("foreman stop " + "a" * 64)
                self.github.ruling("  foreman stop\r\nThe issue is no longer needed.")
                result = self.walk([])
                self.assertEqual((result["stop"], result.get("halt"), result["path"][-1]), ("cancelled", None, entry))
                self.assertEqual(len(self.calls), calls)
                self.assertEqual(self.github.pr_closes, [("Example/Engine", 12, CANCELLATION)] if published else [])
                self.assertEqual(len(self.github.merge_calls), stage == "merge")
        # Verify's stop is not a stage result the run can wait on, so review.inspect reads a stop posted during verify.
        github = FakeGitHub()
        github.open_pull_request("Example/Engine", BRANCH, "main", "Change", "Closes #8")
        github.ruling("foreman stop")
        stage = StageRun(workspace=None, forge=github, check_log=None, max_rounds=3, start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), dispatch=None, apply=None)
        state = {"run": RunState(repo="Example/Engine", issue=8), "work": WorkState(branch=BRANCH), "review": ReviewState(),
                 "checks": ChecksState(verified_head=HEAD), "plan": PlanState(), "path": []}
        self.assertEqual(review.inspect(state, stage), {"stop": "cancelled", "path": ["review"]})
        self.assertEqual(github.pr_closes, [("Example/Engine", 12, CANCELLATION)])
        self.checkpoint, self.github = self.cwd.parent / "others.sqlite3", FakeGitHub()
        self.pause_after("implement", built)
        self.github.ruling("foreman stop", login="someone-else")
        self.github.ruling("foreman stop " + "a" * 64)
        self.assertEqual((self.walk([APPROVE])["stop"], self.github.pr_closes), ("merged", []))

    def test_a_cancellation_outside_merge_leaves_a_merged_pull_request_and_ends_cancelled(self):
        """Only merge records a stop as too late. Elsewhere a stop or a gate's cancel answer leaves a PR that GitHub merged
        alone and still ends the run cancelled, before any worker call."""
        github = FakeGitHub()
        github.open_pull_request("Example/Engine", BRANCH, "main", "Change", "Closes #8")
        github.opened[BRANCH] = github.opened[BRANCH].model_copy(update={"state": "MERGED"})
        github.ruling("foreman stop")
        stage = StageRun(workspace=None, forge=github, check_log=None, max_rounds=3, start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"),
                         dispatch=Mock(side_effect=AssertionError("no worker call")), apply=None)
        state = {"run": RunState(repo="Example/Engine", issue=8), "work": WorkState(branch=BRANCH, published_head=HEAD),
                 "review": ReviewState(), "checks": ChecksState(verified_head=HEAD), "plan": PlanState(), "path": []}
        for step, label in ((implement.prepare, "prepare"), (verify.drive, "verify"), (review.inspect, "review")):
            with self.subTest(label):
                self.assertEqual(step(state, stage), {"stop": "cancelled", "path": [label]})
        github.rulings.clear()
        gate = StageRun(workspace=None, forge=github, check_log=None, max_rounds=3, start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), dispatch=Mock(),
                        apply=Mock(return_value={"human_question": "Which sort order should the repair keep?"}))
        with patch("foreman.steps.interrupt", return_value=GateReply(interrupt="asked", answer="cancel")):
            update, _ = gate.consult(Mock(**{"invoke.return_value": {}}), "review", state, lambda current: [current["checks"].verified_head])
        self.assertEqual((update["stop"], update.get("halt_detail"), update["human_question"]), ("cancelled", None, ""))
        self.assertEqual(github.pr_closes, [])

    def test_a_gate_has_no_route_for_an_unmatched_result(self):
        """CI proves every route value has a route (rules.routing), so consult takes no route and marks no routing defect."""
        self.assertEqual(list(signature(StageRun.consult).parameters), ["self", "graph", "name", "state", "scope", "owned", "answered"])

    def test_admission_and_a_resumed_halt_read_the_stop_first(self):
        """TR6: triage.admit reads the stop before anything else, and a halted run that resumes sees a stop posted meanwhile."""
        self.github.ruling("foreman stop")
        admitted = self.walk([])
        self.assertEqual((admitted["stop"], admitted["path"], admitted["run"].base_sha, self.calls), ("cancelled", ["admit"], "", []))
        self.checkpoint, self.github = self.cwd.parent / "held.sqlite3", FakeGitHub()
        self.github.hold = "Holding merges."
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_held")
        self.github.hold = None
        self.github.ruling("foreman stop")
        resumed = self.walk([])
        self.assertEqual((resumed["stop"], resumed["path"][-1], self.github.merge_calls), ("cancelled", "merge", []))
        self.assertEqual(self.github.pr_closes, [("Example/Engine", 12, CANCELLATION)])

    def test_a_stop_counts_only_after_the_latest_ready_event(self):
        """TR6: a stop counts only when GitHub created it after the issue's latest Ready label event. Removing and re-adding
        Ready spends an old stop, and an old comment edited into a stop is no new one. A stop after that event still cancels."""
        self.github.ruling("foreman stop")
        self.github.ruling("Looks fine.")
        self.assertEqual(self.walk([])["stop"], "cancelled")
        self.github.ready_events.append(self.github.now())  # The human removes and re-adds Ready.
        edited = self.github.rulings[-1]
        self.github.rulings[-1] = edited.model_copy(update={"body": "foreman stop"})  # GitHub keeps its created time.
        self.checkpoint, self.github.queue_merges = self.cwd.parent / "ready-again.sqlite3", False
        waiting = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual((waiting["stop"], waiting["path"][-1], self.github.pr_closes), ("merge_pending", "merge", []))
        self.github.ruling("foreman stop")
        stopped = self.walk([])
        self.assertEqual((stopped["stop"], self.github.pr_closes), ("cancelled", [("Example/Engine", 12, CANCELLATION)]))

    def test_a_failed_stop_read_or_close_stops_with_the_stage_github_halt(self):
        """A stop read or PR close that GitHub fails stops with git_failed, and with readiness_unavailable in merge."""
        for stage, failing, halt in (("implement", "read", "git_failed"), ("implement", "close", "git_failed"),
                                     ("merge", "read", "readiness_unavailable"), ("merge", "close", "readiness_unavailable")):
            with self.subTest(after=stage, failing=failing), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.github = Path(directory) / "runs.sqlite3", FakeGitHub()
                replies = [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]
                if stage == "merge":
                    self.github.queue_merges = False
                    self.walk(replies)
                else:
                    self.pause_after(stage, replies)
                self.github.ruling("foreman stop")
                if failing == "close":
                    self.github.fail_next("close_pull_request")
                else:
                    self.github.issue_comment_records = Mock(side_effect=ForgeError("GitHub is down."))
                result = self.walk([])
                self.assertEqual((result.get("halt"), result["stop"], self.github.pr_closes), (halt, halt, []))
                self.assertIn("GitHub could not settle the cancellation", result["halt_detail"])

    def test_pull_request_closed_without_merge_exits_closed(self):
        self.github.parents[("Example/Engine", 8)] = 7
        self.github.queue_merges = False
        closed = self.github.readiness("Example/Engine", 8, BRANCH, HEAD).model_copy(update={"outcome": "closed"})
        closed.pr.state = "closed"
        readings = iter([self.github.readiness("Example/Engine", 8, BRANCH, HEAD)])
        with patch.object(self.github, "readiness", side_effect=lambda *args: next(readings, closed)):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual((result["stop"], result["merge"].status, run_exit_code(result)), ("closed", "closed", 1))
        self.assertNotIn("halt", result)
        self.assertEqual(result["path"][-1], "merge")
        self.assertEqual(self.github.release_labels(), [])

    def test_review_question_is_bound_and_the_marked_reply_resumes_inspect(self):
        """RV9: the reviewer's question is bound to the verified head and the round's findings, the status comment shows
        its reply marker, and the human's marked reply on the issue resumes review."""
        question = "Should the list include archived items?"
        note = {**FINDING, "severity": "P2"}
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {"findings": [note], "human_question": question}])
        self.assertEqual(asked["stop"], "needs_human")
        marker = re.search(r"foreman answer [0-9a-f-]{71}", asked["halt_detail"])[0]
        self.assertIn(f"Start with {marker}", self.github.status["Example/Engine", 8])
        self.assertEqual(self.github.reviews, [])
        self.github.ruling(f"{marker}\nActive items only.")
        resumed = self.walk([APPROVE])
        self.assertEqual(resumed["stop"], "merged")
        self.assertIn("Active items only.", self.prompts[-1])
        [saved] = resumed["plan"].human_qa
        self.assertEqual((saved["stage"], saved["answer"]), ("review", "Active items only."))
        self.assertEqual(HumanPrompt(stage="review", question=question, revision=saved["revision"]).answer_marker, marker)
        self.assertEqual(resumed["path"].count("implement"), 1)
        self.assertEqual(len(self.github.reviews), 1)

    def test_a_review_reply_bound_to_other_findings_does_not_resume(self):
        question = "Should the list include archived items?"
        first, other = {**FINDING, "severity": "P2"}, {**FINDING, "id": "f2", "severity": "P2", "location": {"side": "head", "path": "items.txt", "line": 1}}
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {"findings": [first], "human_question": question}])
        answered = self.walk([{"findings": [other], "human_question": question}], answer="Active items only.")
        # The same question about other findings on the same head is a new question with its own marker.
        self.assertEqual(answered["stop"], "needs_human")
        earlier = re.search(r"foreman answer \S+", asked["halt_detail"])[0]
        self.assertNotEqual(re.search(r"foreman answer \S+", answered["halt_detail"])[0], earlier)
        self.github.ruling(f"{earlier}\nArchived items too.")
        calls = list(self.calls)
        self.assertEqual(self.walk([])["stop"], "needs_human")
        self.question = ""
        with self.assertRaisesRegex(AnswerError, "needs a matching answer"):
            self.walk([], answer="Archived items too.")
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.github.reviews, [])

    def test_a_review_reply_bound_to_another_head_does_not_resume(self):
        question = "Should the list include archived items?"
        repaired = "c" * 40
        heads = iter([HEAD, repaired])

        class Repairing(FakeWorkspace):
            def commit(self, message):
                super().commit(message)
                self.current = WorktreeState(next(heads), True, self.current.branch)

        workspace = Repairing(self.cwd)
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {"findings": [FINDING], "human_question": question}],
                          workspace=workspace)
        # The answered review repairs its P1, and the reviewer asks the same question about the same finding at the new head.
        again = self.walk([{"findings": [FINDING], "human_question": None}, {"implementation": "built"},
                           {"findings": [FINDING], "human_question": question}], answer="Active items only.", workspace=workspace)
        self.assertEqual(again["stop"], "needs_human")
        self.assertEqual(again["checks"].verified_head, repaired)
        earlier = re.search(r"foreman answer \S+", asked["halt_detail"])[0]
        current = re.search(r"foreman answer \S+", again["halt_detail"])[0]
        self.assertNotEqual(current, earlier)
        self.github.ruling(f"{earlier}\nArchived items too.")
        calls = list(self.calls)
        self.assertEqual(self.walk([], workspace=workspace)["stop"], "needs_human")
        self.assertEqual(self.calls, calls)
        self.github.ruling(f"{current}\ncancel")
        self.assertEqual(self.walk([], workspace=workspace)["stop"], "cancelled")
        self.assertFalse(self.github.merge_calls)

    def test_review_answer_survives_failed_post_and_fresh_process(self):
        script = r"""
import json, sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun
from foreman.github import ForgeError
from foreman.runner import run_live_delivery
from foreman.worker import CliWorker
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, checker_reply

phase, pin, detail, checkpoint = sys.argv[1:]
answer = "Active items only."
question = "Should the list include archived items?"
forge = FakeGitHub()
workspace = FakeWorkspace(Path.cwd())
replies = iter({
    "initial": [{"triage": "supplied_slice"}, {"implementation": "built"}, {**APPROVE, "human_question": question}],
    "answer": [APPROVE],
    "resume": [],
    "cancel": [],
}[phase])
if phase in ("answer", "cancel"):
    # The coordinator relays the human's reply under the marker the question posted.
    forge.answer(detail, answer if phase == "answer" else "cancel")

def worker(args, cwd, env):
    if phase == "answer":
        assert answer in args[-1]
    return workspace.reply(args, checker_reply(args) or next(replies))

def unavailable(*args, **kwargs):
    raise ForgeError("Review post temporarily unavailable")

if phase == "answer":
    forge.post_review = unavailable
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=SkillPin.model_validate_json(pin), matt_skills=SkillPin.model_validate_json(pin).model_copy(update={"directory": "skills"}))):
    result = run_live_delivery(
        repo="Example/Engine", issue=8, checkpoint=Path(checkpoint),
        run=LiveRun(forge, workspace), dispatcher=CliWorker(workspace.root, worker, scratch_root=Path(checkpoint).parent),
        answer=answer if phase in ("answer", "resume") else None,
    )
print(json.dumps({"stop": result["stop"], "qa": result["plan"].human_qa, "path": result["path"], "detail": result.get("halt_detail", ""),
                  "answers": result.get("human_answers"), "rulings": result["review"].settled_rulings}))
"""
        paths = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
        bootstrap = f"import sys; sys.path[:0] = {paths!r}\n"

        def invoke(phase, detail="", checkpoint="review.sqlite3"):
            process = subprocess.run([sys.executable, "-c", bootstrap + script, phase, self.pin.model_dump_json(), detail, str(self.cwd.parent / checkpoint)],
                                     cwd=self.cwd, text=True, capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)

        asked = invoke("initial")
        self.assertEqual(asked["stop"], "needs_human")
        interrupted = invoke("answer", asked["detail"])
        self.assertEqual(interrupted["stop"], "review_not_posted")
        resumed = invoke("resume")
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual(resumed["answers"], ["Active items only."])
        [saved] = resumed["qa"]
        self.assertEqual(saved, {"stage": "review", "question": "Should the list include archived items?",
                                 "answer": "Active items only.", "revision": saved["revision"]})
        self.assertIn(HumanPrompt(stage="review", question=saved["question"], revision=saved["revision"]).answer_marker, asked["detail"])
        self.assertEqual(resumed["rulings"], interrupted["rulings"])
        self.assertEqual(resumed["path"].count("implement"), 1)
        # The human's reply under the marker can cancel instead.
        asked = invoke("initial", checkpoint="cancel.sqlite3")
        self.assertEqual(invoke("cancel", asked["detail"], "cancel.sqlite3")["stop"], "cancelled")
    def test_unavailable_and_policy_outcomes_fail_closed(self):
        ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        for index, sequence in enumerate([[ForgeError("HTTP 503")], [ready, ForgeError("HTTP 503")],
                                          [ready.model_copy(update={"outcome": "policy"})]]):
            self.checkpoint = self.cwd.parent / f"unavailable-{index}.sqlite3"
            with patch.object(self.github, "readiness", side_effect=sequence):
                result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
            self.assertEqual((result["stop"], run_exit_code(result)), ("readiness_unavailable", 75))
            self.assertEqual(result["merge"].status, "armed" if index == 1 else "not_armed")

    def test_receipt_must_match_comment_and_verified_head(self):
        for state, commit in (("COMMENTED", "d" * 40), ("APPROVED", HEAD)):
            with self.subTest(state=state, commit=commit), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint = Path(directory) / "run.sqlite3"
                receipt = {"id": 1, "state": state, "commit_id": commit}
                with patch.object(self.github, "post_review", return_value=receipt):
                    result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
                self.assertEqual(result["stop"], "review_not_posted")
                self.assertIsNone(result["review"].review_receipt)
                self.assertFalse(self.github.merge_calls)

    def test_owned_moved_head_is_fetched_repaired_and_reviewed_again(self):
        workspace = FakeWorkspace(self.cwd)
        moved = "d" * 40
        self.github.queue_merges = False
        observe = self.github.readiness
        remote = observe("Example/Engine", 8, BRANCH, HEAD).model_copy(deep=True)
        remote.outcome, remote.pr.head.sha = "head_changed", moved

        def read(repo, issue, branch, sha):
            if sha == HEAD and self.github.merge_calls:
                # Someone pushed a descendant of the armed head, which GitHub dequeues.
                self.github.queue_merges = True
                return remote
            return observe(repo, issue, branch, sha)

        def refreshed(branch, head):
            workspace.current = WorktreeState(head, True, branch)
            self.github.branches[BRANCH] = head
        original_commit = workspace.commit
        # The repair commits on the fetched head, so its tree is checked and published at that head.
        commit = lambda message: refreshed(BRANCH, moved) if fetch.called else original_commit(message)
        with patch.object(self.github, "readiness", side_effect=read), patch.object(workspace, "refresh", side_effect=refreshed) as fetch, \
                patch.object(workspace, "commit", side_effect=commit), \
                patch.object(self.github, "descends", wraps=self.github.descends) as lineage:
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE, {"implementation": "renewed"}, APPROVE],
                               workspace=workspace)
        lineage.assert_called_once_with("Example/Engine", HEAD, moved)
        fetch.assert_called_once_with(BRANCH, moved)
        self.assertEqual(result["stop"], "merged", result)
        self.assertEqual((result["checks"].verified_head, result["review"].reviewed_head, result["review"].review_receipt["commit_id"]), (moved,) * 3)
        self.assertEqual([call[3] for call in self.github.merge_calls], [HEAD, moved])

    def test_external_conflict_repair_renews_entire_lifecycle(self):
        workspace = FakeWorkspace(self.cwd)
        observe = self.github.readiness
        readings = iter([observe("Example/Engine", 8, BRANCH, HEAD).model_copy(update={"outcome": "conflict"})])
        repaired = "d" * 40
        original_commit, commits = workspace.commit, []

        def commit(message):
            commits.append(message)
            if len(commits) == 1:
                return original_commit(message)
            workspace.current = WorktreeState(repaired, True, BRANCH)
        with patch.object(self.github, "readiness", side_effect=lambda *args: next(readings, None) or observe(*args)), \
                patch.object(workspace, "commit", side_effect=commit):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE, {"implementation": "renewed"}, APPROVE],
                               workspace=workspace)
        self.assertEqual(result["stop"], "merged")
        # The repair Implement prompt comes before the new head's Verify and Review.
        repair = json.loads(self.prompts[-3].split("Delivery state:\n")[1])
        self.assertIn(": conflict;", repair["checks"]["failure_output"])
        self.assertEqual((result["review"].reviewed_head, result["checks"].verified_head, result["review"].review_receipt["commit_id"]), (repaired,) * 3)
        self.assertEqual(len(self.github.reviews), 2)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, repaired)])

    def test_blocking_findings_reach_the_next_implement_prompt(self):
        built = {"implementation": "Built."}
        advisory = {**FINDING, "id": "advice", "severity": "P2"}
        self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [advisory, FINDING]}, built, APPROVE])
        # Implement, Verify and Review follow the first review.
        state = json.loads(self.prompts[-3].split("Delivery state:\n")[1])
        self.assertEqual(ReviewState.model_validate(state["review"]).findings, [FINDING])

    def test_review_posts_only_changed_lines_inline(self):
        findings = [
            FINDING,
            {**FINDING, "id": "f2", "location": {**FINDING["location"], "line": 1}, "summary": "Untouched context."},
            {**FINDING, "id": "f3", "location": {"side": "head", "path": "README.md", "line": 1}, "summary": "Unchanged dependency."},
        ]
        built = {"implementation": "Built."}
        result = self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": findings}, built, APPROVE])
        self.assertEqual(result["merge"].status, "merged")
        # Triage, spec check, Implement and Verify come before the first Review prompt.
        self.assertIn(f"git diff {BASE}...{HEAD}", self.prompts[4])
        _, _, head, body, comments = self.github.reviews[0]
        self.assertEqual(head, HEAD)
        self.assertIn("head items.txt:1 [P1] Untouched context.", body)
        self.assertIn("head README.md:1 [P1] Unchanged dependency.", body)
        self.assertEqual(len(comments), 1)
        self.assertEqual((comments[0]["path"], comments[0]["line"], comments[0]["side"]), ("items.txt", 2, "RIGHT"))
        self.assertIn(FINDING["evidence"], comments[0]["body"])

    def test_unparsable_review_patch_is_reported_in_the_review(self):
        built = {"implementation": "Built."}
        with patch.object(FakeWorkspace, "review_diff", lambda self, base, head: ReviewDiff(f"git diff {base}...{head}", "@@ -1 +1 @@\n")):
            result = self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]}, built, APPROVE])
        self.assertEqual(result["merge"].status, "merged")
        _, _, _, body, comments = self.github.reviews[0]
        self.assertIn("The review patch could not be parsed, so every finding is listed here instead of inline.", body)
        self.assertIn(f"head items.txt:2 [P1] {FINDING['summary']}", body)
        self.assertEqual(comments, [])

    def test_review_failures_halt_without_merging(self):
        changes = {**APPROVE, "findings": [FINDING]}
        for method, stop, replies in (
            ("review_target", "git_failed", []),
            ("post_review", "review_not_posted", [changes]),
        ):
            with self.subTest(method=method), patch.object(self.github, method, side_effect=ForgeError("Unavailable")):
                self.checkpoint = self.cwd.parent / f"{method}.sqlite3"
                result = self.walk([{"triage": "supplied_slice"}, {"implementation": "Built."}, *replies])
                self.assertEqual(result.get("stop"), stop)
                self.assertEqual(result.get("halt_detail"), "Unavailable")
                if replies:
                    self.assertEqual(result["review"].findings, [FINDING])
                self.assertEqual(self.github.merge_calls, [])

    def test_posting_retry_preserves_round_and_does_not_dispatch(self):
        with patch.object(self.github, "post_review", side_effect=ForgeError("Unavailable")):
            stopped = self.walk([{"triage": "supplied_slice"}, {"implementation": "Built."}, APPROVE])
        self.assertEqual(stopped["halt"], "review_not_posted")
        self.assertEqual(len(stopped["review"].review_findings), 1)
        calls = self.calls.copy()
        result = self.walk([])
        self.assertEqual(self.calls, calls)
        self.assertEqual(len(result["review"].review_findings), 1)
        self.assertEqual(result["review"].review_receipt["state"], "COMMENTED")
        self.assertEqual(result["merge"].status, "merged")

    def test_review_halts_when_git_fetch_times_out(self):
        real_run = subprocess.run
        def fetch(command, **kwargs):
            if command[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(command, 30)
            return real_run(command, **kwargs)

        workspace = FakeWorkspace(self.cwd)
        workspace.review_diff = lambda base, head: LiveWorkspace(self.cwd).review_diff(base, head)
        with patch("foreman.workspace.subprocess.run", side_effect=fetch):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "Built."}], workspace=workspace)
        self.assertEqual(result.get("stop"), "git_failed")
        self.assertRegex(result.get("halt_detail", ""), r"^git fetch timed out after 30 seconds")
        self.assertEqual(self.github.reviews, [])
        self.assertEqual(self.github.merge_calls, [])

    def test_load_slice_issue(self):
        result = self.walk([{"triage": "question"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["run"].request, "Issue title\n\n" + slice_body(EMPTY_SLICE))
        self.assertEqual(result["plan"].issue_kind, "slice")
        self.assertEqual({k: v for k, v in result["plan"].slice.items() if k != "provenance"},
                         {**EMPTY_SLICE, "depends_on": [], "blocker_numbers": []})
        self.assertEqual(result["path"][:4], ["triage", "spec_check", "prepare", "implement"])

    def test_missing_or_invalid_slice_stops_before_triage(self):
        for body in ("Issue body", "Issue body\n\n```json\n{\n```", slice_body({"screen": ""})):
            with self.subTest(body=body), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.github.body = body
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([])
                self.assertEqual(result["stop"], "missing_slice")
                self.assertEqual(self.calls, [])
                self.assertEqual(self.walk([])["stop"], "missing_slice")

    def test_missing_inactive_method_halts_the_cli_before_loading_the_issue(self):
        pin = fixture_pin(self.cwd / "incomplete")
        source = Path(pin.repository)
        absent = "benchmark-checklist/SKILL.md"
        (source / "pstack" / "skills" / absent).unlink()
        git(source, "add", "--", "pstack")
        git(source, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
            "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Synthetic skill fixture")
        commit = git(source, "rev-parse", "HEAD")
        config = replace(load_config(), pstack=pin.model_copy(update={"commit": commit}), matt_skills=pin.model_copy(update={"directory": "skills", "commit": commit}))
        worker = Mock()
        with (
            patch("foreman.runner.load_config", return_value=config),
            patch.object(self.github, "load_issue") as load_issue,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                worker_runner=worker, checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 1)
        self.assertIn("done skill_missing", stdout.getvalue())
        self.assertIn(f"method missing at pin ({absent})", stdout.getvalue())
        self.assertIn(commit, stdout.getvalue())
        # Only the status mirror reads the issue, after the halt.
        load_issue.assert_called_once_with("Example/Engine", 8)
        worker.assert_not_called()

    def test_empty_method_halts_before_dispatch(self):
        for method, content, detail in (
            ("architect/SKILL.md", "", "method is empty"),
            ("why/SKILL.md", "", "method is empty"),
        ):
            with self.subTest(method=method, detail=detail), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                pin = fixture_pin(Path(directory) / "fixture")
                source = Path(pin.repository)
                (source / "pstack" / "skills" / method).write_text(content, encoding="utf-8")
                git(source, "add", "--", "pstack")
                git(source, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
                    "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Synthetic invalid method")
                commit = git(source, "rev-parse", "HEAD")
                config = replace(load_config(), pstack=pin.model_copy(update={"commit": commit}), matt_skills=pin.model_copy(update={"directory": "skills", "commit": commit}))
                with patch("foreman.runner.load_config", return_value=config), patch.object(self.github, "load_issue") as load_issue:
                    result = self.walk([])
                self.assertEqual(result.get("stop", ""), "skill_missing")
                self.assertEqual(run_exit_code(result), 1)
                self.assertEqual(result.get("skill_reason", ""), "method_missing")
                self.assertIn(method, result.get("halt_detail", ""))
                self.assertIn(commit, result.get("halt_detail", ""))
                self.assertIn(detail, result.get("halt_detail", ""))
                self.assertEqual(self.calls, [])
                # Only the status mirror reads the issue, after the halt.
                load_issue.assert_called_once_with("Example/Engine", 8)

    def test_unavailable_pin_has_a_distinct_reason(self):
        commit = "f" * 40
        pin = self.pin.model_copy(update={"commit": commit})
        with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
            result = self.walk([], preflight="exit 9")
        self.assertEqual(result["checks"], ChecksState(preflight="exit 9"))
        self.assertEqual(result["stop"], "skill_missing")
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})
        self.assertEqual(run_exit_code(result), 1)
        self.assertEqual(result["skill_reason"], "pin_unavailable")
        self.assertEqual(result["halt_detail"], f"pstack pin unavailable, commit {commit}")
        self.assertEqual(self.calls, [])

    def test_missing_pin_on_resume_preserves_checks(self):
        self.github.hold = "Maintenance"
        halted = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE], preflight="exit 0")
        self.assertEqual(halted["stop"], "merge_held")
        result = self.walk([], skills=Mock(side_effect=SkillMissing(self.pin)))
        assert_channels(self, result, halted, names={"run", "checks", "plan", "review"})
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            saved = saver.get_tuple({"configurable": {"thread_id": "example/engine#8"}})
        assert_channels(self, saved.checkpoint["channel_values"], halted)

    def test_worker_unavailable_returns_the_resumable_cli_exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                worker_runner=lambda args, cwd, env: priced_quota(args),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 75)
        self.assertIn("done worker_unavailable", stdout.getvalue())
        self.assertIn("No qualified worker available for triage.", stdout.getvalue())

    def test_resume_passes_checkpoint_pin_to_the_run_skill_source(self):
        self.github.labels = ["Ready"]
        source = FixtureSkills()
        paused = self.walk([{"triage": "needs_human", "human_question": "Which items?"}], skills=source)
        self.assertEqual(paused["stop"], "needs_human")
        # An admitted run keeps its checkpoint, so resuming it never needs Ready on the live issue.
        self.github.labels = []
        changed = self.pin.model_copy(update={"commit": "f" * 40})
        with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=changed, matt_skills=changed.model_copy(update={"directory": "skills"}))):
            resumed = self.walk([{"triage": "question"}, {"research": "Only open items."}],
                                answer="Only open items", skills=source)
        self.assertEqual(resumed["stop"], "answered")
        self.assertEqual(source.pins, [self.pin, self.pin])

    def assert_methods_carried(self, prompts):
        """Every prompt whose stage lists a skill for all work carries the run's methods (rules.skills)."""
        texts = {name: stage_text(name) for name, stage in STAGES.items() if stage.prompt}
        listed = {name for name, stage in STAGES.items() if any(rule.when is None and rule.skill.kind != "target_skill" for rule in stage.skills)}
        stages = [max((name for name, text in texts.items() if prompt.startswith(text)), key=lambda name: len(texts[name])) for prompt in prompts]
        carried = [stage for stage, prompt in zip(stages, prompts) if stage in listed]
        self.assertTrue(carried, stages)
        self.assertEqual([stage for stage, prompt in zip(stages, prompts) if stage in listed and "Apply the methods listed below." in prompt], carried)

    def test_calls_after_a_gate_answer_carry_the_pinned_methods(self):
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"},
                           {**APPROVE, "human_question": "Should the list include archived items?"}])
        self.assertEqual(asked["stop"], "needs_human")
        before = len(self.prompts)
        self.assertEqual(self.walk([APPROVE], answer="Active items only.")["stop"], "merged")
        self.assert_methods_carried(self.prompts[before:])

    def test_an_answer_whose_skills_cannot_be_exported_leaves_the_run_at_its_question(self):
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"},
                           {**APPROVE, "human_question": "Should the list include archived items?"}])
        failed = self.walk([], answer="Active items only.", skills=Mock(side_effect=SkillMissing(self.pin)))
        self.assertEqual(failed["stop"], "skill_missing")
        # The halt is reported, never saved, so the posted answer resumes the run once the pin can be fetched.
        self.assertEqual(asked["stop"], "needs_human")
        resumed = self.walk([APPROVE])
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual(resumed["human_answers"], ["Active items only."])

    def test_calls_after_a_merge_wake_that_routes_back_carry_the_pinned_methods(self):
        self.github.queue_merges = False
        original = self.github.readiness
        source = FixtureSkills()
        pending = lambda *args: original(*args).model_copy(update={"outcome": "pending", "recovery": "Wait for checks."})
        red = lambda *args: original(*args).model_copy(update={"outcome": "red", "recovery": "Repair."})
        with patch.object(self.github, "readiness", side_effect=pending):
            waiting = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE], skills=source)
        self.assertEqual(waiting["stop"], "merge_pending")
        before = len(self.prompts)
        def routed(*args):
            # The wake finds failed checks, which route the run back to implement. The repaired head then waits for its checks.
            return (red if len(self.prompts) == before else pending)(*args)
        with patch.object(self.github, "readiness", side_effect=routed):
            repaired = self.walk([{"implementation": "built"}, APPROVE], skills=source, wake=True)
        self.assertIn("implement.edit", repaired["steps"][len(waiting["steps"]):])
        self.assert_methods_carried(self.prompts[before:])
        # The woken run exports the pins it started with.
        self.assertEqual(source.pins, [self.pin, self.pin])

    def test_a_stop_wake_exports_no_skills(self):
        source = FixtureSkills()
        asked = self.walk([{"triage": "needs_human", "human_question": "Which items?"}], skills=source)
        self.assertEqual(asked["stop"], "needs_human")
        self.github.ruling("foreman stop")
        self.assertEqual(self.walk([], skills=source, wake=True)["stop"], "cancelled")
        self.assertEqual(source.pins, [self.pin])

    def test_resume_allocates_fresh_scratch_without_checkpointing_paths(self):
        self.github.labels = ["Ready"]
        paths = []

        def quota(args, cwd, env):
            scratch = Path(env["TMPDIR"])
            self.assertFalse(scratch.resolve().is_relative_to(self.cwd.resolve()))
            self.assertNotIn(scratch, paths)
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertTrue(all(not path.exists() for path in paths))
            (scratch / "scratch.txt").write_text("discard on quota")
            paths.append(scratch)
            return priced_quota(args)

        paused = run_live_delivery(
            repo="Example/Engine", issue=8, checkpoint=self.checkpoint,
            dispatcher=CliWorker(self.cwd, quota, scratch_root=self.checkpoint.parent), run=LiveRun(self.github, FakeWorkspace(self.cwd)),
        )
        self.assertEqual(paused["stop"], "worker_unavailable")
        self.assertTrue(all(not path.exists() for path in paths))
        before_resume = len(paths)
        self.assertGreater(before_resume, 1)
        replies = iter([{"triage": "question"}, {"research": "Only open items."}])

        def answer(args, cwd, env):
            quota(args, cwd, env)
            return worker_reply(args, checker_reply(args) or next(replies))

        resumed = run_live_delivery(
            repo="Example/Engine", issue=8, checkpoint=self.checkpoint,
            dispatcher=CliWorker(self.cwd, answer, scratch_root=self.checkpoint.parent), run=LiveRun(self.github, FakeWorkspace(self.cwd)),
        )
        self.assertEqual(resumed["stop"], "answered")
        self.assertGreater(len(paths), before_resume)
        self.assertTrue(all(not path.exists() for path in paths))
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            checkpoints = repr(list(saver.list({"configurable": {"thread_id": "example/engine#8"}})))
        for path in paths:
            self.assertNotIn(str(path), checkpoints)

    def test_worker_unavailable_keeps_attempts_and_ready_resumes_the_same_stage(self):
        def exhaust(node, model, state, instruction):
            if node == "spec_check":
                return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
            if node == "triage":
                return Outcome("ran", {"triage": "supplied_slice", "work_kind": "feature", "human_question": None, "slice": None}, usage=SCRIPTED_USAGE)
            return Outcome("quota", usage=SCRIPTED_USAGE)

        paused = run_live_delivery(
            repo="Example/Engine", issue=8, checkpoint=self.checkpoint,
            dispatcher=exhaust, run=LiveRun(self.github, FakeWorkspace(self.cwd)),
        )
        self.assertEqual(paused["stop"], "worker_unavailable")
        self.assertEqual(run_exit_code(paused), 75)
        self.assertNotIn("done", paused["path"])
        self.assertEqual(paused["pstack_pin"], self.pin.model_dump())
        attempts = paused["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["ran", "ran"] + ["quota"] * 4)
        self.assertTrue(all(a["stage"] == "implement" and a["commit"] == self.pin.commit for a in attempts[2:]))

        resumed = self.walk([{"implementation": "built"}, APPROVE])
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual(resumed["path"].count("triage"), 1)
        self.assertEqual(resumed["attempts"][:len(attempts)], attempts)
        self.assertEqual([a["stage"] for a in resumed["attempts"][len(attempts):]], ["implement", "verify", "review"])

    def test_a_run_keeps_the_release_it_saved_at_its_first_start_when_ready_resumes_it(self):
        def exhaust(node, model, state, instruction):
            if node == "triage":
                return Outcome("ran", {"triage": "supplied_slice", "work_kind": "feature", "human_question": None, "slice": None}, usage=SCRIPTED_USAGE)
            return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE) if node == "spec_check" else Outcome("quota", usage=SCRIPTED_USAGE)

        first, moved = "1" * 40, "2" * 40
        with patch("foreman.runner.release", side_effect=[first, moved]) as release:
            paused = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=self.checkpoint,
                                       dispatcher=exhaust, run=LiveRun(self.github, FakeWorkspace(self.cwd)))
            self.assertEqual((paused["stop"], paused["run"].release), ("worker_unavailable", first))
            resumed = self.walk([{"implementation": "built"}, APPROVE])
        self.assertEqual((resumed["stop"], resumed["run"].release), ("merged", first))
        release.assert_called_once_with()

    def test_a_resume_reuses_the_recorded_related_pin_after_the_related_branch_moves(self):
        """Admission pins the related repository once and records the pin. A resumed run reads that commit even after the
        related default branch moved: every worker gets the checkout as a read root, and the procedures that name it get the
        pinned commit in their instructions, while Verify's prompt does not name it."""
        active = RelatedWorkspace(self.cwd)
        pinned, moved = active.related_head, "2" * 40

        def exhaust(node, model, state, instruction):
            if node == "spec_check":
                return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
            if node == "triage":
                self.assertIn(f"Example/Api at {pinned}: {active.related_checkout}", instruction)
                return Outcome("ran", {"triage": "supplied_slice", "work_kind": "feature", "human_question": None, "slice": None}, usage=SCRIPTED_USAGE)
            return Outcome("quota", usage=SCRIPTED_USAGE)

        self.github.ready_again(8)
        paused = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=self.checkpoint, dispatcher=exhaust,
                                   run=LiveRun(self.github, active))
        self.assertEqual(paused["stop"], "worker_unavailable")
        recorded = [RelatedCheckout(repository="Example/Api", commit=pinned, checkout=str(active.related_checkout))]
        self.assertEqual((paused["related"], active.pins), (recorded, [("Example/Api", "main")]))
        active.related_head = moved
        resumed = self.walk([{"implementation": "built"}, APPROVE], workspace=active)
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual((resumed["related"], active.pins), (recorded, [("Example/Api", "main")]))
        self.assertEqual(len(self.argvs), 3)
        for argv, prompt in zip(self.argvs, self.prompts):
            # Codex grants the checkout read access in its profile. Claude adds it as a directory whose edits it denies.
            grant = f'"{active.related_checkout}"="read"' if argv[0] == "codex" else f"--add-dir {active.related_checkout}"
            self.assertIn(grant, " ".join(argv[:-1]))
            self.assertNotIn(moved, prompt)
        implement, verify, review = self.prompts
        for prompt in (implement, review):
            self.assertIn(f"Example/Api at {pinned}: {active.related_checkout}", prompt)
        self.assertNotIn("Example/Api", verify)
        self.assertNotIn("related", json.loads(verify.split("Delivery state:\n")[1]))

    def test_load_parent_and_file_slices_only_once(self):
        self.github = FakeGitHub(["Ready"])
        result = self.walk([{"triage": "plan", "work_kind": "bug_fix"}, {
            "spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER],
        }])
        self.assertEqual(result["plan"].issue_kind, "parent")
        self.assertEqual(result["run"].request, "Issue title\n\nIssue body")
        self.assertEqual(result["stop"], "children_filed")
        self.assertEqual([child.number for child in self.github.sub_issues("Example/Engine", 8)], [101, 102])
        self.assertEqual([x[0] for x in self.github.created], [BUILDER["title"], CONSUMER["title"]])
        # Children are created unreleased. Every child gets the Ready label after every link and returns to admission.
        self.assertEqual([labels for _, _, labels in self.github.created], [["slice"], ["slice"]])
        self.assertEqual(self.github.release_labels(), [("Example/Engine", 101, ["Ready"], []), ("Example/Engine", 102, ["Ready"], [])])
        self.assertEqual(result["released"], ["Example/Engine#101", "Example/Engine#102"])
        for text in (SLICE["acceptance"], SLICE["screen"], "unique_item_id"):
            self.assertIn(text, self.github.created[0][1])
        self.assertIn(SLICE["title"], self.github.created[1][1])
        child_payload = {**CONSUMER, "depends_on": [BUILDER["title"]], "blocker_numbers": [101]}
        expected = [{**BUILDER, "depends_on": [], "blocker_numbers": []}, child_payload]
        for item, (_, body, _) in zip(expected, self.github.created):
            prose, fence, encoded = body.partition("\n\n```json\n")
            self.assertEqual(fence, "\n\n```json\n")
            stored = json.loads(encoded.removesuffix("\n```"))
            self.assertEqual(stored.pop("provenance"), {"author": result["plan"].spec_candidate["author"], "revision": result["plan"].spec_candidate["revision"]})
            self.assertEqual(stored, {**item, "work_kind": "bug_fix"})
            marker, _, rest = prose.partition("\n\n")
            self.assertRegex(marker, r"^<!-- foreman slice [0-9a-f]{64} -->$")
            self.assertTrue(rest.startswith("Acceptance:\n" + item["acceptance"]))
        self.assertEqual(self.github.dependencies, {102: [101]})
        resumed = self.walk([])
        self.assertEqual(self.github.dependencies, {102: [101]})
        self.assertEqual(resumed["stop"], "children_filed")
        self.assertEqual([child.number for child in self.github.sub_issues("Example/Engine", 8)], [101, 102])
        self.assertEqual(len(self.github.created), 2)
        self.assertEqual(self.github.release_labels(), [("Example/Engine", 101, ["Ready"], []), ("Example/Engine", 102, ["Ready"], [])])
        self.assertEqual(resumed["released"], ["Example/Engine#101", "Example/Engine#102"])
        self.assertEqual([blocker.number for blocker in self.github.blockers("Example/Engine", 102)], [101])
        # The child carries Ready, and its open blocker keeps it unadmitted.
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "102"],
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
                worker_runner=lambda *_: self.fail("An unadmitted issue must not dispatch."),
            )
        self.assertEqual(code, 1)
        self.assertEqual(transcript(stdout.getvalue()), "claude spend $0 of $3\nadmit\ndone blocked\nOpen blockers: https://github.com/Example/Engine/issues/101\n")
        self.github.closed.add(101)
        self.assertIn("no merged pull request", self.walk([], issue=102)["halt_detail"])
        self.github.merged.add(101)
        # The finished parent run files and labels nothing more. Merge release names the dependents to admit.
        self.assertEqual(self.walk([])["stop"], "children_filed")
        self.assertEqual(self.github.dependencies, {102: [101]})
        self.assertEqual(self.github.release_labels(), [("Example/Engine", 101, ["Ready"], []), ("Example/Engine", 102, ["Ready"], [])])
        child = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE], issue=102)
        self.assertEqual(child["stop"], "merged", child.get("halt_detail"))
        self.assertEqual(child["path"][:4], ["triage", "spec_check", "prepare", "implement"])
        self.assertEqual({k: v for k, v in child["plan"].slice.items() if k != "provenance"}, child_payload)
        self.assertEqual(child["plan"].work_kind, "bug_fix")

    def test_seam_draft_returns_to_draft_and_its_file_list_round_trips_to_children(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.checkpoint = self.cwd.parent / f"edit-{count}.sqlite3"
                self.github = FakeGitHub(["Ready"])
                (self.cwd / "items.py").write_text('def visible(items):\n    return [i for i in items if i["open"]]\n')
                edits = [{**EMPTY_SLICE, "title": f"Edit items {i}", "files": ["items.py"],
                          "acceptance": "Include closed items in visible(items)."} for i in range(count)]
                bad = [{**item, "uses": [{"name": "items", "exists": True, "path": "items.py"}]} for item in edits]
                spec = {"spec": "Change visible(items) in items.py.", "spec_gate": "ready", "slices": edits}
                result = self.walk([{"triage": "plan"}, {**spec, "slices": bad}, spec])
                self.assertEqual(result["path"], ["triage", "spec", "spec", "spec_check"])
                self.assertEqual(result["plan"].spec_failures, 1)
                attempts = [a for a in result["attempts"] if a["stage"] == "spec"]
                self.assertEqual(len(attempts), 2)
                self.assertTrue(all(a["usage"]["input"] > 0 for a in attempts))
                for text in ("seam_field", "slices.0.uses", "a slice has no builds or uses seams"):
                    self.assertIn(text, self.prompts[-2])
                candidate = result["plan"].spec_candidate
                self.assertEqual([s["files"] for s in candidate["slices"]], [["items.py"]] * count)
                self.assertEqual(len(self.github.created), count)
                self.assertEqual(self.github.dependencies, {})
                for index, (_, body, _) in enumerate(self.github.created):
                    child = _load_slice(body)
                    self.assertEqual(child.files, ["items.py"])
                    self.assertNotIn("items.py", body.partition("```json")[0])
                    self.assertEqual(child.acceptance, edits[index]["acceptance"])
                    self.assertEqual(child.depends_on, [])
                    self.assertEqual(child.provenance.revision, candidate["revision"])
                child = self.walk([{"triage": "question"}, {"implementation": "built"}, APPROVE], issue=101)
                self.assertEqual(child["plan"].slice["files"], ["items.py"])
                self.assertEqual(child["path"][:4], ["triage", "spec_check", "prepare", "implement"])
                self.assertEqual(child["plan"].slice["provenance"]["author"], candidate["author"])

    def test_mislabel_cap_and_mixed_errors_never_file_children(self):
        seam = {"name": "items", "exists": False, "path": None}
        for count in (1, 2):
            for malformed in (False, True):
                with self.subTest(count=count, malformed=malformed):
                    self.checkpoint = self.cwd.parent / f"cap-{count}-{malformed}.sqlite3"
                    self.github = FakeGitHub(["Ready"])
                    slices = [{**EMPTY_SLICE, "title": f"Edit {i}", "builds": [seam]} for i in range(count)]
                    if malformed:
                        slices[-1]["unexpected"] = True
                    spec = {"spec": "Edit items", "spec_gate": "ready", "slices": slices}
                    result = self.walk([{"triage": "plan"}, spec, {**spec, "spec": "Revision two"}, {**spec, "spec": "Revision three"}])
                    self.assertEqual(result["stop"], "invalid_output" if malformed else "spec_cap")
                    self.assertEqual(result["plan"].spec_failures, 0 if malformed else 3)
                    self.assertIsNone(result["plan"].spec_candidate)
                    self.assertEqual([labels for _, _, labels in self.github.created if "slice" in labels], [])
                    self.assertTrue(all(a["usage"] for a in result["attempts"]))

    def test_mislabels_with_invalid_questions_are_terminal(self):
        seam = {"name": "items", "exists": False, "path": None}
        question = {"question": "Which policy applies?", "why_code_unanswered": "A policy decision is needed.",
                    "checked_paths": ["items.py"]}
        for count in (1, 2):
            for gate, questions in (("ready", [question]), ("needs_human", [])):
                with self.subTest(count=count, gate=gate):
                    self.checkpoint = self.cwd.parent / f"mixed-questions-{count}-{gate}.sqlite3"
                    self.github = FakeGitHub(["Ready"])
                    slices = [{**EMPTY_SLICE, "title": f"Edit {i}", "builds": [seam]} for i in range(count)]
                    spec = {"spec": "Edit items", "spec_gate": gate, "questions": questions, "slices": slices}
                    with self.assertRaises(InvalidOutput) as raised:
                        parse_output(SpecOutput, spec)
                    self.assertIs(type(raised.exception), InvalidOutput)
                    self.assertIn("questions must be non-empty on needs_human and empty on ready", str(raised.exception))
                    result = self.walk([{"triage": "plan"}, spec, {**spec, "spec": "Revision two"}, {**spec, "spec": "Revision three"}])
                    self.assertEqual(result["stop"], "invalid_output")
                    self.assertEqual(result["path"], ["triage", "spec"])
                    self.assertEqual(result["plan"].spec_failures, 0)
                    self.assertIsNone(result["plan"].spec_candidate)
                    self.assertEqual(self.github.sub_issues("Example/Engine", 8), [])
                    self.assertEqual([labels for _, _, labels in self.github.created if "slice" in labels], [])
                    # The invalid draft and its repair turn, which answers with the same mislabel, are both charged.
                    attempts = [a for a in result["attempts"] if a["stage"] == "spec"]
                    self.assertEqual(len(attempts), 2)
                    self.assertTrue(all(attempt["usage"] for attempt in attempts))

    def test_link_failure_leaves_child_blocked_and_parent_retry_repairs_it(self):
        self.github = FakeGitHub(["Ready"])
        with patch.object(self.github, "add_blocker", side_effect=CommandTimedOut("gh api")):
            halted = self.walk([{"triage": "plan"}, {
                "spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER],
            }])
            self.assertEqual(halted["stop"], "publication_unreconciled")
            self.assertEqual(halted["halt_detail"], "GitHub does not show #101 blocking #102.")
            self.assertEqual(self.github.release_labels(), [])
            self.github.issue_labels[102].append("Ready")
            self.assertEqual(self.walk([], issue=102)["halt_detail"], "Planned blocker links are missing.")
            self.github.issue_labels[102].remove("Ready")
        self.assertEqual(self.walk([])["stop"], "children_filed")
        self.assertEqual(self.github.release_labels(), [("Example/Engine", 101, ["Ready"], []), ("Example/Engine", 102, ["Ready"], [])])
        self.assertEqual(len(self.github.created), 2)
        self.assertEqual([blocker.number for blocker in self.github.blockers("Example/Engine", 102)], [101])

    def test_a_timed_out_child_write_is_adopted_only_in_this_release_s_shape(self):
        """FS1: a child create that landed and then timed out is adopted once. A found child whose stored JSON differs only
        by the older shape's builds and uses keys is not this candidate's child, so filing stops publication_unreconciled."""
        for old_shape in (False, True):
            with self.subTest(old_shape=old_shape):
                self.checkpoint = self.cwd.parent / f"adopt-{old_shape}.sqlite3"
                github = self.github = FakeGitHub(["Ready"])
                create = github.create_issue

                def timed_out(repo, title, body, labels, *, marker, child=None):
                    number = create(repo, title, body, labels, marker=marker, child=child)
                    if old_shape:
                        prose, fence, stored = github.bodies[repo, number].partition("```json\n")
                        older = {**json.loads(stored.removesuffix("\n```")), "builds": [], "uses": []}
                        github.bodies[repo, number] = f"{prose}{fence}{json.dumps(older, indent=2)}\n```"
                    raise CommandTimedOut("gh issue create")

                with patch.object(github, "create_issue", side_effect=timed_out):
                    result = self.walk([{"triage": "plan"}, {"spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER]}])
                if old_shape:
                    self.assertEqual((result["stop"], result["halt_detail"]), ("publication_unreconciled",
                        f"Issue #101 carries the marker for {BUILDER['title']} with a different repository or body."))
                    self.assertEqual(github.release_labels(), [])
                else:
                    self.assertEqual(result["stop"], "children_filed")
                    self.assertEqual(github.dependencies, {102: [101]})
                self.assertEqual(len(github.created), 1 if old_shape else 2)

    def test_a_slice_issue_of_the_older_shape_halts_at_admit_naming_both_keys(self):
        """A stored slice is read strictly in this release's shape: no base is pinned and no worker is called."""
        self.github.body = slice_body({**EMPTY_SLICE, "builds": [], "uses": []})
        with patch.object(FakeWorkspace, "pin_base", side_effect=AssertionError("no base is pinned")):
            result = self.walk([])
        self.assertEqual(result["stop"], "missing_slice")
        self.assertEqual(result["halt_detail"], "builds: Extra inputs are not permitted; uses: Extra inputs are not permitted")
        self.assertEqual(self.calls, [])

    def run_labels(self, issue=8):
        return [label for label in self.github.load_issue("Example/Engine", issue)["labels"] if label in RUN_LABELS]

    def test_the_issue_shows_one_run_label_and_one_status_comment_across_the_run(self):
        paused = self.walk([{"triage": "needs_human", "human_question": "Which items?"}])
        self.assertEqual(paused["stop"], "needs_human")
        self.assertEqual(self.run_labels(), ["Needs answer"])
        status = self.github.status["Example/Engine", 8]
        self.assertIn("Which items?", status)
        self.assertRegex(status, r"Start with foreman answer [0-9a-f-]{71} and put the answer on the next line")
        merged = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE], answer="All items.")
        self.assertEqual(merged["stop"], "merged")
        self.assertEqual(self.run_labels(), ["Done"])
        self.assertEqual([(add, remove) for _, _, add, remove in self.github.label_calls],
                         [(["Doing"], ["Ready"]), (["Needs answer"], ["Doing"]), (["Doing"], ["Needs answer"]), (["Done"], ["Doing"])])
        self.assertEqual(self.github.created_labels, ["Doing", "Needs answer", "Done"])
        # Each admitted stage that starts shows on the issue, then the outcome with its links. Its steps and calls
        # rewrite the run report under the same first line.
        bodies = [first for first, _ in itertools.groupby(body.split("\n\n")[0] for _, _, body in self.github.status_writes)]
        self.assertEqual(bodies, ["Foreman is running triage.", "Foreman needs an answer in triage.", "Foreman is running triage.",
                                  "Foreman is running spec.", "Foreman is running implement.", "Foreman is running verify.",
                                  # Verify's evidence comment goes on the pull request.
                                  "Verify evidence.", "Foreman is running review.",
                                  "Foreman is running merge.", "The reviewed pull request merged."])
        status = self.github.status["Example/Engine", 8]
        for fact in ("Pull request: #12", f"Verified head: {HEAD}", f"Merge commit: {MERGE_COMMIT}"):
            self.assertIn(fact, status)
        self.assertTrue(status.endswith(STATUS_MARKER))
        owned = [c for c in self.github.issue_comment_records("Example/Engine", 8) if (c.body or "").endswith(STATUS_MARKER)]
        self.assertEqual(len(owned), 1)
        # A replay of the finished run writes nothing.
        writes = (list(self.github.label_calls), list(self.github.status_writes))
        self.assertEqual(self.walk([])["stop"], "merged")
        self.assertEqual((self.github.label_calls, self.github.status_writes), writes)

    def test_an_unadmitted_issue_keeps_ready_and_gets_no_status(self):
        for labels, dependencies in ((["slice"], {}), (["slice", "Ready"], {8: [9]})):
            with self.subTest(labels=labels), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.github = Path(directory) / "runs.sqlite3", FakeGitHub(labels)
                self.github.dependencies = dependencies
                self.assertEqual(self.walk([])["stop"], "blocked")
                self.assertEqual((self.github.label_calls, self.github.status), ([], {}))
                self.assertEqual(self.github.load_issue("Example/Engine", 8)["labels"], labels)

    def test_a_pause_stays_doing_and_the_checkpoint_corrects_a_human_label_race(self):
        self.github.queue_merges = False
        queued = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(queued["stop"], "merge_pending")
        self.assertEqual(self.run_labels(), ["Doing"])
        self.assertIn("waits for GitHub to merge the pull request", self.github.status["Example/Engine", 8])
        self.assertIn("Pull request: #12", self.github.status["Example/Engine", 8])
        # A duplicate wake resumes the step the run waits in and writes nothing (ME14).
        labels, writes = list(self.github.label_calls), list(self.github.status_writes)
        self.assertEqual(self.walk([])["stop"], "merge_pending")
        self.assertEqual((self.github.label_calls, self.github.status_writes), (labels, writes))
        # A human who swaps the run label mid-run changes nothing: the next write follows the checkpoint.
        self.github.edited[8] = ["slice", "Done", "Ready"]
        self.assertEqual(self.walk([])["stop"], "merge_pending")
        self.assertEqual(self.github.load_issue("Example/Engine", 8)["labels"], ["slice", "Doing"])

    def test_a_wake_that_leaves_the_run_in_its_wait_writes_nothing(self):
        """ME14: a wake into a saved wait in arm or observe that finds nothing new writes nothing and records no step, ten
        times over. The read that moves the run on records the next step once, and one that routes the run out of merge,
        pauses it at merge's gate, or finds a stop or a halt is written."""
        original = self.github.readiness
        pending = lambda *args: original(*args).model_copy(update={"outcome": "pending", "recovery": "Wait for checks."})
        reds = []

        def red(*args):  # Checks fail once, so the repaired head waits in arm again.
            reds.append(True)
            return original(*args).model_copy(update={"outcome": "red" if len(reds) == 1 else "pending", "recovery": "Repair."})
        foreign = "c" * 40

        def moved_head(*args):
            reading = original(*args)
            head = reading.pr.head.model_copy(update={"sha": foreign})
            return reading.model_copy(update={"outcome": "head_changed", "pr": reading.pr.model_copy(update={"head": head})})
        steps = {"arm": ["merge.arm"], "observe": ["merge.arm", "merge.observe"]}
        for wait, found in (("arm", "moved"), ("arm", "route"), ("arm", "gate"), ("arm", "stop"), ("arm", "halt"),
                            ("observe", "stop"), ("observe", "halt")):
            with self.subTest(wait=wait, found=found), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.github = Path(directory) / "runs.sqlite3", FakeGitHub()
                self.github.queue_merges = False
                with patch.object(self.github, "readiness", side_effect=pending if wait == "arm" else original):
                    waiting = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
                    self.assertEqual((waiting["stop"], waiting["steps"][-len(steps[wait]):]), ("merge_pending", steps[wait]))
                    writes, shown = list(self.github.status_writes), self.github.status["Example/Engine", 8]
                    self.assertIn(f"Step: {wait}", shown)
                    for _ in range(10):
                        self.assertEqual(self.walk([])["steps"], waiting["steps"])
                    self.assertEqual(self.github.status_writes, writes)
                    self.assertIn(f"Steps taken: {' '.join(waiting['steps'])}\n", shown)
                if found == "moved":
                    # Run one's check passed, so arm arms and the run waits in observe.
                    moved = self.walk([])
                    self.assertEqual((moved["stop"], moved["steps"]), ("merge_pending", [*waiting["steps"], "merge.observe"]))
                    self.assertIn("Step: observe", self.github.status["Example/Engine", 8])
                    self.assertGreater(len(self.github.status_writes), len(writes))
                    continue
                if found in ("route", "gate"):
                    # Failed checks route the run back to implement; a head nobody verified pauses it at merge's gate.
                    self.github.foreign_heads.add(foreign)
                    with patch.object(self.github, "readiness", side_effect=red if found == "route" else moved_head):
                        left = self.walk([{"implementation": "built"}, APPROVE] if found == "route" else [])
                    written = [body for _, _, body in self.github.status_writes[len(writes):]]
                    self.assertTrue(written)
                    if found == "route":
                        self.assertIn("implement.edit", left["steps"][len(waiting["steps"]):])
                        self.assertTrue(any("Stage: implement" in body for body in written))
                    else:
                        self.assertEqual(left["stop"], "needs_human")
                        self.assertIn("foreman answer ", self.github.status["Example/Engine", 8])
                    continue
                if found == "stop":
                    self.github.ruling("foreman stop")
                else:
                    self.github.hold = "Holding merges." if wait == "arm" else None
                with patch.object(self.github, "readiness", side_effect=ForgeError("GitHub is down.")) if found == "halt" and wait == "observe" \
                        else patch.object(self.github, "readiness", side_effect=pending if wait == "arm" else original):
                    ended = self.walk([])
                self.assertEqual(ended["stop"], "cancelled" if found == "stop" else "merge_held" if wait == "arm" else "readiness_unavailable")
                self.assertGreater(len(self.github.status_writes), len(writes))
                self.assertNotEqual(self.github.status["Example/Engine", 8], shown)

    def test_a_failed_status_write_never_changes_the_run_and_the_next_invocation_converges(self):
        self.github.queue_merges = False
        self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.github.queue_merges = True
        # The label lands and every comment write fails, as a crash between them would leave it.
        self.github.failures["upsert_comment", False] = [ForgeError("Injected write failure.")] * 20
        merged = self.walk([])
        self.github.failures.clear()
        self.assertEqual(merged["stop"], "merged")
        self.assertEqual(self.run_labels(), ["Done"])
        self.assertIn("waits for GitHub", self.github.status["Example/Engine", 8])
        self.github.fail_next("edit_labels")
        self.assertEqual(self.walk([])["stop"], "merged")
        self.assertTrue(self.github.status["Example/Engine", 8].startswith("The reviewed pull request merged."))

    def test_a_status_write_that_fails_after_its_label_lands_converges_on_the_next_write(self):
        self.github.fail_next("edit_labels", after_write=True)
        with self.assertRaises(ForgeError):
            write_status(self.github, "Example/Engine", 8, running("triage"))
        self.assertEqual((self.run_labels(), self.github.status), (["Doing"], {}))
        write_status(self.github, "Example/Engine", 8, running("triage"))
        self.assertEqual(self.github.label_calls, [("Example/Engine", 8, ["Doing"], ["Ready"])])
        self.assertTrue(self.github.status["Example/Engine", 8].startswith("Foreman is running triage."))

    def test_an_issue_github_answers_for_under_another_identity_never_takes_the_status(self):
        load = self.github.load_issue
        for foreign in ({"repo": "Other/Engine"}, {"number": 9}):
            with self.subTest(foreign=foreign), patch.object(self.github, "load_issue",
                                                             side_effect=lambda repo, number: {**load(repo, number), **foreign}):
                write_status(self.github, "Example/Engine", 8, running("triage"))
                self.assertFalse(shows(self.github, "Example/Engine", 8, "Ready"))
        self.assertEqual((self.github.label_calls, self.github.status), ([], {}))

    def test_run_labels_match_whatever_case_github_keeps(self):
        # GitHub keeps an existing label's case when Foreman adds it by its own name.
        self.github.edited[8] = ["slice", "doing"]
        write_status(self.github, "Example/Engine", 8, running("implement"))
        self.assertEqual(self.github.label_calls, [])
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Doing"))
        write_status(self.github, "Example/Engine", 8, Status("Done", "The reviewed pull request merged."))
        self.assertEqual(self.github.label_calls, [("Example/Engine", 8, ["Done"], ["doing"])])
        self.assertEqual(self.github.load_issue("Example/Engine", 8)["labels"], ["slice", "Done"])

    def test_a_halt_before_admission_comments_and_keeps_ready_for_the_rerun(self):
        with patch.object(self.github, "blockers", side_effect=ForgeError("GitHub is unavailable.")):
            halted = self.walk([])
        self.assertEqual(halted["stop"], "git_failed")
        self.assertEqual(self.github.label_calls, [])
        self.assertEqual(self.github.load_issue("Example/Engine", 8)["labels"], ["slice", "Ready"])
        self.assertIn("Foreman halted with git_failed.", self.github.status["Example/Engine", 8])
        self.assertIn("Rerun Foreman on this issue to resume.", self.github.status["Example/Engine", 8])
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merged")
        self.assertEqual(self.run_labels(), ["Done"])

    def test_an_unsaved_early_result_leaves_the_status_alone(self):
        self.github.queue_merges = False
        self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        writes = (list(self.github.label_calls), list(self.github.status_writes))
        moved = FakeWorkspace(self.cwd)
        moved.validate_origin = Mock(side_effect=WorkspaceError("origin moved"))
        # The refused resume never enters the stage it would resume, so merge reads nothing from GitHub.
        with patch.object(self.github, "readiness", wraps=self.github.readiness) as readiness:
            self.assertEqual(self.walk([], workspace=moved)["stop"], "git_failed")
        readiness.assert_not_called()
        self.assertEqual((self.github.label_calls, self.github.status_writes), writes)
        self.assertEqual(self.run_labels(), ["Doing"])

    def test_a_retried_release_never_puts_ready_back_on_a_started_child(self):
        self.github = FakeGitHub(["Ready"])
        edit = self.github.edit_labels

        def unavailable_for_102(repo, number, add, remove):
            if number == 102:
                raise ForgeError("GitHub is unavailable.")
            edit(repo, number, add, remove)
        with patch.object(self.github, "edit_labels", side_effect=unavailable_for_102):
            halted = self.walk([{"triage": "plan"}, {"spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER]}])
        self.assertEqual((halted["stop"], halted["halt_detail"]), ("publication_unreconciled", "GitHub does not show #102 released."))
        # The released child's own run started, under a label whose case GitHub kept.
        self.github.issue_labels[101][:] = ["slice", "doing"]
        self.assertEqual(self.walk([])["stop"], "children_filed")
        self.assertEqual(self.github.release_labels(), [("Example/Engine", 101, ["Ready"], []), ("Example/Engine", 102, ["Ready"], [])])
        self.assertEqual(self.github.issue_labels[101], ["slice", "doing"])

    def test_injected_workers_reach_merge_after_a_real_check_in_the_repo(self):
        (self.cwd / "expected").touch()
        result = self.walk(
            [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE],
            check="test -f expected",
        )
        self.assertEqual(self.calls, ["claude", "codex", "codex", "claude", "claude"])
        self.assertEqual(result["path"], ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "verify", "review", "merge"])
        self.assertEqual(result["checks"].check_exit_code, 0)
        self.assertEqual(result["checks"].verified_head, HEAD)
        self.assertEqual(result["work"].head_sha, HEAD)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(result["merge"].status, "merged")

    def test_cli_redacts_the_check_subprocess_stdout_and_stderr(self):
        check = self.cwd / "verify.sh"
        check.write_text(
            f"echo 'Check stdout {SYNTHETIC_QUESTION_TOKEN}'\n"
            f"echo 'Check stderr {SYNTHETIC_QUESTION_TOKEN}' >&2\n"
        )
        fake = FakeWorkspace(self.cwd, check="sh verify.sh")
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                worker_runner=lambda args, cwd, env: fake.reply(args, checker_reply(args) or next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, fake),
            )
        self.assertEqual(code, 0)
        self.assertIn("done merged\n", stdout.getvalue())
        self.assertIn("Check stdout [redacted:GitHub Token]\n", stdout.getvalue())
        self.assertIn("Check stderr [redacted:GitHub Token]\n", stdout.getvalue())
        # Stderr carries only the release of Verify's captures once the run finishes.
        self.assertRegex(stderr.getvalue(), r"\Areleased [^\n]+\n\Z")
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, stdout.getvalue() + stderr.getvalue())

    def test_cli_prints_a_start_line_per_released_issue_including_on_replay(self):
        self.github.create_issue("Example/Engine", "Dependent", "Body", ["slice"], marker="")
        self.github.create_issue("Example/Engine", "Later", "Body", ["slice"], marker="")
        self.github.dependencies = {101: [8], 102: [8]}
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        outputs = []
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                    worker_runner=lambda args, cwd, env: fake.reply(args, checker_reply(args) or next(replies)),
                    checkpoint=self.checkpoint, live_run=LiveRun(self.github, fake),
                )
            self.assertEqual(code, 0)
            outputs.append([line for line in stdout.getvalue().splitlines() if line.startswith("start ")])
        # The replay of the finished run prints the same lines, and the receiver reads them from the checkpoint.
        self.assertEqual(outputs, [["start Example/Engine#101", "start Example/Engine#102"]] * 2)
        self.assertEqual(released_issues(self.checkpoint, issue_thread("Example/Engine", 8)), ["Example/Engine#101", "Example/Engine#102"])
        self.assertEqual(len(self.github.merge_calls), 1)

    def test_check_streams_in_write_order_before_the_command_exits(self):
        check = self.cwd / "stream.py"
        acknowledgement = self.cwd / "logged"
        check.write_text(
            "import os, pathlib, time\n"
            f"os.write(1, b'First synthetic {SYNTHETIC_QUESTION_TOKEN}\\n')\n"
            f"ack = pathlib.Path({str(acknowledgement)!r})\n"
            "deadline = time.monotonic() + 5\n"
            "while not ack.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
            "if not ack.exists(): raise SystemExit(9)\n"
            f"os.write(2, b'Second synthetic {SYNTHETIC_QUESTION_TOKEN}\\n')\n"
            "os.write(1, b'Third\\n')\n"
            "os.write(2, b'Fourth')\n"
            "raise SystemExit(7)\n"
        )

        def log(line):
            _write_check_log(line)
            acknowledgement.touch()

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = LiveWorkspace(self.cwd).run(shlex.join([sys.executable, str(check)]), log=log)
        self.assertEqual(code, 7)
        self.assertEqual(stdout.getvalue(), (
            "First synthetic [redacted:GitHub Token]\n"
            "Second synthetic [redacted:GitHub Token]\nThird\nFourth"
        ))

    def test_cli_uses_one_workspace_root_for_the_worker_check_and_checkpoint(self):
        root = self.cwd / "checkout"
        root.mkdir()
        live_run = LiveRun(self.github, FakeWorkspace(root, check="touch checked"))
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])

        def worker(args, cwd, env):
            self.assertEqual(cwd, root)
            return live_run.workspace.reply(args, checker_reply(args) or next(replies))

        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                worker_runner=worker, checkpoint=self.checkpoint, live_run=live_run,
            )
        self.assertEqual(code, 0)
        self.assertTrue((root / "checked").exists())
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        rerun = {"repo": "Example/Engine", "issue": 8, "checkpoint": self.checkpoint, "dispatcher": ScriptHandler()}
        self.assertEqual(run_live_delivery(**rerun, run=live_run)["run"].workspace, str(root.resolve()))
        with self.assertRaisesRegex(ValueError, "Run this issue from"):
            run_live_delivery(**rerun, run=LiveRun(self.github, FakeWorkspace(self.cwd)))

    def test_nonzero_check_exit_consumes_the_cap_without_dispatched_verify_or_review(self):
        result = self.walk([{"triage": "supplied_slice"}] + [{"implementation": "built"}] * 3, check="exit 17")
        self.assertEqual(result["checks"].check_exit_code, 17)
        self.assertEqual(result["stop"], "verify_cap")
        self.assertEqual(self.calls, ["claude", "codex", "codex", "codex", "codex"])
        self.assertNotIn("merge", result["path"])

    def test_parent_cannot_implement_without_a_slice(self):
        self.github = FakeGitHub(["Ready"])
        for triage in ({"triage": "supplied_slice"}, {"triage": "supplied_slice", "slice": None}):
            with self.subTest(triage=triage), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint = Path(directory) / "run.sqlite3"
                self.calls.clear()
                result = self.walk([triage, triage, {"implementation": "built"}, APPROVE])
                self.assertEqual(result["path"], ["triage"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"].startswith("triage output from claude-opus-5-5-high: slice:"))
                # The repair turn repeats the missing slice, so the second invalid answer stops the run.
                self.assertEqual(self.calls, ["claude", "claude"])
                self.assertEqual(self.github.merge_calls, [])
                self.assertEqual([labels for _, _, labels in self.github.created if "slice" in labels], [])

    def test_cli_preflight_stops_before_dispatch_and_uses_engine_checkout(self):
        root = self.cwd / "checkout"
        root.mkdir()
        preflight = "echo probed > preflight.log; exit 9"
        self.github.settings["FOREMAN_PREFLIGHT"] = preflight
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                worker_runner=lambda args, cwd, env: worker_reply(args, checker_reply(args) or {"triage": "supplied_slice"}),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(root, check="touch checked")),
            )
        self.assertEqual(code, 75)
        self.assertIn("done env_unavailable", stdout.getvalue())
        self.assertIn(f"Preflight {preflight!r} exited with 9", stdout.getvalue())
        self.assertEqual((root / "preflight.log").read_text(), "probed\n")
        self.assertFalse((self.cwd / "preflight.log").exists())
        self.assertFalse((root / "checked").exists())

    def test_cli_preflight_output_streams_through_redacted_log(self):
        preflight = f"echo {SYNTHETIC_QUESTION_TOKEN}; echo warning >&2; exit 9"
        stdout = io.StringIO()
        self.github.settings["FOREMAN_PREFLIGHT"] = preflight
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(stdout):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                worker_runner=lambda args, cwd, env: worker_reply(args, checker_reply(args) or {"triage": "supplied_slice"}),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 75)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, stdout.getvalue())
        self.assertIn("[redacted:GitHub Token]", stdout.getvalue())
        self.assertIn("warning", stdout.getvalue())

    def test_cli_rejects_changed_answer_without_traceback(self):
        self.walk([{"triage": "needs_human", "human_question": "Which items?"}], preflight="exit 9")
        self.walk([{"triage": "supplied_slice"}], answer="Only open")
        stderr = io.StringIO()
        with contextlib.chdir(self.cwd), contextlib.redirect_stderr(stderr):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                 "--answer", "All items"],
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 1)
        self.assertIn("already answered differently", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_a_waiting_run_keeps_the_login_and_caps_its_start_read(self):
        """TR10: FOREMAN_HUMAN_LOGIN and FOREMAN_MAX_BUDGET_USD change while a run waits for an answer, and the run then
        continues. The answer and stop reads use the login saved at the start, and dispatch uses the saved caps."""
        self.github.labels = ["Ready"]
        self.github.settings["FOREMAN_MAX_BUDGET_USD"] = "2"
        paused = self.walk([{"triage": "needs_human", "human_question": "Which items?"}])
        self.assertEqual((paused["stop"], paused["start"].human_login, paused["start"].max_budget_usd), ("needs_human", "fixture-owner", "2"))
        reads = len(self.github.variable_reads)
        self.github.settings.update({"FOREMAN_HUMAN_LOGIN": "someone-else", "FOREMAN_MAX_BUDGET_USD": "50"})
        self.github.answer(self.question, "All items.", login="someone-else")

        def no_worker(*args):
            raise AssertionError("an unanswered question dispatches no worker")
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()):
            code = main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"], checkpoint=self.checkpoint,
                        worker_runner=no_worker, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)))
        self.assertEqual(code, 3)
        self.assertIn("Foreman needs an answer in triage.", self.github.status["Example/Engine", 8])
        # A stop under the new login is not the human's, so the answered run goes on.
        self.github.ruling("foreman stop", login="someone-else")
        self.argvs.clear()
        resumed = self.walk([{"triage": "question"}, {"research": "Only open items."}], answer="Only open items")
        self.assertEqual(resumed["stop"], "answered")
        self.assertEqual((resumed["start"].human_login, resumed["start"].max_budget_usd), ("fixture-owner", "2"))
        claude = next(args for args in self.argvs if args[0] == "claude")
        self.assertLessEqual(Decimal(claude[claude.index("--max-budget-usd") + 1]), Decimal("2"))
        self.assertEqual(len(self.github.variable_reads), reads)

    def test_rerunning_same_command_after_preflight_recovers_without_using_a_round(self):
        preflight = "test -f ready"
        halted = self.walk([{"triage": "supplied_slice"}], preflight=preflight)
        self.assertEqual(halted.get("stop"), "env_unavailable")
        self.assertEqual(len(halted["review"].review_findings), 0)
        self.assertEqual(halted.get("path"), ["triage", "spec_check", "prepare"])
        self.assertEqual(self.calls, ["claude", "codex"])

        (self.cwd / "ready").touch()
        result = self.walk(
            [{"implementation": "built"}, APPROVE],
            preflight=preflight,
        )
        self.assertEqual(result.get("stop"), "merged")
        self.assertEqual(len(result["review"].review_findings), 1)
        self.assertNotIn("halt_detail", result)
        self.assertEqual(result.get("path"), ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "verify", "review", "merge"])

    def test_answer_resume_is_idempotent_after_environment_halt(self):
        preflight = "test -f ready"
        question = self.walk(
            [{"triage": "needs_human", "human_question": "Which items?"}], preflight=preflight,
        )
        self.assertEqual(question.get("stop"), "needs_human")
        halted = self.walk([{"triage": "supplied_slice"}], answer="Only open")
        self.assertEqual(halted.get("stop"), "env_unavailable")
        self.assertEqual(halted.get("human_answers"), ["Only open"])
        with self.assertRaisesRegex(AnswerError, "already answered differently"):
            self.walk([], answer="All items")
        (self.cwd / "ready").touch()
        resumed = self.walk([{"implementation": "built"}, APPROVE], answer="Only open")
        self.assertEqual(resumed.get("stop"), "merged")
        self.assertEqual(resumed.get("human_answers"), ["Only open"])
        revision = resumed["plan"].human_qa[0].get("revision", "")
        self.assertRegex(revision, r"^[0-9a-f]{64}$")
        expected_qa = [{"stage": "triage", "question": "Which items?", "answer": "Only open", "revision": revision}]
        self.assertEqual(resumed["plan"].human_qa, expected_qa)
        repeated = self.walk([], answer="Only open")
        self.assertEqual(repeated.get("stop"), "merged")
        self.assertEqual(repeated["plan"].human_qa, expected_qa)

    def test_non_implement_routes_skip_preflight(self):
        for issue, replies, stop in (
            (8, [{"triage": "question"}, {"research": "answer"}], "answered"),
            (9, [{"triage": "plan"}, {"spec": "not ready", "spec_gate": "needs_human",
                                       "questions": [{"question": "Which items?", "why_code_unanswered": "No policy in code.", "checked_paths": []}]}], "needs_human"),
        ):
            with self.subTest(stop=stop):
                self.github = FakeGitHub(["Ready"])
                result = self.walk(replies, issue=issue, preflight="touch probed; exit 9")
                self.assertEqual(result.get("stop"), stop)
                self.assertFalse((self.cwd / "probed").exists())

    def test_parent_and_slice_probe_once_plus_failed_check_reprobe(self):
        for issue, labels in ((8, ["Ready"]), (9, ["slice", "Ready"])):
            with self.subTest(labels=labels):
                self.github = FakeGitHub(labels)
                marker = f"checked-{issue}"
                probe_log = f"probes-{issue}"
                result = self.walk(
                    [{"triage": "supplied_slice", "slice": EMPTY_SLICE},
                     {"implementation": "built"}, {"implementation": "fixed"}, APPROVE], issue=issue,
                    check=f"test -f {marker} || {{ touch {marker}; exit 1; }}",
                    preflight=f"echo probed >> {probe_log}",
                )
                self.assertEqual(result.get("stop"), "merged")
                self.assertEqual(len(result["review"].review_findings), 1)
                self.assertEqual(result.get("path", []).count("implement"), 2)
                self.assertEqual((self.cwd / probe_log).read_text().splitlines(), ["probed", "probed"])

    def test_successful_slice_probes_only_once(self):
        result = self.walk(
            [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE],
            preflight="echo probed >> probes",
        )
        self.assertEqual(result.get("stop"), "merged")
        self.assertEqual((self.cwd / "probes").read_text(), "probed\n")

    def test_parent_preflight_halts_at_prepare(self):
        self.github = FakeGitHub(["Ready"])
        result = self.walk([{"triage": "supplied_slice", "slice": EMPTY_SLICE}], preflight="exit 9")
        self.assertEqual(result.get("stop"), "env_unavailable")
        self.assertEqual(result.get("path"), ["triage", "spec_check", "prepare"])
        self.assertEqual(self.calls, ["claude", "codex"])

    def test_parent_stops_sliced_without_running_preflight(self):
        self.github = FakeGitHub(["Ready"])
        result = self.walk(
            [{"triage": "plan"}, {"spec": "Plan", "spec_gate": "ready", "slices": [EMPTY_SLICE]}],
            preflight="touch probed; exit 9",
        )
        self.assertEqual(result.get("stop"), "children_filed")
        self.assertFalse((self.cwd / "probed").exists())

    def test_failed_check_with_failed_preflight_preserves_rounds(self):
        (self.cwd / "ready").touch()
        workspace = FakeWorkspace(self.cwd)
        result = self.walk(
            [{"triage": "supplied_slice"}, {"implementation": "built"}],
            check="test -f checked || { touch checked; rm ready; exit 1; }", preflight="test -f ready", workspace=workspace,
        )
        self.assertEqual(result.get("stop"), "env_unavailable")
        self.assertEqual(len(result["review"].review_findings), 0)
        self.assertEqual(result["checks"].check_exit_code, 1)
        self.assertEqual(result["checks"].verify, "")
        self.assertIn("test -f ready", result.get("halt_detail", ""))
        self.assertIn("exited with 1", result.get("halt_detail", ""))
        self.assertEqual(result.get("path"), ["triage", "spec_check", "prepare", "implement", "check"])

        (self.cwd / "ready").touch()
        resumed = self.walk([APPROVE], workspace=workspace)
        self.assertEqual(resumed.get("stop"), "merged")
        self.assertEqual(resumed["checks"].check_exit_code, 0)
        self.assertEqual(len(resumed["review"].review_findings), 1)
        self.assertEqual(self.calls, ["claude", "codex", "codex", "claude", "claude"])

    def test_failed_check_consumes_round_with_healthy_or_unspecified_environment(self):
        for issue, preflight in enumerate(("", "echo probed >> preflight.log"), start=8):
            with self.subTest(preflight=preflight):
                marker = f"checked-{issue}"
                built = {"implementation": "built"}
                result = self.walk(
                    [{"triage": "supplied_slice"}, built, built, APPROVE], issue=issue,
                    check=f"test -f {marker} || {{ touch {marker}; exit 1; }}", preflight=preflight,
                )
                self.assertEqual(result.get("stop"), "merged")
                self.assertEqual(len(result["review"].review_findings), 1)
                self.assertEqual(result.get("path", []).count("implement"), 2)
        self.assertEqual((self.cwd / "preflight.log").read_text(), "probed\nprobed\n")

    def test_check_that_cannot_start_stops_without_spending_a_round(self):
        (self.cwd / "non-executable").write_text("#!/bin/sh\nexit 0\n")
        cases = (
            ("./non-executable", 126, "command is not executable", ""),
            ("./missing-check", 127, "command not found", "echo probed >> preflight.log"),
        )
        for issue, (command, exit_code, reason, preflight) in enumerate(cases, start=8):
            with self.subTest(exit_code=exit_code):
                result = self.walk(
                    [{"triage": "supplied_slice"}, {"implementation": "built"}],
                    issue=issue, check=command, preflight=preflight,
                )
                self.assertEqual(result.get("stop"), "env_unavailable")
                self.assertEqual(len(result["review"].review_findings), 0)
                self.assertEqual(result["checks"].check_exit_code, exit_code)
                self.assertEqual(result["checks"].verify, "")
                for detail in (command, str(exit_code), reason):
                    self.assertIn(detail, result.get("halt_detail", ""))
        self.assertEqual((self.cwd / "preflight.log").read_text(), "probed\n")

    def test_interrupt_during_setup_has_no_pending_usage(self):
        with patch.object(FakeWorkspace, "run", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}], preflight="true")
        result = self.walk([{"implementation": "built"}, APPROVE], preflight="true")
        self.assertEqual(result["stop"], "merged")
        self.assertNotIn("interrupted", [a["outcome"] for a in result["attempts"]])

    def test_crash_after_prepare_rejects_unowned_edits_before_preflight(self):
        crash = Mock()
        crash.invoke.side_effect = SystemExit("crash after prepare")
        with patch("foreman.steps.implement.DISPATCH", crash):  # Prepare saved the attempt; edit dispatches next.
            with self.assertRaisesRegex(SystemExit, "crash after prepare"):
                self.walk([{"triage": "supplied_slice"}], preflight="touch preflight_ran")
        self.assertTrue((self.cwd / "preflight_ran").exists())
        (self.cwd / "preflight_ran").unlink()
        workspace = FakeWorkspace(self.cwd)
        workspace.current = WorktreeState(HEAD, False, BRANCH)
        resumed = self.walk([{"implementation": "built"}, APPROVE], workspace=workspace)
        self.assertEqual(resumed["stop"], "dirty_worktree")
        self.assertIn("Unowned edits", resumed["halt_detail"])
        self.assertFalse((self.cwd / "preflight_ran").exists())
        self.assertEqual([a["stage"] for a in resumed["attempts"]], ["triage", "spec_check", "spec_check"])

    def test_env_unavailable_resumes_before_implement(self):
        with patch.object(FakeWorkspace, "run", return_value=9):
            result = self.walk([{"triage": "supplied_slice"}], preflight="true")
        self.assertEqual(result["stop"], "env_unavailable")
        self.assertEqual(self.walk([{"implementation": "built"}, APPROVE], preflight="true")["stop"], "merged")

    def test_resume_at_interrupted_dispatch_stops_with_unknown_usage(self):
        def interrupted_run(node, model, state, instruction):
            if node == "spec_check":
                return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
            if node == "triage":
                return Outcome("ran", {"triage": "supplied_slice", "work_kind": "feature", "slice": None, "human_question": None}, usage=SCRIPTED_USAGE)
            self.assertEqual(node, "implement")
            raise RuntimeError("worker stopped")

        ready = self.cwd / "ready"
        ready.touch()
        with self.assertRaisesRegex(RuntimeError, "worker stopped"):
            self.github.settings["FOREMAN_PREFLIGHT"] = "test -f ready"
            run_live_delivery(
                repo="Example/Engine", issue=8,
                checkpoint=self.checkpoint, run=LiveRun(self.github, FakeWorkspace(self.cwd)),
                dispatcher=interrupted_run,
            )
        ready.unlink()
        result = self.walk([])
        self.assertEqual(result.get("stop"), "budget_exceeded")
        self.assertIn("usage_unknown", result["halt_detail"])
        self.assertEqual(len(result["review"].review_findings), 0)
        self.assertEqual(self.calls, [])

    def test_dirty_or_invalid_worktree_after_commit_is_never_published(self):
        for snapshot, halt in ((WorktreeState(HEAD, False, BRANCH), "dirty_worktree"), (WorktreeInspectionFailed("Invalid HEAD"), "git_failed")):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}], snapshots=(snapshot,))
                self.assertEqual(result["stop"], halt)
                self.assertEqual(result["path"], ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish"])
                self.assertEqual(result["checks"].check_exit_code, 0)
                self.assertIsNone(result["work"].published_head)
                self.assertEqual((self.github.branches, self.github.opened, self.github.merge_calls), ({}, {}, []))

    def test_a_moved_base_reviews_the_head_again_with_its_verify_evidence(self):
        # Merge returns a known checked head to Review. Verify doesn't drive it again, and Review keeps its evidence.
        evidence = {"tested_head": HEAD, "criteria": [{"criterion": "The page lists items.", "result": "passed",
                                                       "observation": "The list showed items.", "evidence": "captures/items.png"}]}
        # Doctor admits only a base that tracks the verify skill, so every run drives Verify; S21 removes the no_skill route.
        self.github, self.prompts = FakeGitHub(), []
        FakeWorkspace(self.cwd)
        (self.cwd / ".claude/skills/verify-web").mkdir(parents=True)
        (self.cwd / ".claude/skills/verify-web/SKILL.md").write_text("# Verify the web app\n")
        subprocess.run(["git", "-C", str(self.cwd), "add", ".claude"], check=True)
        subprocess.run(["git", "-c", "user.name=Foreman Test", "-c", "user.email=test@example.invalid", "-C", str(self.cwd),
                        "commit", "-q", "-m", "Verify skill"], check=True)
        observe = self.github.readiness

        def base_moves_after_the_first_review(*args):
            if len(self.github.reviews) == 1:
                self.github.base = "c" * 40
            return observe(*args)

        with patch.object(self.github, "readiness", side_effect=base_moves_after_the_first_review):
            result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, evidence, APPROVE, APPROVE])
        self.assertEqual(result["stop"], "merged")
        self.assertEqual([review[2] for review in self.github.reviews], [HEAD, HEAD])
        self.assertEqual(sum("# Use the change as a user would" in prompt for prompt in self.prompts), 1)
        self.assertEqual((result["verify"].outcome, result["verify"].tested_head), ("posted", HEAD))
        no_skill = "Verify: the repository has no verify skill"
        self.assertEqual([no_skill in review[3] for review in self.github.reviews], [False] * 2)
        self.assertEqual(len([body for _, number, body in self.github.status_writes if number == 12]), 1)

    def test_head_moved_before_review_re_establishes_checked_evidence(self):
        moved = WorktreeState("b" * 40, True, BRANCH)
        result = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, {"implementation": "built"}, APPROVE],
                           snapshots=(CLEAN_WORKTREE, moved))
        # Verify exits head_moved before any worker call, and Implement checks the current head again.
        self.assertEqual(self.calls, ["claude", "codex", "codex", "codex", "claude", "claude"])
        self.assertEqual(result["path"], ["triage", "spec_check", *["prepare", "implement", "check", "commit", "publish"] * 2,
                                          "verify", "review", "merge"])
        self.assertEqual(result["stop"], "merged")
        self.assertEqual(len(result["review"].review_findings), 1)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_retry_after_failed_hold_read_arms_once(self):
        reason = "HTTP 503: Service Unavailable"
        # Admission's doctor reads the variables first. The merge hold read that follows fails.
        ready = self.github.variables("Example/Engine")
        with patch.object(self.github, "variables", side_effect=[ready, VariablesUnreadable(reason)]):
            held = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(held["stop"], "merge_held")
        self.assertEqual(held["merge"].status, "not_armed")
        self.assertEqual(held["halt_detail"], f"could not read FOREMAN_MERGE_HOLD: {reason}")
        self.assertEqual(held["merge"].command, [
            "gh", "pr", "merge", "<number>", "--repo", "Example/Engine", "--auto", "--squash", "--match-head-commit", HEAD,
        ])
        self.assertEqual(self.github.merge_calls, [])
        released = self.walk([])
        self.assertEqual(released["stop"], "merged")
        self.assertEqual(released["merge"].status, "merged")
        self.assertEqual(released["path"], held["path"])
        self.assertNotIn("halt_detail", released)
        self.assertEqual(self.walk([]), released)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_live_workspace_reports_head_status_and_branch(self):
        for status, branch in (("", BRANCH + "\n"), ("?? untracked\n", ""), ("", "")):
            with self.subTest(status=status, branch=branch), patch("foreman.workspace.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, (HEAD + "\n").encode()),
                subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, status.encode()),
                subprocess.CompletedProcess(["git", "branch", "--show-current"], 0, branch.encode()),
            ]) as run:
                self.assertEqual(LiveWorkspace(self.cwd).inspect(), WorktreeState(HEAD, not status, branch.strip() or None))
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"], ["git", "branch", "--show-current"],
                ])
                self.assertTrue(all(call.kwargs["cwd"] == self.cwd for call in run.call_args_list))

    def test_review_diff_fetches_an_unrelated_new_base_commit(self):
        upstream = self.cwd / "upstream"
        checkout = self.cwd / "checkout"

        def git(root: Path, *args: str) -> str:
            return subprocess.check_output(
                ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", *args],
                cwd=root, text=True, stderr=subprocess.PIPE,
            ).strip()

        git(self.cwd, "init", "-b", "main", str(upstream))
        (upstream / "items.txt").write_text("old\n")
        git(upstream, "add", "items.txt")
        git(upstream, "commit", "-m", "Base")
        git(self.cwd, "clone", "--no-local", str(upstream), str(checkout))
        git(checkout, "switch", "-c", BRANCH)
        (checkout / "items.txt").write_text("new\n")
        git(checkout, "commit", "-am", "Review head")
        git(checkout, "push", "origin", BRANCH)
        head = git(checkout, "rev-parse", "HEAD")
        (upstream / "main.txt").write_text("Unrelated change\n")
        git(upstream, "add", "main.txt")
        git(upstream, "commit", "-m", "Advance main")
        base = git(upstream, "rev-parse", "HEAD")
        with self.assertRaises(subprocess.CalledProcessError):
            git(checkout, "cat-file", "-e", base)

        reviewed = LiveWorkspace(checkout).review_diff(base, head)
        self.assertEqual(git(checkout, "cat-file", "-t", base), "commit")
        self.assertIn(f"{base}...{head}", reviewed.command)
        self.assertIn("-old\n+new\n", reviewed.patch)
        self.assertNotIn("main.txt", reviewed.patch)

    def test_live_workspace_rejects_invalid_head_before_the_remaining_inspection(self):
        for head in ("", "abc123", "z" * 40):
            with self.subTest(head=head), patch(
                "foreman.workspace.subprocess.run",
                return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, (head + "\n").encode(), b""),
            ) as run:
                with self.assertRaisesRegex(WorktreeInspectionFailed, "full 40-hex HEAD SHA"):
                    LiveWorkspace(self.cwd).inspect()
                run.assert_called_once()

    def test_main_displays_the_forge_failure(self):
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        detail = "GraphQL: Auto-merge is disabled"
        with (
            contextlib.chdir(self.cwd),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            patch.object(self.github, "arm_auto_merge", side_effect=MergeNotArmed(detail)) as arm,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8"],
                worker_runner=lambda args, cwd, env: fake.reply(args, checker_reply(args) or next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, fake),
            )
        self.assertEqual(code, 75)
        self.assertEqual(arm.call_args.args[:4], ("Example/Target", 8, BRANCH, HEAD))
        self.assertIn(f"merge not armed\ncheck exit 0\ndone merge_not_armed\n{detail}", transcript(stdout.getvalue()))

    def test_parent_arms_its_pull_request_without_filing_slices(self):
        self.github = FakeGitHub(["Ready"])
        result = self.walk([{"triage": "supplied_slice", "slice": EMPTY_SLICE}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["path"], ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "verify", "review", "merge"])
        self.assertEqual(result["merge"].status, "merged")
        self.assertEqual(result["stop"], "merged")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(self.github.created, [])

    def test_main_dry_run_reports_a_skipped_merge(self):
        worker = Mock()
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            patch.object(FakeGitHub, "arm_auto_merge") as arm,
            patch("foreman.skill_rules.git") as git_call,
        ):
            code = main(["run", "delivery", "--repo", "Example/Target", "--request", "List items", "--dry-run"],
                        worker_runner=worker, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)))
        self.assertEqual(code, 0)
        self.assertIn(
            "merge skipped (dry run): gh pr merge '<number>' --repo Example/Target --auto --squash --match-head-commit '<sha>'\n"
            "done merge_pending\nPR #1 at <sha>: dry run, so Foreman did not arm it.",
            transcript(stdout.getvalue()),
        )
        self.assertIn("pstack pin not checked (dry run)", stdout.getvalue())
        worker.assert_not_called()
        arm.assert_not_called()
        git_call.assert_not_called()

    def test_main_rerun_arms_once_after_the_repository_hold_is_cleared(self):
        """ME11: the operator sets FOREMAN_MERGE_HOLD after the run's start and before arm, then clears it. Arm reads the hold
        and reports merge_held. The run arms once after the hold is cleared."""
        repo = "Example/Target"
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        live_run = LiveRun(self.github, FakeWorkspace(self.cwd))

        def worker(args, cwd, env=None):
            reply = checker_reply(args) or next(replies)
            if reply is APPROVE:
                self.github.hold = "  Maintenance window "
            return live_run.workspace.reply(args, reply)

        def invoke():
            with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(["run", "delivery", "--repo", repo, "--issue", "8"], worker_runner=worker,
                            checkpoint=self.checkpoint, live_run=live_run)
            self.assertEqual(code, 75 if self.github.hold else 0)
            return stdout.getvalue()

        held = invoke()
        self.assertIn(f"done merge_held\nFOREMAN_MERGE_HOLD is set: {self.github.hold}\n", held)
        retried = invoke()
        self.assertIn("\npull request lines 1\nmerge not armed\ncheck exit 0\ndone merge_held", transcript(retried))
        self.assertEqual(self.github.merge_calls, [])
        self.github.hold = ""
        armed = invoke()
        self.assertIn("merged " + MERGE_COMMIT + "\ncheck exit 0\ndone merged", transcript(armed))
        terminal = invoke()
        # Triage, spec check, Implement, Verify and Review each report their fixture usage.
        self.assertIn("restored raw tokens 85413", terminal)
        self.assertTrue(terminal.endswith("this invocation: no nodes executed\n"))
        # The new run and both resumes read the variables for doctor's quick check, and each of their arms reads the hold live.
        self.assertEqual(self.github.variable_reads, [repo] * 6)
        self.assertEqual(self.github.merge_calls, [(repo, 8, BRANCH, HEAD)])

    def test_whitespace_merge_hold_reports_done(self):
        self.github.hold = " "
        workspace = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8"],
                worker_runner=lambda args, cwd, env=None: workspace.reply(args, checker_reply(args) or next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, workspace),
            )
        self.assertEqual(code, 75)
        self.assertIn("done merge_held\nFOREMAN_MERGE_HOLD is set.\n", stdout.getvalue())

    def test_resumed_report_covers_all_receipts_in_fresh_processes(self):
        script = r'''
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from foreman.cli import main
from foreman.config import ModelSpec, SkillPin, load_config
from foreman.delivery import LiveRun
from test_runner import EMPTY_SLICE, FakeGitHub, FakeWorkspace, worker_reply
from tests.skill_fixture import checker_reply

checkpoint, pin, mode = sys.argv[1:4]

def worker(args, cwd, env):
    if args[0] == "claude":
        schema = json.loads(args[args.index("--json-schema") + 1])
    else:
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    title = schema["title"]
    with Path("calls.log").open("a") as log:
        log.write(title + "\n")
    state = json.loads(args[-1].split("Delivery state:\n")[1])
    if title == "TriageOutput":
        reply = {"triage": "plan"}
    elif title == "SpecOutput":
        if not state.get("human_answers") or mode == "pause":
            reply = {"spec": "Need decision", "spec_gate": "needs_human",
                     "questions": [{"question": "Which items?",
                         "why_code_unanswered": "items.py defines items but no visibility policy.",
                         "checked_paths": ["items.py"]}]}
        else:
            reply = {"spec": "Plan", "spec_gate": "ready", "slices": [EMPTY_SLICE]}
    else:
        reply = checker_reply(args)
    result = worker_reply(args, reply)
    if args[0] == "claude":
        envelope = json.loads(result.stdout)
        envelope["modelUsage"] = {"claude-opus-5-5": {
            "inputTokens": 10, "outputTokens": 3, "cacheReadInputTokens": 100,
            "cacheCreationInputTokens": 2, "costUSD": 0.1,
        }}
        envelope["total_cost_usd"] = 0.1
        if mode == "unknown" and state.get("human_answers"):
            envelope.pop("modelUsage")
            envelope.pop("usage")
        result = type(result)(args, result.returncode, json.dumps(envelope), result.stderr)
    return result

config = replace(load_config(), pstack=SkillPin.model_validate_json(pin), matt_skills=SkillPin.model_validate_json(pin).model_copy(update={"directory": "skills"}))
config.nodes["spec"] = replace(config.nodes["spec"], models=(ModelSpec.parse("claude-opus-5-5-high"),))
with patch("foreman.runner.load_config", return_value=config):
    sys.exit(main(sys.argv[4:], worker_runner=worker, checkpoint=Path(checkpoint),
                  live_run=LiveRun(FakeGitHub(["Ready"]), FakeWorkspace(Path.cwd()))))
'''
        bootstrap = (f"import sys; sys.path[:0] = {[str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent.resolve())]!r}\n")
        for case, cap, mode, code in (("complete", 1000000, "success", 0), ("cap", 45, "success", 75),
                                      ("repaused", 1000000, "pause", 3), ("unknown", 1000000, "unknown", 1)):
            with self.subTest(case=case):
                root = self.cwd / case
                root.mkdir()
                (root / "items.py").write_text("ITEMS = []\n")
                checkpoint = self.cwd.parent / f"{case}.sqlite"

                detail = ""
                def invoke(*flags, expected):
                    nonlocal detail
                    answer = flags[1] if flags[:1] == ("--answer",) else ""
                    process = subprocess.run(
                        [sys.executable, "-c", bootstrap + script, str(checkpoint), self.pin.model_dump_json(), mode,
                         "run", "delivery", "--repo", "a/b", "--issue", "8", *flags],
                        cwd=root, text=True, capture_output=True, env={**planning_answer_env(detail, answer), "FOREMAN_FIXTURE_SETTINGS": json.dumps(
                            {"FOREMAN_HUMAN_LOGIN": "fixture-owner", "FOREMAN_MAX_TOKENS": str(cap), "FOREMAN_MAX_BUDGET_USD": "100"})},
                    )
                    self.assertEqual(process.returncode, expected, process.stderr + process.stdout)
                    detail = process.stdout
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saver.serde = checkpoint_serializer()
                        graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(FakeGitHub(), FakeWorkspace(root)),
                                                 checkpointer=saver)
                        stored = snapshot_result(graph.get_state({"configurable": {"thread_id": "a/b#8"}}, subgraphs=True))
                    return process.stdout, stored["attempts"]

                first, initial = invoke(expected=3)
                self.assertIn(f"work tokens used / work cap: 30 / {cap}; complete; unknown attempts 0", first)
                resumed, attempts = invoke("--answer", "Open items", expected=code)
                restored, current = resumed.split("this invocation:\n")
                self.assertIn("restored raw tokens 230", restored)
                self.assertIn("usage below is cumulative across all invocations", current)
                self.assertNotIn("\ntriage ", current)
                # One spec worker ran in this invocation, whether its output was applied, stopped or asked again.
                self.assertEqual(sum(bool(re.fullmatch(r"spec \S+", line)) for line in current.splitlines()), 1)
                count = 4 if case == "complete" else 3
                self.assertEqual(len(attempts), count)
                self.assertEqual(attempts[:2], initial)
                partial = case == "unknown"
                self.assertIn(f"raw tokens {230 if partial else 115 * count} (known subtotal)", current)
                self.assertIn(f"work tokens used / work cap: {30 if partial else 15 * count} / {cap}; "
                              f"{'partial' if partial else 'complete'}; unknown attempts {int(partial)}", current)
                for index, stage in enumerate(("triage", "spec", "spec", "spec_check")[:count], 1):
                    lines = [line for line in current.splitlines() if line.startswith(f"attempt {index} ")]
                    self.assertEqual(len(lines), 1)
                    self.assertIn(f" {stage} ", lines[0])
                    self.assertIn("work=unknown" if partial and index == 3 else
                                  "input=10 output=3 cache_read=100 cache_creation=2; work=15", lines[0])
                for bucket, total in (("input", 10), ("output", 3), ("cache_read", 100), ("cache_creation", 2)):
                    available = 2 if partial else count
                    self.assertIn(f"{bucket} tokens {total * available}; available {available}/{count}", current)
                if case == "cap":
                    self.assertIn("Run used 45 work tokens; cap is 45", current)
                    self.assertIn("Measured work overshoot: 0.", current)
                if partial:
                    # A missing usage envelope is invalid output, charged at the remaining allowance (D10).
                    self.assertIn("done invalid_output", current)
                    self.assertIn(f"work=unknown (charged {cap - 30})", current)
                    self.assertNotIn("45 /", current)
                if case == "repaused":
                    self.assertIn("paused needs_human", current)
                calls = (root / "calls.log").read_text()
                terminal, reopened = invoke(expected=code)
                self.assertEqual(reopened, attempts)
                self.assertEqual((root / "calls.log").read_text(), calls)
                self.assertEqual(calls.splitlines(), ["TriageOutput", "SpecOutput", "SpecOutput"] + (["SpecCheckOutput"] if count == 4 else []))
                # A replay names the returned issues again after the report.
                self.assertTrue(re.sub(r"(?m)^start \S+\n", "", terminal).endswith("this invocation: no nodes executed\n"))
                for line in current.splitlines():
                    if line.startswith(("raw tokens", "work tokens", "attempt ", "input tokens", "output tokens",
                                        "cache_read tokens", "cache_creation tokens")):
                        self.assertIn("restored " + line, terminal)

    def test_paired_questions_survive_fresh_processes(self):
        script = r'''
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from langgraph.checkpoint.sqlite import SqliteSaver
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import ScriptHandler
from foreman.runner import run_live_delivery
from foreman.state import PlanState, RunState, ChecksState, ReviewState, StartSettings, SCHEMA_VERSION, checkpoint_serializer
from foreman.workspace import LiveWorkspace
from test_runner import FakeGitHub, EMPTY_SLICE
from tests.skill_fixture import fixture_skills

case, pin, answer = sys.argv[1:]
config = replace(load_config(), pstack=SkillPin.model_validate_json(pin), matt_skills=SkillPin.model_validate_json(pin).model_copy(update={"directory": "skills"}))
root = Path.cwd()
workspace = LiveWorkspace(root)
live = LiveRun(FakeGitHub(["Ready"]), workspace, skills=fixture_skills)
checkpoint = root / "run.sqlite"

def dispatch(stage, model, state, instruction):
    with (root / "calls.jsonl").open("a") as log:
        log.write(json.dumps({"stage": stage, "instruction": instruction,
                             "plan": state["plan"].model_dump(),
                             "answers": state.get("human_answers", [])}) + "\n")
    count = len(state.get("human_answers", []))
    if stage == "triage":
        return ScriptHandler(triage="needs_human" if case.startswith("triage") and not count else "plan")(stage, model, state, instruction)
    if stage == "spec":
        pause = count < (2 if case in {"twice", "triage-spec"} else 0 if case == "triage" else 1)
        result = ScriptHandler(spec_gate="needs_human" if pause else "ready", slices=[EMPTY_SLICE])(stage, model, state, instruction)
        if pause:
            result.output["questions"][0]["question"] = f"Policy after {count} answers?"
        return result
    return ScriptHandler()(stage, model, state, instruction)

if case == "split" and not checkpoint.exists():
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        saver.serde = checkpoint_serializer()
        graph = compile_delivery(dispatch, config, run=live, checkpointer=saver)
        graph.update_state({"configurable": {"thread_id": "a/b#8"}}, {
            "schema_version": SCHEMA_VERSION, "pstack_pin": config.pstack.model_dump(), "matt_skills_pin": config.matt_skills.model_dump(),
            "run": RunState(repo="a/b", issue=8, workspace=str(root), base_branch="main", base_sha=workspace.inspect().head), "start": StartSettings(human_login="fixture-owner", max_tokens=1000000, max_budget_usd="3"),
            "checks": ChecksState(check="exit 0"), "review": ReviewState(),
            "plan": PlanState(triage="plan", issue_kind="parent", spec="PRE-SPLIT"),
            "pr_size": {"head": workspace.inspect().head, "merge_base": "a" * 40, "lines": 401},
            "path": ["triage"],
        }, as_node="triage")
with patch("foreman.runner.load_config", return_value=config), patch.object(LiveWorkspace, "validate_origin"):
    result = run_live_delivery(repo="a/b", issue=8, checkpoint=checkpoint,
    dispatcher=dispatch, run=live, answer=answer or None)
print(result["stop"], result.get("halt_detail", ""))
'''
        bootstrap = f"import sys; sys.path[:0] = {[str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent.resolve())]!r}\n"
        for case in ("normal", "triage", "triage-spec", "twice", "split"):
            with self.subTest(case=case):
                root = self.cwd / case
                root.mkdir()
                def git(*args):
                    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
                git("init", "-q", "-b", "main")
                git("remote", "add", "origin", ".")
                (root / ".gitignore").write_text("*\n")
                (root / "items.py").write_text("ITEMS = []\n")
                (root / ".foreman").mkdir()
                (root / CONTRACT_PATH).write_bytes(contract_text())
                (root / VERIFY_SKILL_PATH).parent.mkdir(parents=True)
                (root / VERIFY_SKILL_PATH).write_text("# Verify the web app\n")
                git("add", "-f", ".gitignore", "items.py", CONTRACT_PATH, VERIFY_SKILL_PATH)
                def commit():
                    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture")
                commit()
                detail = ""
                def invoke(answer=""):
                    nonlocal detail
                    process = subprocess.run([sys.executable, "-c", bootstrap + script, case,
                        self.pin.model_dump_json(), answer], cwd=root, capture_output=True, text=True,
                        env=planning_answer_env(detail, answer))
                    self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
                    detail = process.stdout
                    return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
                invoke()
                calls = invoke("Open items")
                if case in {"twice", "triage-spec"}:
                    calls = invoke("Keep sorting")
                specs = [call for call in calls if call["stage"] == "spec"]
                resumed = specs[-1]
                self.assertEqual(sum(call["stage"] == "triage" for call in calls),
                                 0 if case == "split" else 2 if case.startswith("triage") else 1)
                qa = resumed["plan"]["human_qa"]
                self.assertEqual([item["answer"] for item in qa],
                                 ["Open items", "Keep sorting"] if case in {"twice", "triage-spec"} else ["Open items"])
                if case == "triage-spec":
                    self.assertEqual([item["stage"] for item in qa], ["triage", "spec"])
                if case == "triage":
                    self.assertEqual(len(specs), 1)
                else:
                    questions = (["Policy after 0 answers?", "Policy after 1 answers?"]
                                 if case == "twice" else ["Policy after 1 answers?"]
                                 if case == "triage-spec" else ["Policy after 0 answers?"])
                    self.assertEqual([item["question"] for item in qa if item["stage"] == "spec"], [
                        question + "\nWhy code cannot answer: The issue supplies no policy and no checkout file covers it."
                        "\nChecked paths: (none)" for question in questions])

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import _write_check_log, main, run_exit_code
from foreman.delivery import LiveRun
from dataclasses import replace
from unittest.mock import patch
from foreman.config import SkillPin, load_config
from test_runner import APPROVE, HEAD, SYNTHETIC_QUESTION_TOKEN, FakeGitHub, FakeWorkspace, worker_reply, EMPTY_SLICE, slice_body

def run(args, cwd, env):
    prompt = args[-1]
    state = json.loads(prompt.split("Delivery state:\\n")[1])
    if args[0] == "codex":
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    else:
        schema = json.loads(args[args.index("--json-schema") + 1])
    with Path("workers.log").open("a") as log:
        log.write(schema["title"] + "\\n")
    if schema["title"] == "TriageOutput":
        assert not state.get("human_answers")
        assert state["run"]["request"].endswith("List items")
        return worker_reply(args, {"triage": "plan"})
    if schema["title"] == "SpecOutput":
        if not state.get("human_answers"):
            return worker_reply(args, {
                "spec": "A decision is open", "spec_gate": "needs_human",
                "questions": [{"question": f"Which items? {SYNTHETIC_QUESTION_TOKEN}",
                    "why_code_unanswered": "No policy in code.", "checked_paths": []}],
            })
        assert state["human_answers"] == ["Only open items"]
        assert state["run"]["request"].endswith("List items")
        assert state["plan"]["human_qa"][-1]["question"].startswith("Which items? [redacted:GitHub Token]")
        assert "Why code cannot answer:" in state["plan"]["human_qa"][-1]["question"]
        return worker_reply(args, {"spec": "List open items", "spec_gate": "ready", "slices": [EMPTY_SLICE]})
    if schema["title"] == "ImplementOutput":
        return workspace.reply(args, {"implementation": "built"})
    return worker_reply(args, APPROVE)

class Forge(FakeGitHub):
    def arm_auto_merge(self, *args):
        with Path("merges.log").open("a") as log:
            log.write("merge\\n")
        return super().arm_auto_merge(*args)

workspace = FakeWorkspace(Path.cwd(), check="echo checked >> check.log")
forge = Forge()
forge.body = slice_body({**EMPTY_SLICE, "files": ["/missing.py"]})
pin = SkillPin.model_validate_json(sys.argv[2])
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
    sys.exit(main(
        sys.argv[3:], worker_runner=run, checkpoint=Path(sys.argv[1]),
        live_run=LiveRun(forge, workspace),
    ))
"""
        detail = ""
        def invoke(*flags, pin=None, expected=0):
            nonlocal detail
            answer = flags[1] if flags[:1] == ("--answer",) else ""
            bootstrap = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
                         f"sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n")
            process = subprocess.run(
                [sys.executable, "-c", bootstrap + script, str(self.checkpoint), (pin or self.pin).model_dump_json(), "run", "delivery",
                 "--repo", "Example/Engine", "--issue", "8", *flags],
                cwd=self.cwd, text=True, capture_output=True, check=False, env=planning_answer_env(detail, answer),
            )
            self.assertEqual(process.returncode, expected, process.stderr + process.stdout)
            detail = process.stdout
            return process.stdout

        first = invoke("--request", "List items", expected=3)
        self.assertIn("paused needs_human\nWhich items? [redacted:GitHub Token]", first)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, first)
        waiting = invoke(expected=3)
        self.assertIn("restored triage ", waiting)
        self.assertIn("restored spec ", waiting)
        self.assertIn("restored paused needs_human\nrestored Which items? [redacted:GitHub Token]", waiting)
        self.assertIn("this invocation: no nodes executed", waiting)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, waiting)
        self.assertFalse((self.cwd / "check.log").exists())
        completed = invoke("--answer", "Only open items", pin=self.pin.model_copy(update={"commit": "f" * 40}))
        restored, current = completed.split("this invocation:\n")
        self.assertEqual(restored.count("restored triage "), 1)
        self.assertEqual(restored.count("restored spec "), 1)
        self.assertNotIn("\ntriage ", current)
        self.assertEqual(sum(line.startswith("spec ") for line in current.splitlines()), 1)
        self.assertIn("merged " + MERGE_COMMIT, completed)
        self.assertIn("done merged", completed)
        terminal = invoke()
        self.assertIn("restored done merged", terminal)
        self.assertIn("restored merged " + MERGE_COMMIT, terminal)
        self.assertIn("restored check exit 0", terminal)
        self.assertIn("this invocation: no nodes executed", terminal)
        self.assertEqual((self.cwd / "workers.log").read_text().splitlines(),
                         ["TriageOutput", "SpecOutput", "SpecOutput", "SpecCheckOutput", "ImplementOutput", "VerifyOutput", "ReviewOutput"])
        self.assertEqual((self.cwd / "merges.log").read_text(), "merge\n")
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            stored = saver.get({"configurable": {"thread_id": "example/engine#8"}})
        self.assertEqual(stored["channel_values"]["path"],
                         ["triage", "spec", "spec", "spec_check", "prepare", "implement", "check", "commit", "publish", "verify", "review",
                          "merge"])
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")

    def test_restored_snapshot_matches_result_for_merge_and_interrupts(self):
        for replies in ([{"triage": "needs_human", "human_question": "Which items?"}],
                        [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]):
            with self.subTest(replies=replies):
                self.checkpoint = self.cwd.parent / f"{len(replies)}-{type(replies[0]).__name__}.sqlite3"
                expected = self.walk(replies)
                presentation = RunPresentation()
                self.assertEqual(self.walk([], presentation=presentation), expected)
                self.assertEqual(presentation.restored, expected)
                self.assertEqual(presentation.updates, [])

    def test_fresh_process_retries_quota_and_an_older_merge_checkpoint(self):
        script = r'''
import json
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch
from foreman.cli import main
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, worker_reply, fixture_process, priced_quota

phase = sys.argv[3]

def record(event):
    with Path("effects.log").open("a") as log:
        log.write(event + "\n")

class Workspace(FakeWorkspace):
    def commit(self, message):
        record("commit")
        super().commit(message)

    def push(self, branch, repo):
        record("push")
        super().push(branch, repo)

class Forge(FakeGitHub):
    def arm_auto_merge(self, *args):
        record("merge")
        return super().arm_auto_merge(*args)

    def post_review(self, *args, marker):
        record("post_review")
        return super().post_review(*args, marker=marker)

def worker(args, cwd, env):
    if args[0] == "codex":
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    else:
        schema = json.loads(args[args.index("--json-schema") + 1])
    stage = schema["title"]
    record(stage)
    if stage == "SpecCheckOutput":
        return worker_reply(args, {})
    if stage == "TriageOutput":
        return worker_reply(args, {"triage": "supplied_slice"})
    if stage == "ImplementOutput":
        if phase == "quota":
            return priced_quota(args)
        return workspace.reply(args, {"implementation": "built"})
    if stage == "VerifyOutput":
        return worker_reply(args, {})
    assert stage == "ReviewOutput", stage
    return worker_reply(args, APPROVE)

workspace = Workspace(Path.cwd(), check="echo checked >> check.log")
forge = Forge()
forge.hold = "Maintenance" if phase == "held" else ""
pin = SkillPin.model_validate_json(sys.argv[2])
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
    sys.exit(main(sys.argv[4:], worker_runner=worker, checkpoint=Path(sys.argv[1]),
                  live_run=LiveRun(forge, workspace)))
'''
        bootstrap = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
                     f"sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n")
        for scenario in ("held", "quota"):
            with self.subTest(scenario=scenario):
                root = self.cwd / scenario
                root.mkdir()
                checkpoint = self.cwd.parent / f"{scenario}.sqlite3"

                def invoke(phase, expected):
                    process = subprocess.run(
                        [sys.executable, "-c", bootstrap + script, str(checkpoint), self.pin.model_dump_json(),
                         phase, "run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                        cwd=root, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(process.returncode, expected, process.stderr)
                    return process.stdout

                def saved_path():
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saver.serde = checkpoint_serializer()
                        saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})
                    return saved["channel_values"]["path"]

                first = invoke(scenario, 75)
                previous_path = saved_path()
                if scenario == "held":
                    # Replaying the older pre-merge checkpoint ends with exactly the same path.
                    retried = invoke("held", 75)
                    restored, current = retried.split("this invocation:\n")
                    self.assertEqual(saved_path(), previous_path)
                    self.assertIn("restored review ", restored)
                    self.assertEqual(transcript(current).split("\n", 1)[1], "pull request lines 1\nmerge not armed\ncheck exit 0\ndone merge_held\nFOREMAN_MERGE_HOLD is set: Maintenance\n")
                    self.assertEqual((root / "effects.log").read_text().splitlines(),
                                     ["TriageOutput", "SpecCheckOutput", "ImplementOutput", "commit", "push", "VerifyOutput", "ReviewOutput",
                                      "post_review"])
                    self.assertEqual((root / "check.log").read_text(), "checked\n")
                else:
                    self.assertIn("done worker_unavailable", first)
                    self.assertFalse((root / "check.log").exists())
                    attempts = sum(model.family != "grok" for model in load_config().nodes["implement"].models)
                    self.assertEqual((root / "effects.log").read_text().splitlines(),
                                     ["TriageOutput", "SpecCheckOutput"] + ["ImplementOutput"] * attempts)

                resumed = invoke("ready", 0)
                restored, current = resumed.split("this invocation:\n")
                self.assertNotIn("\ntriage ", current)
                if scenario == "held":
                    self.assertEqual(transcript(current).split("\n", 1)[1], "pull request lines 1\nmerged " + MERGE_COMMIT + "\ncheck exit 0\ndone merged\n")
                    expected_workers = ["TriageOutput", "SpecCheckOutput", "ImplementOutput", "commit", "push", "VerifyOutput", "ReviewOutput",
                                        "post_review", "merge"]
                else:
                    # Ready restores the run from before the halted stage, so implement runs once in this invocation.
                    self.assertNotIn("implement", restored)
                    self.assertEqual(sum(line.startswith("implement ") for line in current.splitlines()), 1)
                    self.assertEqual(saved_path(), ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "verify", "review",
                                                    "merge"])
                    expected_workers = (["TriageOutput", "SpecCheckOutput"] + ["ImplementOutput"] * attempts
                                        + ["ImplementOutput", "commit", "push", "VerifyOutput", "ReviewOutput", "post_review", "merge"])
                terminal = invoke("ready", 0)
                self.assertIn("restored merged " + MERGE_COMMIT, terminal)
                self.assertIn("restored check exit 0", terminal)
                self.assertIn("restored done merged", terminal)
                self.assertTrue(terminal.endswith("this invocation: no nodes executed\n"))
                self.assertEqual((root / "effects.log").read_text().splitlines(), expected_workers)
                self.assertEqual((root / "check.log").read_text(), "checked\n")

def comment(body, *, login="fixture-owner", action="created", **issue):
    """An issue_comment delivery for issue 8, as GitHub sends it."""
    return {"action": action, "issue": {"number": 8, **issue}, "repository": {"full_name": "Example/Engine"},
            "comment": {"user": {"login": login}, "body": body}, "sender": {"login": login}}


class WakeTest(unittest.TestCase):
    """Runs that GitHub deliveries start and wake through foreman accept, start and wake, with the runner tests' fixtures."""

    # The runner tests' fixtures, borrowed without inheriting their tests.
    setUpClass = classmethod(LiveRunnerTest.setUpClass.__func__)
    walk = LiveRunnerTest.walk

    def setUp(self):
        LiveRunnerTest.setUp(self)
        self.enterContext(patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(self.cwd / "runtime"), "FOREMAN_APP_ID": "1"}))
        self.enterContext(patch("foreman.cli.credentials.environment", return_value={"GH_TOKEN": "synthetic"}))

    def command(self, argv, replies=(), skills=export_skills):
        workspace = FakeWorkspace(self.cwd)
        workspace.remote, workspace.base_files[CONTRACT_PATH] = self.github.branches, contract_text("exit 0")
        outputs, results = iter(replies), []
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(argv, checkpoint=self.checkpoint, live_run=LiveRun(self.github, workspace, skills=skills), report=results.append,
                        worker_runner=lambda args, cwd, env: workspace.reply(args, checker_reply(args) or next(outputs)))
        return code, out.getvalue(), results[-1] if results else None

    def deliver(self, event, payload):
        path = self.cwd.parent / "delivery.json"
        path.write_text(json.dumps(payload))
        code, out, _ = self.command(["accept", "--event", event, str(path)])
        self.assertEqual(code, 0)
        return out.strip()

    def wake(self, issue=8):
        return self.command(["wake", "--repo", "Example/Engine", "--issue", str(issue)])

    def capped(self):
        """Walk a run whose check passes once, so its PR opens, and then fails until the attempt cap halts the run."""
        counter = self.cwd.parent / "checks"
        check = (f"{sys.executable} -c \"import pathlib, sys; p = pathlib.Path('{counter}'); n = len(p.read_text()) if p.exists() else 0; "
                 "p.write_text('x' * (n + 1)); sys.exit(7 if n else 0)\"")
        built = {"implementation": "built"}
        halted = self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]}, built, built, built], check=check)
        self.assertEqual((halted["stop"], self.github.opened[BRANCH].state), ("verify_cap", "OPEN"))
        return halted

    def github_log(self):
        """Log, in order, the run's reads of its PR and of the issue, and the GitHub writes of its PR's close. The close is
        LiveForge's own, made through GitHub commands answered from the fake. The PR is queued while its armed head waits."""
        github, log, dequeued, texts = self.github, [], [], []
        pull_request_for, issue_comment_records, close = github.pull_request_for, github.issue_comment_records, github.close_pull_request

        def reply(repo, command, **kwargs):
            line = " ".join(command)
            if "dequeuePullRequest" in line:
                log.append("dequeue")
                dequeued.append(True)
                return "{}"
            if "graphql" in line:
                queued = bool(github.merge_calls) and not github.queue_merges and not dequeued
                return json.dumps({"id": "PR_12", "state": github.opened[BRANCH].state,
                                   "mergeQueueEntry": {"id": "queue-12", "state": "QUEUED"} if queued else None})
            if "--method POST" in line:
                log.append("comment")
            elif "--method PATCH" in line:
                log.append("close")
                close(repo, int(command[2].rsplit("/", 1)[1]), texts[-1])
            return json.dumps([[]] if "comments?per_page=100" in line else {"state": "closed"})

        def closing(repo, number, text):
            texts.append(text)
            with patch.object(LiveForge, "_run_command", side_effect=reply):
                LiveForge().close_pull_request(repo, number, text)
        self.enterContext(patch.object(github, "pull_request_for", side_effect=lambda *a: log.append("read PR") or pull_request_for(*a)))
        self.enterContext(patch.object(github, "issue_comment_records",
                                       side_effect=lambda *a: log.append("read issue") or issue_comment_records(*a)))
        self.enterContext(patch.object(github, "close_pull_request", side_effect=closing))
        return log

    def stop_during_call(self, kind):
        """Post the human's stop while the worker call that returns kind runs, and deliver it then, while the run is running."""
        reply, woken = FakeWorkspace.reply, []

        def during(workspace, args, output):
            if kind in output and not woken:
                self.github.ruling("foreman stop")
                woken.append(self.deliver("issue_comment", comment("foreman stop")))
            return reply(workspace, args, output)
        self.enterContext(patch.object(FakeWorkspace, "reply", during))
        return woken


class StopWakeTest(WakeTest):
    """TR16: a stop posted while a worker call runs is never dropped. accept answers its delivery with a wake while the run
    runs, and the wake, made once the run stops running, handles it. The wake's wait for the run's lock is issue_lock's own.
    A stop whose delivery is lost while the run waits on the merge queue is brought in by a check event's wake."""

    def test_a_stop_posted_during_a_call_that_halts_worker_unavailable_ends_the_run(self):
        """TR16's first run: every builder answers quota, so the call ends in a worker_unavailable halt. The stop's wake
        ends the run cancelled with no further worker call."""
        woken = self.stop_during_call("implementation")
        posting = FakeWorkspace.reply

        def out_of_quota(workspace, args, output):
            answered = posting(workspace, args, output)
            return priced_quota(args) if "implementation" in output else answered
        self.enterContext(patch.object(FakeWorkspace, "reply", out_of_quota))
        halted = self.walk([{"triage": "supplied_slice"}, *[{"implementation": "built"}] * 4])
        self.assertEqual((halted["halt"], woken), ("worker_unavailable", ["wake Example/Engine#8"]))
        calls = len(self.calls)
        code, _, stopped = self.wake()
        self.assertEqual((code, stopped["stop"], stopped.get("halt"), len(self.calls)), (1, "cancelled", None, calls))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Done"))

    def test_a_stop_posted_during_a_call_ends_the_run_once_it_stops_running(self):
        """The call ends in a question at implement's gate, and in a halt. Each stop's wake ends the run cancelled with no
        further worker call."""
        woken = self.stop_during_call("implementation")
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built", "human_question": "Keep the sort order?"}])
        self.assertEqual((asked["stop"], woken), ("needs_human", ["wake Example/Engine#8"]))
        calls = len(self.calls)
        code, _, stopped = self.wake()
        self.assertEqual((code, stopped["stop"], len(self.calls)), (1, "cancelled", calls))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Done"))

        self.checkpoint, self.github = self.cwd.parent / "halted.sqlite3", FakeGitHub()
        woken.clear()
        halted = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}], snapshots=(WorktreeState(HEAD, False, BRANCH),))
        self.assertEqual((halted["stop"], woken), ("dirty_worktree", ["wake Example/Engine#8"]))
        calls = len(self.calls)
        code, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], stopped.get("halt"), len(self.calls), self.github.pr_closes), ("cancelled", None, calls, []))
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "ignored")

    def test_a_check_event_brings_in_a_lost_stop_on_a_run_waiting_on_the_merge_queue(self):
        """TR16, run 4: a run waits on the merge queue with its PR open, and the stop's delivery is lost. A check_suite completed
        wakes it. The wake reads the PR, then the stop, removes the PR from the queue, closes it and ends cancelled."""
        self.github.queue_merges = False
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_pending")
        calls, log = len(self.calls), self.github_log()
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("check_suite", check_delivery()), "wake Example/Engine#8")
        _, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], self.github.pr_closes, len(self.calls)), ("cancelled", [("Example/Engine", 12, CANCELLATION)], calls))
        # The read after the close finds whether GitHub merged the PR before the close landed (DQ1).
        self.assertEqual(log, ["read PR", "read issue", "dequeue", "comment", "close", "read PR"])
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Done"))

    def test_a_stop_posted_during_research_finds_the_run_answered(self):
        """The run posts its answer and ends answered, so the stop's wake reads nothing and writes nothing."""
        self.github = FakeGitHub(["Ready"])
        woken = self.stop_during_call("research")
        answered = self.walk([{"triage": "question"}, {"research": "Yes, it sorts."}])
        self.assertEqual((answered["stop"], woken), ("answered", ["wake Example/Engine#8"]))
        writes = list(self.github.status_writes)
        self.github.issue_comment_records = Mock(side_effect=AssertionError("The ended run reads nothing."))
        self.assertEqual(self.wake()[:2], (0, "not waiting example/engine#8\n"))
        self.assertEqual(self.github.status_writes, writes)

    def test_a_failed_stop_read_changes_nothing_and_the_stop_posted_again_ends_the_run(self):
        """GitHub does not answer the runner's stop read on a halted run, and then its close of the PR. Each time the status
        comment keeps the halt and gains the git_failed report above it, naming what failed, and the stop posted again ends
        the run cancelled."""
        self.capped()
        shown, calls = self.github.status["Example/Engine", 8], len(self.calls)
        self.github.ruling("foreman stop")
        records = self.github.issue_comment_records
        self.github.issue_comment_records = Mock(side_effect=ForgeError("gh api timed out."))
        code, _, unchanged = self.wake()
        self.assertNotEqual(code, 0)
        self.assertEqual((unchanged["stop"], self.github.pr_closes, len(self.calls)), ("verify_cap", [], calls))
        report = self.github.status["Example/Engine", 8]
        self.assertTrue(report.startswith("Foreman did not continue this run (git_failed).\n\nThe read of the issue failed because "
                                          "GitHub gave no answer. No setting needs to change. Post the stop again.\n\n"), report)
        self.assertIn(shown.removesuffix(STATUS_MARKER).strip(), report)
        self.github.issue_comment_records = records
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Stopped"))
        with patch.object(self.github, "close_pull_request", side_effect=ForgeError("gh api timed out.")):
            self.assertEqual(self.wake()[2]["stop"], "verify_cap")
        self.assertTrue(self.github.status["Example/Engine", 8].startswith(
            "Foreman did not continue this run (git_failed).\n\nThe close of the pull request failed because GitHub gave no answer."))
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        code, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], self.github.pr_closes, len(self.calls)), ("cancelled", [("Example/Engine", 12, CANCELLATION)], calls))
        self.assertNotIn("git_failed", self.github.status["Example/Engine", 8])


class NumberedGitHub(FakeGitHub):
    """GitHub with one pull request for each issue's run, numbered apart from the issue, each with its own checks and queue."""

    def __init__(self):
        super().__init__()
        self.outcomes: dict[int, str] = {}
        self.landed: set[int] = set()
        self.moved: dict[int, str] = {}
        self.reading: dict[int, Callable[[], None]] = {}

    @staticmethod
    def number(issue):
        return 1000 + issue

    def open_pull_request(self, repo, head, base, title, body):
        super().open_pull_request(repo, head, base, title, body)
        self.opened[head] = self.opened[head].model_copy(update={"number": self.number(int(head.split("-")[0]))})

    def review_target(self, repo, issue, branch, sha):
        return super().review_target(repo, issue, branch, sha).model_copy(update={"number": self.number(issue)})

    def arm_auto_merge(self, repo, issue, branch, sha, observation):
        return super().arm_auto_merge(repo, issue, branch, sha, observation).model_copy(update={"pr": self.number(issue)})

    def readiness(self, repo, issue, branch, sha):
        merged = issue in self.landed and any(call[1] == issue for call in self.merge_calls)
        head = self.moved.get(issue, sha)
        pr = PullSnapshot(number=self.number(issue), state="closed" if merged else "open", draft=False, merged=merged,
                          head={"sha": head, "ref": branch}, base={"sha": self.base, "ref": "main"}, mergeable=True,
                          merge_commit_sha=MERGE_COMMIT if merged else None, mergeable_state="clean")
        outcome = "head_changed" if head != sha else "ready" if merged else self.outcomes.get(issue, "ready")
        if callback := self.reading.pop(issue, None):
            callback()
        return Observation(repo=repo, pr=pr, checks=[], outcome=outcome, recovery="Synthetic.", queue_policy={"merge_method": "SQUASH"})


def check_delivery(event="check_suite", repo="Example/Engine", sha="cd" * 20):
    """A check_suite completed or status delivery, which names a commit and no pull request Foreman can match."""
    payload = {"check_suite": {"head_sha": sha, "pull_requests": []}, "action": "completed"} if event == "check_suite" else {"sha": sha, "state": "success"}
    return {**payload, "repository": {"full_name": repo}}


def pull_delivery(number, action="synchronize", repo="Example/Engine"):
    return {"action": action, "pull_request": {"number": number}, "repository": {"full_name": repo}}


class MergeStageTest(WakeTest):
    """Accept reads whether a saved run is in merge without its lock, through the checkpoint library: the stage its saved
    record has under way or paused in, or for a halted run the stage the halt came from. A run that ended is in no stage."""

    REPLIES = [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]

    def in_merge_during_read(self, replies):
        """Walk the run and return what accept reads while merge's step reads GitHub, with the run's lock held."""
        observe, seen = self.github.readiness, []

        def read(*args):
            seen.append(runs_in_merge(self.checkpoint, {}, "Example/Engine"))
            return observe(*args)
        with patch.object(self.github, "readiness", side_effect=read):
            result = self.walk(replies)
        return result, seen[0]

    def test_a_run_reads_as_in_merge_whatever_its_step_and_however_it_came_there(self):
        """Arm running on first entry from review; observe running after a wake while the old pause is still saved; arm running
        after Ready resumed a halt in merge; a run halted in merge; a run paused at merge's gate. Each reads as in merge, and
        the same runs read as in no stage once they ended merged."""
        merging = [MergeRun(8, 12)]

        def fresh(name):
            self.checkpoint, self.github = self.cwd.parent / f"{name}.sqlite3", FakeGitHub()
        result, running = self.in_merge_during_read(self.REPLIES)
        self.assertEqual((result["stop"], running), ("merged", merging))
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Engine"), [])

        fresh("observe")
        self.github.queue_merges = False
        self.assertEqual(self.walk(self.REPLIES)["stop"], "merge_pending")
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Engine"), merging)
        self.github.queue_merges = True
        result, running = self.in_merge_during_read([])
        self.assertEqual((result["stop"], running), ("merged", merging))

        fresh("halted")
        self.github.hold = "Release freeze."
        self.assertEqual(self.walk(self.REPLIES)["stop"], "merge_held")
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Engine"), merging)
        self.github.hold = None
        self.github.ready_again(8)
        result, running = self.in_merge_during_read([])
        self.assertEqual((result["stop"], running), ("merged", merging))

        fresh("gate")
        observe, foreign = self.github.readiness, "c" * 40
        self.github.foreign_heads.add(foreign)

        def moved(*args):
            reading = observe(*args)
            head = reading.pr.head.model_copy(update={"sha": foreign})
            return reading.model_copy(update={"outcome": "head_changed", "pr": reading.pr.model_copy(update={"head": head})})
        with patch.object(self.github, "readiness", side_effect=moved):
            self.assertEqual(self.walk(self.REPLIES)["stop"], "needs_human")
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Engine"), merging)
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Other"), [])

    def test_the_run_reports_last_step_is_in_merge_whenever_accept_reads_the_run_in_merge(self):
        """Running in merge, waiting in merge, halted in merge and paused at merge's gate: the last entry of the steps the
        issue's run report lists is a merge step whenever accept reads the run as in merge."""
        def reported():
            status = self.github.status["Example/Engine", 8]
            taken = next(line for line in status.splitlines() if line.startswith("Steps taken: "))
            return taken.split()[-1].partition(".")[0], runs_in_merge(self.checkpoint, {}, "Example/Engine")

        def fresh(name):
            self.checkpoint, self.github = self.cwd.parent / f"{name}.sqlite3", FakeGitHub()
        in_merge, seen = ("merge", [MergeRun(8, 12)]), []
        fresh("running")
        readiness = self.github.readiness

        def read(*args):
            seen.append(reported())
            return readiness(*args)
        with patch.object(self.github, "readiness", side_effect=read):
            self.assertEqual(self.walk(self.REPLIES)["stop"], "merged")
        self.assertEqual(seen[0], in_merge)

        fresh("waiting")
        self.github.queue_merges = False
        self.assertEqual((self.walk(self.REPLIES)["stop"], reported()), ("merge_pending", in_merge))

        fresh("halted")
        self.github.hold = "Release freeze."
        self.assertEqual((self.walk(self.REPLIES)["stop"], reported()), ("merge_held", in_merge))

        fresh("gate")
        readiness, foreign = self.github.readiness, "c" * 40
        self.github.foreign_heads.add(foreign)

        def moved(*args):
            reading = readiness(*args)
            head = reading.pr.head.model_copy(update={"sha": foreign})
            return reading.model_copy(update={"outcome": "head_changed", "pr": reading.pr.model_copy(update={"head": head})})
        with patch.object(self.github, "readiness", side_effect=moved):
            self.assertEqual((self.walk(self.REPLIES)["stop"], reported()), ("needs_human", in_merge))

    def test_a_halted_run_reads_as_in_merge_under_the_runners_later_records(self):
        """A run halted merged_unreviewed in merge, with the too-late record a stop's wake writes on top, still reads as in merge."""
        unreviewed = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        unreviewed.pr.state, unreviewed.pr.merged, unreviewed.pr.head.sha = "closed", True, "c" * 40
        readings = iter([self.github.readiness("Example/Engine", 8, BRANCH, HEAD)])
        with patch.object(self.github, "readiness", side_effect=lambda *args: next(readings, unreviewed)):
            halted = self.walk(self.REPLIES)
        self.assertEqual(halted["stop"], "merged_unreviewed")
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Engine"), [MergeRun(8, 12)])
        self.github.opened[BRANCH] = self.github.opened[BRANCH].model_copy(update={"state": "MERGED"})
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        ended = self.wake()[2]
        self.assertEqual((ended["halt"], ended["halt_detail"].endswith(TOO_LATE)), ("merged_unreviewed", True))
        self.assertEqual(runs_in_merge(self.checkpoint, {}, "Example/Engine"), [MergeRun(8, 12)])

    def test_a_run_in_another_stage_or_halted_in_another_stage_is_not_in_merge(self):
        """Review runs, and a run halts in implement: neither is in merge, and neither has a process left waiting on it."""
        reply, seen = FakeWorkspace.reply, []
        review = {**APPROVE}

        def during(workspace, args, output):
            if output is review:
                seen.append(runs_in_merge(self.checkpoint, {}, "Example/Engine"))
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", during):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, review])
        self.assertEqual(seen, [[]])
        self.checkpoint, self.github = self.cwd.parent / "halted.sqlite3", FakeGitHub()
        halted = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}], snapshots=(WorktreeState(HEAD, False, BRANCH),))
        self.assertEqual((halted["stop"], runs_in_merge(self.checkpoint, {}, "Example/Engine")), ("dirty_worktree", []))


class SettledRunsTest(WakeTest):
    """A delivery reads a saved run with no step left, ended or halted, once, and again only when its thread has a newer
    record, so its cost does not grow with them. A run with a step under way or paused is read on every delivery."""

    def reads(self, runs):
        """The runs in merge one delivery finds, and the threads whose saved state it reads."""
        get_state, read = Pregel.get_state, list[str]()

        def counted(graph, config, *args, **kwargs):
            read.append(config["configurable"]["thread_id"])
            return get_state(graph, config, *args, **kwargs)
        with patch.object(Pregel, "get_state", counted):
            found = runs.in_merge("Example/Engine")
        return set(found), {int(thread.rpartition("#")[2]) for thread in read}

    def merge_run(self, issue):
        return self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE], issue=issue)

    def issues(self, *titles):
        return [self.github.create_issue("Example/Engine", title, slice_body({**EMPTY_SLICE, "title": title}), ["slice", "Ready"], marker="")
                for title in titles]

    def saved_runs(self):
        """An ended run, a run halted in merge and run 8 waiting in arm for its check, each read by one delivery."""
        github = self.github = NumberedGitHub()
        ended, held = self.issues("Ended", "Held")
        github.landed.add(ended)
        self.assertEqual(self.merge_run(ended)["stop"], "merged")
        github.hold = "Release freeze."
        self.assertEqual(self.merge_run(held)["stop"], "merge_held")
        github.hold, github.outcomes[8] = None, "pending"
        self.assertEqual(self.merge_run(8)["stop"], "merge_pending")
        runs, merging = CheckpointRuns(self.checkpoint), {MergeRun(held, github.number(held)), MergeRun(8, github.number(8))}
        self.assertEqual(self.reads(runs), (merging, {ended, held, 8}))
        return runs, ended, held, merging

    def test_an_ended_or_halted_run_is_not_read_again_and_a_waiting_run_is_read_on_every_delivery(self):
        runs, _, _, merging = self.saved_runs()
        self.assertEqual(self.reads(runs), (merging, {8}))
        self.assertEqual(self.reads(runs), (merging, {8}))

    def test_ready_resuming_a_halted_run_makes_it_read_again(self):
        runs, _, held, _ = self.saved_runs()
        self.github.landed.add(held)
        self.github.ready_again(held)
        self.assertEqual(self.walk([], issue=held)["stop"], "merged")
        waiting = {MergeRun(8, self.github.number(8))}
        self.assertEqual(self.reads(runs), (waiting, {held, 8}))
        self.assertEqual(self.reads(runs), (waiting, {8}))

    def test_a_run_forgotten_is_dropped_and_a_new_run_in_its_thread_is_read(self):
        runs, ended, _, merging = self.saved_runs()
        thread = issue_thread("Example/Engine", ended)
        forget_run(self.checkpoint, thread)
        self.assertEqual(self.reads(runs), (merging, {8}))
        self.assertNotIn(thread, runs.settled)
        self.github.landed.discard(ended)
        self.github.hold = "Release freeze."
        self.assertEqual(self.merge_run(ended)["stop"], "merge_held")
        self.assertEqual(self.reads(runs), (merging | {MergeRun(ended, self.github.number(ended))}, {ended, 8}))

    def test_a_too_late_record_on_a_halted_run_makes_it_read_again(self):
        """A stop's wake records too late on top of a run halted merged_unreviewed, so the next delivery reads it again."""
        runs = CheckpointRuns(self.checkpoint)
        unreviewed = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        unreviewed.pr.state, unreviewed.pr.merged, unreviewed.pr.head.sha = "closed", True, "c" * 40
        readings = iter([self.github.readiness("Example/Engine", 8, BRANCH, HEAD)])
        with patch.object(self.github, "readiness", side_effect=lambda *args: next(readings, unreviewed)):
            self.assertEqual(self.walk(MergeStageTest.REPLIES)["stop"], "merged_unreviewed")
        merging = {MergeRun(8, 12)}
        self.assertEqual(self.reads(runs), (merging, {8}))
        self.assertEqual(self.reads(runs), (merging, set()))
        self.github.opened[BRANCH] = self.github.opened[BRANCH].model_copy(update={"state": "MERGED"})
        self.github.ruling("foreman stop")
        self.assertTrue(self.wake()[2]["halt_detail"].endswith(TOO_LATE))
        self.assertEqual(self.reads(runs), (merging, {8}))
        self.assertEqual(self.reads(runs), (merging, set()))

    def test_the_receiver_reads_every_saved_run_before_it_listens(self):
        self.assertEqual(self.walk(MergeStageTest.REPLIES)["stop"], "merged")
        first = list[tuple[set[MergeRun], set[int]]]()

        def listening(address, secret, runs, *rest):
            first.append(self.reads(runs))
            raise OSError("synthetic stop")
        with patch("foreman.cli.webhook_secret", return_value=b"synthetic"), patch("foreman.cli.listener", listening):
            self.assertEqual(self.command(["receive", "--port", "0"])[0], 1)
        self.assertEqual(first, [(set(), set())])


def refusal(thread, version):
    """The refusal of a run saved under another schema, in the designer's words (fable-receiver-old-schema-ruling.md)."""
    release = "an older" if version is None or version < SCHEMA_VERSION else "another"
    found = "missing" if version is None else repr(version)
    return (f"The run saved for {thread} was written by {release} Foreman release (schema_version {found}; this release reads "
            f"{SCHEMA_VERSION}). This release cannot read, wake or resume it, and it started nothing. To continue that run, use "
            "the release that saved it. This release cannot start a new run for this issue while that record is in its store. "
            "The record is left untouched, never deleted or migrated.")


class UnreadableRunTest(WakeTest):
    """A saved run this release cannot read is in no stage: it never stops the receiver or a delivery for another run, and only
    a delivery for its own issue is refused (fable-receiver-old-schema-ruling.md)."""

    def setUp(self):
        super().setUp()
        github = self.github = NumberedGitHub()
        self.old = github.create_issue("Example/Engine", "Old", slice_body({**EMPTY_SLICE, "title": "Old"}), ["slice", "Ready"], marker="")
        github.hold = "Release freeze."
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE], issue=self.old)["stop"], "merge_held")
        github.hold, github.outcomes[8] = None, "pending"
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_pending")
        # The old run's latest record would read as halted in merge, but this release reads nothing of it but its schema.
        self.thread = issue_thread("Example/Engine", self.old)
        self.save_old(schema_version=SCHEMA_VERSION - 1)
        self.refusal = refusal(self.thread, SCHEMA_VERSION - 1)
        self.ready = {"action": "labeled", "label": {"name": "Ready"}, "issue": {"number": self.old, "labels": [{"name": "Ready"}]},
                      "repository": {"full_name": "Example/Engine"}, "sender": {"login": "fixture-owner"}}
        self.stop = comment("foreman stop", number=self.old)

    def save_old(self, **values):
        """Overwrite values on the old run's latest record."""
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            saved = saver.get_tuple({"configurable": {"thread_id": self.thread}})
            saved.checkpoint["channel_values"].update(values)
            saver.put(saved.config, saved.checkpoint, saved.metadata, {})

    def receiver(self):
        """The receiver as foreman receive builds it, after its fill, answering deliveries; each answer only is recorded."""
        built, started = list[CheckpointRuns](), list[list[str]]()

        def listening(address, secret, runs, *rest):
            built.append(runs)
            raise OSError("synthetic stop")
        with patch("foreman.cli.webhook_secret", return_value=b"synthetic"), patch("foreman.cli.listener", listening):
            self.assertEqual(self.command(["receive", "--port", "0"])[0], 1)
        self.assertEqual(len(built), 1, "the receiver did not reach its listener")
        server = listener(("127.0.0.1", 0), b"synthetic", built[0], lambda args: started.append(args) or 0, lambda repo, issue: [])
        self.enterContext(server)
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        self.addCleanup(serving.join)
        self.addCleanup(server.shutdown)

        def post(event, payload):
            body = json.dumps(payload).encode()
            signature = "sha256=" + hmac.new(b"synthetic", body, hashlib.sha256).hexdigest()
            request = Request(f"http://127.0.0.1:{server.server_address[1]}/", body,
                              {"X-GitHub-Event": event, "X-GitHub-Delivery": "d-1", "X-Hub-Signature-256": signature})
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    with urlopen(request, timeout=10) as response:
                        reply = response.status, response.read().decode().strip()
                except HTTPError as exc:
                    reply = exc.code, exc.read().decode().strip()
                while runs := [thread for thread in threading.enumerate() if thread.name.startswith("foreman ")]:
                    for run in runs:
                        run.join()
            return reply
        return post, started

    def test_the_receiver_starts_beside_it_and_a_check_wakes_the_run_waiting_in_arm(self):
        post, started = self.receiver()
        self.assertEqual(post("check_suite", check_delivery()), (202, "wake Example/Engine#8"))
        self.assertEqual(started, [["wake", "--repo", "Example/Engine", "--issue", "8"]])

    def test_it_is_read_once(self):
        get_tuple, read = SqliteSaver.get_tuple, list[str]()

        def counted(saver, config):
            read.append(config["configurable"]["thread_id"])
            return get_tuple(saver, config)
        runs = CheckpointRuns(self.checkpoint)
        with patch.object(SqliteSaver, "get_tuple", counted):
            runs.fill()
            self.assertEqual(read.count(self.thread), 1)
            for _ in range(2):
                self.assertEqual(runs.in_merge("Example/Engine"), [MergeRun(8, NumberedGitHub.number(8))])
        self.assertEqual(read.count(self.thread), 1)

    def test_a_ready_and_a_comment_for_its_issue_are_refused_and_the_next_delivery_is_handled(self):
        post, started = self.receiver()
        self.assertEqual(post("issues", self.ready), (409, self.refusal))
        self.assertEqual(post("issue_comment", self.stop), (409, self.refusal))
        self.assertEqual(started, [])
        self.assertEqual(post("check_suite", check_delivery()), (202, "wake Example/Engine#8"))
        self.assertEqual(started, [["wake", "--repo", "Example/Engine", "--issue", "8"]])

    def test_foreman_accept_answers_the_same(self):
        for event, payload in (("issues", self.ready), ("issue_comment", self.stop)):
            with self.subTest(event=event):
                path = self.cwd.parent / "delivery.json"
                path.write_text(json.dumps(payload))
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    code, out, _ = self.command(["accept", "--event", event, str(path)])
                self.assertEqual((code, out, err.getvalue().strip()), (1, "", self.refusal))
        self.assertEqual(self.deliver("check_suite", check_delivery()), "wake Example/Engine#8")

    def test_foreman_start_and_wake_refuse_it_and_read_nothing_else(self):
        """Even a record that would read as ended is refused: whether the old run ended is not read."""
        self.save_old(stop="merged", halt=None)
        calls = len(self.calls)
        for command in ("start", "wake"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()) as err:
                code, out, _ = self.command([command, "--repo", "Example/Engine", "--issue", str(self.old)])
                self.assertEqual((code, out, err.getvalue().strip()), (1, "", self.refusal))
        self.assertEqual(len(self.calls), calls)

    def test_whether_ready_resumes_it_is_refused_too(self):
        """The old run's record would read as halted in merge, which Ready resumes, but whether Ready resumes it is not read."""
        with self.assertRaises(CheckpointVersionError) as raised:
            ready_resumes(self.checkpoint, self.thread)
        self.assertEqual(str(raised.exception), self.refusal)


class FirstSaveTest(WakeTest):
    """A run's first record, at step -1, holds only its input, as a pending write, so a delivery that reads the store before
    the run saves its next record finds no run yet, not a run saved under another schema."""

    def test_a_read_at_the_first_record_finds_no_run_yet(self):
        thread, reads, put = issue_thread("Example/Engine", 8), list[tuple[object, object]](), SqliteSaver.put

        def read_then_put(saver, config, *args):
            if config["configurable"]["checkpoint_ns"] == "":
                reads.append((open_run(self.checkpoint, thread), runs_in_merge(self.checkpoint, {}, "Example/Engine")))
            return put(saver, config, *args)
        with patch.object(SqliteSaver, "put", read_then_put):
            self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merged")
        self.assertEqual(reads[:2], [(None, []), (None, [])])

    def first_step(self, thread):
        """The run's saved state after its first saved step, and every record and pending write its thread holds."""
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            records = list(saver.list({"configurable": {"thread_id": thread}}))
        first = next((record.checkpoint["channel_values"] for record in reversed(records)
                      if record.config["configurable"]["checkpoint_ns"] == "" and record.metadata["source"] == "loop"), None)
        return first, [(record.config, record.pending_writes) for record in records]

    def test_ready_after_a_crash_at_the_first_record_starts_the_run_an_empty_thread_starts(self):
        """A host crash between a run's first and second save leaves only the step -1 record and its pending input. A wake then
        finds no run and writes nothing. The next Ready starts one run, with its own doctor read, whose saved state after its
        first step is the one the same start saves on a thread with no record."""
        thread, put, replies = issue_thread("Example/Engine", 8), SqliteSaver.put, [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]

        def crash_after_first_save(saver, config, checkpoint, metadata, *args):
            if config["configurable"]["checkpoint_ns"] == "" and metadata.get("step") != -1:
                raise OSError("synthetic host crash")
            return put(saver, config, checkpoint, metadata, *args)
        with patch.object(SqliteSaver, "put", crash_after_first_save), self.assertRaisesRegex(OSError, "synthetic host crash"):
            self.walk([], preflight="echo left over")
        dead = self.first_step(thread)
        self.assertEqual(dead[0], None)
        self.assertEqual([config["configurable"]["checkpoint_ns"] for config, _ in dead[1]], [""])
        self.assertTrue(dead[1][0][1])
        reads, calls = len(self.github.variable_reads), len(self.calls)
        self.assertEqual(self.wake()[:2], (0, f"not waiting {thread}\n"))
        self.assertEqual((self.first_step(thread), self.github.variable_reads[reads:], self.calls[calls:]), (dead, [], []))

        self.assertEqual(self.walk(replies)["stop"], "merged")
        self.assertEqual(self.github.variable_reads[reads:], ["Example/Engine"] * 2)
        self.assertGreater(len(self.calls), calls)
        restarted = self.first_step(thread)[0]
        self.checkpoint = self.cwd.parent / "empty.sqlite3"
        self.assertEqual(self.walk(replies)["stop"], "merged")
        fresh = self.first_step(thread)[0]
        self.assertIsNotNone(fresh)
        self.assertEqual(restarted, fresh)
        self.assertEqual(restarted["checks"].preflight, "")

    def crash_in_first_save(self):
        """Start a run that the host crash cuts off after the library saves its input record and before the run's next save."""
        put = SqliteSaver.put

        def crash(saver, config, checkpoint, metadata, *args):
            if config["configurable"]["checkpoint_ns"] == "" and metadata["source"] != "input":
                raise OSError("synthetic host crash")
            return put(saver, config, checkpoint, metadata, *args)
        with patch.object(SqliteSaver, "put", crash), self.assertRaisesRegex(OSError, "synthetic host crash"):
            self.walk([], preflight="echo left over")

    def records(self, thread):
        """Every record the thread holds, newest first: its graph, kind, step, values and pending writes."""
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            return [(record.config["configurable"]["checkpoint_ns"].partition(":")[0], record.metadata["source"], record.metadata["step"],
                     record.checkpoint["channel_values"], [(channel, value) for _, channel, value in record.pending_writes or []])
                    for record in saver.list({"configurable": {"thread_id": thread}})]

    def history(self, thread):
        """The thread's records apart from ids and the skills' scratch directory, which differ between any two starts."""
        return re.sub(r"foreman-skills-[\w-]+|[0-9a-f]{32,64}", "<id>", repr(self.records(thread)))

    def where(self, thread):
        """The saved run's next steps and its stage."""
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            graph, config = shape(saver), {"configurable": {"thread_id": thread}}
            saved = graph.get_state(config)
            return saved.next, saved_stage(graph, config, saved)

    def test_a_second_crash_in_the_new_runs_first_save_leaves_the_issue_startable(self):
        """Ready after a crash empties the thread before the new run saves, so a second crash in the same window leaves what
        the first left: a wake finds no run and writes nothing, and the next Ready starts one run."""
        thread = issue_thread("Example/Engine", 8)
        self.crash_in_first_save()
        self.crash_in_first_save()
        self.assertEqual([record[1:3] for record in self.records(thread)], [("input", -1)])
        self.assertIsNone(open_run(self.checkpoint, thread))
        dead, reads, calls = self.history(thread), len(self.github.variable_reads), len(self.calls)
        self.assertEqual(self.wake()[:2], (0, f"not waiting {thread}\n"))
        self.assertEqual((self.history(thread), self.github.variable_reads[reads:], self.calls[calls:]), (dead, [], []))
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merged")
        self.assertEqual([record[1] for record in self.records(thread) if not record[0]].count("input"), 1)

    def test_a_start_on_a_dead_thread_that_halts_skill_missing_ends_as_on_an_empty_thread(self):
        """The halt is in triage. A stop ends the run cancelled with no step left, so the smoke's read finds it not waiting,
        runs_in_merge settles it, and a later foreman run saves no record."""
        thread, found = issue_thread("Example/Engine", 8), {}
        for dead in (False, True):
            self.checkpoint, self.github = self.cwd.parent / f"skill-missing-{dead}.sqlite3", FakeGitHub()
            if dead:
                self.crash_in_first_save()
            halted = self.walk([], skills=Mock(side_effect=SkillMissing(self.pin)))
            stage = self.where(thread)
            self.github.ruling("foreman stop")
            stopped, settled = self.wake()[2], {}
            runs_in_merge(self.checkpoint, settled)
            saved, stopped_history, calls = saved_run(self.checkpoint, thread), self.history(thread), len(self.calls)
            self.walk([])
            found[dead] = (halted["stop"], stage, stopped["stop"], self.where(thread), saved and saved.waiting, thread in settled,
                           self.history(thread) == stopped_history, self.calls[calls:])
        self.assertEqual(found[True], ("skill_missing", ((), "triage"), "cancelled", ((), None), False, True, True, []))
        self.assertEqual(found[True], found[False])

    def test_a_start_on_a_dead_thread_saves_the_history_the_same_start_saves_on_an_empty_thread(self):
        """Nothing of the dead start's record or pending input is left on the thread, so the new run's whole history is its own."""
        thread = issue_thread("Example/Engine", 8)
        starts = {"merged": lambda: self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]),
                  "skill_missing": lambda: self.walk([], skills=Mock(side_effect=SkillMissing(self.pin)))}
        for stop, start in starts.items():
            with self.subTest(stop=stop):
                histories = []
                for dead in (True, False):
                    self.checkpoint, self.github = self.cwd.parent / f"{stop}-{dead}.sqlite3", FakeGitHub()
                    if dead:
                        self.crash_in_first_save()
                    self.assertEqual(start()["stop"], stop)
                    histories.append(self.history(thread))
                self.assertNotIn("echo left over", histories[0])
                self.assertEqual(histories[0], histories[1])


class MergeWakeTest(WakeTest):
    """ME14: GitHub's pull request and check deliveries wake the runs in merge of one repository. Accept matches a pull
    request by the number saved on the run and a finished check by repository alone, and each wake decides under its run's
    lock from the saved run."""

    def woken(self, event, payload):
        line = self.deliver(event, payload)
        return set() if line == "ignored" else {int(answer.rpartition("#")[2]) for answer in line.split(", ")}

    def merge_run(self, issue, **kwargs):
        return self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, kwargs.pop("review", APPROVE)], issue=issue, **kwargs)

    def test_each_delivery_wakes_the_runs_in_merge_it_names_and_each_wake_decides_from_github(self):
        github = self.github = NumberedGitHub()
        two, three, four, five, six, seven = (github.create_issue("Example/Engine", title, slice_body({**EMPTY_SLICE, "title": title}),
                                                                  ["slice", "Ready"], marker="")
                                              for title in ("Two", "Three", "Four", "Five", "Six", "Seven"))
        github.outcomes[1] = github.outcomes[8] = "pending"
        self.assertEqual(self.merge_run(8)["stop"], "merge_pending")
        self.assertEqual((self.merge_run(two)["stop"], [call[1] for call in github.merge_calls]), ("merge_pending", [two]))
        github.hold = "Release freeze."
        self.assertEqual(self.merge_run(three)["stop"], "merge_held")
        github.hold = None
        github.moved[four], heard = "c" * 40, {}
        github.foreign_heads.add("c" * 40)
        self.assertEqual(self.merge_run(four)["stop"], "needs_human")
        waiting = {8, two, three, four}

        github.outcomes[five] = "pending"

        def delivered(name):
            def deliver():
                heard[name] = self.woken("check_suite", check_delivery())
                github.outcomes[five] = "ready"  # The check passed after arm's read and before its pause.
            return deliver
        github.reading[five] = delivered("five")
        self.assertEqual(self.merge_run(five)["stop"], "merge_pending")
        reply, review = FakeWorkspace.reply, {**APPROVE}

        def during(workspace, args, output):
            if output is review:
                heard["six"] = self.woken("check_suite", check_delivery(sha=HEAD))
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", during):
            self.assertEqual(self.merge_run(six, review=review)["stop"], "merge_pending")
        github.landed.add(seven)
        github.reading[seven] = delivered("seven")
        self.assertEqual(self.merge_run(seven)["stop"], "merged")
        self.assertEqual(heard, {"five": waiting | {five}, "six": waiting | {five}, "seven": waiting | {five, six, seven}})

        reads, statuses = len(github.variable_reads), list(github.status_writes)
        self.enterContext(patch("foreman.runner.repository", side_effect=AssertionError("A wake runs no doctor.")))
        records = github.issue_comment_records
        with patch.object(github, "issue_comment_records", side_effect=AssertionError("The ended run reads nothing.")):
            self.assertEqual(self.wake(seven)[:2], (0, f"not waiting example/engine#{seven}\n"))
        github.issue_comment_records = records
        code, _, armed = self.wake(five)
        self.assertEqual((code, armed["stop"], [call[1] for call in github.merge_calls].count(five)), (75, "merge_pending", 1))

        def wake_all(woken):
            return {issue: self.wake(issue)[2] for issue in sorted(woken)}
        github.outcomes[8] = "ready"
        armed_before, merge_calls, statuses = list(github.merge_calls), [], list(github.status_writes)
        for event, payload in (("check_suite", check_delivery(sha=HEAD)), ("check_suite", check_delivery(sha=HEAD)),
                               ("status", check_delivery("status", sha=MERGE_COMMIT)), ("check_suite", check_delivery(sha="9" * 40))):
            woken = self.woken(event, payload)
            self.assertEqual(woken, {8, two, three, four, five, six})
            results = wake_all(woken)
            merge_calls.append([call[1] for call in github.merge_calls[len(armed_before):]])
            self.assertEqual((results[three]["stop"], results[four]["stop"]), ("merge_held", "needs_human"))
        self.assertEqual(merge_calls, [[8]] * 4)  # Run one arms once, on the first wake after its check passed.
        writes = [write for write in github.status_writes[len(statuses):] if write[1] != 8]
        self.assertEqual((writes, github.pr_closes), ([], []))

        github.landed.add(two)
        self.assertEqual(self.woken("pull_request", pull_delivery(github.number(two), "closed")), {two})
        self.assertEqual(wake_all({two})[two]["stop"], "merged")
        statuses = list(github.status_writes)
        for issue in (three, four):
            self.assertEqual(self.woken("pull_request", pull_delivery(github.number(issue))), {issue})
            # The wake reads the pull request and then the issue, finds no stop and no answer, and resumes nothing.
            with (patch.object(github, "pull_request_for", wraps=github.pull_request_for) as pull,
                  patch.object(github, "issue_comment_records", wraps=records) as comments):
                self.assertEqual(wake_all({issue})[issue]["stop"], {three: "merge_held", four: "needs_human"}[issue])
            self.assertEqual((pull.call_count > 0, comments.call_count > 0), (True, True))
        self.assertEqual(github.status_writes, statuses)
        self.assertEqual(self.woken("pull_request", pull_delivery(github.number(8), repo="Example/Other")), set())
        self.assertEqual(self.woken("pull_request", pull_delivery(8)), set())
        self.assertEqual(self.woken("pull_request_review", pull_delivery(github.number(8), "submitted")), {8})
        # A wake reads FOREMAN_MERGE_HOLD only where arm arms, for runs five and one, and no wake reads the login or a cap variable.
        self.assertEqual(len(github.variable_reads), reads + 2)


class SkillsWakeTest(WakeTest):
    """A gate answer's or a merge wake's continuation whose skills cannot be exported leaves its run as saved, and the
    command's exit and error output are the only record (#426)."""

    def failed_wake(self, source="pstack"):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code, out, result = self.command(["wake", "--repo", "Example/Engine", "--issue", "8"],
                                             skills=Mock(side_effect=SkillMissing(self.pin, source=source)))
        return code, out, errors.getvalue(), result

    def saved(self):
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            return saver.get_tuple({"configurable": {"thread_id": "example/engine#8"}}).config

    def assert_not_continued(self, code, out, errors, result, saved, source="pstack"):
        self.assertEqual((code, result["stop"]), (1, "skill_missing"))
        # Error output is redacted, so each saved pin's commit is printed literally only on the command's output.
        self.assertIn(f"skills not exported: {source} pin unavailable, commit [redacted:Hex High Entropy String]. The run was not continued.", errors)
        self.assertIn(f"pstack commit {self.pin.commit}", out)
        self.assertIn(f"matt_skills commit {self.pin.commit}", out)
        self.assertEqual(self.saved(), saved)

    def test_a_gate_answer_whose_skills_cannot_be_exported_exits_naming_the_pin(self):
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"},
                           {**APPROVE, "human_question": "Should the list include archived items?"}])
        self.assertEqual(asked["stop"], "needs_human")
        saved = self.saved()
        self.github.answer(asked["halt_detail"], "Active items only.")
        self.assert_not_continued(*self.failed_wake(), saved)

    def test_a_merge_wake_whose_skills_cannot_be_exported_exits_naming_the_pin(self):
        self.github.queue_merges = False
        original = self.github.readiness
        pending = lambda *args: original(*args).model_copy(update={"outcome": "pending", "recovery": "Wait for checks."})
        self.enterContext(patch.object(self.github, "readiness", side_effect=pending))
        waiting = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(waiting["stop"], "merge_pending")
        saved = self.saved()
        self.assert_not_continued(*self.failed_wake(), saved)

    def test_a_wake_whose_matt_skills_pin_cannot_be_exported_names_that_pin_and_its_commit(self):
        asked = self.walk([{"triage": "supplied_slice"}, {"implementation": "built"},
                           {**APPROVE, "human_question": "Should the list include archived items?"}])
        saved = self.saved()
        self.github.answer(asked["halt_detail"], "Active items only.")
        self.assert_not_continued(*self.failed_wake("matt_skills"), saved, source="matt_skills")


class HaltExitCodeTest(unittest.TestCase):
    def live(self, replies=(), *, snapshots=(), body=None, check="exit 0", preflight="", hold=None, labels=None):
        confine_host(self)
        root = Path(self.enterContext(TemporaryDirectory())) / "checkout"
        root.mkdir()
        forge = FakeGitHub(labels)
        forge.hold = hold
        forge.settings["FOREMAN_PREFLIGHT"] = preflight
        if body is not None:
            forge.body = body
        outputs = iter(replies)
        workspace = FakeWorkspace(root, snapshots, check=check)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"],
                checkpoint=root.parent / "runs.sqlite3", live_run=LiveRun(forge, workspace, skills=fixture_skills),
                worker_runner=(lambda args, cwd, env: workspace.reply(args, checker_reply(args) or next(outputs))),
            )
        return code, output.getvalue()

    def assert_halt(self, result, reason, detail, code=1):
        self.assertEqual(result[0], code, result[1])
        self.assertIn(f"done {reason}\n{detail}\n", result[1])

    def test_dirty_worktree_after_commit(self):
        self.assert_halt(self.live(
            [{"triage": "supplied_slice"}, {"implementation": "built"}],
            snapshots=(WorktreeState(HEAD, False, BRANCH),),
        ), "dirty_worktree", f"The checkout differs from the checked commit {HEAD} on {BRANCH}. It is preserved.", 75)

    def test_worktree_inspection_failure_after_commit(self):
        self.assert_halt(self.live(
            [{"triage": "supplied_slice"}, {"implementation": "built"}],
            snapshots=(WorktreeInspectionFailed("Invalid HEAD"),),
        ), "git_failed", "Invalid HEAD", 75)

    def test_verify_cap(self):
        self.assert_halt(self.live(
            [{"triage": "supplied_slice"}] + [{"implementation": "built"}] * 3, check="exit 7",
        ), "verify_cap", "Verification failed after 3 rounds. Check exit 7 failed.")

    def test_review_cap(self):
        changes = {**APPROVE, "findings": [FINDING]}
        unresolved = changes
        self.assert_halt(self.live(
            [{"triage": "supplied_slice"}] + [{"implementation": "built"}, changes]
            + [{"implementation": "built"}, unresolved] * 2,
        ), "review_cap", "Review requested changes after 3 rounds. Last review: " + "P1 Add a check.")

    def test_missing_slice_stops_at_admit_before_any_worker(self):
        result = self.live(body="No JSON")
        self.assert_halt(result, "missing_slice", "slice issue needs a fenced JSON object")
        self.assertEqual(transcript(result[1]), "claude spend $0 of $3\nadmit\ndone missing_slice\nslice issue needs a fenced JSON object\n")

    def test_resumable_halts(self):
        replies = [{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE]
        self.assert_halt(self.live(replies, hold="Maintenance"), "merge_held", "FOREMAN_MERGE_HOLD is set: Maintenance", 75)

    def test_environment_halts_have_detail_and_resumable_exit(self):
        self.assert_halt(self.live(
            [{"triage": "supplied_slice"}], preflight="exit 9",
        ), "env_unavailable", "Preflight 'exit 9' exited with 9", 75)
        for command, code, reason in (
            ("./missing-check", 127, "command not found"),
            ("./non-executable", 126, "command is not executable"),
        ):
            with self.subTest(code=code):
                result = self.live(
                    [{"triage": "supplied_slice"}, {"implementation": "built"}], check=command,
                    preflight="touch non-executable" if code == 126 else "",
                )
                self.assert_halt(result, "env_unavailable", f"Check {command!r} exited with {code} ({reason})", 75)

    def test_invalid_merge_sha_halts_with_detail_and_resumable_exit(self):
        detail = "Merge arming requires a full 40-hex commit SHA."
        with self.assertRaisesRegex(MergeNotArmed, detail):
            LiveForge().arm_auto_merge("Example/Engine", 8, BRANCH, "invalid", FakeGitHub().readiness("Example/Engine", 8, BRANCH, HEAD))
        result = {"review": ReviewState(), "checks": ChecksState(), "run": RunState(repo="Example/Engine"), **halted(Halt.merge_not_armed, detail, "merge"), "stop": "merge_not_armed", "path": ["merge"], "merge": MergeState(status="not_armed"), "plan": PlanState()}
        with patch("foreman.cli.run_live_delivery", side_effect=lambda **kwargs: (kwargs["report"](result), result)[1]), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8"])
        self.assert_halt((code, output.getvalue()), "merge_not_armed", detail, 75)

    def test_answered_is_success(self):
        code, output = self.live([{"triage": "question"}, {"research": "The answer."}], labels=["Ready"])
        self.assertEqual(code, 0, output)
        self.assertIn("done answered\n", output)

    def test_empty_details_are_rejected(self):
        for detail in ("", " ", "\n"):
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, "requires a nonempty halt detail"):
                halted(Halt.git_failed, detail, "triage")

    def test_dry_run_success_and_human_stops(self):
        for handler, stop, expected in (
            (ScriptHandler(), "merge_pending", 0),
            (ScriptHandler(triage="plan", slices=[EMPTY_SLICE]), "children_filed", 0),
            (ScriptHandler(triage="needs_human"), "needs_human", 3),
            (ScriptHandler(triage="plan", spec_gate="needs_human"), "needs_human", 3),
        ):
            with self.subTest(stop=stop), patch("foreman.cli.ScriptHandler", return_value=handler):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = main(["run", "delivery", "--repo", "Example/Engine", "--request", "test", "--dry-run"])
                self.assertEqual(code, expected, output.getvalue())
                self.assertIn(f"done {stop}\n", output.getvalue())
                if stop == "needs_human" and handler.spec_gate == "needs_human":
                    self.assertIn("Which policy should apply?\nWhy code cannot answer:", output.getvalue())
                elif stop == "needs_human":
                    self.assertIn("done needs_human\nWhich scope should this change cover?\n", output.getvalue())


class HaltedRunTest(unittest.TestCase):
    """A halted live run through a Forge that records every call: what it saves, reads and reports, and that it files nothing."""

    def setUp(self):
        # The checkpoint sits beside the checkout, so the run's scratch stays in the test's own directory.
        self.root = Path(self.enterContext(TemporaryDirectory())) / "checkout"
        self.root.mkdir()
        self.checkpoint = self.root.parent / "runs.sqlite3"
        self.fake = FakeGitHub()
        self.forge = Mock(spec=FakeGitHub, wraps=self.fake)
        self.run = LiveRun(self.forge, FakeWorkspace(self.root), skills=fixture_skills)
        self.flags = ["run", "delivery", "--repo", "Example/Engine", "--issue", "8"]

    def invoke(self, **kwargs):
        if saved_workspace(self.checkpoint, "example/engine#8") is None:
            self.fake.ready_again(8)  # A fresh run starts from Ready.
        failed = lambda *args: Outcome("failed", detail="bad output", usage=SCRIPTED_USAGE)
        return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=self.checkpoint, run=self.run, **({"dispatcher": failed} | kwargs))

    def stored(self):
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            return saver.get({"configurable": {"thread_id": "example/engine#8"}})["channel_values"]

    def test_a_skill_failure_before_the_graph_is_durable_and_reads_the_issue_once_per_invocation(self):
        missing = Mock(side_effect=SkillMissing(load_config().pstack))
        self.run = LiveRun(self.forge, self.run.workspace, skills=missing)
        first = self.invoke()
        stored = self.stored()
        self.assertEqual((stored["run"].issue, stored["run"].workspace, stored["run"].repo, stored["checks"].check, stored["start"].max_tokens),
                         (8, str(self.root.resolve()), "Example/Engine", "", 20_000_000))
        self.assertEqual(stored["pstack_pin"], first["pstack_pin"])
        self.assertEqual(self.invoke(), first)
        missing.assert_called_once()
        # The halt comes before triage. Only the status mirror reads the issue, once per invocation.
        self.assertEqual([call.args for call in self.forge.load_issue.call_args_list], [("Example/Engine", 8)] * 2)
        self.forge.create_issue.assert_not_called()
        self.forge.comment_issue.assert_not_called()

    def test_a_missing_cli_exits_75_and_a_dry_run_never_calls_the_forge(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.fake.body = "Issue body"
            self.fake.labels = ["Ready"]
            code = main(self.flags, checkpoint=self.checkpoint, live_run=self.run,
                        worker_runner=Mock(side_effect=FileNotFoundError("missing CLI")))
        # With no CLI installed, dispatch pauses for a qualified worker, and a pause files nothing.
        self.assertEqual(code, 75)
        self.forge.create_issue.assert_not_called()
        self.forge.comment_issue.assert_not_called()
        self.forge.reset_mock()
        for exit_code, expected in ((0, 0), (1, 1)):
            with (patch("foreman.cli.LiveForge", side_effect=AssertionError("dry Forge")), patch("foreman.delivery.DryRun.run", return_value=exit_code),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(main(["run", "delivery", "--repo", "Example/Engine", "--request", "test", "--dry-run"], live_run=self.run), expected)
        self.assertEqual(self.forge.mock_calls, [])

    def test_a_blocked_run_reports_once_and_a_quota_pause_dispatches_again_without_repeating_triage(self):
        blocker = StoredSlice.model_validate({**EMPTY_SLICE, "title": "blocker"})
        number = self.fake.create_issue("Example/Engine", blocker.title, "", ["slice"], child=blocker, marker="")
        self.fake.dependencies[8] = [number]
        self.fake.body = slice_body({**EMPTY_SLICE, "depends_on": [blocker.title], "blocker_numbers": [number]})
        reports = []
        result = self.invoke(report=reports.append)
        self.assertEqual([report["stop"] for report in reports], ["blocked"])
        self.assertEqual(result["stop"], "blocked")
        self.forge.create_issue.assert_not_called()
        self.fake.closed.add(number)
        self.fake.merged.add(number)

        def quota(stage, *args):
            if stage == "triage":
                return Outcome("ran", {"triage": "supplied_slice", "human_question": None, "work_kind": "feature", "slice": None}, usage=ONE_TOKEN)
            return Outcome("quota", usage=SCRIPTED_USAGE)
        paused = self.invoke(dispatcher=quota)
        self.assertEqual(paused["stop"], "worker_unavailable")
        again = self.invoke(dispatcher=quota)
        self.assertEqual(again["stop"], "worker_unavailable")
        self.assertGreater(len(again["attempts"]), len(paused["attempts"]))
        self.assertEqual([attempt["stage"] for attempt in again["attempts"]].count("triage"), 1)
        reads = [call.args[-1] for call in self.forge.load_issue.call_args_list]
        # Every status write reads the issue's labels. Admit owns the blocker, so spec never reads it.
        self.assertEqual(set(reads), {8})
        self.assertEqual({attempt["stage"] for attempt in again["attempts"]}, {"triage", "spec_check"})

    def test_a_skill_failure_keeps_a_resumable_checkpoint(self):
        for stop in ("git_failed", "worker_unavailable"):
            with self.subTest(stop=stop):
                self.checkpoint = self.root.parent / f"{stop}.sqlite3"
                workspace = FakeWorkspace(self.root)
                workspace.create_branch = Mock(side_effect=WorkspaceError("git failed"))
                self.run = LiveRun(self.forge, workspace, skills=fixture_skills)

                def dispatch(stage, *args):
                    if stop == "worker_unavailable":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    if stage == "spec_check":
                        return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
                    return Outcome("ran", {"triage": "supplied_slice", "human_question": None, "work_kind": "feature", "slice": None}, usage=ONE_TOKEN)
                self.assertEqual(self.invoke(dispatcher=dispatch)["stop"], stop)
                before = self.stored()
                self.run = LiveRun(self.forge, workspace, skills=Mock(side_effect=SkillMissing(load_config().pstack)))
                reports = []
                self.invoke(dispatcher=dispatch, report=reports.append)
                self.assertEqual([result["stop"] for result in reports], ["skill_missing"])
                self.assertEqual(self.stored(), before)
                self.run = LiveRun(self.forge, workspace, skills=fixture_skills)
                self.assertEqual(self.invoke(dispatcher=dispatch)["stop"], stop)

    def test_a_saved_stop_exits_with_its_code_after_the_run_report(self):
        for stop, code in (("invalid_output", 1), ("git_failed", 75), ("needs_human", 3)):
            self.checkpoint = self.root.parent / f"{stop}.sqlite3"
            if code == 75:
                self.run.workspace.create_branch = Mock(side_effect=WorkspaceError("git failed"))
                dispatcher = CliWorker(self.root, lambda args, *unused: worker_reply(args, checker_reply(args) or {"triage": "supplied_slice"}),
                                       scratch_root=self.checkpoint.parent)
                self.invoke(dispatcher=dispatcher)
            else:
                self.invoke()
            with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(lambda *args: Outcome("failed", detail="bad output", usage=SCRIPTED_USAGE), load_config(), run=self.run, checkpointer=saver)
                graph.update_state({"configurable": {"thread_id": "example/engine#8"}}, {"stop": stop}, as_node="research")
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                actual = main(self.flags, checkpoint=self.checkpoint, live_run=self.run, worker_runner=lambda *args: subprocess.CompletedProcess([], 0, "invalid", ""))
            self.assertEqual(actual, code, stream.getvalue())
            self.assertIn("this invocation", stream.getvalue())
