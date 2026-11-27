import tests
from tests import RUN_ID

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from foreman.readiness import MergeState
from foreman.adapters import Outcome
from foreman.config import load_config
from foreman.contract import CONTRACT_PATH
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.redaction import redact
from foreman.github import CLOSE_MARKER, LiveForge
from foreman.runner import ready_resumes, issue_thread
from foreman.steps import CANCELLATION, implement
from foreman.workspace import WorkState, WorkspaceError, LiveWorkspace
from foreman.container import ContainerSpec, DockerHost, Mount, TimedOut, stream
from publication_fixture import CHECK, EMPTY_SLICE, IMAGE, PublicationFixture, REPO, TOTALS, VERIFY_SKILL_PATH, resume_id, run_subprocess
from test_delivery import BaseMergeFixture

BUILDER_SESSION = "00000000-0000-4000-8000-00000000b11d"


class PublicationTest(PublicationFixture):
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
                self.assertEqual(result["stop"], "merged")
                self.assertEqual(result["work"].attempt_head, tip)
                self.assertEqual(self.git("rev-parse", "HEAD^"), tip)
                self.assertEqual(self.git("rev-parse", before), before)
                if kind != "missing":
                    self.assertEqual(self.git("rev-parse", "main"), local)

    def test_a_new_run_never_meets_an_earlier_runs_branch(self):
        """An earlier run of the issue left its branch a commit past the pinned base, with the checkout on it or on main. The
        new run builds on a branch of its own, so prepare's leftover-branch git_failed halts are unreachable for it."""
        for checkout in ("main", "earlier branch"):
            with self.subTest(checkout=checkout):
                self.reset_fixture()
                earlier = self.git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Earlier run")
                leftover = self.branch
                self.git("branch", leftover, earlier)
                if checkout != "main":
                    self.git("switch", "-q", leftover)
                self.branch, self.ref = "8-list-items-once-ffffffff", "refs/heads/8-list-items-once-ffffffff"
                with patch("foreman.delivery.uuid4", return_value=SimpleNamespace(hex="f" * 32)):
                    result = self.walk()
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                self.assertEqual(result["work"].branch, self.branch)
                self.assertEqual(self.git("rev-parse", leftover), earlier)

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
                self.assertEqual(result["stop"], "merged")
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
        self.assertEqual(result["stop"], "merged")
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

    def test_a_file_the_worker_deletes_is_committed_and_published(self):
        (self.checkout / "obsolete.md").write_text("old\n")
        self.git("add", "obsolete.md")
        self.git("commit", "-m", "Obsolete")
        self.git("push", "origin", self.base)
        def edit():
            (self.checkout / "obsolete.md").unlink()
            self.edit()
        result = self.walk(edit)
        self.assertEqual(result["stop"], "merged")
        head = result["work"].head_sha
        self.assertEqual(self.git("ls-tree", "-r", "--name-only", head).split(), [VERIFY_SKILL_PATH, CONTRACT_PATH, "items.txt"])
        self.assertIn(head, self.git("ls-remote", "origin"))

    def test_two_review_rounds_count_whole_pr_while_origin_advances(self):
        heads = []
        def edit():
            heads.append(self.git("rev-parse", "HEAD"))
            (self.checkout / "src").mkdir(exist_ok=True)
            (self.checkout / "src/items.py").write_text("one\n" * len(heads))
            self.edit()
        original = LiveWorkspace.create_branch
        prepares = []
        def prepare(workspace, branch, base, *, pinned=""):
            if prepares:
                self.advance_origin()
            prepares.append(branch)
            return original(workspace, branch, base, pinned=pinned)
        with patch.object(LiveWorkspace, "create_branch", prepare):
            result = self.walk(edit, reviews=["changes", "approve"])
        self.assertEqual(result["stop"], "merged")
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
            workspace.reconcile(tip, self.branch, "main")
        self.assertEqual(workspace.inspect(), before)

    def test_rewritten_and_rewound_origin_keep_checkout_and_comparison_semantics(self):
        parent = self.advance_origin()
        workspace = LiveWorkspace(self.checkout)
        workspace.create_branch(self.branch, "main")
        self.edit()
        before = workspace.inspect()
        old = self.git("rev-parse", "main")
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", old)
        candidate = workspace.reconcile(parent, self.branch, "main")
        self.assertIsNotNone(candidate)
        self.assertEqual(workspace._base_merge_base("main", parent), old)
        unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "Unrelated")
        self.git("--git-dir", str(self.remote), "fetch", str(self.checkout), unrelated)
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/main", unrelated)
        with self.assertRaisesRegex(WorkspaceError, "restore related base history"):
            workspace.reconcile(parent, self.branch, "main")
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
                self.assertEqual(result.get("merge", MergeState()).status, "merged")
                self.assertEqual(self.remote_head(), result.get("work", WorkState()).head_sha)
                self.assertEqual(result["checks"].verified_head, self.remote_head())
                self.assertEqual(self.created, [self.remote_head()])
                self.assertEqual(self.git("rev-parse", f"{self.ref}^"), self.git("--git-dir", str(self.remote), "rev-parse", self.base))
                self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "one")
                message = self.git("show", "-s", "--format=%B", "HEAD")
                self.assertLessEqual(len(message.splitlines()[0]), 72)
                self.assertTrue(message.endswith(f"Co-Authored-By: {result['models_used']['implement']} <{email}>"))

    def test_a_cancel_at_the_repair_gate_closes_the_pull_request_and_keeps_its_branch(self):
        """A cancellation after publication closes the run's open PR with one cancellation comment. The close leaves the branch,
        with a person's commit on it (DQ2). A PR that is no longer open is left alone."""
        for closed in (False, True):
            with self.subTest(already_closed=closed):
                self.reset_fixture()
                questions = iter((None, "Which sort order should the repair keep?"))
                paused = self.walk(reviews=["changes"], ask=lambda: next(questions))
                self.assertEqual(paused["stop"], "needs_human")
                parent = self.remote_head()
                person = self.git("commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "A person's fix")
                self.git("push", "origin", f"{person}:{self.ref}")
                self.closed = closed
                marker = re.search(r"foreman answer [0-9a-f-]{71}", paused["halt_detail"])
                assert marker is not None
                self.comments.append(f"{marker[0]}\ncancel")
                result = self.walk(answer="cancel")
                self.assertEqual((result["stop"], result.get("halt")), ("cancelled", None))
                self.assertEqual((self.closed, self.remote_head()), (True, person))
                # The PR keeps Verify's evidence comment and gains only the cancellation.
                self.assertEqual([comment for comment in self.pr_comments if not comment.startswith("Verify evidence.")],
                                 [] if closed else [f"{CANCELLATION}\n\n{CLOSE_MARKER}"])

    def test_second_attempt_adds_a_commit_to_the_same_pull_request(self):
        edits = iter(("one\n", "two\n"))
        result = self.walk(lambda: (self.checkout / "items.txt").write_text(next(edits)), ["changes", "approve"])
        self.assertEqual(result.get("merge", MergeState()).status, "merged")
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
        self.assertEqual(result["stop"], "merged")
        self.assertEqual([a["outcome"] for a in result["attempts"] if a["stage"] == "implement"], ["quota", "ran"])

    def test_no_change_with_evidence_on_a_first_attempt_stops_without_pushing(self):
        evidence = "items.txt already lists each item once on the pinned base."
        result = self.walk(lambda: None, no_change=lambda: evidence)
        self.assertEqual(result.get("stop"), "no_change")
        self.assertIsNone(result.get("halt"))
        self.assertEqual(result.get("halt_detail"), evidence)
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "spec_check", "implement"])
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
        self.assertEqual(self.created, [])

    def answer(self, edit, no_change):
        """A builder answer in its own session, so dispatch can repair an invalid one there."""
        edit()
        return Outcome("ran", {"implementation": "diff", "no_change": no_change, "human_question": None},
                       usage=SCRIPTED_USAGE, session_id=BUILDER_SESSION, totals=TOTALS)

    def test_an_invalid_no_change_answer_gets_one_repair_turn_and_no_gate(self):
        # IM8: edit's own check rejects the answer, so the builder repairs it once in its session.
        def commit_edit():
            self.edit()
            self.worker_commit()
        for name, edit, claim, error in (
                ("clean tree without evidence", lambda: None, None, "no_change: the tree has no edits."),
                ("committed edits with evidence", commit_edit, "The acceptance already holds.", "no_change: the tree has edits")):
            with self.subTest(name):
                self.reset_fixture()
                edits, claims, repairs = iter((edit, self.edit)), iter((claim, None)), []
                def build(model, state, instruction):
                    plan = state["dispatch_plan"]
                    repairs.append((plan.get("repair"), resume_id(plan)))
                    return self.answer(next(edits), next(claims))
                result = self.walk(model_edit=build)
                self.assertEqual(result["stop"], "merged")
                (first, opened), (repair, resumed) = repairs
                self.assertEqual((first, opened), (None, None))
                self.assertIn(error, repair)
                self.assertEqual(resumed, BUILDER_SESSION)
                self.assertEqual([a["stage"] for a in result["attempts"]].count("implement"), 2)
                self.assertEqual(result["plan"].human_qa, [])

    def test_a_second_invalid_no_change_answer_stops_with_invalid_output(self):
        def draft():
            (self.checkout / "draft.txt").write_text("draft\n")
        def revert():
            (self.checkout / "draft.txt").unlink()
        nothing = lambda: None
        repaired = "no_change: a repair cannot report no_change."
        for name, edits, claims, reviews, error, published in (
                ("clean tree without evidence", (nothing, nothing), (None, None), None, "no_change: the tree has no edits.", 0),
                ("no_change on a review repair", (self.edit, nothing, nothing), (None, "The finding is already fixed.", "The finding is already fixed."),
                 ["changes"], repaired, 1),
                ("no_change after a failed check", (draft, revert, nothing), (None, "The check already passes.", "The check already passes."),
                 None, repaired, 0)):
            with self.subTest(name):
                self.reset_fixture()
                steps, answers = iter(edits), iter(claims)
                result = self.walk(reviews=reviews, model_edit=lambda model, state, instruction: self.answer(next(steps), next(answers)))
                self.assertEqual((result["stop"], result.get("halt")), ("invalid_output", "invalid_output"))
                self.assertIn(error, result["halt_detail"])
                self.assertEqual([a["stage"] for a in result["attempts"]].count("implement"), len(edits))
                self.assertEqual(result["plan"].human_qa, [])
                self.assertEqual(len(self.created), published)
                if not published:
                    self.assertEqual(self.git("ls-remote", "origin", self.ref), "")

    def test_a_checkout_curated_to_the_attempt_parent_asks_the_builder_again(self):
        def edit():
            self.edit()
            self.worker_commit()
            (self.checkout / "extra.txt").write_text("uncommitted\n")
        rejected = self.walk(edit)
        self.assertEqual(rejected["stop"], "implementation_rejected")
        (self.checkout / "extra.txt").unlink()
        self.git("reset", "--keep", "main")
        emptied = self.walk(self.no_worker)
        self.assertEqual(emptied["stop"], "implementation_rejected")
        self.assertIn("none of the edits the builder returned", emptied["halt_detail"])
        result = self.walk(self.edit)
        self.assertEqual(result["stop"], "merged")
        self.assertEqual([a["stage"] for a in result["attempts"]].count("implement"), 2)

    def test_worker_commit_is_detected_using_the_actual_head(self):
        def commit():
            self.git("commit", "--allow-empty", "-m", "Out-of-band commit")

        result = self.walk(commit)
        self.assertEqual(result.get("stop"), "implementation_rejected")
        self.assertIn("nonempty", result.get("halt_detail", ""))
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "spec_check", "implement"])
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
                self.assertEqual(result["stop"], "merged")
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
                    self.assertEqual(recovered["stop"], "merged")
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
                self.assertEqual(resumed["stop"], "merged")
                self.assertEqual(resumed["attempts"][:len(result["attempts"])], result["attempts"])

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
                self.assertEqual(result["stop"], "merged")
                files = self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines()
                for name in paths:
                    self.assertNotIn(name, files)
                    self.assertTrue((self.checkout / name).is_file())

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
                self.assertEqual(self.walk(edit)["stop"], "merged")
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
        self.assertEqual(self.walk(self.no_worker)["stop"], "merged")
        self.assertNotIn("items.txt", self.git("ls-tree", "-r", "--name-only", "HEAD").splitlines())

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

    def test_reconciled_candidate_mutation_is_rejected_before_publication(self):
        original = LiveWorkspace.reconcile
        def mutate(workspace, *args, **kwargs):
            candidate = original(workspace, *args, **kwargs)
            (self.checkout / "extra.txt").write_text("later")
            self.worker_commit()
            return candidate
        def edit():
            self.edit()
            self.worker_commit()
        with patch.object(LiveWorkspace, "reconcile", mutate):
            result = self.walk(edit)
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("edits changed after reconciliation", result["halt_detail"])
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
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("edits differ from the checked tree", result["halt_detail"])
        self.assertEqual((self.checkout / "items.txt").read_text(), "stale change\n")
        self.assertFalse(self.created)

    def test_oversized_candidate_is_reported_and_published(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                self.reset_fixture()
                def edit():
                    (self.checkout / "items.txt").write_text("one\n" * 401)
                    if committed:
                        self.worker_commit()
                result = self.walk(edit)
                self.assertEqual(result["stop"], "merged")
                candidate = result["work"].candidate
                self.assertEqual(candidate["disposition"], "adopted" if committed else "edits")
                self.assertEqual(result["pr_size"], candidate["size"])
                self.assertEqual(result["pr_size"]["lines"], 401)
                self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), result["checks"].checked_tree)
                self.assertEqual(self.created, [result["work"].head_sha])

    def test_first_publication_uses_bound_size_without_pr_lookup(self):
        result = self.walk(self.edit)
        self.assertEqual(result["stop"], "merged")
        bound = result["work"].candidate["size"]
        self.assertEqual(bound["head"], self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(result["pr_size"], bound)

    def test_changed_candidate_cannot_commit_after_its_check(self):
        with patch.object(LiveWorkspace, "commit", side_effect=SystemExit("before commit")):
            with self.assertRaises(SystemExit):
                self.walk(self.edit)
        head = self.git("rev-parse", "HEAD")
        (self.checkout / "new-deliverable.md").write_text("oversized\n" * 401)
        result = self.walk(self.no_worker)
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("edits differ from the checked tree", result["halt_detail"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.created)

    def test_missing_checkpointed_edits_cannot_commit(self):
        with patch.object(LiveWorkspace, "commit", side_effect=SystemExit("before commit")):
            with self.assertRaises(SystemExit):
                self.walk(self.edit)
        (self.checkout / "items.txt").unlink()
        head = self.git("rev-parse", "HEAD")
        result = self.walk(self.no_worker)
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("edits differ from the checked tree", result["halt_detail"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.created)

    def test_resumed_edits_commit_must_match_checked_tree(self):
        original = LiveWorkspace.commit
        def crash(workspace, message):
            (self.checkout / "extra.md").write_text("unmeasured\n")
            original(workspace, message)
            raise SystemExit("after commit")
        with patch.object(LiveWorkspace, "commit", crash), self.assertRaises(SystemExit):
            self.walk(self.edit)
        result = self.walk(self.no_worker)
        self.assertEqual(result["work"].candidate["disposition"], "edits")
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("commit differs from the checked tree", result["halt_detail"])
        self.assertFalse(self.created)

    def test_check_rejects_commit_after_the_builder_returned(self):
        real, runs = LiveWorkspace.run, []

        def crash(workspace, command, **kwargs):
            # Prepare's check at the base runs first, so the process ends at the candidate's check.
            runs.append(command)
            if len(runs) == 2:
                raise SystemExit("before verification")
            return real(workspace, command, **kwargs)
        with patch.object(LiveWorkspace, "run", crash):
            with self.assertRaises(SystemExit):
                self.walk(self.edit)
        (self.checkout / "extra.md").write_text("unmeasured\n")
        self.worker_commit()
        head = self.git("rev-parse", "HEAD")
        result = self.walk(self.no_worker)
        self.assertEqual(result["checks"].verified_head, "")
        self.assertEqual(result["stop"], "dirty_worktree")
        self.assertIn("changed after the builder returned", result["halt_detail"])
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
        self.assertEqual(recovered["stop"], "merged")
        self.assertEqual(recovered["work"].candidate["disposition"], "edits")
        self.assertEqual(recovered["attempts"][:len(rejected["attempts"])], rejected["attempts"])

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
        self.assertEqual(self.walk(self.no_worker)["stop"], "merged")

    def test_rerun_retries_publication_without_rerunning_the_worker(self):
        self.create_error = True
        failed = self.walk()
        self.assertEqual(failed.get("stop"), "publish_failed")
        self.assertEqual(failed.get("work", WorkState()).head_sha, self.remote_head())
        self.create_error = False
        result = self.walk(lambda: self.fail("The committed change should be reused"))
        self.assertEqual(result.get("merge", MergeState()).status, "merged")
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
                self.assertEqual(result.get("stop"), "merged")
                self.assertEqual(len(self.created), 1)
                if completed and worker_done:
                    self.assertEqual(self.remote_head(), head)

    def test_failed_repository_check_is_repaired_and_its_receipt_tree_is_published(self):
        edits = iter((lambda: (self.checkout / "draft.txt").write_text("draft\n"), self.edit))
        logs = []
        result = self.walk(lambda: next(edits)(), check_log=logs.append)
        self.assertEqual(result["stop"], "merged")
        self.assertEqual(result["checks"].verify_failures, 1)
        self.assertEqual([a["stage"] for a in result["attempts"]].count("implement"), 2)
        self.assertEqual(result["path"].count("check"), 2)
        head = result["work"].head_sha
        self.assertEqual((self.remote_head(), self.created), (head, [head]))
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", f"{head}^{{tree}}"), result["checks"].checked_tree)
        self.assertEqual(result["checks"].verified_head, head)

    def test_check_cap_and_tree_changing_checks_never_publish(self):
        cases = (("failing check", CHECK, None),
                 # It leaves the clean base unchanged, so prepare's check at the base passes.
                 ("tree-changing check", 'test -z "$(git status --porcelain)" || echo run >> check.log', "changed the candidate tree"))
        for case, check, failure in cases:
            with self.subTest(case=case):
                self.reset_fixture(check=check)
                rounds = []
                def edit():
                    rounds.append(1)
                    (self.checkout / "draft.txt").write_text(f"draft {len(rounds)}\n")
                result = self.walk(edit)
                cap = load_config().max_review_rounds
                self.assertEqual(result["stop"], "failed_check_cap")
                self.assertEqual((len(rounds), result["checks"].verify_failures), (cap, cap))
                self.assertEqual(result["checks"].verify, "fail")
                self.assertEqual(result["checks"].checked_tree, "")
                if failure:
                    self.assertIn(failure, result["checks"].failure_output)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "main"))
                self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
                self.assertEqual(self.created, [])
                self.assertEqual((self.checkout / "draft.txt").read_text(), f"draft {cap}\n")

    def test_implement_question_waits_for_a_posted_answer_or_cancel(self):
        for reply in ("Active items only.", "cancel"):
            with self.subTest(reply=reply):
                self.reset_fixture()
                questions = iter(("Should archived items be listed?", None))
                paused = self.walk(ask=lambda: next(questions))
                self.assertEqual(paused["stop"], "needs_human")
                self.assertIn("Should archived items be listed?", paused["halt_detail"])
                self.assertEqual((self.checkout / "items.txt").read_text(), "one\n")
                self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
                marker = re.search(r"foreman answer [0-9a-f-]{71}", paused["halt_detail"])
                assert marker is not None
                self.comments.append(f"{marker[0]}\n{reply}")
                result = self.walk(self.edit, answer=reply, ask=lambda: next(questions))
                implements = [a for a in result["attempts"] if a["stage"] == "implement"]
                if reply == "cancel":
                    self.assertEqual(result["stop"], "cancelled")
                    self.assertEqual(len(implements), 1)
                    self.assertEqual((self.checkout / "items.txt").read_text(), "one\n")
                    self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
                    self.assertEqual(self.created, [])
                else:
                    self.assertEqual(result["stop"], "merged")
                    self.assertEqual(len(implements), 2)
                    self.assertEqual([(qa["stage"], qa["answer"]) for qa in result["plan"].human_qa], [("implement", reply)])
                    self.assertEqual(self.created, [result["work"].head_sha])

    def test_publication_crashes_replay_twice_against_each_observation(self):
        from foreman.workspace import WorktreeInspectionFailed
        windows = (("commit", LiveWorkspace, "commit"), ("push", LiveWorkspace, "push"), ("create", LiveForge, "open_pull_request"))
        effects_by_window = {"commit": ["commit"], "push": ["commit", "push"], "create": ["commit", "push", "create"]}
        for window, owner, operation in windows:
            for observation in ("matching", "mismatched", "unreadable"):
                with self.subTest(window=window, observation=observation):
                    self.reset_fixture()
                    effects, crash = [], [window]
                    for name, cls, method in windows:
                        def record(*args, _name=name, _original=getattr(cls, method), **kwargs):
                            result = _original(*args, **kwargs)
                            effects.append(_name)
                            if _name in crash:
                                crash.clear()
                                raise SystemExit(f"after {_name}")
                            return result
                        self.fixture.enter_context(patch.object(cls, method, record))
                    with self.assertRaises(SystemExit):
                        self.walk(self.edit)
                    self.assertEqual(effects, effects_by_window[window])
                    head = self.git("rev-parse", "HEAD")
                    foreign = ""
                    if observation == "mismatched":
                        if window == "commit":
                            self.git("commit", "--amend", "-m", "Foreign message")
                            foreign = self.git("rev-parse", "HEAD")
                        elif window == "push":
                            foreign = self.git("commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "Foreign push")
                            self.git("push", "origin", f"{foreign}:{self.ref}")
                        else:
                            self.pr_body = "Closes #9"
                    unreadable = {"commit": patch.object(LiveWorkspace, "inspect", side_effect=WorktreeInspectionFailed("status unreadable")),
                                  "push": patch.object(LiveWorkspace, "remote_head", side_effect=WorkspaceError("ls-remote unreadable"))}
                    for replay in (1, 2):
                        with ExitStack() as stack:
                            if observation == "unreadable" and replay == 1:
                                if window == "create":
                                    self.pr_view_error = "HTTP 502"
                                else:
                                    stack.enter_context(unreadable[window])
                            result = self.walk(self.no_worker)
                            self.pr_view_error = ""
                        if observation == "unreadable" and replay == 1:
                            self.assertEqual(result["stop"], "git_failed" if window == "commit" else "publication_unreconciled")
                            self.assertEqual(effects, effects_by_window[window])
                        elif observation == "mismatched":
                            stop = "dirty_worktree" if window == "commit" else "publish_failed"
                            self.assertEqual(result["stop"], stop)
                            self.assertEqual(effects, effects_by_window[window])
                            if window == "commit":
                                self.assertEqual(self.git("rev-parse", "HEAD"), foreign)
                            if window == "push":
                                self.assertEqual(self.remote_head(), foreign)
                        else:
                            self.assertEqual(result["stop"], "merged")
                            self.assertEqual(effects, ["commit", "push", "create"])
                            self.assertEqual(self.created, [head])
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
                self.assertEqual(self.walk(self.edit if before_worker else self.no_worker).get("stop"), "merged")

    def test_existing_branch_at_expected_base_is_reused(self):
        self.git("branch", self.branch, "main")
        self.assertEqual(self.walk().get("stop"), "merged")

    def test_prepare_setup_failure_stops_before_creating_attempt(self):
        logs = []
        self.set_contract(setup=["echo unavailable; exit 9"])
        result = self.walk(self.no_worker, check_log=logs.append)
        self.assertEqual(result["stop"], "env_unavailable")
        self.assertIn("Setup command 'echo unavailable; exit 9' exited with 9.", result["halt_detail"])
        # Setup ran on the owned branch at the attempt parent, and the attempt was never saved.
        self.assertEqual(self.git("branch", "--show-current"), self.branch)
        self.assertFalse(result.get("work", WorkState()).attempt_head)
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "spec_check"])
        self.assertEqual(result["path"][-2:], ["spec_check", "prepare"])
        self.assertIn("unavailable", "".join(logs))

    def test_quota_resume_guards_preserve_attempt_and_partial_tree(self):
        for change in ("unchanged", "cleared", "changed", "branch", "head", "setup"):
            with self.subTest(change=change):
                self.reset_fixture()
                logs = []
                self.set_contract(setup=["echo probe; test ! -f .git/block-setup"])
                def stop():
                    (self.checkout / "items.txt").write_text("partial\n")
                    return Outcome("quota", usage=SCRIPTED_USAGE)
                paused = self.walk(stop, check_log=logs.append)
                self.assertEqual(paused["stop"], "worker_unavailable")
                work, attempts = paused["work"], paused["attempts"]
                self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")
                self.assertEqual([a["outcome"] for a in attempts], ["ran", "ran"] + ["quota"] * 4)
                self.assertEqual("".join(logs).count("probe"), 1)
                if change == "cleared":
                    (self.checkout / "items.txt").unlink()
                elif change == "changed":
                    (self.checkout / "items.txt").write_text("foreign edit\n")
                elif change == "branch":
                    self.git("switch", "-c", "foreign")
                elif change == "head":
                    self.git("commit", "--allow-empty", "-m", "Foreign HEAD")
                elif change == "setup":
                    (self.checkout / ".git/block-setup").touch()
                # Ready after worker_unavailable resumes implement at prepare, which keeps the attempt's owned edits and
                # rechecks branch and parent. The resumed process builds a container again, and edit builds it before
                # the builder's call, so a setup that now fails stops env_unavailable there.
                accepted = change in {"unchanged", "cleared"}
                def finish():
                    if change == "unchanged":
                        self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")
                    self.edit()
                resumed = self.walk(finish if accepted else self.no_worker, check_log=logs.append)
                self.assertEqual("".join(logs).count("probe"), 1 if change in {"changed", "branch", "head"} else 2)
                self.assertEqual(resumed["attempts"][:len(attempts)], attempts)
                self.assertEqual(resumed["work"].attempt_head, work.attempt_head)
                if accepted:
                    self.assertEqual(resumed["stop"], "merged")
                    self.assertEqual([a["stage"] for a in resumed["attempts"][len(attempts):]],
                                     ["implement", "verify", "review"])
                elif change == "setup":
                    self.assertEqual((resumed["attempts"], resumed["stop"]), (attempts, "env_unavailable"))
                else:
                    self.assertEqual(resumed["attempts"], attempts)
                    self.assertEqual(resumed["stop"], "dirty_worktree")
                    # The halt keeps the partial edits the quota call returned owned, so the foreign change stays dirty.
                    self.assertEqual(resumed["work"], work.model_copy(update={"owned": resumed["work"].owned}))
                    self.assertNotEqual(resumed["work"].owned, "")

    def test_dirty_initial_worktree_has_specific_detail(self):
        (self.checkout / "foreign.txt").touch()
        self.set_contract(setup=["touch setup_ran; exit 9"])
        result = self.walk(lambda: self.fail("unowned edits must halt"))
        self.assertFalse((self.checkout / "setup_ran").exists())
        self.assertEqual(result.get("stop"), "dirty_worktree")
        self.assertIn("Unowned edits", result["halt_detail"])

    def test_a_dirty_worktree_halt_resumes_on_ready_once_the_tree_is_restored(self):
        """Ready added again re-enters the halted step, which inspects the tree again (intake, glossary Halt)."""
        for restored in (True, False):
            with self.subTest(restored=restored):
                self.reset_fixture()
                (self.checkout / "foreign.txt").touch()
                halted = self.walk(self.no_worker)
                self.assertEqual((halted["stop"], halted["path"][-1:]), ("dirty_worktree", ["prepare"]))
                self.assertTrue(ready_resumes(self.checkpoint, issue_thread(REPO, 8)))
                if restored:
                    (self.checkout / "foreign.txt").unlink()
                resumed = self.walk(self.edit if restored else self.no_worker)
                self.assertEqual(resumed["attempts"][:len(halted["attempts"])], halted["attempts"])
                later = [a["stage"] for a in resumed["attempts"][len(halted["attempts"]):]]
                if restored:
                    self.assertEqual(resumed["stop"], "merged")
                    self.assertEqual(later, ["implement", "verify", "review"])
                else:
                    self.assertEqual((resumed["stop"], resumed["halt_detail"]), ("dirty_worktree", halted["halt_detail"]))
                    self.assertEqual(later, [])
                    self.assertTrue((self.checkout / "foreign.txt").exists())

    def test_IM3_a_branch_move_found_at_commit_halts_dirty_worktree_and_resumes_once_restored(self):
        """Implement preserves the tree on a branch mismatch (target.yaml implement invariants), and Ready resumes it."""
        original = implement.commit

        def move_then_commit(state, run):
            self.git("switch", "-q", "-c", "moved")
            return original(state, run)
        with patch.object(implement, "commit", move_then_commit):
            halted = self.walk(self.edit)
        self.assertEqual((halted["stop"], halted["path"][-1]), ("dirty_worktree", "commit"), halted.get("halt_detail"))
        self.assertEqual(self.git("branch", "--show-current"), "moved")
        self.assertEqual(self.git("rev-parse", "HEAD"), halted["work"].attempt_head)
        self.assertEqual((self.checkout / "items.txt").read_text(), "one\n")
        self.assertEqual((self.published_heads, self.created), ([], []))
        self.assertTrue(ready_resumes(self.checkpoint, issue_thread(REPO, 8)))
        self.git("switch", "-q", self.branch)
        resumed = self.walk(self.no_worker)
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.created, [self.remote_head()])

    def moving_check(self, move, exit_code=None):
        """The check command moves the checkout the first time it runs on the candidate, and may fail. A "switch" puts it on
        another branch; a "commit" commits the candidate on the delivery branch."""
        real, ran, seen = LiveWorkspace.run, [], []

        def run(workspace, command, **kwargs):
            code = real(workspace, command, **kwargs)
            seen.extend([command] if command == CHECK else [])
            # Prepare's check at the base runs first, so the candidate's first check is the second.
            if len(seen) == 2 and command == CHECK and not ran:
                ran.append(command)
                if move == "switch":
                    self.git("switch", "-q", "-c", "moved")
                else:
                    self.git("add", "--all")
                    self.git("commit", "-q", "--allow-empty", "-m", "Check commit")
                return code if exit_code is None else exit_code
            return code
        return patch.object(LiveWorkspace, "run", run)

    def restore(self, move, attempt_head):
        """Undo the check's move: back to the delivery branch, or the committed candidate back to edits on the attempt parent."""
        if move == "switch":
            self.git("switch", "-q", self.branch)
        else:
            self.git("reset", "-q", attempt_head)

    def test_IM3_a_check_that_moves_the_checkout_and_passes_halts_commit_and_resumes_once_restored(self):
        """A check command owns nothing in the checkout, so commit refuses its move and Ready resumes once it is undone."""
        for move in ("switch", "commit"):
            with self.subTest(move=move):
                self.reset_fixture()
                with self.moving_check(move):
                    halted = self.walk(self.edit)
                self.assertEqual((halted["stop"], halted["path"][-1]), ("dirty_worktree", "commit"), halted.get("halt_detail"))
                attempt_head = halted["work"].attempt_head
                checkout = LiveWorkspace(self.checkout).inspect()
                if move == "switch":
                    self.assertEqual((checkout.branch, checkout.head, checkout.clean), ("moved", attempt_head, False))
                else:
                    self.assertEqual((checkout.branch, checkout.clean), (self.branch, True))
                    self.assertEqual(self.git("rev-parse", "HEAD^"), attempt_head)
                self.assertEqual((self.checkout / "items.txt").read_text(), "one\n")
                self.assertEqual((self.published_heads, self.created), ([], []))
                self.restore(move, attempt_head)
                resumed = self.walk(self.no_worker)
                self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
                self.assertEqual(self.created, [self.remote_head()])

    def test_IM3_a_failing_check_that_moves_the_checkout_halts_before_the_next_builder_call(self):
        """Dispatch refuses the check's move before the builder edits again, and Ready resumes once it is undone.
        A candidate of worker commits is moved back to the attempt parent, so restoring it means that parent too."""
        for move, committed in (("switch", False), ("switch", True), ("commit", False)):
            with self.subTest(move=move, committed=committed):
                self.reset_fixture()
                calls = []

                def edit():
                    calls.append(LiveWorkspace(self.checkout).inspect().branch)
                    self.edit()
                    if committed:
                        self.worker_commit()
                with self.moving_check(move, exit_code=1):
                    halted = self.walk(edit)
                self.assertEqual((halted["stop"], halted["path"][-1]), ("dirty_worktree", "implement"), halted.get("halt_detail"))
                self.assertEqual(calls, [self.branch])
                attempt_head = halted["work"].attempt_head
                checkout = LiveWorkspace(self.checkout).inspect()
                if move == "switch":
                    self.assertEqual((checkout.branch, checkout.head, checkout.clean), ("moved", attempt_head, False))
                else:
                    self.assertEqual(checkout.branch, self.branch)
                    self.assertNotEqual(checkout.head, attempt_head)
                self.assertEqual((self.published_heads, self.created), ([], []))
                self.restore(move, attempt_head)
                if move == "switch":
                    self.git("reset", "-q", "--soft", attempt_head)
                resumed = self.walk(edit)
                self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
                self.assertEqual(calls, [self.branch, self.branch])
                self.assertEqual(self.created, [self.remote_head()])

    def test_IM3_a_branch_move_between_commit_and_publish_halts_dirty_worktree(self):
        """Publish names a checkout off its branch dirty_worktree, not a write GitHub rejected, and Ready resumes it."""
        original = implement.publish

        def move_then_publish(state, run):
            self.git("switch", "-q", "-c", "moved")
            return original(state, run)
        with patch.object(implement, "publish", move_then_publish):
            halted = self.walk(self.edit)
        self.assertEqual((halted["stop"], halted["path"][-1]), ("dirty_worktree", "publish"), halted.get("halt_detail"))
        checkout = LiveWorkspace(self.checkout).inspect()
        self.assertEqual((checkout.branch, checkout.head, checkout.clean), ("moved", halted["work"].head_sha, True))
        self.assertEqual(self.git("ls-remote", str(self.remote), self.ref), "")
        self.assertEqual((self.published_heads, self.created), ([], []))
        self.git("switch", "-q", self.branch)
        resumed = self.walk(self.no_worker)
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.created, [self.remote_head()])

    def test_a_checkout_publish_cannot_inspect_halts_git_failed_and_resumes_once_repaired(self):
        """Foreman's own read of the checkout failed and nothing was written, so publish names it git_failed and Ready resumes it."""
        original = implement.publish

        def break_head_then_publish(state, run):
            self.git("symbolic-ref", "HEAD", "refs/heads/missing")
            return original(state, run)
        with patch.object(implement, "publish", break_head_then_publish):
            halted = self.walk(self.edit)
        self.assertEqual((halted["stop"], halted["path"][-1]), ("git_failed", "publish"), halted.get("halt_detail"))
        self.assertEqual(self.git("ls-remote", str(self.remote), self.ref), "")
        self.assertEqual((self.published_heads, self.created), ([], []))
        self.git("symbolic-ref", "HEAD", self.ref)
        resumed = self.walk(self.no_worker)
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.created, [self.remote_head()])

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
                self.assertEqual(result.get("stop"), "publish_failed" if case == "rejected push" else "git_failed")
                self.assertIn(detail, result["halt_detail"])
                self.assertEqual(self.created, [])
                hook.unlink()
                if case == "rejected push":
                    self.assertEqual(self.walk(self.no_worker).get("stop"), "merged")

    def test_wrong_base_open_pr_stops_without_creation(self):
        from foreman.github import PublishedPullRequest
        wrong = PublishedPullRequest(number=12, state="OPEN", headRefOid="a" * 40, headRefName=self.branch,
                                     baseRefName="release", baseRefOid="b" * 40, autoMergeRequest=None, body="Closes #8")
        with patch.object(LiveForge, "pull_request_for", return_value=wrong):
            result = self.walk()
        self.assertEqual(result["stop"], "publish_failed")
        self.assertIn("does not match this run's repository, issue, branch and base", result["halt_detail"])
        self.assertEqual(self.created, [])

    def test_git_calls_disable_prompts_and_bound_network_and_commit(self):
        from foreman.workspace import WorkspaceError
        workspace = LiveWorkspace(self.checkout)
        for operation in ("fetch", "push", "ls-remote", "commit"):
            with self.subTest(operation=operation), patch("foreman.workspace.subprocess.run", side_effect=subprocess.TimeoutExpired(operation, 30)) as call:
                with self.assertRaisesRegex(WorkspaceError, f"git {operation} timed out after 30 seconds"):
                    workspace._git(operation)
                # The first call is the command. Only commit, which writes the index, has a second: the lookup of its lock.
                command = call.call_args_list[0]
                self.assertEqual(command.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(command.kwargs["timeout"], 30)

    def test_a_timed_out_read_leaves_the_index_lock_another_git_made(self):
        """Foreman removes an index lock only after a command that writes the index. A lock made while a read ran is another
        Git's, which may still be alive, so it stays."""
        from foreman.workspace import WorkspaceError
        lock = self.checkout / ".git/index.lock"
        reads = ("ls-files", "diff", "status")
        real = subprocess.run

        def another_git_locks(command, **kwargs):
            if command[1] in reads:
                lock.touch()
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return real(command, **kwargs)
        for read in reads:
            with self.subTest(read=read), patch("foreman.workspace.subprocess.run", another_git_locks):
                with self.assertRaisesRegex(WorkspaceError, f"git {read} timed out"):
                    LiveWorkspace(self.checkout)._git(read, environment={"GIT_OPTIONAL_LOCKS": "0"})
                self.assertTrue(lock.exists())
                lock.unlink()

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
                self.branch = f"8-{slug}-{RUN_ID[:8]}"
                self.ref = f"refs/heads/{self.branch}"
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
                    self.assertEqual(self.walk().get("stop"), "merged")
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
                self.assertEqual(result.get("stop"), "merged")
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
        # D10: unknown usage is charged at the remaining allowance, which leaves none for another worker.
        recovered = self.walk(unexpected)
        self.assertEqual(recovered.get("stop"), "budget_exceeded")
        self.assertIn("usage_unknown", recovered["halt_detail"])
        self.assertEqual((self.checkout / "items.txt").read_text(), "partial\n")


class BaseMergeRecoveryTest(BaseMergeFixture):
    """Merge B across interrupts and the stops prepare and commit make (implement invariants)."""

    def merging(self):
        return run_subprocess(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=self.checkout, capture_output=True).returncode == 0

    def assert_preserved(self, published):
        """The checkout is clean at the published head, with no merge in progress."""
        self.assertEqual((self.git("status", "--porcelain"), self.git("rev-parse", "HEAD"), self.merging()), ("", published, False))

    def test_a_base_merge_is_owned_by_the_attempt_when_ready_resumes_prepare_after_the_builder_halts(self):
        """Build note (#1296): the merge starts inside prepare before the attempt's fingerprint is saved. When the builder's call
        halts before it answers, Ready re-enters prepare, which owns the merge in progress instead of reading it as someone
        else's edits."""
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
        exhausted = True

        def halting(model, state, instruction):
            if exhausted and state["work"].base_merge is not None:
                return Outcome("budget_exhausted", usage=SCRIPTED_USAGE)
            return edit(model, state, instruction)
        halted = self.walk(model_edit=halting, reviews=["approve", "approve"])
        self.assertEqual((halted["stop"], halted["path"][-1]), ("budget_exceeded", "implement"), halted.get("halt_detail"))
        # The merge is in progress in the checkout, with nothing committed.
        self.assertEqual((self.git("rev-parse", "MERGE_HEAD"), self.git("rev-parse", "HEAD")), (seen["base"], self.published_heads[0]))
        exhausted = False
        resumed = self.walk(model_edit=halting, reviews=["approve"])
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        # Ready re-entered prepare, which accepted the merge in progress as the attempt's, and the builder resolved it.
        self.assertEqual((seen["merge"].unmerged, seen["merging"]), (["items.txt", "notes.txt"], seen["base"]))
        self.assertEqual(self.merge_parents(), [self.published_heads[0], seen["base"]])

    def test_a_base_whose_check_fails_merges_nothing_and_ready_halts_again(self):
        self.reset_fixture("git rev-parse HEAD; ! grep -qs '^<<<<<<<' items.txt notes.txt")
        logs = []
        edit, seen = self.builder({"items.txt": "<<<<<<< main\n"}, lambda: self.fail("No worker runs when the base's check fails"))
        halted = self.walk(model_edit=edit, reviews=["approve", "approve"], check_log=logs.append)
        self.assertEqual((halted["stop"], halted["path"][-1]), ("base_check_failed", "prepare"), halted.get("halt_detail"))
        self.assertIn("Nothing was merged", halted["halt_detail"])
        # The check ran at the base head: its first line is the commit it ran at.
        self.assertEqual(logs[-1].strip(), seen["base"])
        self.assert_preserved(self.published_heads[0])
        self.assertEqual([attempt["stage"] for attempt in halted["attempts"]].count("implement"), 1)
        # Ready checks the base again and halts the same way with no paid call.
        again = self.walk(self.no_worker, check_log=logs.append)
        self.assertEqual((again["stop"], again["attempts"]), ("base_check_failed", halted["attempts"]))
        self.assertEqual(logs[-1].strip(), seen["base"])
        self.assert_preserved(self.published_heads[0])

    def test_a_base_merge_that_cannot_start_stops_git_failed_with_the_tree_preserved(self):
        """GitHub confirmed the conflict, then the base was rewound into the PR's history, so Git has nothing to merge."""
        pinned = self.git("--git-dir", str(self.remote), "rev-parse", self.base)
        edit, _ = self.builder({"items.txt": "main\n"}, lambda: self.fail("No worker runs when the merge cannot start"))
        original = LiveWorkspace.base_head

        def rewound(workspace, base):
            if self.published_heads:
                self.git("--git-dir", str(self.remote), "update-ref", f"refs/heads/{self.base}", pinned)
            return original(workspace, base)
        with patch.object(LiveWorkspace, "base_head", rewound):
            result = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual((result["stop"], result["path"][-1]), ("git_failed", "prepare"), result.get("halt_detail"))
        self.assertIn(f"Git could not start merging the base head {pinned}", result["halt_detail"])
        self.assert_preserved(self.published_heads[0])

    def test_IM4_a_merge_commit_that_landed_before_its_checkpoint_is_adopted_once(self):
        """The merge leg of IM4: a resume adopts the clean commit whose parents are the attempt head and the base head. A commit
        of the same tree and message with only the attempt head as parent is not adopted."""
        for single_parent in (False, True):
            with self.subTest(single_parent=single_parent):
                self.reset_fixture()
                edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
                original = LiveWorkspace.commit

                def landed(workspace, message):
                    merging = self.merging()
                    original(workspace, message)
                    if merging:
                        raise SystemExit("the checkpoint failed after the merge commit")
                with patch.object(LiveWorkspace, "commit", landed), self.assertRaises(SystemExit):
                    self.walk(model_edit=edit, reviews=["approve", "approve"])
                first, landed_head = self.published_heads[0], self.git("rev-parse", "HEAD")
                self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()[1:], [first, seen["base"]])
                if single_parent:
                    single = self.git("commit-tree", "HEAD^{tree}", "-p", first, "-m", self.git("log", "-1", "--format=%B"))
                    self.git("update-ref", f"refs/heads/{self.branch}", single)
                commits = []
                original_git = LiveWorkspace._git

                def resume_git(workspace, *args, **kwargs):
                    if args[0] == "commit":
                        commits.append(args)
                    return original_git(workspace, *args, **kwargs)
                with patch.object(LiveWorkspace, "_git", resume_git):
                    resumed = self.walk(self.no_worker, reviews=["approve"])
                self.assertEqual(commits, [])
                if single_parent:
                    self.assertEqual((resumed["stop"], resumed["path"][-1]), ("dirty_worktree", "commit"), resumed.get("halt_detail"))
                    self.assertEqual(self.remote_head(), first)
                else:
                    self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
                    self.assertEqual((self.remote_head(), self.merge_parents()), (landed_head, [first, seen["base"]]))

    def killed(self, edit, *, after=lambda: None):
        """The process dies inside prepare once Git has recorded the base merge as under way, before prepare saves it."""
        original = LiveWorkspace.merge

        def dies(workspace, head):
            original(workspace, head)
            after()
            raise SystemExit("killed inside the base merge")
        with patch.object(LiveWorkspace, "merge", dies), self.assertRaises(SystemExit):
            self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertTrue(self.merging())
        self.assertTrue(ready_resumes(self.checkpoint, issue_thread(REPO, 8)))

    def assert_unrecognized(self, result, *paths):
        """Prepare preserves the checkout, and the status comment names its path and each path that differs."""
        self.assertEqual((result["stop"], result["path"][-1]), ("dirty_worktree", "prepare"), result.get("halt_detail"))
        assert self.status is not None
        self.assertIn(f"The checkout {self.checkout} is preserved", result["halt_detail"])
        for words in ("is preserved", *paths):
            self.assertIn(words, self.status)
        self.assertEqual(self.remote_head(), self.published_heads[0])

    def test_IM19_a_prepare_killed_inside_its_base_merge_abandons_it_and_a_persons_edit_stops_dirty_worktree(self):
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
        sessions = []

        def build(model, state, instruction):
            sessions.append(resume_id(state["dispatch_plan"]))
            edit(model, state, instruction)
            return Outcome("ran", {"implementation": "diff", "no_change": None, "human_question": None},
                           usage=SCRIPTED_USAGE, session_id=BUILDER_SESSION, totals=TOTALS)
        self.killed(build)
        killed_base = seen["base"]
        self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), killed_base)
        # The base branch gains a commit while the run is down.
        newer = self.advance_base({"items.txt": "newer\n", "notes.txt": "newer\n"})
        resumed = self.walk(model_edit=build, reviews=["approve"])
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        # The re-entry abandoned the killed merge and merged the new head. The builder's one call came after that merge, in
        # a fresh session.
        self.assertEqual((seen["merging"], resumed["work"].base_merge.head), (newer, newer))
        self.assertEqual(sessions, [None, None])
        self.assertEqual(self.merge_parents(), [self.published_heads[0], newer])

        self.reset_fixture()
        edit, _ = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, lambda: self.fail("No worker runs"))
        self.killed(edit)
        (self.checkout / "notes.txt").write_text("a person's edit\n")
        halted = self.walk(self.no_worker)
        self.assert_unrecognized(halted, "notes.txt")
        self.assertTrue(self.merging())
        self.assertEqual((self.checkout / "notes.txt").read_text(), "a person's edit\n")

    def test_a_person_s_change_inside_an_unfinished_base_merge_stops_dirty_worktree_naming_it(self):
        def staged():
            (self.checkout / "other.txt").write_text("a person's edit\n")
            self.git("add", "other.txt")
            (self.checkout / "other.txt").write_text("main\n")

        def another_merge():
            mine = self.git("commit-tree", "MERGE_HEAD^{tree}", "-p", "MERGE_HEAD", "-m", "A person's commit")
            self.git("merge", "--abort")
            run_subprocess(["git", "merge", "--no-commit", "--no-ff", "-q", mine], cwd=self.checkout, capture_output=True)
            return mine
        for name, change, named in (("staged edit", staged, "other.txt"),
                                    ("edit to a cleanly merged file", lambda: (self.checkout / "other.txt").write_text("edit\n"), "other.txt"),
                                    ("untracked file", lambda: (self.checkout / "scratch.txt").write_text("x\n"), "scratch.txt"),
                                    ("merge of another commit", another_merge, "never fetched")):
            with self.subTest(name):
                self.reset_fixture()
                edit, _ = self.builder({"items.txt": "main\n", "notes.txt": "main\n", "other.txt": "main\n"},
                                       lambda: self.fail("No worker runs"))
                self.killed(edit)
                change()
                self.assert_unrecognized(self.walk(self.no_worker), named)
                self.assertTrue(self.merging())

    def test_the_host_s_git_configuration_does_not_change_the_merge_the_re_entry_recognizes(self):
        """The host turns on rerere and diff3 conflicts between the kill and the resume."""
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
        self.killed(edit)
        host = self.checkout.parent / "host.gitconfig"
        host.write_text("[rerere]\n\tenabled = true\n[merge]\n\tconflictStyle = diff3\n")
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(host)}):
            resumed = self.walk(model_edit=edit, reviews=["approve"])
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.merge_parents(), [self.published_heads[0], seen["base"]])

    def test_a_kill_that_leaves_no_merge_recorded_stops_dirty_worktree(self):
        """Git was killed while it wrote the merged files, before it recorded the merge."""
        edit, _ = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, lambda: self.fail("No worker runs"))

        def partial(workspace, head):
            (self.checkout / "items.txt").write_text("main\n")
            raise SystemExit("killed inside the base merge")
        with patch.object(LiveWorkspace, "merge", partial), self.assertRaises(SystemExit):
            self.walk(model_edit=edit, reviews=["approve", "approve"])
        halted = self.walk(self.no_worker)
        self.assert_unrecognized(halted, "items.txt", "No merge is under way")

    def test_a_base_force_pushed_after_the_kill_is_still_recognized_and_merged_from_its_new_head(self):
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
        self.killed(edit)
        killed_base = seen["base"]
        rewritten = self.git("-c", "user.name=Base", "-c", "user.email=base@example.com", "--git-dir", str(self.remote),
                             "commit-tree", f"{killed_base}^{{tree}}", "-p", f"{killed_base}^", "-m", "Base rewritten")
        self.git("--git-dir", str(self.remote), "update-ref", f"refs/heads/{self.base}", rewritten)
        resumed = self.walk(model_edit=edit, reviews=["approve"])
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual((seen["merging"], self.merge_parents()), (rewritten, [self.published_heads[0], rewritten]))

    def test_a_git_command_killed_at_its_limit_inside_the_base_merge_halts_and_ready_merges_again(self):
        """Foreman killed the command and saw it end, so it removes the index lock that command left. The merge itself runs
        with no limit, as the check at the base does."""
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
        real = subprocess.run
        limits = []

        def at_limit(command, **kwargs):
            result = real(command, **kwargs)
            if command[:3] == ["git", "merge", "--no-commit"]:
                limits.append(kwargs["timeout"])
                (self.checkout / ".git/index.lock").touch()
                raise subprocess.TimeoutExpired(command, 10)
            return result
        with patch("foreman.workspace.subprocess.run", at_limit):
            halted = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual((halted["stop"], halted["path"][-1]), ("git_failed", "prepare"), halted.get("halt_detail"))
        self.assertIn("timed out", halted["halt_detail"])
        self.assertEqual(limits, [None])
        self.assertFalse((self.checkout / ".git/index.lock").exists())
        self.assertTrue(self.merging())
        self.assertTrue(ready_resumes(self.checkpoint, issue_thread(REPO, 8)))
        resumed = self.walk(model_edit=edit, reviews=["approve"])
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.merge_parents(), [self.published_heads[0], seen["base"]])

    def test_a_leftover_index_lock_on_a_resume_stops_git_failed_and_ready_goes_on_once_it_is_gone(self):
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve)
        lock = self.checkout / ".git/index.lock"
        self.killed(edit, after=lock.touch)
        halted = self.walk(self.no_worker)
        self.assertEqual((halted["stop"], halted["path"][-1]), ("git_failed", "prepare"), halted.get("halt_detail"))
        for words in ("index.lock", str(self.checkout)):
            self.assertIn(words, halted["halt_detail"])
        self.assertTrue(lock.exists() and self.merging())
        lock.unlink()
        self.assertTrue(ready_resumes(self.checkpoint, issue_thread(REPO, 8)))
        resumed = self.walk(model_edit=edit, reviews=["approve"])
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.merge_parents(), [self.published_heads[0], seen["base"]])

    def test_a_read_killed_at_its_limit_leaves_no_index_lock_of_its_own_and_keeps_another_gits(self):
        """Real Git, no mock. Porcelain diff refreshes the index under its lock once it has compared a touched file. A clean
        filter that outlasts the limit while the index it reads is locked holds the abandon proof's diff in that refresh."""
        edit, _ = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, lambda: self.fail("No worker runs"))
        self.killed(edit)
        git_dir = self.checkout / ".git"
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info/attributes").write_text("* filter=slow\n")
        self.git("config", "filter.slow.clean", 'test -e "${GIT_INDEX_FILE:-.git/index}.lock" && sleep 3; cat')
        merged = next(line.split("\t", 1)[1] for line in self.git("ls-files", "--stage").splitlines() if line.split()[2] == "0")
        lock = git_dir / "index.lock"
        for touched, another_git in enumerate((None, "another Git's lock\n"), start=1):
            with self.subTest(another_git=another_git):
                os.utime(self.checkout / merged, (touched, touched))
                if another_git:
                    lock.write_text(another_git)
                with patch.dict("foreman.workspace.GIT_TIMEOUTS", {"status": 1, "diff": 1}), \
                        self.assertRaisesRegex(WorkspaceError, "timed out after 1 seconds"):
                    LiveWorkspace(self.checkout).abandon_merge(self.branch, self.published_heads[0], self.base)
                self.assertEqual(lock.read_text() if lock.exists() else None, another_git)
                self.assertTrue(self.merging())
        lock.unlink()

    def test_IM18_a_builder_commit_on_a_conflict_repair_stops_commit_and_publishes_nothing(self):
        def commit_itself():
            self.resolve()
            self.git("commit", "-qam", "The builder's own merge commit")
        edit, _ = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, commit_itself)
        result = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual((result["stop"], result["path"][-1]), ("dirty_worktree", "commit"), result.get("halt_detail"))
        self.assertEqual(self.remote_head(), self.published_heads[0])
        self.assertEqual(self.git("log", "-1", "--format=%s"), "The builder's own merge commit")


