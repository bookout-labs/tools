import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.delivery import LiveRun
from foreman.redaction import redact
from foreman.github import LiveForge
from foreman.runner import run_live_delivery
from foreman.workspace import LiveWorkspace
from test_runner import BRANCH, EMPTY_SLICE, slice_body

REPO = "Example/Engine"
REF = f"refs/heads/{BRANCH}"
run_subprocess = subprocess.run


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.base = "main"
        self.branch = BRANCH
        self.ref = REF
        self.slice = EMPTY_SLICE
        root = Path(self.enterContext(TemporaryDirectory()))
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        self.remote = root / "remote.git"
        self.checkpoint = root / "runs.sqlite3"
        self.git("init", "-b", self.base)
        self.git("config", "user.name", "Foreman Test")
        self.git("config", "user.email", "foreman@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("commit", "--allow-empty", "-m", "Base")
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", self.base)
        self.created = []
        self.published_heads = []
        self.create_error = False
        self.enterContext(patch("foreman.github.subprocess.run", side_effect=self.subprocess))

    def git(self, *args):
        return run_subprocess(["git", *args], cwd=self.checkout, check=True, capture_output=True, text=True).stdout.strip()

    def remote_head(self):
        return self.git("--git-dir", str(self.remote), "rev-parse", self.ref)

    def subprocess(self, command, **kwargs):
        if command[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(command, 0, b"https://github.com/Example/Engine.git\n", b"")
        if command[0] != "gh":
            return run_subprocess(command, **kwargs)
        if command[1:3] == ["repo", "view"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"defaultBranchRef": {"name": self.base}}), "")
        if command[1] != "api":
            self.assertEqual(command[command.index("--repo") + 1], REPO)
        match command[1:3]:
            case ["api", "repos/Example/Engine/issues/8/dependencies/blocked_by"]:
                response = [[]]
            case ["issue", "view"]:
                response = {"title": self.slice["title"], "body": slice_body(self.slice), "labels": [{"name": "slice"}]}
            case ["pr", "list"]:
                self.assertEqual(command[command.index("--head") + 1], self.branch)
                response = [{"baseRefName": "main", "headRefName": self.branch, "isCrossRepository": False}] if self.created else []
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
                response = []
            case ["pr", "view"]:
                if not self.created:
                    return subprocess.CompletedProcess(command, 1, "", f'no pull requests found for branch "{self.branch}"')
                response = {"number": 12, "state": "OPEN", "headRefOid": self.remote_head(), "headRefName": self.branch,
                            "baseRefName": self.base, "baseRefOid": self.git("rev-parse", self.base), "autoMergeRequest": None}
            case ["api", "repos/Example/Engine/pulls/12/reviews"]:
                return subprocess.CompletedProcess(command, 0, "{}", "")
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
                self.assertEqual(workspace.inspect().branch, self.branch)
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
        self.assertEqual(self.git("rev-parse", f"{self.ref}^"), self.git("rev-parse", "main"))
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "one")

    def test_second_attempt_adds_a_commit_to_the_same_pull_request(self):
        edits = iter(("one\n", "two\n"))
        result = self.walk(lambda: (self.checkout / "items.txt").write_text(next(edits)), ["changes", "approve"])
        self.assertEqual(result.get("merge"), "armed")
        first, second = self.published_heads
        self.assertEqual(self.created, [first])
        self.assertEqual(second, self.remote_head())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", f"{self.ref}^"), first)
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "two")

    def test_no_changes_fails_without_pushing(self):
        result = self.walk(lambda: None)
        self.assertEqual(result.get("stop"), "failed")
        self.assertEqual(result.get("halt_detail"), "no changes")
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
        self.assertEqual(self.created, [])

    def test_worker_commit_is_detected_using_the_actual_head(self):
        def commit():
            self.git("commit", "--allow-empty", "-m", "Out-of-band commit")

        result = self.walk(commit)
        self.assertEqual(result.get("stop"), "failed")
        self.assertIn("worker changed HEAD", result.get("halt_detail", ""))
        self.assertEqual(result.get("head_sha"), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
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

    def test_crash_after_branch_creation_reuses_branch(self):
        original = LiveWorkspace.create_branch
        def crash(workspace, branch, base):
            original(workspace, branch, base)
            raise SystemExit("synthetic crash after branch")
        with patch.object(LiveWorkspace, "create_branch", crash), self.assertRaises(SystemExit):
            self.walk(lambda: self.fail("worker must follow preparation checkpoint"))
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(result.get("stop"), "verified")

    def test_crash_after_worker_checkpoint_never_repeats_worker(self):
        with patch.object(LiveWorkspace, "commit", side_effect=SystemExit("synthetic crash before commit")), self.assertRaises(SystemExit):
            self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(self.walk(lambda: self.fail("worker already checkpointed")).get("stop"), "verified")

    def test_crash_after_commit_recovers_without_worker_or_second_commit(self):
        original = LiveWorkspace.commit
        def crash(workspace, message):
            original(workspace, message)
            raise SystemExit("synthetic crash after commit")
        with patch.object(LiveWorkspace, "commit", crash), self.assertRaises(SystemExit):
            self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        head = self.git("rev-parse", "HEAD")
        with patch.object(LiveWorkspace, "commit", side_effect=AssertionError("duplicate commit")):
            result = self.walk(lambda: self.fail("duplicate worker"))
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual(self.remote_head(), head)

    def test_crash_after_push_skips_finished_push(self):
        original = LiveWorkspace.push
        def crash(workspace, branch, repo):
            original(workspace, branch, repo)
            raise SystemExit("synthetic crash after push")
        with patch.object(LiveWorkspace, "push", crash), self.assertRaises(SystemExit):
            self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        original_git = LiveWorkspace._git
        def no_duplicate(workspace, *args, **kwargs):
            self.assertNotEqual(args[0], "push")
            return original_git(workspace, *args, **kwargs)
        with patch.object(LiveWorkspace, "_git", no_duplicate):
            self.assertEqual(self.walk(lambda: self.fail("duplicate worker")).get("stop"), "verified")

    def test_crash_after_pr_create_reuses_pr(self):
        original = LiveForge.open_pull_request
        def crash(forge, *args):
            original(forge, *args)
            raise SystemExit("synthetic crash after PR")
        with patch.object(LiveForge, "open_pull_request", crash), self.assertRaises(SystemExit):
            self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(self.walk(lambda: self.fail("duplicate worker")).get("stop"), "verified")
        self.assertEqual(len(self.created), 1)

    def test_index_lock_is_resumable_without_repeating_worker(self):
        lock = self.checkout / ".git/index.lock"
        def edit():
            (self.checkout / "items.txt").write_text("one\n")
            lock.touch()
        result = self.walk(edit)
        self.assertEqual(result.get("stop"), "git_failed")
        self.assertIn("index.lock", result["halt_detail"])
        lock.unlink()
        self.assertEqual(self.walk(lambda: self.fail("duplicate worker")).get("stop"), "verified")

    def test_existing_branch_at_expected_base_is_reused(self):
        self.git("branch", self.branch, "main")
        self.assertEqual(self.walk(lambda: (self.checkout / "items.txt").write_text("one\n")).get("stop"), "verified")

    def test_quota_stop_continues_owned_partial_edits(self):
        from foreman.dispatch import QuotaExhausted
        def stop():
            (self.checkout / "items.txt").write_text("partial\n")
            raise QuotaExhausted("synthetic-model")
        self.assertEqual(self.walk(stop).get("stop"), "implement_quota")
        def finish():
            self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")
            (self.checkout / "items.txt").write_text("finished\n")
        self.assertEqual(self.walk(finish).get("stop"), "verified")

    def test_foreign_edits_after_quota_stop_are_rejected(self):
        from foreman.dispatch import QuotaExhausted
        def stop():
            (self.checkout / "items.txt").write_text("partial\n")
            raise QuotaExhausted("synthetic-model")
        self.walk(stop)
        (self.checkout / "foreign.txt").write_text("unowned\n")
        result = self.walk(lambda: self.fail("foreign edits must halt"))
        self.assertEqual(result.get("stop"), "dirty_worktree")
        self.assertIn("Edits changed", result["halt_detail"])

    def test_dirty_initial_worktree_has_specific_detail(self):
        (self.checkout / "foreign.txt").touch()
        result = self.walk(lambda: self.fail("unowned edits must halt"))
        self.assertEqual(result.get("stop"), "dirty_worktree")
        self.assertIn("Unowned edits", result["halt_detail"])

    def test_push_rejection_halts_and_retry_converges(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho 'synthetic rejection' >&2\nexit 1\n")
        hook.chmod(0o755)
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(result.get("stop"), "publish_failed")
        self.assertIn("rejected", result["halt_detail"])
        hook.unlink()
        self.assertEqual(self.walk(lambda: self.fail("duplicate worker")).get("stop"), "verified")

    def test_missing_remote_halts_with_detail(self):
        self.git("remote", "remove", "origin")
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(result.get("stop"), "publish_failed")
        self.assertIn("origin", result["halt_detail"])

    def test_wrong_base_open_pr_stops_without_creation(self):
        from foreman.github import ForgeError, PullRequest
        wrong = PullRequest(number=12, state="OPEN", headRefOid="a" * 40, headRefName=self.branch,
                            baseRefName="release", baseRefOid="b" * 40, autoMergeRequest=None)
        with patch.object(LiveForge, "_view_pr", return_value=wrong):
            with self.assertRaisesRegex(ForgeError, "selected base"):
                LiveForge().open_pull_request(REPO, self.branch, "main", "Synthetic", "Closes #8")
        self.assertEqual(self.created, [])

    def test_commit_subject_and_dispatch_model_trailer(self):
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        message = self.git("show", "-s", "--format=%B", "HEAD")
        self.assertLessEqual(len(message.splitlines()[0]), 72)
        self.assertTrue(message.endswith(f"Co-Authored-By: {result['models_used']['implement']} <noreply@openai.com>"))

    def test_git_calls_disable_prompts_and_bound_network_and_commit(self):
        from foreman.workspace import WorkspaceError
        workspace = LiveWorkspace(self.checkout)
        for operation in ("fetch", "push", "ls-remote", "commit"):
            with self.subTest(operation=operation), patch("foreman.workspace.subprocess.run", side_effect=subprocess.TimeoutExpired(operation, 30)) as call:
                with self.assertRaisesRegex(WorkspaceError, f"git {operation} timed out after 30 seconds"):
                    workspace._git(operation)
                self.assertEqual(call.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(call.call_args.kwargs["timeout"], 30)

    def test_auth_failure_under_pty_returns_without_prompting(self):
        import os
        import pty
        import sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        class RejectAuth(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="synthetic"')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), RejectAuth)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        master, slave = pty.openpty()
        try:
            url = f"http://127.0.0.1:{server.server_port}/synthetic.git"
            code = (
                "from pathlib import Path; from foreman.workspace import LiveWorkspace; "
                f"LiveWorkspace(Path({str(self.checkout)!r}))._git('-c', 'credential.helper=', 'ls-remote', {url!r}, timeout=3)"
            )
            result = run_subprocess([sys.executable, "-c", code], stdin=slave, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("terminal prompts disabled", result.stderr)
        finally:
            os.close(master)
            os.close(slave)
            server.shutdown()
            server.server_close()
            thread.join()

    def test_synthetic_token_never_reaches_dry_run_writes_or_print(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from foreman.cli import _print_run
        from foreman.delivery import run_delivery
        from foreman.github import pull_request_command
        synthetic = "ghp_SECRETtoken123"
        result = run_delivery(FailoverDispatcher(ScriptHandler()), repo=REPO, request="synthetic fixture",
                              slice={**EMPTY_SLICE, "title": f"Fix {synthetic}"})
        output = StringIO()
        with redirect_stdout(output):
            _print_run(result)
        self.assertNotIn(synthetic, output.getvalue())
        self.assertNotIn(synthetic, json.dumps(result["commands"]))
        self.assertNotIn(synthetic, json.dumps(pull_request_command(REPO, self.branch, "main", f"Fix {synthetic}", synthetic)))

    def test_default_branch_is_parsed_and_cached_once(self):
        forge = LiveForge()
        command = ["gh", "repo", "view", REPO, "--json", "defaultBranchRef"]
        with patch("foreman.github.subprocess.run", return_value=subprocess.CompletedProcess(command, 0, '{"defaultBranchRef":{"name":"trunk"}}', "")) as call:
            self.assertEqual(forge.default_branch(REPO), "trunk")
            self.assertEqual(forge.default_branch(REPO), "trunk")
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0], command)

    def test_live_title_is_redacted_and_long_subject_ends_at_word(self):
        synthetic = "ghp_SECRETtoken123"
        self.slice = {**EMPTY_SLICE, "title": f"Fix {synthetic} " + "synthetic words " * 9}
        from foreman.redaction import redact
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", redact(self.slice["title"]).lower()).strip("-")[:60].rstrip("-")
        self.branch = f"8-{slug}"
        self.ref = f"refs/heads/{self.branch}"
        # The fixture asserts the redacted GitHub title at the subprocess boundary.
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(result.get("stop"), "verified")
        message = self.git("show", "-s", "--format=%B", "HEAD")
        self.assertNotIn(synthetic, message)
        self.assertNotIn(synthetic.lower(), self.branch)
        self.assertLessEqual(len(message.splitlines()[0]), 72)
        self.assertTrue(message.splitlines()[0].endswith("words") or message.splitlines()[0].endswith("synthetic"))

    def test_wrong_push_remote_is_rejected_before_network(self):
        from foreman.workspace import WorkspaceError
        def remote(command, **kwargs):
            value = b"https://github.com/Example/Other.git" if "--push" in command else b"https://github.com/Example/Engine.git"
            return subprocess.CompletedProcess(command, 0, value, b"")
        with patch("foreman.workspace.subprocess.run", side_effect=remote) as call:
            with self.assertRaisesRegex(WorkspaceError, "origin does not point"):
                LiveWorkspace(self.checkout).push(self.branch, REPO)
        self.assertEqual(call.call_count, 2)

    def test_crash_during_worker_continues_partial_edits(self):
        def crash():
            (self.checkout / "items.txt").write_text("partial\n")
            raise SystemExit("synthetic crash during worker")
        with self.assertRaises(SystemExit):
            self.walk(crash)
        def finish():
            self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")
            (self.checkout / "items.txt").write_text("finished\n")
        self.assertEqual(self.walk(finish).get("stop"), "verified")

    def test_index_lock_during_prepare_retries_before_worker(self):
        lock = self.checkout / ".git/index.lock"
        lock.touch()
        result = self.walk(lambda: self.fail("preparation must finish first"))
        self.assertEqual(result.get("stop"), "git_failed")
        self.assertIn("index.lock", result["halt_detail"])
        lock.unlink()
        self.assertEqual(self.walk(lambda: (self.checkout / "items.txt").write_text("one\n")).get("stop"), "verified")

    def test_repository_default_branch_drives_branch_pr_and_merge(self):
        self.git("branch", "-m", "main", "trunk")
        self.base = "trunk"
        self.git("push", "origin", self.base)
        result = self.walk(lambda: (self.checkout / "items.txt").write_text("one\n"))
        self.assertEqual(result.get("delivery_base"), "trunk")
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.git("rev-parse", "trunk"))
