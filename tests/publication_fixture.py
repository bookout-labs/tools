"""The publication fixture: a real checkout and bare origin with GitHub faked, and the target repository's contract file."""

import tests
from tests import RUN_ID

from contextlib import ExitStack, closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.adapters import NativeUsage, NativeUsageTokens, Outcome
from foreman.config import load_config
from foreman.contract import CONTRACT_PATH
from foreman.credentials import Subscription
from foreman.delivery import LiveRun
from foreman.doctor import EVENTS, GreenChecks
from foreman.review_round import QUESTIONS
from foreman.dry_run import ScriptHandler
from foreman.github import BOT_LOGIN, LiveForge
from foreman.redaction import redact
from foreman.runner import run_live_delivery
from foreman.workspace import LiveWorkspace
from container_double import HostContainers
from skill_fixture import fixture_pin

# What a scripted answer's CLI reported for its conversation, so a later call can resume it (#446 K2).
TOTALS = NativeUsage(family="claude", aggregation="cli_model_totals", entries={"claude-opus-5-5": NativeUsageTokens(
    input_tokens=9, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0, total_cost_usd=0.1)})


def resume_id(plan: object) -> str | None:
    """The id of the session a dispatch plan, saved or validated, resumes."""
    resume = plan.get("resume") if isinstance(plan, dict) else getattr(plan, "resume")
    return resume["id"] if resume else None


# An App installation subscribed to every event doctor requires, with read on each permission they need.
SUBSCRIBED = Subscription(events=frozenset(EVENTS), permissions={grant.key: "read" for grant in EVENTS.values()})

VERIFY_SKILL_PATH = ".claude/skills/verify-web/SKILL.md"
BRANCH = f"8-list-items-once-{RUN_ID[:8]}"
SLICE = {
    "title": "List items once",
    "acceptance": "The view lists every item once.",
    "files": [],
    "depends_on": [],
    "screen": "docs/screens/items.md",
    "properties": [{"name": "Unique", "constraint": "unique_item_id"}],
    "stateful": False,
}
EMPTY_SLICE = {**SLICE, "screen": "", "properties": []}


IMAGE = "registry.example/engine-check@sha256:" + "0" * 64


def contract_text(check="exit 0", *, setup=(), secrets=(), extra="", production="{command: 'true', not_deployed_exit: 3, timeout_seconds: 60}"):
    """A target repository's contract file naming the check, the verify skill, the container image, its setup commands, the
    secrets the app needs and the production check."""
    return (f"check: {json.dumps(check)}\nverify_skill: {VERIFY_SKILL_PATH}\nimage: {IMAGE}\nsetup: {json.dumps(list(setup))}\n"
            f"secrets: {json.dumps(list(secrets))}\nstart: {{command: 'true', health: 'true', timeout_seconds: 60}}\n"
            f"production_check: {production}\n{extra}").encode()


def slice_body(item):
    return "Issue body\n\n```json\n" + json.dumps(item) + "\n```"


REPO = "Example/Engine"
REF = f"refs/heads/{BRANCH}"
run_subprocess = subprocess.run


