import tests

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from foreman.github import CommandTimedOut, ForgeError, PublishedPullRequest, status_body
from foreman.skill_rules import target_skills
from foreman.state import ChecksState, PlanState, ReviewState, RunState, VerifyState
from foreman.steps import verify
from foreman.workspace import WorkState, WorktreeState

HEAD, MOVED = "b" * 40, "c" * 40
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

    def pull_request_for(self, repo, branch):
        head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        if isinstance(head, Exception):
            raise head
        return head

    def upsert_comment(self, repo, number, text, facts, *, marker):
        if self.error:
            raise self.error
        body = status_body(text, facts, marker)
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
    run = MagicMock(forge=forge or Forge(), workspace=workspace or Workspace())
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
                      ".claude/skills/verify-api/SKILL.md", symlink=".claude/skills/verify-link/SKILL.md") as root:
            found = [path.relative_to(root).as_posix() for path in target_skills(root, "verify-<app>/SKILL.md")]
        self.assertEqual(found, [".cursor/skills/verify-web/SKILL.md", ".claude/skills/verify-api/SKILL.md"])
        self.assertEqual(target_skills("", "verify-<app>/SKILL.md"), [])

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
                            (Forge(heads=[None]), "publish_failed"), (Forge(heads=[ForgeError("down")]), "git_failed")):
            with self.subTest(halt=halt, forge=forge):
                self.assertEqual(verify.post(driven, stage(forge))["halt"], halt)

    def test_vf5_a_changed_checkout_stops_before_posting(self):
        dirty = Workspace([WorktreeState(HEAD, True, "8-items"), WorktreeState(HEAD, False, "8-items")])
        run = stage(workspace=dirty)
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH"):
            update = verify.drive(state(root), run)
        self.assertEqual((update["halt"], update["path"]), ("dirty_worktree", ["verify"]))
        self.assertEqual(run.forge.posted, [])

    def test_a_resumed_drive_posts_the_evidence_it_reached(self):
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            update = verify.drive(state(root, verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])), stage())
        self.assertEqual(update, {"stop": ""})
        dispatch.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
