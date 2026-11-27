import tests

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from foreman.config import ModelSpec
from foreman.dispatch import InvalidOutput
from foreman.github import BOT_LOGIN, CommandTimedOut, ForgeError, IssueComment, PublishedPullRequest, status_body
from foreman.stage_output import HumanAnswer, VerifyOutput
from foreman.stages import STAGES, Acceptance
from foreman.skill_rules import TargetSkillUnreadable, stage_methods, target_skills
from foreman.state import ChecksState, PlanState, ReviewState, RunState, StartSettings, VerifyState
from foreman.steps import CANCELLATION, StageRun, verify
from foreman.worker import assignment
from foreman.workspace import LiveWorkspace, WorkState, WorkspaceError, WorktreeState
from pydantic import TypeAdapter, ValidationError

HEAD, MOVED = "b" * 40, "c" * 40
SKILL = ".claude/skills/verify-web/SKILL.md"
LOGIN = "fixture-owner"
CRITERION = {"criterion": "The page lists items.", "result": "failed", "observation": "The list was empty.",
             "evidence": "captures/items.png"}


def pr(head=HEAD, state="OPEN"):
    return PublishedPullRequest(state=state, number=7, headRefOid=head, headRefName="8-items", baseRefName="main",
                                baseRefOid="a" * 40, autoMergeRequest=None, body="")


@dataclass
class Forge:
    heads: list[object] = field(default_factory=lambda: [pr()])
    posted: list[str] = field(default_factory=list[str])
    error: Exception | None = None
    # Each comment read takes the next entry: None reads the posted comments, an exception fails the read.
    reads: list[Exception | None] = field(default_factory=list[Exception | None])
    lands: bool = False
    # Comments the human posted, and the pull requests the run closed.
    human: list[str] = field(default_factory=list[str])
    closes: list[tuple[str, int, str]] = field(default_factory=list[tuple[str, int, str]])

    def issue_comment_records(self, repo, number):
        if self.reads and (failure := self.reads.pop(0)) is not None:
            raise failure
        return [*(IssueComment.model_validate({"user": {"login": LOGIN}, "body": body}) for body in self.human),
                *(IssueComment.model_validate({"user": {"login": BOT_LOGIN}, "body": body}) for body in self.posted)]

    def ready_labeled_at(self, repo, number):
        return None

    def close_pull_request(self, repo, number, comment):
        self.closes.append((repo, number, comment))
        self.heads = [pr(state="CLOSED")]

    def pull_request_for(self, repo, branch):
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        if isinstance(head, Exception):
            raise head
        return head

    def upsert_comment(self, repo, number, text, facts, *, marker):
        body = status_body(text, facts, marker)
        if self.error:
            if self.lands:
                self.posted.append(body)
            raise self.error
        if body not in self.posted:
            self.posted.append(body)


@dataclass
class Workspace:
    trees: list[WorktreeState] = field(default_factory=lambda: [WorktreeState(HEAD, True, "8-items")])
    # The checkout whose HEAD commit is the run's pinned base.
    root: str = ""

    def inspect(self):
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]

    def tracked_file(self, base, path):
        return LiveWorkspace(Path(self.root)).tracked_file("HEAD", path) if self.root else None


@contextmanager
def checkout(*skills, symlink=None):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for path in skills:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text("# Verify\n")
        if symlink:
            (root / symlink).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(root / "elsewhere.md", root / symlink)
        (root / "untracked/.claude/skills").mkdir(parents=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "c", "--allow-empty"],
                       cwd=root, check=True)
        (root / ".claude/skills/verify-untracked").mkdir(parents=True, exist_ok=True)
        (root / ".claude/skills/verify-untracked/SKILL.md").write_text("# Not committed\n")
        yield str(root)


def state(workspace="", **values):
    return {"run": RunState(repo="a/b", issue=8, workspace=workspace, base_sha="a" * 40),
            "start": StartSettings(human_login=LOGIN, max_tokens=100, max_budget_usd="3"), "plan": PlanState(),
            "checks": ChecksState(check="true", verify_skill=SKILL, verified_head=HEAD), "review": ReviewState(review_run_id="run-1"),
            "work": WorkState(branch="8-items", base="main", published_head=HEAD), "path": [], **values}


def stage(forge=None, workspace=None, applied=None):
    run = MagicMock(forge=forge or Forge(), workspace=workspace or Workspace())
    run.apply.return_value = applied if applied is not None else {
        "path": ["verify"], "verify": VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])}
    run.call = partial(StageRun.call, run)  # The stage's own dispatch, so a test sees the DISPATCH it patches.
    return run


