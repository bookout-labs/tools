import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.delivery import LiveRun
from foreman.github import LiveForge
from foreman.runner import run_live_delivery
from foreman.workspace import LiveWorkspace
from test_runner import BRANCH, EMPTY_SLICE, slice_body

REPO = "Example/Engine"
REF = f"refs/heads/{BRANCH}"
run_subprocess = subprocess.run


class PublicationTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.remote = root / "remote.git"
        self.checkpoint = root / "runs.sqlite3"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Foreman Test")
        self.git("config", "user.email", "foreman@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("commit", "--allow-empty", "-m", "Base")
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.created = []
        self.published_heads = []
        self.create_error = False
        self.enterContext(patch("foreman.github.subprocess.run", side_effect=self.subprocess))

    def git(self, *args):
        return run_subprocess(["git", *args], cwd=self.checkout, check=True, capture_output=True, text=True).stdout.strip()

    def remote_head(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", REF)

    def subprocess(self, command, **kwargs):
        if command[0] != "gh":
            return run_subprocess(command, **kwargs)
        self.assertEqual(command[command.index("--repo") + 1], REPO)
        match command[1:3]:
            case ["issue", "view"]:
                response = {"title": EMPTY_SLICE["title"], "body": slice_body(EMPTY_SLICE), "labels": [{"name": "slice"}]}
            case ["pr", "list"]:
                self.assertEqual(command[command.index("--head") + 1], BRANCH)
                response = [{"baseRefName": "main", "headRefName": BRANCH, "isCrossRepository": False}] if self.created else []
            case ["pr", "create"]:
                self.assertEqual(command[3:], [
                    "--repo", REPO, "--base", "main", "--head", BRANCH,
                    "--title", EMPTY_SLICE["title"], "--body", "Closes #8",
                ])
                if self.create_error:
                    return subprocess.CompletedProcess(command, 1, "", "Temporary failure")
                self.created.append(self.remote_head())
                return subprocess.CompletedProcess(command, 0, f"https://github.com/{REPO}/pull/12\n", "")
            case ["variable", "list"]:
                response = []
            case ["pr", "view"]:
                response = {"number": 12, "state": "OPEN", "headRefOid": self.remote_head(), "headRefName": BRANCH,
                            "baseRefName": "main", "autoMergeRequest": None}
            case ["pr", "merge"]:
                self.assertEqual(command[-2:], ["--match-head-commit", self.remote_head()])
                return subprocess.CompletedProcess(command, 0, "", "")
            case _:
                self.fail(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

    def walk(self, edit, reviews=None):
        script = ScriptHandler(reviews=reviews)
        workspace = LiveWorkspace(self.checkout)

        def handler(node, model, state, instruction):
            if node == "implement":
                self.assertEqual(workspace.inspect().branch, BRANCH)
                edit()
            if node == "review":
                self.published_heads.append(self.remote_head())
            return script(node, model, state, instruction)

        return run_live_delivery(
            repo=REPO, issue=8, check="test -f items.txt", checkpoint=self.checkpoint,
            run=LiveRun(LiveForge(), workspace), dispatcher=FailoverDispatcher(handler),
        )

    def test_graph_commits_and_pushes_before_opening_one_pull_request(self):
        self.git("switch", "-c", "unrelated")
        self.git("commit", "--allow-empty", "-m", "Unrelated change")
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(result.get("merge"), "armed")
        self.assertEqual(self.remote_head(), result.get("head_sha"))
        self.assertEqual(result.get("verified_head"), self.remote_head())
        self.assertEqual(self.created, [self.remote_head()])
        self.assertEqual(self.git("rev-parse", f"{REF}^"), self.git("rev-parse", "main"))
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{REF}:items.txt"), "one")

    def test_second_attempt_adds_a_commit_to_the_same_pull_request(self):
        edits = iter(("one\n", "two\n"))
        result = self.walk(lambda: (self.checkout / "items.txt").write_text(next(edits)), ["changes", "approve"])
        self.assertEqual(result.get("merge"), "armed")
        first, second = self.published_heads
        self.assertEqual(self.created, [first])
        self.assertEqual(second, self.remote_head())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", f"{REF}^"), first)
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{REF}:items.txt"), "two")

    def test_no_changes_fails_without_pushing(self):
        result = self.walk(lambda: None)
        self.assertEqual(result.get("stop"), "failed")
        self.assertEqual(result.get("halt_detail"), "no changes")
        self.assertEqual(self.git("ls-remote", "origin", REF), "")
        self.assertEqual(self.created, [])

    def test_worker_commit_is_detected_using_the_actual_head(self):
        def commit():
            self.git("commit", "--allow-empty", "-m", "Out-of-band commit")

        result = self.walk(commit)
        self.assertEqual(result.get("stop"), "failed")
        self.assertIn("worker changed HEAD", result.get("halt_detail", ""))
        self.assertEqual(result.get("head_sha"), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("ls-remote", "origin", REF), "")
        self.assertEqual(self.created, [])

    def test_rerun_retries_publication_without_rerunning_the_worker(self):
        self.create_error = True
        failed = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(failed.get("stop"), "publish_failed")
        self.assertEqual(failed.get("head_sha"), self.remote_head())
        self.create_error = False
        result = self.walk(lambda: self.fail("The committed change should be reused"))
        self.assertEqual(result.get("merge"), "armed")
        self.assertEqual(self.created, [failed.get("head_sha")])
