import tests

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from foreman.dispatch import InvalidOutput
from foreman.github import BOT_LOGIN, CommandTimedOut, ForgeError, IssueComment, PublishedPullRequest, status_body
from foreman.stage_output import HumanAnswer, VerifyOutput
from foreman.stages import STAGES, Acceptance
from foreman.skill_rules import TargetSkillUnreadable, target_skills
from foreman.state import ChecksState, PlanState, ReviewState, RunState, VerifyState
from foreman.steps import CANCELLATION, StageRun, verify
from foreman.workspace import WorkState, WorkspaceError, WorktreeState
from pydantic import TypeAdapter, ValidationError

HEAD, MOVED = "b" * 40, "c" * 40
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

    def inspect(self):
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]


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
    return {"run": RunState(repo="a/b", issue=8, workspace=workspace), "plan": PlanState(),
            "checks": ChecksState(check="true", verified_head=HEAD), "review": ReviewState(review_run_id="run-1"),
            "work": WorkState(branch="8-items", base="main", published_head=HEAD), "path": [], **values}


def stage(forge=None, workspace=None, applied=None):
    run = MagicMock(forge=forge or Forge(), workspace=workspace or Workspace(), human_login=LOGIN)
    run.apply.return_value = applied if applied is not None else {
        "path": ["verify"], "verify": VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])}
    return run


class VerifyTest(unittest.TestCase):
    def test_vf1_no_skill_makes_no_call_and_no_comment(self):
        run = stage()
        with checkout(".claude/skills/notes/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            update = verify.drive(state(root), run)
        self.assertEqual((update["stop"], update["verify"]), ("no_skill", VerifyState(outcome="no_skill", tested_head=HEAD)))
        self.assertNotIn("path", update)
        dispatch.invoke.assert_not_called()
        self.assertEqual(run.forge.posted, [])

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
            update = verify.drive(state(root), run)
        self.assertEqual((update["halt"], update["path"]), ("dirty_worktree", ["verify"]))
        self.assertNotIn("verify", update)
        self.assertEqual(run.forge.posted, [])

    def test_a_resumed_drive_posts_stale_evidence_when_the_head_moved_meanwhile(self):
        driven = state(verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION]))
        run = stage(Forge(heads=[pr(MOVED)]))
        self.assertEqual(verify.drive(driven, run), {"stop": ""})
        posted = verify.post(driven, run)
        self.assertEqual((posted["stop"], posted["verify"].stale), ("head_moved", True))
        self.assertIn("stale", run.forge.posted[0])

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
        run = StageRun(workspace=Workspace(), forge=forge, check_log=None, max_rounds=3, human_login=LOGIN, dispatch=MagicMock(), apply=apply)
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            ended = verify.compile_stage(run).invoke(state(root, verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])))
        self.assertEqual((ended["stop"], ended.get("halt"), ended["path"]), ("cancelled", None, ["verify"]))
        self.assertEqual(forge.closes, [("a/b", 7, CANCELLATION)])
        self.assertEqual(forge.posted, [])
        dispatch.invoke.assert_not_called()
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