class AttemptContainerTest(PublicationFixture):
    """Prepare builds the run's attempt container from the contract at the pinned base, and edit's check runs in it (item 3)."""

    SECRET, VALUE = "PARTNER_AUTH_SECRET", "partner-test-value-6f1c2a"

    def test_prepare_builds_the_image_runs_setup_in_order_in_the_checkout_and_the_check_runs_there(self):
        installs = self.checkout.parent / "setup.log"
        self.set_contract(setup=[f"echo one >> {installs}", f"echo two >> {installs}"])
        self.assertEqual(self.walk().get("stop"), "merged")
        (name, spec), = self.containers.specs.items()
        self.assertEqual((spec.image, spec.workdir, installs.read_text()), (IMAGE, self.checkout, "one\ntwo\n"))
        ran = [(ran.command, ran.cwd) for ran in self.containers.ran]
        # Prepare runs the check at the base, then edit runs it on the candidate (:525, :527).
        self.assertEqual(ran, [("test -e .git", self.checkout), (f"echo one >> {installs}", self.checkout),
                               (f"echo two >> {installs}", self.checkout), (self.check, self.checkout), (self.check, self.checkout)])
        # The network is the container's only during setup.
        self.assertEqual(self.containers.isolated, {name})

    def test_a_contract_whose_network_is_run_keeps_the_container_connected(self):
        self.set_contract(extra="network: run\n")
        self.assertEqual(self.walk().get("stop"), "merged")
        self.assertEqual((list(self.containers.specs), self.containers.isolated), (["container-1"], set()))

    def test_a_setup_that_changes_the_checkout_stops_dirty_worktree_before_the_builder(self):
        self.set_contract(setup=["echo installed > items.txt"])
        result = self.walk(self.no_worker)
        self.assertEqual((result["stop"], result["path"][-1]), ("dirty_worktree", "prepare"))
        self.assertIn("setup commands changed tracked files", result["halt_detail"])

    def test_each_secret_gets_its_test_value_and_no_text_foreman_writes_holds_it(self):
        # The check passes on the clean base and fails on every candidate.
        self.set_contract(f'echo "auth=${self.SECRET}"; test -z "$(git status --porcelain)"', secrets=[self.SECRET])
        logged = []
        with patch.dict(os.environ, {f"FOREMAN_TEST_{self.SECRET}": self.VALUE}):
            result = self.walk(check_log=lambda line: logged.append(redact(line)))
        self.assertEqual(result["stop"], "failed_check_cap")
        (_, spec), = self.containers.specs.items()
        self.assertEqual(dict(spec.secrets), {self.SECRET: self.VALUE})
        # The check printed the value, and the saved failure, the run log, the status comment and the checkpoint hold none of it.
        self.assertIn("auth=[redacted:Test Value]", result["checks"].failure_output)
        self.assertIn("auth=[redacted:Test Value]\n", logged)
        assert self.status is not None
        for text in (result["checks"].failure_output, "".join(logged), self.status, result["halt_detail"]):
            self.assertNotIn(self.VALUE, text)
        for store in self.checkpoint.parent.glob(f"{self.checkpoint.name}*"):
            self.assertNotIn(self.VALUE.encode(), store.read_bytes())

    def test_a_missing_or_empty_test_value_stops_env_unavailable_naming_it(self):
        self.set_contract(secrets=[self.SECRET])
        for value in (None, "  "):
            with self.subTest(value=value), patch.dict(os.environ):
                os.environ.pop(f"FOREMAN_TEST_{self.SECRET}", None)
                if value is not None:
                    os.environ[f"FOREMAN_TEST_{self.SECRET}"] = value
                result = self.walk(self.no_worker)
                self.assertEqual((result["stop"], result["path"][-1]), ("env_unavailable", "prepare"))
                self.assertIn(f"FOREMAN_TEST_{self.SECRET} is not set on the Foreman host", result["halt_detail"])
                self.assertEqual(self.containers.specs, {})

    def test_a_container_lost_during_the_check_is_no_check_result(self):
        """A check whose container vanished exits non-zero, but that exit is no failed check and no Git failure."""
        run, checks = self.containers.run, []

        def lost(name, command, **kwargs):
            # Prepare's check at the base runs first, so the candidate's check is the second.
            checks.extend([command] if command == self.check else [])
            if command == self.check and len(checks) == 2:
                self.containers.live.discard(name)
            return run(name, command, **kwargs)
        with patch.object(self.containers, "run", side_effect=lost):
            result = self.walk()
        self.assertEqual((result["stop"], result["path"][-1]), ("env_unavailable", "check"))
        self.assertIn(f"The container that ran {self.check!r} is gone", result["halt_detail"])
        self.assertEqual((result["checks"].verify_failures, result["checks"].verify), (0, ""))
        # Ready added again builds a new container and runs the check there.
        self.assertEqual(self.walk(self.no_worker).get("stop"), "merged")
        self.assertEqual(list(self.containers.specs), ["container-1", "container-2"])

    def test_the_container_mounts_the_checkout_gits_directory_read_only_and_the_runs_scratch_and_nothing_else(self):
        scratch = self.checkout.parent / "8.scratch"
        workspace = LiveWorkspace(self.checkout, scratch=scratch, containers=self.containers)
        workspace.prepare_container(base=self.git("rev-parse", "HEAD"), log=None)
        (_, spec), = self.containers.specs.items()
        common = (self.checkout / ".git").resolve()
        self.assertEqual(spec.mounts, (Mount(self.checkout), Mount(common, read_only=True), Mount(scratch)))
        self.assertTrue(scratch.is_dir())
        # A second use in the same process finds the container running and builds none.
        workspace.prepare_container(base=self.git("rev-parse", "HEAD"), log=None)
        self.assertEqual(len(self.containers.specs), 1)

    def test_the_docker_launch_carries_only_the_image_mounts_network_and_test_value_names(self):
        """No App key, gh or Git credential, SSH agent, docker socket or host HOME enters the container, and no test value is
        an argument: docker reads each from its own environment."""
        spec = ContainerSpec("/runs/8", IMAGE, self.checkout, (Mount(self.checkout), Mount(Path("/clone/.git"), read_only=True)),
                             {self.SECRET: self.VALUE})
        calls = []

        def docker(args, **kwargs):
            calls.append((args, kwargs.get("env")))
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("foreman.container.subprocess.run", side_effect=docker), \
                patch.dict(os.environ, {"GH_TOKEN": "ghs_hosttoken", "SSH_AUTH_SOCK": "/agent.sock"}):
            name = DockerHost().create(spec, 1800)
        (network, _), (launch, env) = calls
        self.assertEqual(network, ["docker", "network", "create", "--label", "foreman.checkout=/runs/8", name])
        self.assertEqual(launch, ["docker", "run", "--detach", "--rm", "--init", "--name", name, "--label", "foreman.checkout=/runs/8",
                                  "--network", name, "--workdir", str(self.checkout),
                                  "--mount", f"type=bind,source={self.checkout},target={self.checkout}",
                                  "--mount", "type=bind,source=/clone/.git,target=/clone/.git,readonly",
                                  "--env", self.SECRET, IMAGE, "tail", "-f", "/dev/null"])
        self.assertNotIn(self.VALUE, " ".join(launch))
        self.assertEqual(env[self.SECRET], self.VALUE)
        self.assertIn(f"{ContainerSpec.__name__}(label='/runs/8'", repr(spec))
        self.assertNotIn(self.VALUE, repr(spec))

    def test_a_command_still_running_at_its_limit_is_ended_and_reported_at_that_limit(self):
        """The docker exec client a check or setup runs through is killed at the limit, and its result is the limit."""
        lines: list[str] = []
        self.assertEqual(stream(["sh", "-c", "echo $$; exec sleep 5"], lines.append, 0.5), TimedOut(0.5))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(lines[0]), 0)