class PublicationFixture(unittest.TestCase):
    """A real checkout and bare origin, with GitHub faked at the subprocess boundary."""
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.pin = fixture_pin(root / "fixture")

    def setUp(self):
        self.fixture = self.enterContext(ExitStack())
        self.reset_fixture()

    def reset_fixture(self, check="test -f items.txt"):
        self.fixture.close()
        # GitHub is faked below the App's credentials, so the App's installation is too.
        self.fixture.enter_context(patch.object(LiveForge, "app_installation", return_value="installed"))
        self.fixture.enter_context(patch.object(LiveForge, "app_subscription", return_value=SUBSCRIBED))
        self.fixture.enter_context(patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=self.pin, matt_skills=self.pin.model_copy(update={"directory": "skills"}))))
        self.base = "main"
        self.branch = BRANCH
        self.ref = REF
        self.slice = EMPTY_SLICE
        self.check = check
        self.containers = HostContainers()
        root = Path(self.fixture.enter_context(TemporaryDirectory()))
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.remote = root / "remote.git"
        self.checkpoint = root / "runs.sqlite3"
        self.git("init", "-b", self.base)
        self.git("config", "user.name", "Foreman Test")
        self.git("config", "user.email", "foreman@example.com")
        self.git("config", "commit.gpgsign", "false")
        # The base carries the repository's contract file and the verify skill it names.
        (self.checkout / CONTRACT_PATH).parent.mkdir()
        (self.checkout / CONTRACT_PATH).write_bytes(contract_text(check))
        (self.checkout / VERIFY_SKILL_PATH).parent.mkdir(parents=True)
        (self.checkout / VERIFY_SKILL_PATH).write_text("# Verify the change\n")
        self.git("add", "--", CONTRACT_PATH, VERIFY_SKILL_PATH)
        self.git("commit", "-m", "Base")
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", self.base)
        self.created = []
        self.published_heads = []
        self.comments = []
        self.labels = ["slice", "Ready"]
        # The repository's run settings, which each start reads once.
        self.settings = {"FOREMAN_HUMAN_LOGIN": "fixture-owner"}
        self.status: str | None = None  # Foreman's status comment on the issue.
        self.pr_body = "Closes #8"
        self.pr_view_error = ""
        self.create_error = False
        self.queued = False
        self.closed = False
        # GitHub reports a conflict while this is False and the PR head does not contain the base head.
        self.mergeable = True
        # When GitHub created each human comment, by body, and when Ready was last added to the issue.
        self.comment_times: dict[str, str] = {}
        self.ready_at: str | None = None
        self.pr_comments: list[str] = []
        # The queue merges an armed pull request unless a test holds it.
        self.queue_merges = True
        self.fixture.enter_context(patch("foreman.github.subprocess.run", side_effect=self.subprocess))

    def git(self, *args):
        return run_subprocess(["git", *args], cwd=self.checkout, check=True, capture_output=True, text=True).stdout.strip()

    @property
    def merged(self):
        return self.queued and self.queue_merges

    def remote_head(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", self.ref)

    def subprocess(self, command, **kwargs):
        if command[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(command, 0, "https://github.com/Example/Engine.git\n", "")
        if command[0] != "gh":
            return run_subprocess(command, **kwargs)
        if command[1:3] == ["repo", "view"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"defaultBranchRef": {"name": self.base}}), "")
        if command[1] != "api":
            self.assertEqual(command[command.index("--repo") + 1], REPO)
        match command[1:3]:
            case ["api", "repos/bookout-labs/tools/issues?state=open&per_page=100"]:
                response = [[]]
            case ["api", "repos/bookout-labs/tools/issues"]:
                response = {"number": 101}
            case ["api", "repos/Example/Engine/issues/8/events?per_page=100"]:
                response = [[{"event": "labeled", "label": {"name": "Ready"}, "created_at": self.ready_at}] if self.ready_at else []]
            case ["api", "repos/Example/Engine/issues/8/dependencies/blocking"]:
                response = [[]]
            case ["api", "repos/Example/Engine/issues/8/dependencies/blocked_by"]:
                response = [[]]
            case ["api", "repos/Example/Engine/issues/8/comments?per_page=100"]:
                response = [[*({"user": {"login": "fixture-owner"}, "body": body,
                                **({"created_at": self.comment_times[body]} if body in self.comment_times else {})} for body in self.comments),
                             *([{"id": 1, "user": {"login": BOT_LOGIN}, "body": self.status}] if self.status else [])]]
            case ["api", "repos/Example/Engine/issues/12/comments?per_page=100"]:
                response = [[{"id": 2, "user": {"login": BOT_LOGIN}, "body": body} for body in self.pr_comments]]
            case ["api", "repos/Example/Engine/issues/12/comments"]:
                self.pr_comments.append(next(arg.removeprefix("body=") for arg in command if arg.startswith("body=")))
                response = {}
            case ["api", "repos/Example/Engine/issues/8/comments" | "repos/Example/Engine/issues/comments/1"]:
                self.status = next(arg.removeprefix("body=") for arg in command if arg.startswith("body="))
                response = {}
            case ["api", url] if url.startswith("repos/Example/Engine/labels"):
                response = {}
            case ["issue", "edit"]:
                for flag, names in zip(command, command[1:]):
                    if flag == "--remove-label":
                        self.labels = [label for label in self.labels if label not in names.split(",")]
                    elif flag == "--add-label":
                        self.labels += [label for label in names.split(",") if label not in self.labels]
                response = {}
            case ["api", "repos/Example/Engine/issues/8"]:
                response = {"number": 8, "repository_url": f"https://api.github.com/repos/{REPO}", "title": self.slice["title"], "body": slice_body(self.slice),
                            "labels": [{"name": label} for label in self.labels]}
            case ["pr", "create"]:
                self.assertEqual(command[3:], [
                    "--repo", REPO, "--base", self.base, "--head", self.branch,
                    "--title", " ".join(redact(self.slice["title"]).split()), "--body", "Closes #8",
                ])
                if self.create_error:
                    return subprocess.CompletedProcess(command, 1, "", "Temporary failure")
                self.created.append(self.remote_head())
                return subprocess.CompletedProcess(command, 0, f"https://github.com/{REPO}/pull/12\n", "")
            case ["variable", "list"]:
                response = [{"name": name, "value": value} for name, value in self.settings.items()]
            case ["pr", "view"]:
                if self.pr_view_error:
                    return subprocess.CompletedProcess(command, 1, "", self.pr_view_error)
                if not self.created:
                    return subprocess.CompletedProcess(command, 1, "", f'no pull requests found for branch "{self.branch}"')
                response = {"number": 12, "state": "CLOSED" if self.closed else "OPEN", "headRefOid": self.remote_head(), "headRefName": self.branch,
                            "baseRefName": self.base, "baseRefOid": self.git("--git-dir", str(self.remote), "rev-parse", self.base),
                            "isCrossRepository": False, "autoMergeRequest": None, "body": self.pr_body}
            case ["api", "graphql"] if "dequeuePullRequest" in command[4]:
                self.queued = False
                response = {}
            case ["api", "graphql"]:
                response = dict(id="PR_12", number=12, state="MERGED" if self.merged else "OPEN", headRefOid=self.remote_head(),
                    baseRefName=self.base, baseRefOid=self.git("--git-dir", str(self.remote), "rev-parse", self.base),
                    isDraft=False, autoMergeRequest=None,
                    mergeQueueEntry=dict(id="queue-12", state="QUEUED", headCommit=dict(oid=self.remote_head())) if self.queued else None)
            case ["api", "repos/Example/Engine/pulls/12/reviews"]:
                response = ({"id": 1, "state": "COMMENTED", "commit_id": self.remote_head(), "body": "posted"}
                            if "POST" in command else [[]])
            case ["api", "repos/Example/Engine/pulls/12"] if "PATCH" in command:
                self.closed = True
                response = {"state": "closed"}
            case ["api", "repos/Example/Engine/pulls/12"]:
                mergeable = self.mergeable or run_subprocess(["git", "--git-dir", str(self.remote), "merge-base", "--is-ancestor", self.base, self.ref]).returncode == 0
                response = dict(number=12, state="closed" if self.merged else "open", draft=False, mergeable=mergeable, mergeable_state="clean" if mergeable else "dirty",
                    merged=self.merged, merge_commit_sha="e" * 40 if self.merged else None, head=dict(sha=self.remote_head(), ref=self.branch),
                    base=dict(sha=self.git("--git-dir", str(self.remote), "rev-parse", self.base), ref=self.base))
            case ["api", url] if url.endswith("/collaborators/fixture-owner/permission"):
                response = {"permission": "write", "role_name": "write"}
            case ["api", url] if "/rules/branches/" in url:
                response = [dict(type="required_status_checks", parameters=dict(required_status_checks=[dict(context="test", integration_id=15368)])),
                            dict(type="merge_queue", parameters=dict(merge_method="SQUASH"))]
            case ["api", url] if "/branches/" in url:
                response = dict(commit=dict(sha=self.git("--git-dir", str(self.remote), "rev-parse", self.base)), protection=dict(required_status_checks=dict(checks=[], contexts=[])))
            case ["api", url] if "/check-runs" in url:
                response = [dict(check_runs=[dict(id=1, name="test", head_sha=self.remote_head(),
                    app=dict(id=15368), check_suite=dict(id=30), status="completed", conclusion="success")])]
            case ["api", url] if "/statuses" in url:
                response = [[]]
            case ["pr", "merge"]:
                self.queued = True
                self.assertEqual(command[-2:], ["--match-head-commit", self.remote_head()])
                return subprocess.CompletedProcess(command, 0, "", "")
            case _:
                self.fail(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    def set_contract(self, check=None, **terms):
        """The base's contract with these terms and the fixture's check, or this one, committed and pushed before the run pins
        the base."""
        self.check = check or self.check
        (self.checkout / CONTRACT_PATH).write_bytes(contract_text(self.check, **terms))
        self.git("commit", "-qm", "Contract", "--", CONTRACT_PATH)
        self.git("push", "-q", "origin", self.base)

    def edit(self):
        (self.checkout / "items.txt").write_text("one\n")

    def no_worker(self):
        self.fail("completed worker must not repeat")

    def walk(self, edit=None, reviews=None, family=None,
             answer=None, check_log=None, ask=None, model_edit=None, no_change=None):
        script = ScriptHandler(reviews=reviews)
        workspace = LiveWorkspace(self.checkout, containers=self.containers)
        self.doctor_passed()

        def handler(node, model, state, instruction):
            if node == "implement":
                self.assertEqual(workspace.inspect().branch, self.branch)
                self.assertEqual(instruction.count("Foreman verifies the final tree, commits and publishes"), 1)
                if family and model.family != family:
                    return Outcome("cli_missing")
                outcome = model_edit(model, state, instruction) if model_edit else (edit or self.edit)()
                if isinstance(outcome, Outcome):
                    return outcome
            if node == "review" and QUESTIONS["a"] in instruction:
                # One entry per review round, at its first pass.
                self.published_heads.append(self.remote_head())
            result = script(node, model, state, instruction)
            if node == "implement" and ask is not None and result.output is not None:
                result.output["human_question"] = ask()
            if node == "implement" and no_change is not None and result.output is not None:
                result.output["no_change"] = no_change()
            return result

        return run_live_delivery(
            repo=REPO, issue=8, checkpoint=self.checkpoint,
            answer=answer,
            run=LiveRun(LiveForge(), workspace, check_log=check_log), dispatcher=handler,
        )

    def doctor_passed(self):
        """The repository passed doctor's full check for the contract at its base, as every admitted repository has. The
        fixture's check fails at the base until the builder's edit, so admission would not run it green."""
        shown = run_subprocess(["git", "--git-dir", str(self.remote), "show", f"{self.base}:{CONTRACT_PATH}"], capture_output=True)
        if shown.returncode:
            # A base the test removed has no contract to check.
            return
        text = shown.stdout
        with closing(sqlite3.connect(self.checkpoint)) as connection:
            GreenChecks(connection).record(REPO, hashlib.sha256(text).hexdigest())

    def advance_origin(self, *, parent=None):
        parent = parent or self.git("--git-dir", str(self.remote), "rev-parse", "main")
        head = self.git("commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "Origin advance")
        self.git("push", "origin", f"{head}:refs/heads/main")
        return head
