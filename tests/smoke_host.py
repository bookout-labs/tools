"""A fake Foreman host for smoke tests: the real receiver's listener and accept, fed signed deliveries by a fake GitHub, whose
start and wake run the real runner against the host's checkpoint store."""
import tests

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import threading
from unittest.mock import Mock, patch

from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler
from foreman.receiver import listener
from foreman.runner import CheckpointRuns, run_live_delivery
from foreman.smoke_command import SMOKE_REPO
from skill_fixture import fixture_skills
from test_runner import FakeGitHub, FakeWorkspace

SECRET = b"synthetic-secret"
HUMAN = "ghost"


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fake_gh(directory: Path, login: str | None) -> Path:
    """Put a gh on PATH whose stored session is login, or none, and return the log of every call: its arguments and any token it got."""
    log, gh = directory / "gh.log", directory / "bin/gh"
    gh.parent.mkdir(parents=True)
    gh.write_text(f"""#!{sys.executable}
import json, os, sys
with open({str(log)!r}, "a") as log:
    log.write(json.dumps({{"args": sys.argv[1:], "tokens": [name for name in os.environ if name.startswith(("GH_", "GITHUB_"))]}}) + "\\n")
if sys.argv[1:3] == ["api", "user"]:
    sys.exit(print({login!r}) if {login!r} else sys.exit("not logged in to any GitHub hosts"))
""")
    gh.chmod(0o755)
    os.environ["PATH"] = f"{gh.parent}{os.pathsep}{os.environ['PATH']}"
    return log


def gh_calls(log: Path) -> list[dict[str, list[str]]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


class FakeHost:
    """GitHub as the smoke meets it: the Ready label the human adds is delivered, then a pending check's check_suite once the
    run waits at arm, then the queue's merge as a pull_request delivery once it waits at observe. A delivery named in lost
    never comes."""

    def __init__(self, root: Path, *, lost=(), merges_at_arm=False, receiver=True):
        self.state = root / "state"
        self.store = self.state / "foreman/checkpoints.sqlite3"
        self.receiver, self.lost, self.commands, self.results, self.sent, self.errors = receiver, set(lost), [], [], [], []
        self.fake = FakeGitHub(labels=[])
        # The smoke repository's own variables hold the human login and its share of the smoke's caps.
        self.fake.settings = {"FOREMAN_HUMAN_LOGIN": HUMAN, "FOREMAN_MAX_TOKENS": "1000000", "FOREMAN_MAX_BUDGET_USD": "1.25"}
        self.fake.queue_merges = merges_at_arm
        self.forge = Mock(wraps=self.fake)
        self.forge.smoke_identity = Mock(return_value=(True, True, True))
        self.forge.smoke_issues = Mock(return_value=[])
        self.forge.smoke_pull_requests = Mock(return_value=[])
        checked = [] if merges_at_arm else ["pending"]
        def readiness(*args):
            observation = self.fake.readiness(*args)
            return observation.model_copy(update={"outcome": checked.pop()}) if checked else observation
        self.forge.readiness.side_effect = readiness
        self.workspace = FakeWorkspace(root / "run")
        self.workspace.run = Mock(return_value=0)
        self.handler = ScriptHandler()
        self.server = listener(("127.0.0.1", 0), SECRET, CheckpointRuns(self.store), self.command, lambda repo, issue: [])

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def dispatch(self, node, *args):
        if node == "implement":
            from foreman.workspace import WorktreeState
            self.workspace.current = WorktreeState(self.workspace.current.head, False, self.workspace.current.branch)
        outcome = self.handler(node, *args)
        return replace(outcome, prompt_bytes=123, native_usage={
            "family": args[0].family, "aggregation": "turn_total",
            "entries": {"turn": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0, "total_cost_usd": 0.0}}})

    def command(self, argv):
        """The receiver's start or wake, run by the real runner as foreman start and wake run it, in the receiver's thread."""
        self.commands.append(argv)
        action, repo, issue = argv[0], argv[2], int(argv[4])
        try:
            result = run_live_delivery(repo=repo, issue=issue, checkpoint=self.store, dispatcher=self.dispatch,
                                       run=LiveRun(self.forge, self.workspace, skills=fixture_skills), wake=action == "wake")
        except BaseException as exc:
            self.errors.append(exc)
            raise
        self.results.append(result)
        if result["stop"] == "merge_pending":
            if result.get("merge") is None or result["merge"].status != "armed":
                self.deliver("check_suite", {"action": "completed", "repository": {"full_name": repo},
                                             "check_suite": {"head_sha": result["checks"].verified_head, "pull_requests": []}})
            else:
                self.fake.queue_merges = True
                self.deliver("pull_request", {"action": "closed", "repository": {"full_name": repo},
                                              "pull_request": {"number": 12, "merged": True}})
        return 0

    def add_ready(self, repo, issue):
        """The human adds Ready, and GitHub delivers the label event with the human as its sender."""
        self.fake.issue_labels[issue].append("Ready")
        self.deliver("issues", {"action": "labeled", "label": {"name": "Ready"}, "repository": {"full_name": repo},
                                "issue": {"number": issue}, "sender": {"login": HUMAN}})

    def deliver(self, event, payload):
        if event in self.lost:
            return
        self.sent.append(event)
        body = json.dumps(payload).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/", body, {"X-GitHub-Event": event, "Content-Length": str(len(body)),
                                               "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()})
        assert connection.getresponse().status == 202
        connection.close()

    def outside(self, **kwargs):
        """A run that the receiver did not start: the smoke, or anything else, moving the run on itself."""
        raise AssertionError(f"a run was moved on outside the receiver: {kwargs.get('repo')}#{kwargs.get('issue')}")

    @contextmanager
    def serving(self):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}))
            # A host with no live receiver: nothing answers on the receiver's port.
            stack.enter_context(patch("foreman.smoke_command.PORT", self.port if self.receiver else unused_port()))
            stack.enter_context(patch("foreman.smoke_command.LiveForge", return_value=self.forge))
            stack.enter_context(patch("foreman.smoke_command.add_ready", side_effect=self.add_ready))
            stack.enter_context(patch("foreman.smoke_command.human_session"))
            stack.enter_context(patch("foreman.cli.run_live_delivery", side_effect=self.outside))
            if self.receiver:
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
            try:
                yield self
            finally:
                if self.receiver:
                    self.server.shutdown()
                self.server.server_close()
                for worker in threading.enumerate():
                    if worker.name.startswith("foreman "):
                        worker.join(timeout=30)


__all__ = ["FakeHost", "HUMAN", "SMOKE_REPO", "fake_gh", "gh_calls", "unused_port"]
