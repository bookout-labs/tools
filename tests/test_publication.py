import tests
from foreman.fixture_manifest import FixtureManifest

from contextlib import ExitStack
from dataclasses import replace
import json
import multiprocessing
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.readiness import MergeState
from foreman.adapters import Outcome
from foreman.config import load_config
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.delivery import LiveRun
from foreman.redaction import redact
from foreman.github import LiveForge
from foreman.runner import run_live_delivery
from foreman.workspace import WorkState, WorkspaceError, LiveWorkspace
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
        self.queued = False
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
            case ["api", "repos/bookout-labs/tools/issues?state=open&per_page=100"]:
                response = [[]]
            case ["api", "repos/bookout-labs/tools/issues"]:
                response = {"number": 101}
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
                            "baseRefName": self.base, "baseRefOid": self.git("--git-dir", str(self.remote), "rev-parse", self.base), "autoMergeRequest": None}
            case ["api", "graphql"]:
                response = dict(number=12, state="OPEN", headRefOid=self.remote_head(),
                    baseRefName=self.base, baseRefOid=self.git("--git-dir", str(self.remote), "rev-parse", self.base),
                    isDraft=False, autoMergeRequest=None,
                    mergeQueueEntry=dict(id="queue-12", state="QUEUED", headCommit=dict(oid=self.remote_head())) if self.queued else None)
            case ["api", "repos/Example/Engine/pulls/12/reviews"]:
                response = ({"id": 1, "state": "COMMENTED", "commit_id": self.remote_head(), "body": "posted"}
                            if "POST" in command else [[]])
            case ["api", "repos/Example/Engine/pulls/12"]:
                response = dict(number=12, state="open", draft=False, mergeable=True, mergeable_state="clean",
                    merge_commit_sha=None, head=dict(sha=self.remote_head(), ref=self.branch),
                    base=dict(sha=self.git("--git-dir", str(self.remote), "rev-parse", self.base), ref=self.base))
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

    def edit(self):
        (self.checkout / "items.txt").write_text("one\n")

    def no_worker(self):
        self.fail("completed worker must not repeat")

    def walk(self, edit=None, reviews=None, family=None, fact_record="tests/fixtures/manifest.toml",
             answer=None, preflight="", check_log=None):
        script = ScriptHandler(reviews=reviews)
        workspace = LiveWorkspace(self.checkout)

        def handler(node, model, state, instruction):
            if node == "implement":
                self.assertEqual(workspace.inspect().branch, self.branch)
                self.assertEqual(instruction.count("Foreman owns commits and publishing"), 1)
                if family and model.family != family:
                    return Outcome("cli_missing")
                outcome = (edit or self.edit)()
                if isinstance(outcome, Outcome):
                    return outcome
            if node == "review":
                self.published_heads.append(self.remote_head())
            return script(node, model, state, instruction)

        return run_live_delivery(
            repo=REPO, issue=8, check="test -f items.txt", preflight=preflight, checkpoint=self.checkpoint,
            fact_record=fact_record, answer=answer,
            run=LiveRun(LiveForge(), workspace, check_log=check_log), dispatcher=handler,
        )

    def advance_origin(self, *, parent=None):
        parent = parent or self.git("--git-dir", str(self.remote), "rev-parse", "main")
        head = self.git("commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "Origin advance")
        self.git("push", "origin", f"{head}:refs/heads/main")
        return head

    def test_new_branch_uses_origin_with_stale_missing_or_detached_local_base(self):
        for kind in ("stale", "missing", "newer", "divergent"):
            with self.subTest(kind=kind):
                self.reset_fixture()
                local = self.git("rev-parse", "main")
                tip = self.advance_origin()
                if kind != "stale":
                    detached = tip if kind == "newer" else local
                    if kind in ("newer", "divergent"):
                        detached = self.git("commit-tree", "HEAD^{tree}", "-p", detached, "-m", "Detached")
                    self.git("switch", "--detach", detached)
                    if kind == "missing":
                        self.git("branch", "-d", "main")
                before = self.git("rev-parse", "HEAD")
                result = self.walk()
                self.assertEqual(result["stop"], "verified")
                self.assertEqual(result["work"].attempt_head, tip)
                self.assertEqual(self.git("rev-parse", "HEAD^"), tip)
                self.assertEqual(self.git("rev-parse", before), before)
                if kind != "missing":
                    self.assertEqual(self.git("rev-parse", "main"), local)

    def test_creation_uses_resolved_sha_even_if_shared_remote_ref_moves(self):
        old = self.git("rev-parse", "main")
        tip = self.advance_origin()
        original = LiveWorkspace._git
        def moving(workspace, *args, **kwargs):
            value = original(workspace, *args, **kwargs)
            if args[:2] == ("rev-parse", "--verify"):
                self.git("update-ref", "refs/remotes/origin/main", old)
            return value
        with patch.object(LiveWorkspace, "_git", moving):
            LiveWorkspace(self.checkout).create_branch(self.branch, "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)

    def test_prepare_crash_and_checkpoint_resume_keep_selected_parent(self):
        for checkpointed in (False, True):
            with self.subTest(checkpointed=checkpointed):
                self.reset_fixture()
                tip = self.advance_origin()
                original = LiveWorkspace.create_branch if not checkpointed else LiveWorkspace.commit
                def crash(*args, **kwargs):
                    if not checkpointed:
                        original(*args, **kwargs)
                    raise SystemExit("crash")
                method = "commit" if checkpointed else "create_branch"
                with patch.object(LiveWorkspace, method, crash), self.assertRaises(SystemExit):
                    self.walk(self.edit if checkpointed else self.no_worker)
                self.advance_origin()
                result = self.walk(self.no_worker if checkpointed else self.edit)
                self.assertEqual(result["stop"], "verified")
                self.assertEqual(result["work"].attempt_head, tip)
                self.assertEqual(self.git("rev-parse", "HEAD^"), tip)

    def test_incompatible_existing_branch_is_not_adopted(self):
        for kind in ("orphan", "stale", "extra"):
            with self.subTest(kind=kind):
                self.reset_fixture()
                if kind == "orphan":
                    self.git("switch", "--orphan", self.branch)
                    self.git("commit", "--allow-empty", "-m", "Unrelated")
                else:
                    self.git("switch", "-c", self.branch)
                    if kind == "extra":
                        self.git("commit", "--allow-empty", "-m", "Unrelated extra commit")
                existing = self.git("rev-parse", "HEAD")
                self.git("switch", "main")
                tip = self.advance_origin()
                before = LiveWorkspace(self.checkout).inspect()
                result = self.walk(self.no_worker)
                self.assertEqual(result["stop"], "git_failed")
                self.assertEqual(LiveWorkspace(self.checkout).inspect(), before)
                for detail in (self.branch, existing, "origin/main", tip, "Checkout preserved", "Switch to", "delete"):
                    self.assertIn(detail, result["halt_detail"])

    def test_existing_branch_at_origin_is_adopted_from_divergent_detached_head(self):
        tip = self.advance_origin()
        self.git("branch", self.branch, tip)
        detached = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Divergent")
        self.git("switch", "--detach", detached)
        result = self.walk()
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["work"].attempt_head, tip)

    def test_merge_base_operational_errors_keep_their_detail(self):
        original = LiveWorkspace._git
        for failure in (subprocess.CompletedProcess([], 128, b"", b"fatal: object store locked"),
                        subprocess.TimeoutExpired("git merge-base", 30)):
            with self.subTest(failure=failure):
                before = LiveWorkspace(self.checkout).inspect()
                def git(workspace, *args, **kwargs):
                    if args[:2] == ("--no-replace-objects", "merge-base"):
                        with patch("foreman.workspace.subprocess.run", side_effect=(
                            failure if isinstance(failure, Exception) else [failure]
                        )):
                            return original(workspace, *args, **kwargs)
                    return original(workspace, *args, **kwargs)
                with patch.object(LiveWorkspace, "_git", git), self.assertRaises(WorkspaceError) as caught:
                    LiveWorkspace(self.checkout)._base_merge_base("main", before.head)
                self.assertIn("locked" if isinstance(failure, subprocess.CompletedProcess) else "timed out", str(caught.exception))
                self.assertNotIn("unshallow", str(caught.exception))
                self.assertEqual(LiveWorkspace(self.checkout).inspect(), before)

    def test_prepare_transport_failures_do_not_use_cached_base(self):
        for kind in ("missing", "absent", "auth", "timeout"):
            with self.subTest(kind=kind):
                self.reset_fixture()
                self.git("fetch", "origin", "main")
                before = LiveWorkspace(self.checkout).inspect()
                original = LiveWorkspace._git
                def failing(workspace, *args, **kwargs):
                    if args[0] == "fetch" and kind in ("auth", "timeout"):
                        raise WorkspaceError("App authentication denied" if kind == "auth" else "git fetch timed out")
                    return original(workspace, *args, **kwargs)
                if kind == "missing":
                    self.git("--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/main")
                elif kind == "absent":
                    self.git("remote", "remove", "origin")
                with patch.object(LiveWorkspace, "_git", failing):
                    result = self.walk(self.no_worker)
                self.assertEqual(result["stop"], "git_failed")
                self.assertEqual(LiveWorkspace(self.checkout).inspect(), before)
                self.assertEqual(self.git("for-each-ref", "--format=%(refname)", self.ref), "")

    def test_two_review_rounds_count_whole_pr_while_origin_advances(self):
        heads = []
        def edit():
            heads.append(self.git("rev-parse", "HEAD"))
            (self.checkout / "src").mkdir(exist_ok=True)
            (self.checkout / "src/items.py").write_text("one\n" * len(heads))
            self.edit()
        original = LiveWorkspace.create_branch
        prepares = []
        def prepare(workspace, branch, base):
            if prepares:
                self.advance_origin()
            prepares.append(branch)
            return original(workspace, branch, base)
        with patch.object(LiveWorkspace, "create_branch", prepare):
            result = self.walk(edit, reviews=["changes", "approve"])
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(len(heads), 2)
        self.assertEqual(result["work"].attempt_head, heads[1])
        self.assertEqual(self.git("rev-parse", "HEAD^"), heads[1])
        size = LiveWorkspace(self.checkout).pull_request_size(self.git("rev-parse", "HEAD"), "main")
        self.assertEqual(size["lines"], 3)
        self.assertEqual(size["merge_base"], heads[0])

    def test_depth_one_without_local_base_prepares_and_missing_ancestry_is_explicit(self):
        tip = self.advance_origin()
        shallow = self.checkout.parent / "shallow"
        self.git("clone", "--depth=1", "--branch=main", self.remote.as_uri(), str(shallow))
        workspace = LiveWorkspace(shallow)
        workspace._git("switch", "--detach")
        workspace._git("branch", "-d", "main")
        workspace.create_branch(self.branch, "main")
        self.assertEqual(workspace.inspect().head, tip)
        old = self.git("rev-parse", "main")
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", old)
        before = workspace.inspect()
        with self.assertRaisesRegex(WorkspaceError, "git fetch --unshallow origin"):
            workspace.reconcile(tip, self.branch, "main", None)
        self.assertEqual(workspace.inspect(), before)

    def test_rewritten_and_rewound_origin_keep_checkout_and_comparison_semantics(self):
        parent = self.advance_origin()
        workspace = LiveWorkspace(self.checkout)
        workspace.create_branch(self.branch, "main")
        self.edit()
        before = workspace.inspect()
        old = self.git("rev-parse", "main")
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", old)
        candidate = workspace.reconcile(parent, self.branch, "main", None)
        self.assertIsNotNone(candidate)
        self.assertEqual(workspace._base_merge_base("main", parent), old)
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "Unrelated")
        self.git("--git-dir", str(self.remote), "fetch", str(self.checkout), unrelated)
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", unrelated)
        with self.assertRaisesRegex(WorkspaceError, "restore related base history"):
            workspace.reconcile(parent, self.branch, "main", None)
        self.assertEqual(workspace.inspect(), before)

    def test_refresh_adopts_only_the_observed_remote_repair(self):
        self.walk()
        repaired = self.checkout.parent / "repair"
        run_subprocess(["git", "clone", str(self.remote), str(repaired)], check=True, capture_output=True)
        run_subprocess(["git", "switch", self.branch], cwd=repaired, check=True, capture_output=True)
        (repaired / "repair.txt").write_text("external repair")
        for args in [("config", "user.name", "Repair"), ("config", "user.email", "repair@example.com"),
                     ("add", "repair.txt"), ("-c", "commit.gpgsign=false", "commit", "-m", "Repair"),
                     ("push", "origin", self.branch)]:
            run_subprocess(["git", *args], cwd=repaired, check=True, capture_output=True)
        head = self.remote_head()
        self.advance_origin()
        workspace = LiveWorkspace(self.checkout)
        before = workspace.inspect().head
        workspace.create_branch(self.branch, "main")
        self.assertEqual(workspace.inspect().head, before)
        with self.assertRaisesRegex(WorkspaceError, "changed during fetch"):
            LiveWorkspace(self.checkout).refresh(self.branch, before)
        self.assertEqual(LiveWorkspace(self.checkout).inspect().head, before)
        LiveWorkspace(self.checkout).refresh(self.branch, head)
        self.advance_origin()
        workspace.create_branch(self.branch, "main")
        self.assertEqual(LiveWorkspace(self.checkout).inspect().head, head)
        self.assertEqual((self.checkout / "repair.txt").read_text(), "external repair")

    def test_graph_commits_and_pushes_before_opening_one_pull_request(self):
        for family, email in (("gpt", "noreply@openai.com"), ("claude", "noreply@anthropic.com"), ("grok", "noreply@x.ai")):
            with self.subTest(family=family):
                self.reset_fixture()
                self.git("switch", "-c", "unrelated")
                self.git("commit", "--allow-empty", "-m", "Unrelated change")
                result = self.walk(family=family)
                self.assertEqual(result.get("merge", MergeState()).status, "armed")
                self.assertEqual(self.remote_head(), result.get("work", WorkState()).head_sha)
                self.assertEqual(result["checks"].verified_head, self.remote_head())
                self.assertEqual(self.created, [self.remote_head()])
                self.assertEqual(self.git("rev-parse", f"{self.ref}^"), self.git("--git-dir", str(self.remote), "rev-parse", self.base))
                self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "one")
                message = self.git("show", "-s", "--format=%B", "HEAD")
                self.assertLessEqual(len(message.splitlines()[0]), 72)
                self.assertTrue(message.endswith(f"Co-Authored-By: {result['models_used']['implement']} <{email}>"))

    def test_second_attempt_adds_a_commit_to_the_same_pull_request(self):
        edits = iter(("one\n", "two\n"))
        result = self.walk(lambda: (self.checkout / "items.txt").write_text(next(edits)), ["changes", "approve"])
        self.assertEqual(result.get("merge", MergeState()).status, "armed")
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
                return Outcome("quota", usage=SCRIPTED_USAGE)
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

    def test_worker_tree_replays_reconciliation_and_publication(self):
        for crash_at, completed in (("soft_reset", False), ("soft_reset", True), ("commit", True),
                                    ("push", False), ("open_pull_request", False)):
            with self.subTest(crash_at=crash_at, completed=completed):
                self.reset_fixture()
                worker_heads = []
                def edit():
                    self.edit()
                    self.worker_commit("Record Python title-casing tool evidence" +
                                       ("\n\nghp_SECRETtoken123" if crash_at == "push" else ""))
                    worker_heads.append(self.git("rev-parse", "HEAD"))
                owner = LiveForge if crash_at == "open_pull_request" else LiveWorkspace
                original = getattr(owner, crash_at)
                def crash(*args, **kwargs):
                    if completed:
                        original(*args, **kwargs)
                    raise SystemExit(crash_at)
                with patch.object(owner, crash_at, crash), self.assertRaises(SystemExit):
                    self.walk(edit)
                commits = []
                original_commit = LiveWorkspace.commit
                def commit(workspace, message):
                    self.assertFalse(commits, "duplicate commit")
                    commits.append(message)
                    original_commit(workspace, message)
                with patch.object(LiveWorkspace, "commit", commit), patch.object(
                    LiveWorkspace, "reconcile", side_effect=AssertionError("reconciliation must use its checkpoint"),
                ):
                    result = self.walk(self.no_worker)
                self.assertEqual(len(commits), int(crash_at == "soft_reset"))
                self.assertEqual(result["stop"], "verified")
                candidate = result["work"].candidate
                self.assertEqual(candidate["disposition"], "adopted")
                self.assertEqual(candidate["head"], worker_heads[0])
                self.assertNotEqual(result["work"].head_sha, worker_heads[0])
                self.assertEqual(candidate["tree"], self.git("rev-parse", "HEAD^{tree}"))
                self.assertEqual(self.git("rev-parse", "HEAD^"), self.git("rev-parse", "main"))
                message = self.git("show", "-s", "--format=%B", "HEAD")
                self.assertTrue(message.startswith("8: List items once"))
                self.assertIn("Co-Authored-By:", message)
                self.assertNotIn("ghp_SECRETtoken123", message)
                self.assertEqual(self.git("--git-dir", str(self.remote), "rev-list", self.ref, "^main").splitlines(), [result["work"].head_sha])
                self.assertEqual(self.walk(self.no_worker)["work"].head_sha, result["work"].head_sha)

    def test_worker_history_rejections_preserve_checkout_and_usage(self):
        for kind, detail in (("branch", "not attached"), ("detached", "not attached"), ("rewind", "non-descendant"),
                             ("unrelated", "non-descendant"), ("merge", "merge commit"),
                             ("dirty", "uncommitted edits"), ("empty", "is empty"), ("published", "already published")):
            with self.subTest(kind=kind):
                self.reset_fixture()
                if kind == "rewind":
                    self.git("commit", "--allow-empty", "-m", "New base")
                    self.git("push", "origin", "main")
                def edit():
                    if kind == "unrelated":
                        self.git("switch", "--orphan", "orphan")
                        self.edit()
                        self.worker_commit()
                        sha = self.git("rev-parse", "HEAD")
                        self.git("branch", "-f", self.branch, sha)
                        self.git("switch", self.branch)
                        return
                    if kind == "merge":
                        self.git("switch", "-c", "side")
                        (self.checkout / "side.txt").write_text("side")
                        self.worker_commit()
                        self.git("switch", self.branch)
                    self.edit()
                    self.worker_commit()
                    if kind == "branch":
                        self.git("switch", "-c", "other")
                    elif kind == "detached":
                        self.git("switch", "--detach")
                    elif kind == "rewind":
                        self.git("reset", "--keep", self.git("rev-parse", "main^"))
                    elif kind == "merge":
                        self.git("merge", "--no-ff", "side", "-m", "Merge")
                    elif kind == "dirty":
                        (self.checkout / "extra.txt").write_text("dirty")
                    elif kind == "published":
                        self.git("push", "origin", self.branch)
                    elif kind == "empty":
                        self.git("commit", "--allow-empty", "-m", "Empty")
                result = self.walk(edit)
                self.assertEqual(result["stop"], "implementation_rejected")
                self.assertIn(detail, result["halt_detail"])
                self.assertIn(self.git("rev-parse", "HEAD"), result["halt_detail"])
                self.assertIn("then rerun", result["halt_detail"])
                if kind in ("rewind", "unrelated"):
                    self.assertEqual(self.git("status", "--porcelain"), "")
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

    def test_non_descendant_history_is_rejected_before_merge_inspection(self):
        for kind in ("rewind", "unrelated"):
            with self.subTest(kind=kind):
                self.reset_fixture()
                self.git("switch", "-c", "side")
                (self.checkout / "side.txt").write_text("side")
                self.worker_commit()
                self.git("switch", "main")
                self.git("merge", "--no-ff", "side", "-m", "Merge into main")
                main = self.git("rev-parse", "HEAD")
                self.git("commit", "--allow-empty", "-m", "Attempt base")
                self.git("push", "origin", "main")
                parent = self.git("rev-parse", "HEAD")

                def edit():
                    if kind == "rewind":
                        self.git("reset", "--keep", main)
                    else:
                        self.git("switch", "--orphan", "unrelated")
                        self.git("commit", "--allow-empty", "-m", "Unrelated root")
                        self.git("branch", "-f", self.branch, "HEAD")
                        self.git("switch", self.branch)

                result = self.walk(edit)
                head = self.git("rev-parse", "HEAD")
                self.assertEqual(result["stop"], "implementation_rejected")
                self.assertIn(f"Worker HEAD {head} is a non-descendant of {parent}", result["halt_detail"])
                self.assertNotIn("merge commit", result["halt_detail"])

    def scratch_policy(self):
        (self.checkout / ".gitignore").write_text("evidence/\n*.log\nnested/cache/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "Declare disposable paths")
        self.git("push", "origin", "main")

    def test_118_tree_rejects_edits_and_commits_then_resumes(self):
        paths = (
            "README.md", "diff-check.txt", "direct-unit-tests.txt", "evals-offline.txt",
            "evals-provider.txt", "evals.txt", "grant-green.txt", "grant-red.txt",
            "newline-red.txt", "offline-bootstrap/sitecustomize.py", "pyright.txt",
            "quoted-operator-red.txt", "run-eval-provider.py", "run-unit-tests.py",
            "runner-checks.txt", "unit-tests-final.txt",
        )
        for mode in ("staged", "committed", "tracked"):
            with self.subTest(mode=mode):
                self.reset_fixture()
                self.scratch_policy()
                if mode == "tracked":
                    (self.checkout / "evidence").mkdir()
                    (self.checkout / "evidence/README.md").write_text("previous\n")
                    self.git("add", "-f", "evidence/README.md")
                    self.git("commit", "-m", "Old scratch")
                    self.git("push", "origin", "main")
                def edit():
                    self.edit()
                    for name in paths:
                        path = self.checkout / "evidence" / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("scratch\n")
                    if mode in ("staged", "committed", "tracked"):
                        self.git("add", "-f", "evidence")
                    if mode == "committed":
                        self.worker_commit()
                result = self.walk(edit)
                self.assertEqual(result["stop"], "implementation_rejected")
                for name in paths:
                    self.assertIn("evidence/" + name, result["halt_detail"])
                    self.assertTrue((self.checkout / "evidence" / name).is_file())
                self.assertFalse(self.created)
                again = self.walk(self.no_worker)
                self.assertEqual(again["stop"], "implementation_rejected")
                self.assertEqual(again["attempts"], result["attempts"])
                if mode == "committed":
                    self.git("reset", "--soft", result["work"].attempt_head)
                for name in paths:
                    (self.checkout / "evidence" / name).unlink()
                if mode in ("staged", "committed", "tracked"):
                    self.git("add", "--all")
                resumed = self.walk(self.no_worker)
                self.assertEqual(resumed["stop"], "verified")
                self.assertEqual(resumed["attempts"][:2], result["attempts"])

    def test_ignored_runtime_and_local_files_never_enter_the_commit(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.reset_fixture()
                self.scratch_policy()
                ignore = self.checkout / ".gitignore"
                ignore.write_text(ignore.read_text() + ".venv/\n__pycache__/\n")
                self.git("add", ".gitignore")
                self.git("commit", "-m", "Runtime ignores")
                self.git("push", "origin", "main")
                (self.checkout / ".git/info/exclude").write_text(".env\n.DS_Store\n")
                paths = (".venv/bin/python", "__pycache__/m.cpython-312.pyc", ".env", ".DS_Store", "evidence/run.log")
                def edit():
                    self.edit()
                    for name in paths:
                        file = self.checkout / name
                        file.parent.mkdir(parents=True, exist_ok=True)
                        file.write_text("local")
                    if committed:
                        self.worker_commit()
                result = self.walk(edit)
                self.assertEqual(result["stop"], "verified")
                files = self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
                for name in paths:
                    self.assertNotIn(name, files)
                    self.assertTrue((self.checkout / name).is_file())

    def test_reconcile_parses_only_the_declared_record(self):
        for record in (None, "evidence/facts.toml"):
            with self.subTest(record=record):
                self.reset_fixture()
                def edit():
                    self.edit()
                    old = self.checkout / "tests/fixtures/manifest.toml"
                    old.parent.mkdir(parents=True)
                    old.write_text("issue = 1116\n")
                    if record:
                        selected = self.checkout / record
                        selected.parent.mkdir()
                        selected.write_text("[fixtures]\n")
                original = FixtureManifest.from_toml
                parsed = []
                def parse(content):
                    parsed.append(content)
                    return original(content)
                with patch.object(FixtureManifest, "from_toml", side_effect=parse):
                    result = self.walk(edit, fact_record=record)
                self.assertEqual(result["stop"], "verified")
                self.assertEqual(parsed, ["[fixtures]\n"] if record else [])

    def test_undeclared_old_manifest_cannot_exempt_ignored_capture(self):
        for record in (None, "evidence/facts.toml"):
            with self.subTest(record=record):
                self.reset_fixture()
                self.scratch_policy()
                def edit():
                    self.edit()
                    old = self.checkout / "tests/fixtures/manifest.toml"
                    old.parent.mkdir(parents=True)
                    old.write_text('[fixtures."capture.log"]\nkind="captured_run"\n'
                                   'tool="tool"\nversion="1"\nargv=["tool"]\n')
                    (self.checkout / "evidence").mkdir()
                    (old.parent / "capture.log").write_text("unselected capture")
                    self.git("add", "-f", "tests/fixtures/capture.log")
                    if record:
                        (self.checkout / record).write_text("[fixtures]\n")
                result = self.walk(edit, fact_record=record)
                self.assertEqual(result["stop"], "implementation_rejected")
                self.assertIn("tests/fixtures/capture.log", result["halt_detail"])
                self.assertNotIn("Invalid fact record", result["halt_detail"])

    def test_malformed_fact_records_fail_closed(self):
        for content in ('[broken', '[fixtures."../bad"]\nkind="captured_run"\ntool="tool"\nversion="1"\nargv=["tool"]\n'):
            with self.subTest(content=content):
                self.reset_fixture()
                def edit():
                    self.edit()
                    path = self.checkout / "evidence/facts.toml"
                    path.parent.mkdir()
                    path.write_text(content)
                result = self.walk(edit, fact_record="evidence/facts.toml")
                self.assertEqual(result["stop"], "implementation_rejected")
                self.assertIn("evidence/facts.toml", result["halt_detail"])
                self.assertFalse(self.created)

    def test_reconcile_uses_remote_base_when_local_base_differs(self):
        self.scratch_policy()
        self.git("switch", "-c", self.branch)
        self.git("branch", "-f", "main", "HEAD^")
        def edit():
            self.edit()
            (self.checkout / "evidence").mkdir()
            (self.checkout / "evidence/scratch").touch()
            self.git("add", "-f", "evidence/scratch")
        self.assertEqual(self.walk(edit)["stop"], "implementation_rejected")

    def test_deleting_tracked_scratch_is_allowed(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.reset_fixture()
                self.scratch_policy()
                file = self.checkout / "evidence/old.log"
                file.parent.mkdir()
                file.write_text("old scratch")
                self.git("add", "-f", "evidence/old.log")
                self.git("commit", "-m", "Old scratch")
                self.git("push", "origin", "main")
                def edit():
                    self.edit()
                    file.unlink()
                    if committed:
                        self.worker_commit()
                self.assertEqual(self.walk(edit)["stop"], "verified")
                self.assertNotIn("evidence/old.log", self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines())

    def test_nested_ignore_and_negation_ignore_local_excludes(self):
        self.scratch_policy()
        directory = self.checkout / "nested"
        directory.mkdir()
        (directory / ".gitignore").write_text("*.tmp\n!keep.tmp\n")
        self.git("add", "nested/.gitignore")
        self.git("commit", "-m", "Nested policy")
        self.git("push", "origin", "main")
        (self.checkout / ".git/info/exclude").write_text("items.txt\n")
        def edit():
            self.edit()
            (directory / "drop.tmp").write_text("scratch")
            self.git("add", "-f", "nested/drop.tmp")
            (directory / "keep.tmp").write_text("deliverable")
        rejected = self.walk(edit)
        self.assertEqual(rejected["stop"], "implementation_rejected")
        self.assertIn("nested/drop.tmp", rejected["halt_detail"])
        self.assertNotIn("keep.tmp", rejected["halt_detail"])
        self.assertNotIn("items.txt", rejected["halt_detail"])
        (directory / "drop.tmp").unlink()
        self.git("add", "--all")
        with patch.dict("foreman.dry_run.DEFAULT_RISK", {name: {**dimension, "evidence": [dict(side="head", path="nested/keep.tmp", line=1)]} for name, dimension in ScriptHandler().risk.items() if name != "level"}):
            self.assertEqual(self.walk(self.no_worker)["stop"], "verified")
        self.assertNotIn("items.txt", self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines())

    def test_fact_capture_does_not_authorize_adjacent_scratch(self):
        self.scratch_policy()
        def edit():
            self.edit()
            directory = self.checkout / "evidence"
            directory.mkdir()
            (directory / "facts.toml").write_text('[fixtures."capture.txt"]\nkind="captured_run"\ntool="tool"\nversion="1"\nargv=["tool"]\n')
            (directory / "capture.txt").write_text("capture")
            (directory / "scratch.txt").write_text("scratch")
            self.git("add", "-f", "evidence/scratch.txt")
        result = self.walk(edit, fact_record="evidence/facts.toml")
        self.assertEqual(result["stop"], "implementation_rejected")
        self.assertIn("evidence/scratch.txt", result["halt_detail"])
        self.assertNotIn("evidence/capture.txt", result["halt_detail"])
        self.assertNotIn("evidence/facts.toml", result["halt_detail"])
        self.assertFalse(self.created)

    def test_ignore_policy_comes_from_merge_base_and_handles_odd_paths(self):
        self.scratch_policy()
        def edit():
            self.edit()
            (self.checkout / ".gitignore").write_text("!evidence/\n!*.log\nitems.txt\n")
            (self.checkout / "odd\nname.log").write_text("scratch")
            (self.checkout / ":!x.log").write_text("scratch")
            self.git("add", "--all")
        result = self.walk(edit)
        self.assertEqual(result["stop"], "implementation_rejected")
        self.assertIn(repr("odd\nname.log"), result["halt_detail"])
        self.assertIn(":!x.log", result["halt_detail"])
        self.assertNotIn("'items.txt'", result["halt_detail"])

    def test_fact_captures_are_exact_deliverables_under_ignored_directory(self):
        for committed in (False, True):
            for record in ("tests/fixtures/manifest.toml", "evidence/facts.toml"):
                with self.subTest(committed=committed, record=record):
                    self.reset_fixture()
                    self.scratch_policy()
                    def edit():
                        self.edit()
                        path = self.checkout / record
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('[fixtures."capture.log"]\nkind="captured_run"\ntool="tool"\nversion="1"\nargv=["tool"]\n')
                        (path.parent / "capture.log").write_text("captured output\n")
                        for name in ("src/new.py", "tests/test_new.py", "docs/new.md"):
                            file = self.checkout / name
                            file.parent.mkdir(parents=True, exist_ok=True)
                            file.write_text("deliverable\n")
                        if committed:
                            self.git("add", "-f", record, str(path.parent / "capture.log"))
                            self.worker_commit()
                    result = self.walk(edit, fact_record=record)
                    self.assertEqual(result["stop"], "verified")
                    files = self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
                    self.assertIn(record, files)
                    self.assertIn(str(Path(record).parent / "capture.log"), files)
                    self.assertEqual(result["checks"].verified_head, self.remote_head())

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
        self.assertIn("expected implementation commit", result["halt_detail"])
        self.assertFalse(self.created)

    def test_stale_candidate_after_soft_reset_cannot_publish(self):
        original = LiveWorkspace.soft_reset
        def crash(workspace, parent):
            original(workspace, parent)
            raise SystemExit("after soft reset")
        def edit():
            self.edit()
            self.worker_commit()
        with patch.object(LiveWorkspace, "soft_reset", crash), self.assertRaises(SystemExit):
            self.walk(edit)
        (self.checkout / "items.txt").write_text("stale change\n")
        result = self.walk(self.no_worker)
        self.assertEqual(result["stop"], "git_failed")
        self.assertIn("changed after reconciliation", result["halt_detail"])
        self.assertFalse(self.created)

    def test_oversized_candidate_continues_in_fresh_process_for_split_and_override(self):
        for committed in (False, True):
            for answer in ("split", "override"):
                with self.subTest(committed=committed, answer=answer):
                    self.reset_fixture()
                    def edit():
                        (self.checkout / "items.txt").write_text("one\n" * 401)
                        if committed:
                            self.worker_commit()
                    original_run = LiveWorkspace.run
                    checked = []
                    def check(workspace, command, **kwargs):
                        self.assertFalse(self.created)
                        checked.append(self.git("rev-parse", "HEAD"))
                        return original_run(workspace, command, **kwargs)
                    with patch.object(LiveWorkspace, "run", check):
                        paused = self.walk(edit)
                        self.assertEqual(paused["stop"], "needs_human")
                        candidate = paused["work"].candidate
                        self.assertEqual(candidate["disposition"], "adopted" if committed else "edits")
                        head = self.git("rev-parse", "HEAD")
                        self.assertFalse(self.created)
                        self.assertFalse(checked)
                        receiver, sender = multiprocessing.get_context("fork").Pipe(duplex=False)
                        def resume():
                            try:
                                result = self.walk(self.no_worker, answer=answer)
                                sender.send((result, checked, self.created))
                            except BaseException as exc:
                                sender.send(exc)
                        process = multiprocessing.get_context("fork").Process(target=resume)
                        process.start()
                        sender.close()
                        try:
                            self.assertTrue(receiver.poll(30), "resume process did not finish")
                            result = receiver.recv()
                            if isinstance(result, BaseException):
                                raise result
                            resumed, checked, self.created = result
                        finally:
                            receiver.close()
                            process.join(5)
                            if process.is_alive():
                                process.terminate()
                                process.join()
                        self.assertEqual(process.exitcode, 0)
                    self.assertEqual(resumed["work"].candidate, candidate)
                    if answer == "override":
                        self.assertEqual(resumed["stop"], "verified")
                        self.assertEqual(checked, self.created)
                        self.assertEqual(len(checked), 1)
                        self.assertEqual(resumed["pr_size_override"], candidate["size"])
                        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), candidate["size"]["head"])
                        self.assertEqual(resumed["path"][-5:], ["proof", "publish", "review", "merge", "done"])
                    else:
                        self.assertEqual(resumed["stop"], "pr_split_required")
                        self.assertEqual(resumed["plan"].pr_split, paused["plan"].pr_split)
                        self.assertEqual(self.git("rev-parse", "HEAD"), head)
                        self.assertFalse(checked)
                        self.assertFalse(self.created)
                        self.assertNotIn("publish", resumed["path"])
                        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")

    def test_fix_round_requires_new_size_override_before_publication(self):
        def edit(text):
            (self.checkout / "items.txt").write_text(text * 401)
        self.assertEqual(self.walk(lambda: edit("one\n"))["stop"], "needs_human")
        first = self.walk(lambda: edit("two\n"), reviews=["changes"], answer="override")
        self.assertEqual(first["stop"], "needs_human")
        self.assertEqual(len(self.created), 1)
        published = self.remote_head()
        self.assertNotEqual(first["pr_size"], first["pr_size_override"])
        self.assertEqual(first["checks"].verified_head, "")
        second = self.walk(self.no_worker, answer="override")
        self.assertEqual(second["stop"], "verified")
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.git("rev-parse", "HEAD^"), published)
        self.assertEqual(self.remote_head(), second["checks"].verified_head)
        self.assertEqual(second["path"].count("proof"), 2)
        self.assertEqual(second["path"].count("publish"), 2)

    def test_missing_proof_never_publishes(self):
        self.slice = {**EMPTY_SLICE, "screen": "docs/screens/items.md"}
        result = self.walk()
        self.assertEqual(result["stop"], "insufficient_evidence")
        self.assertFalse(self.created)
        self.assertNotIn("publish", result["path"])
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")

    def test_first_publication_uses_bound_size_without_pr_lookup(self):
        result = self.walk(self.edit)
        self.assertEqual(result["stop"], "verified")
        bound = result["work"].candidate["size"]
        self.assertEqual(bound["head"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(result["pr_size"], bound)

    def test_changed_candidate_cannot_commit_with_stale_size(self):
        with patch.object(LiveWorkspace, "commit", side_effect=SystemExit("before commit")):
            with self.assertRaises(SystemExit):
                self.walk(self.edit)
        head = self.git("rev-parse", "HEAD")
        (self.checkout / "new-deliverable.md").write_text("oversized\n" * 401)
        result = self.walk(self.no_worker)
        self.assertEqual(result["stop"], "git_failed")
        self.assertIn("size measurement", result["halt_detail"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.created)

    def test_missing_checkpointed_edits_cannot_commit(self):
        with patch.object(LiveWorkspace, "commit", side_effect=SystemExit("before commit")):
            with self.assertRaises(SystemExit):
                self.walk(self.edit)
        (self.checkout / "items.txt").unlink()
        head = self.git("rev-parse", "HEAD")
        result = self.walk(self.no_worker)
        self.assertEqual(result["stop"], "git_failed")
        self.assertIn("checkpointed implementation edits are missing", result["halt_detail"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.created)

    def test_resumed_edits_commit_must_match_measured_tree(self):
        original = LiveWorkspace.commit
        def crash(workspace, message):
            (self.checkout / "extra.md").write_text("unmeasured\n")
            original(workspace, message)
            raise SystemExit("after commit")
        with patch.object(LiveWorkspace, "commit", crash), self.assertRaises(SystemExit):
            self.walk(self.edit)
        result = self.walk(self.no_worker)
        self.assertEqual(result["work"].candidate["disposition"], "edits")
        self.assertEqual(result["stop"], "git_failed")
        self.assertIn("Commit differs from the measured implementation tree", result["halt_detail"])
        self.assertFalse(self.created)

    def test_verify_rejects_unmeasured_commit(self):
        with patch.object(LiveWorkspace, "run", side_effect=SystemExit("before verification")):
            with self.assertRaises(SystemExit):
                self.walk(self.edit)
        (self.checkout / "extra.md").write_text("unmeasured\n")
        self.worker_commit()
        head = self.git("rev-parse", "HEAD")
        result = self.walk(self.no_worker)
        self.assertEqual(result["checks"].verified_head, "")
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("implementation commit", result["halt_detail"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.published_heads, [])

    def test_recovery_with_edits_uses_reconciled_fingerprint(self):
        def edit():
            self.edit()
            self.worker_commit("Record Python title-casing tool evidence")
            self.git("switch", "-c", "other")
        rejected = self.walk(edit)
        self.assertEqual(rejected["stop"], "implementation_rejected")
        self.git("switch", self.branch)
        self.git("reset", "--soft", self.git("rev-parse", "main"))
        recovered = self.walk(self.no_worker)
        self.assertEqual(recovered["stop"], "verified")
        self.assertEqual(recovered["work"].candidate["disposition"], "edits")
        self.assertEqual(recovered["attempts"][:2], rejected["attempts"])

    def test_reconcile_git_errors_remain_git_failed(self):
        for operation in ("show", "merge-base"):
            for detail in ("timed out", "index.lock"):
                with self.subTest(operation=operation, detail=detail):
                    self.check_reconcile_git_error(detail, operation)

    def check_reconcile_git_error(self, detail, operation):
        self.reset_fixture()
        original = self.subprocess
        def fail(command, **kwargs):
            if command[0] == "git" and "--no-replace-objects" in command and operation in command:
                if detail == "timed out":
                    raise subprocess.TimeoutExpired(f"git {operation}", 10)
                return subprocess.CompletedProcess(command, 128, b"", b"index.lock exists")
            return original(command, **kwargs)
        def edit():
            self.edit()
            self.worker_commit()
        with patch("foreman.workspace.subprocess.run", side_effect=fail):
            result = self.walk(edit)
        self.assertEqual(result["stop"], "git_failed")
        self.assertIn(detail, result["halt_detail"])
        self.assertEqual(self.walk(self.no_worker)["stop"], "verified")

    def test_rerun_retries_publication_without_rerunning_the_worker(self):
        self.create_error = True
        failed = self.walk()
        self.assertEqual(failed.get("stop"), "publish_failed")
        self.assertEqual(failed.get("work", WorkState()).head_sha, self.remote_head())
        self.create_error = False
        result = self.walk(lambda: self.fail("The committed change should be reused"))
        self.assertEqual(result.get("merge", MergeState()).status, "armed")
        self.assertEqual(self.created, [failed.get("work", WorkState()).head_sha])

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

    def test_prepare_preflight_stops_before_creating_attempt(self):
        logs = []
        result = self.walk(self.no_worker, preflight="echo unavailable; exit 9", check_log=logs.append)
        self.assertEqual(result["stop"], "env_unavailable")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertFalse(result.get("work", WorkState()).attempt_head)
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage"])
        self.assertEqual(result["path"][-2:], ["prepare", "done"])
        self.assertIn("unavailable", "".join(logs))

    def test_quota_resume_guards_preserve_attempt_and_partial_tree(self):
        for change in ("unchanged", "cleared", "changed", "branch", "head", "preflight"):
            with self.subTest(change=change):
                self.reset_fixture()
                logs = []
                preflight = "echo probe; test ! -f .git/block-preflight"
                def stop():
                    (self.checkout / "items.txt").write_text("partial\n")
                    return Outcome("quota", usage=SCRIPTED_USAGE)
                paused = self.walk(stop, preflight=preflight, check_log=logs.append)
                self.assertEqual(paused["stop"], "quota_exhausted")
                work, attempts = paused["work"], paused["attempts"]
                self.assertTrue(work.partial_edits)
                self.assertEqual([a["outcome"] for a in attempts], ["ran"] + ["quota"] * 4)
                self.assertEqual("".join(logs).count("probe"), 1)
                if change == "cleared":
                    (self.checkout / "items.txt").unlink()
                elif change == "changed":
                    (self.checkout / "items.txt").write_text("foreign edit\n")
                elif change == "branch":
                    self.git("switch", "-c", "foreign")
                elif change == "head":
                    self.git("commit", "--allow-empty", "-m", "Foreign HEAD")
                elif change == "preflight":
                    (self.checkout / ".git/block-preflight").touch()
                accepted = change in {"unchanged", "cleared"}
                def finish():
                    if change == "unchanged":
                        self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")
                    self.edit()
                resumed = self.walk(finish if accepted else self.no_worker,
                                    preflight=preflight, check_log=logs.append)
                self.assertEqual("".join(logs).count("probe"), 2 if accepted or change == "preflight" else 1)
                self.assertEqual(resumed["attempts"][:len(attempts)], attempts)
                self.assertEqual(resumed["work"].attempt_head, work.attempt_head)
                if accepted:
                    self.assertEqual(resumed["stop"], "verified")
                    self.assertEqual([a["stage"] for a in resumed["attempts"][len(attempts):]],
                                     ["implement", "review"])
                else:
                    self.assertEqual(resumed["attempts"], attempts)
                    self.assertEqual(resumed["stop"], "env_unavailable" if change == "preflight" else "dirty_worktree")
                    self.assertEqual(resumed["work"], work)

    def test_dirty_initial_worktree_has_specific_detail(self):
        (self.checkout / "foreign.txt").touch()
        result = self.walk(lambda: self.fail("unowned edits must halt"), preflight="touch preflight_ran; exit 9")
        self.assertFalse((self.checkout / "preflight_ran").exists())
        self.assertEqual(result.get("stop"), "dirty_worktree")
        self.assertIn("Unowned edits", result["halt_detail"])

    def test_publication_failures(self):
        cases = (("rejected push", "rejected"), ("missing remote", "origin"), ("wrong push URL", "Transport requires"))
        for case, detail in cases:
            with self.subTest(case=case):
                self.reset_fixture()
                hook = self.remote / "hooks/pre-receive"
                hook.write_text("#!/bin/sh\necho 'synthetic rejection' >&2\nexit 1\n")
                hook.chmod(0o755)
                original = self.subprocess
                def remote(command, **kwargs):
                    if case == "missing remote" and command[:3] == ["git", "remote", "get-url"]:
                        return subprocess.CompletedProcess(command, 128, b"", b"origin is missing")
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
        from foreman.runner import RunPresentation
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
                    presentation = RunPresentation()
                    result = run_delivery(ScriptHandler(), repo=REPO, request="synthetic fixture", slice=self.slice, commands=presentation.commands)
                    output = StringIO()
                    with redirect_stdout(output):
                        _print_run(result, presentation, dry_run=True)
                    self.assertNotIn(synthetic, output.getvalue())
                    self.assertNotIn(synthetic, json.dumps(presentation.commands))
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
                self.assertEqual(result.get("work", WorkState()).base, base)
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
