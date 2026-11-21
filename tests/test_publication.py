import tests

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.adapters import Outcome
from foreman.config import load_config
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.delivery import LiveRun
from foreman.redaction import redact
from foreman.github import LiveForge
from foreman.runner import run_live_delivery
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_pin
from test_runner import BRANCH, EMPTY_SLICE, slice_body

REPO = "Example/Engine"
REF = f"refs/heads/{BRANCH}"
run_subprocess = subprocess.run

class PublicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.pin = fixture_pin(root / "fixture")

    def setUp(self):
        self.fixture = self.enterContext(ExitStack())
        self.reset_fixture()

    def reset_fixture(self):
        self.fixture.close()
        self.fixture.enter_context(patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=self.pin)))
        self.base = "main"
        self.branch = BRANCH
        self.ref = REF
        self.slice = EMPTY_SLICE
        root = Path(self.fixture.enter_context(TemporaryDirectory()))
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
        self.fixture.enter_context(patch("foreman.github.subprocess.run", side_effect=self.subprocess))

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

    def edit(self):
        (self.checkout / "items.txt").write_text("one\n")

    def no_worker(self):
        self.fail("completed worker must not repeat")

    def walk(self, edit=None, reviews=None, family=None):
        script = ScriptHandler(reviews=reviews)
        workspace = LiveWorkspace(self.checkout)

        def handler(node, model, state, instruction):
            if node == "implement":
                self.assertEqual(workspace.inspect().branch, self.branch)
                if family and model.family != family:
                    return Outcome("cli_missing")
                outcome = (edit or self.edit)()
                if isinstance(outcome, Outcome):
                    return outcome
            if node == "review":
                self.published_heads.append(self.remote_head())
            return script(node, model, state, instruction)

        return run_live_delivery(
            repo=REPO, issue=8, check="test -f items.txt", checkpoint=self.checkpoint,
            run=LiveRun(LiveForge(), workspace), dispatcher=FailoverDispatcher(handler),
        )

    def test_graph_commits_and_pushes_before_opening_one_pull_request(self):
        for family, email in (("gpt", "noreply@openai.com"), ("claude", "noreply@anthropic.com"), ("grok", "noreply@x.ai")):
            with self.subTest(family=family):
                self.reset_fixture()
                self.git("switch", "-c", "unrelated")
                self.git("commit", "--allow-empty", "-m", "Unrelated change")
                result = self.walk(family=family)
                self.assertEqual(result.get("merge"), "armed")
                self.assertEqual(self.remote_head(), result.get("head_sha"))
                self.assertEqual(result.get("verified_head"), self.remote_head())
                self.assertEqual(self.created, [self.remote_head()])
                self.assertEqual(self.git("rev-parse", f"{self.ref}^"), self.git("rev-parse", self.base))
                self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "one")
                message = self.git("show", "-s", "--format=%B", "HEAD")
                self.assertLessEqual(len(message.splitlines()[0]), 72)
                self.assertTrue(message.endswith(f"Co-Authored-By: {result['models_used']['implement']} <{email}>"))

    def test_second_attempt_adds_a_commit_to_the_same_pull_request(self):
        edits = iter(("one\n", "two\n"))
        result = self.walk(lambda: (self.checkout / "items.txt").write_text(next(edits)), ["changes", "approve"])
        self.assertEqual(result.get("merge"), "armed")
        first, second = self.published_heads
        self.assertEqual(self.created, [first])
        self.assertEqual(second, self.remote_head())
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", f"{self.ref}^"), first)
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "two")

    def test_pre_edit_quota_falls_over_before_checking_changes(self):
        calls = []
        def edit():
            calls.append(1)
            if len(calls) == 1:
                return Outcome("quota")
            self.edit()
        result = self.walk(edit)
        self.assertEqual(result["stop"], "verified")
        self.assertEqual([a["outcome"] for a in result["attempts"] if a["stage"] == "implement"], ["quota", "ran"])

    def test_no_changes_fails_without_pushing(self):
        result = self.walk(lambda: None)
        self.assertEqual(result.get("stop"), "failed")
        self.assertEqual(result.get("halt_detail"), "no changes")
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "implement"])
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
        self.assertEqual(self.created, [])

    def test_worker_commit_is_detected_using_the_actual_head(self):
        def commit():
            self.git("commit", "--allow-empty", "-m", "Out-of-band commit")

        result = self.walk(commit)
        self.assertEqual(result.get("stop"), "implementation_rejected")
        self.assertIn("nonempty", result.get("halt_detail", ""))
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "implement"])
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
        self.assertEqual(self.created, [])

    def worker_commit(self, message=None):
        self.git("add", "--all")
        self.git("commit", "-m", message or "Worker change\n\nCo-Authored-By: gpt-6.1-sol-high <noreply@openai.com>")

    def test_worker_commit_replays_reconciliation_and_publication(self):
        for crash_at in ("push", "open_pull_request"):
            with self.subTest(crash_at=crash_at):
                self.reset_fixture()
                def edit():
                    self.edit()
                    self.worker_commit()
                owner = LiveWorkspace if crash_at == "push" else LiveForge
                with patch.object(owner, crash_at, side_effect=SystemExit), self.assertRaises(SystemExit):
                    self.walk(edit)
                head = self.git("rev-parse", "HEAD")
                with patch.object(LiveWorkspace, "commit", side_effect=AssertionError("duplicate commit")):
                    result = self.walk(self.no_worker)
                self.assertEqual(result["stop"], "verified")
                self.assertEqual(result["head_sha"], head)
                self.assertEqual(result["implementation_candidate"], {
                    "disposition": "adopted", "head": head, "tree": self.git("rev-parse", "HEAD^{tree}"),
                })
                self.assertEqual(self.walk(self.no_worker)["head_sha"], head)

    def test_worker_history_rejections_preserve_checkout_and_usage(self):
        for kind in ("branch", "detached", "rewind", "unrelated", "merge", "dirty", "metadata", "long subject", "empty"):
            with self.subTest(kind=kind):
                self.reset_fixture()
                if kind == "rewind":
                    self.git("commit", "--allow-empty", "-m", "New base")
                def edit():
                    if kind == "unrelated":
                        self.git("switch", "--orphan", "orphan")
                        self.edit()
                        self.worker_commit()
                        sha = self.git("rev-parse", "HEAD")
                        self.git("switch", self.branch)
                        self.git("update-ref", "HEAD", sha)
                        return
                    if kind == "merge":
                        self.git("switch", "-c", "side")
                        (self.checkout / "side.txt").write_text("side")
                        self.worker_commit()
                        self.git("switch", self.branch)
                    self.edit()
                    self.worker_commit("No attribution" if kind == "metadata" else
                                       "x" * 73 if kind == "long subject" else None)
                    if kind == "branch":
                        self.git("switch", "-c", "other")
                    elif kind == "detached":
                        self.git("switch", "--detach")
                    elif kind == "rewind":
                        self.git("update-ref", "HEAD", self.git("rev-parse", "main^"))
                    elif kind == "merge":
                        self.git("merge", "--no-ff", "side", "-m", "Merge")
                    elif kind == "dirty":
                        (self.checkout / "extra.txt").write_text("dirty")
                    elif kind == "empty":
                        self.git("commit", "--allow-empty", "-m", "Empty")
                result = self.walk(edit)
                self.assertEqual(result["stop"], "implementation_rejected")
                before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
                again = self.walk(self.no_worker)
                self.assertEqual(again["stop"], "implementation_rejected")
                self.assertEqual(again["attempts"], result["attempts"])
                self.assertEqual(before, (self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")))
                self.assertFalse(self.created)
                if kind == "branch":
                    self.git("switch", self.branch)
                    recovered = self.walk(self.no_worker)
                    self.assertEqual(recovered["stop"], "verified")
                    self.assertEqual([a for a in recovered["attempts"] if a["stage"] == "implement"],
                                     [a for a in result["attempts"] if a["stage"] == "implement"])

    def test_reconciled_candidate_mutation_is_rejected_before_publication(self):
        original = LiveWorkspace.reconcile
        def mutate(workspace, *args):
            candidate = original(workspace, *args)
            (self.checkout / "extra.txt").write_text("later")
            self.worker_commit()
            return candidate
        def edit():
            self.edit()
            self.worker_commit()
        with patch.object(LiveWorkspace, "reconcile", mutate):
            result = self.walk(edit)
        self.assertEqual(result["stop"], "git_failed")
        self.assertIn("reconciled worker commit", result["halt_detail"])
        self.assertFalse(self.created)

    def test_rerun_retries_publication_without_rerunning_the_worker(self):
        self.create_error = True
        failed = self.walk()
        self.assertEqual(failed.get("stop"), "publish_failed")
        self.assertEqual(failed.get("head_sha"), self.remote_head())
        self.create_error = False
        result = self.walk(lambda: self.fail("The committed change should be reused"))
        self.assertEqual(result.get("merge"), "armed")
        self.assertEqual(self.created, [failed.get("head_sha")])

    def test_crash_windows_resume_without_duplicate_completed_work(self):
        cases = (
            ("branch created", LiveWorkspace, "create_branch", True, False),
            ("worker checkpointed", LiveWorkspace, "commit", False, True),
            ("commit completed", LiveWorkspace, "commit", True, True),
            ("push completed", LiveWorkspace, "push", True, True),
            ("PR created", LiveForge, "open_pull_request", True, True),
        )
        for label, owner, operation, completed, worker_done in cases:
            with self.subTest(window=label):
                self.reset_fixture()
                original = getattr(owner, operation)
                def crash(*args, **kwargs):
                    if completed:
                        original(*args, **kwargs)
                    raise SystemExit(label)
                with patch.object(owner, operation, crash), self.assertRaises(SystemExit):
                    self.walk(self.edit if worker_done else self.no_worker)
                head = self.git("rev-parse", "HEAD")
                original_git = LiveWorkspace._git
                def resume_git(workspace, *args, **kwargs):
                    if operation == "commit" and completed:
                        self.assertNotEqual(args[0], "commit", "duplicate commit")
                    if operation in ("push", "open_pull_request"):
                        self.assertNotEqual(args[0], "push", "duplicate push")
                    return original_git(workspace, *args, **kwargs)
                with patch.object(LiveWorkspace, "_git", resume_git):
                    result = self.walk(self.no_worker if worker_done else self.edit)
                self.assertEqual(result.get("stop"), "verified")
                self.assertEqual(len(self.created), 1)
                if completed and worker_done:
                    self.assertEqual(self.remote_head(), head)

    def test_index_lock_is_resumable(self):
        for phase, before_worker in (("prepare", True), ("commit", False)):
            with self.subTest(phase=phase):
                self.reset_fixture()
                lock = self.checkout / ".git/index.lock"
                def edit_with_lock():
                    self.edit()
                    lock.touch()
                if before_worker:
                    lock.touch()
                result = self.walk(self.no_worker if before_worker else edit_with_lock)
                self.assertEqual(result.get("stop"), "git_failed")
                self.assertIn("index.lock", result["halt_detail"])
                lock.unlink()
                self.assertEqual(self.walk(self.edit if before_worker else self.no_worker).get("stop"), "verified")

    def test_existing_branch_at_expected_base_is_reused(self):
        self.git("branch", self.branch, "main")
        self.assertEqual(self.walk().get("stop"), "verified")

    def test_quota_stop_continues_owned_partial_edits(self):
        from foreman.dispatch import QuotaExhausted
        for kind, stop_reason in (("exception", "implement_quota"), ("outcome", "quota_exhausted")):
            with self.subTest(kind=kind):
                self.reset_fixture()
                def stop():
                    (self.checkout / "items.txt").write_text("partial\n")
                    if kind == "exception":
                        raise QuotaExhausted("synthetic-model")
                    return Outcome("quota")
                self.assertEqual(self.walk(stop).get("stop"), stop_reason)
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

    def test_publication_failures(self):
        cases = (("rejected push", "rejected"), ("missing remote", "origin"), ("wrong push URL", "origin does not point"))
        for case, detail in cases:
            with self.subTest(case=case):
                self.reset_fixture()
                hook = self.remote / "hooks/pre-receive"
                hook.write_text("#!/bin/sh\necho 'synthetic rejection' >&2\nexit 1\n")
                hook.chmod(0o755)
                if case == "missing remote":
                    self.git("remote", "remove", "origin")
                original = self.subprocess
                def remote(command, **kwargs):
                    if case == "wrong push URL" and command[:3] == ["git", "remote", "get-url"] and "--push" in command:
                        return subprocess.CompletedProcess(command, 0, b"https://github.com/Example/Other.git", b"")
                    if case == "wrong push URL":
                        self.assertNotEqual(command[:2], ["git", "push"], "wrong URL must fail before network")
                    return original(command, **kwargs)
                with patch("foreman.workspace.subprocess.run", side_effect=remote):
                    result = self.walk()
                self.assertEqual(result.get("stop"), "publish_failed")
                self.assertIn(detail, result["halt_detail"])
                self.assertEqual(self.created, [])
                hook.unlink()
                if case == "rejected push":
                    self.assertEqual(self.walk(self.no_worker).get("stop"), "verified")

    def test_wrong_base_open_pr_stops_without_creation(self):
        from foreman.github import ForgeError, PullRequest
        wrong = PullRequest(number=12, state="OPEN", headRefOid="a" * 40, headRefName=self.branch,
                            baseRefName="release", baseRefOid="b" * 40, autoMergeRequest=None)
        with patch.object(LiveForge, "_view_pr", return_value=wrong):
            with self.assertRaisesRegex(ForgeError, "selected base"):
                LiveForge().open_pull_request(REPO, self.branch, "main", "Synthetic", "Closes #8")
        self.assertEqual(self.created, [])

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
        import errno
        import select
        import signal
        import time
        url = f"http://127.0.0.1:{server.server_port}/synthetic.git"
        code = (
            "from pathlib import Path; from foreman.workspace import LiveWorkspace; "
            f"LiveWorkspace(Path({str(self.checkout)!r}))._git('-c', 'credential.helper=', 'ls-remote', {url!r}, timeout=3)"
        )
        pid, master = pty.fork()
        if pid == 0:
            # This authentication probe only talks to the loopback server above.
            os.execve(sys.executable, [sys.executable, "-c", code], {**os.environ, "GIT_ALLOW_PROTOCOL": "http"})
        output = bytearray()
        exited = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
            done, status = os.waitpid(pid, os.WNOHANG)
            while done == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
                done, status = os.waitpid(pid, os.WNOHANG)
            exited = done == pid
            self.assertTrue(exited, "authentication hung under a controlling pseudo-terminal")
            self.assertNotEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn("terminal prompts disabled", output.decode(errors="replace"))
        finally:
            if not exited:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)
            server.shutdown()
            server.server_close()
            thread.join()

    def test_synthetic_token_never_reaches_publication_writes(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from foreman.cli import _print_run
        from foreman.delivery import run_delivery
        from foreman.github import pull_request_command
        import re
        synthetic = "ghp_SECRETtoken123"
        for mode in ("dry", "live"):
            with self.subTest(mode=mode):
                self.reset_fixture()
                self.slice = {**EMPTY_SLICE, "title": f"Fix {synthetic} " + "synthetic words " * 9}
                slug = re.sub(r"[^a-z0-9]+", "-", redact(self.slice["title"]).lower()).strip("-")[:60].rstrip("-")
                self.branch, self.ref = f"8-{slug}", f"refs/heads/8-{slug}"
                if mode == "dry":
                    result = run_delivery(FailoverDispatcher(ScriptHandler()), repo=REPO, request="synthetic fixture", slice=self.slice)
                    output = StringIO()
                    with redirect_stdout(output):
                        _print_run(result)
                    self.assertNotIn(synthetic, output.getvalue())
                    self.assertNotIn(synthetic, json.dumps(result["commands"]))
                    self.assertNotIn(synthetic, json.dumps(pull_request_command(REPO, self.branch, self.base, self.slice["title"], synthetic)))
                else:
                    self.assertEqual(self.walk().get("stop"), "verified")
                    message = self.git("show", "-s", "--format=%B", "HEAD")
                    self.assertNotIn(synthetic, message)
                    self.assertNotIn(synthetic.lower(), self.branch)
                    self.assertLessEqual(len(message.splitlines()[0]), 72)
                    self.assertTrue(message.splitlines()[0].endswith(("words", "synthetic")))

    def test_repository_default_branch(self):
        for base in ("main", "trunk"):
            with self.subTest(base=base):
                self.reset_fixture()
                if base != self.base:
                    self.git("branch", "-m", self.base, base)
                    self.base = base
                    self.git("push", "origin", base)
                original = self.subprocess
                with patch("foreman.github.subprocess.run", side_effect=original) as call:
                    result = self.walk()
                self.assertEqual(result.get("delivery_base"), base)
                self.assertEqual(result.get("stop"), "verified")
                self.assertEqual(self.git("rev-parse", "HEAD^"), self.git("rev-parse", base))
                commands = [c.args[0] for c in call.call_args_list if c.args[0][:3] == ["gh", "repo", "view"]]
                self.assertEqual(commands, [["gh", "repo", "view", REPO, "--json", "defaultBranchRef"]])

    def test_crash_during_worker_stops_with_unknown_usage(self):
        def crash():
            (self.checkout / "items.txt").write_text("partial\n")
            raise SystemExit("synthetic crash during worker")
        with self.assertRaises(SystemExit):
            self.walk(crash)
        self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")
        def unexpected():
            self.fail("Recovery dispatched a worker with unknown usage")
        self.assertEqual(self.walk(unexpected).get("stop"), "usage_unknown")