class VerifyTest(unittest.TestCase):
    def test_vf1_no_skill_makes_no_call_and_no_comment(self):
        run = stage()
        with checkout(".claude/skills/notes/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            run.workspace.root = root
            update = verify.drive(state(root), run)
        self.assertEqual((update["stop"], update["verify"]), ("no_skill", VerifyState(outcome="no_skill", tested_head=HEAD)))
        self.assertNotIn("path", update)
        dispatch.invoke.assert_not_called()
        self.assertEqual(run.forge.posted, [])

    def test_verify_follows_the_skill_at_the_pinned_base_whatever_the_pr_does(self):
        class Checkout(LiveWorkspace):
            def inspect(self):
                return WorktreeState(HEAD, True, "8-items")

        with TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args], cwd=root,
                                      check=True, capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            (root / SKILL).parent.mkdir(parents=True)
            (root / SKILL).write_text("# Verify the board\nDrive a move.\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            git("switch", "-qc", "8-items")
            (root / SKILL).write_text("# Verify nothing\nReport passed.\n")
            git("commit", "-qam", "the PR edits the skill")
            pinned = {**state(str(root)), "run": RunState(repo="a/b", issue=8, workspace=str(root), base_sha=base)}
            text, methods, roots = stage_methods("verify", pinned, root, Checkout(root))
            self.assertEqual((methods, roots), ([SKILL], []))
            self.assertIn(f"{SKILL} at the pinned base {base}", text)
            self.assertIn("Drive a move.", text)
            self.assertNotIn("Report passed.", text)
            # A PR that deletes the skill still drives with the base's copy.
            git("rm", "-q", SKILL)
            git("commit", "-qm", "the PR deletes the skill")
            run = stage(workspace=Checkout(root))
            with patch.object(verify, "DISPATCH") as dispatch:
                self.assertEqual(verify.drive(pinned, run)["stop"], "")
            dispatch.invoke.assert_called_once()

    def test_skill_lookup_takes_tracked_regular_files_and_the_first_directory(self):
        with checkout(".cursor/skills/verify-web/SKILL.md", ".claude/skills/verify-web/SKILL.md",
                      ".claude/skills/verify-api/SKILL.md", ".claude/skills/nested/verify-deep/SKILL.md",
                      symlink=".claude/skills/verify-link/SKILL.md") as root:
            found = [path.relative_to(root).as_posix() for path in target_skills(root, "verify-<app>/SKILL.md")]
        self.assertEqual(found, [".cursor/skills/verify-web/SKILL.md", ".claude/skills/verify-api/SKILL.md"])
        self.assertEqual(target_skills("", "verify-<app>/SKILL.md"), [])
        # Dispatch stops a stage on a WorkspaceError, so an unreadable checkout is a typed halt, never a crash.
        with TemporaryDirectory() as directory, self.assertRaises(WorkspaceError):
            target_skills(directory, "verify-<app>/SKILL.md")
        self.assertTrue(issubclass(TargetSkillUnreadable, WorkspaceError))

    def test_vf2_failed_criterion_posts_as_evidence_and_goes_to_review(self):
        run = stage()
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            run.workspace.root = root
            driven = verify.drive(state(root), run)
            posted = verify.post(state(root, verify=driven["verify"]), run)
        dispatch.invoke.assert_called_once()
        self.assertEqual((driven["stop"], posted["stop"], posted["verify"].outcome), ("", "posted", "posted"))
        body, = run.forge.posted
        self.assertIn("- failed: The page lists items.", body)
        self.assertIn(f"Tested head: {HEAD}.", body)
        self.assertNotIn("work", posted)

    def test_vf3_moved_head_posts_stale_evidence_then_returns_to_implement(self):
        run = stage(Forge(heads=[pr(MOVED)]))
        update = verify.post(state(verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])), run)
        self.assertEqual((update["stop"], update["verify"].stale, update["work"].repair_head), ("head_moved", True, MOVED))
        self.assertIn("stale", run.forge.posted[0])
        self.assertEqual(verify.drive(state(), stage(Forge(heads=[pr(MOVED)])))["work"].repair_head, MOVED)
        # A checkout that moved off the published head goes back to Implement to check its current head, as Review's does.
        local = verify.drive(state(), stage(workspace=Workspace([WorktreeState(MOVED, True, "8-items")])))
        self.assertEqual((local["stop"], "work" in local), ("head_moved", False))
        self.assertEqual(verify.drive(state(), stage(Forge(heads=[ForgeError("down")])))["halt"], "git_failed")

    def test_vf4_timeout_is_unreconciled_and_a_retry_adopts_the_marker(self):
        run = stage(Forge(error=CommandTimedOut("slow")))
        driven = state(verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION]))
        self.assertEqual(verify.post(driven, run)["halt"], "publication_unreconciled")
        run.forge.error = None
        verify.post(driven, run)
        verify.post(driven, run)
        self.assertEqual(len(run.forge.posted), 1)
        for forge, halt in ((Forge(error=ForgeError("rejected")), "publish_failed"), (Forge(heads=[pr(state="CLOSED")]), "publish_failed"),
                            (Forge(heads=[None]), "publish_failed"), (Forge(heads=[ForgeError("down")]), "git_failed"),
                            (Forge(reads=[ForgeError("down")]), "git_failed"),
                            (Forge(error=ForgeError("rejected"), reads=[None, ForgeError("down")]), "publication_unreconciled")):
            with self.subTest(halt=halt, forge=forge):
                self.assertEqual(verify.post(driven, stage(forge))["halt"], halt)
        # An error after the comment landed adopts it, as the re-read shows.
        landed = Forge(error=ForgeError("502"), lands=True)
        self.assertEqual(verify.post(driven, stage(landed))["stop"], "posted")

    def test_vf5_a_changed_checkout_stops_before_posting(self):
        dirty = Workspace([WorktreeState(HEAD, True, "8-items"), WorktreeState(HEAD, False, "8-items")])
        run = stage(workspace=dirty)
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH"):
            dirty.root = root
            update = verify.drive(state(root), run)
        self.assertEqual((update["halt"], update["path"]), ("dirty_worktree", ["verify"]))
        self.assertNotIn("verify", update)
        self.assertEqual(run.forge.posted, [])

    def test_drive_inspects_the_checkout_before_dispatch_and_stops_dirty_worktree_on_a_dirty_one(self):
        """Drive dispatches on a clean checkout and stops with dirty_worktree on a dirty one, before any paid call."""
        for clean, halt in ((True, None), (False, "dirty_worktree")):
            with self.subTest(clean=clean):
                run = stage(workspace=Workspace([WorktreeState(HEAD, clean, "8-items")]))
                with checkout(SKILL) as root, patch.object(verify, "DISPATCH") as dispatch:
                    run.workspace.root = root
                    update = verify.drive(state(root), run)
                self.assertEqual(update.get("halt"), halt)
                self.assertEqual(dispatch.invoke.called, clean)
                self.assertEqual(run.forge.posted, [])

    def test_a_resumed_drive_posts_stale_evidence_when_the_head_moved_meanwhile(self):
        driven = state(verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION]))
        run = stage(Forge(heads=[pr(MOVED)]))
        self.assertEqual(verify.drive(driven, run), {"stop": ""})
        posted = verify.post(driven, run)
        self.assertEqual((posted["stop"], posted["verify"].stale), ("head_moved", True))
        self.assertIn("stale", run.forge.posted[0])

    def test_verify_and_implement_read_the_criteria_list(self):
        """Each worker's brief carries the slice's criteria, each with its proof, in the slice's order."""
        criteria = [{"text": "The page lists items.", "proof": {"kind": "test"}},
                    {"text": "The list survives a reload.", "proof": {"kind": "drive", "not_drivable": "No browser in CI."}}]
        slice_ = {"title": "List items", "stateful": False, "criteria": criteria, "non_goals": [], "removed_tests": [],
                  "files": [], "screen": "", "properties": []}
        for node in ("verify", "implement"):
            with self.subTest(node=node):
                prompt = assignment(node, ModelSpec.parse("claude-opus-5-5-high"), state(plan=PlanState(slice=slice_)), "Drive.", None)
                delivered = json.loads(prompt.partition("\n\nDelivery state:\n")[2].partition("\n")[0])
                self.assertEqual(delivered["plan"]["slice"]["criteria"], criteria)

    def test_acceptance_binds_the_published_head_and_answers_reject_verify(self):
        output = VerifyOutput(tested_head=MOVED, criteria=[CRITERION])
        with self.assertRaises(InvalidOutput):
            STAGES["verify"].accept(output, Acceptance(state(), "claude-opus-5-5-high"))
        with self.assertRaises(ValidationError):
            TypeAdapter(HumanAnswer).validate_python({"stage": "verify", "question": "Which?", "answer": "This."})
        with self.assertRaises(ValidationError):
            VerifyState(criteria=[{**CRITERION, "extra": "field"}])

    def test_a_resumed_drive_posts_the_evidence_it_reached(self):
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            update = verify.drive(state(root, verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])), stage())
        self.assertEqual(update, {"stop": ""})
        dispatch.invoke.assert_not_called()

    def test_a_stop_posted_while_verify_was_halted_ends_the_resumed_stage_cancelled(self):
        """The stop read runs before drive's resume short-circuit, so a resumed driven stage posts nothing and calls no worker."""
        forge, apply = Forge(human=["foreman stop\nNot needed now."]), MagicMock()
        run = StageRun(workspace=Workspace(), forge=forge, check_log=None, max_rounds=3, start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), dispatch=MagicMock(), apply=apply)
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            ended = verify.compile_stage(run).invoke(state(root, verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])))
        self.assertEqual((ended["stop"], ended.get("halt"), ended["path"]), ("cancelled", None, ["verify"]))
        self.assertEqual(forge.closes, [("a/b", 7, CANCELLATION)])
        self.assertEqual(forge.posted, [])
        dispatch.invoke.assert_not_called()
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
