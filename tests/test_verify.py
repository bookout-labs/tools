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

from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.dispatch import InvalidOutput
from foreman.github import BOT_LOGIN, CommandTimedOut, ForgeError, IssueComment, PublishedPullRequest, status_body
from foreman.stage_output import HumanAnswer, VerifyOutput
from foreman.stages import STAGES, Acceptance
from foreman.skill_rules import TargetSkillUnreadable, stage_methods, target_skills
from foreman.state import ChecksState, PlanState, ReviewState, RunState, StartSettings, VerifyState
from foreman.steps import CANCELLATION, StageRun, verify
from foreman.worker import assignment
from foreman.workspace import ContainerUnavailable, LiveWorkspace, RelatedCheckout, WorkState, WorkspaceError, WorktreeState
from pydantic import TypeAdapter, ValidationError
from test_delivery import walk

HEAD, MOVED = "b" * 40, "c" * 40
SKILL = ".claude/skills/verify-web/SKILL.md"
LOGIN = "fixture-owner"
CRITERION = {"criterion": "The page lists items.", "result": "failed", "observation": "The list was empty.",
             "evidence": "captures/items.png"}
DRIVEN = {"title": "List items", "stateful": False, "non_goals": [], "removed_tests": [], "files": [], "screen": "", "properties": [],
          "criteria": [{"text": "The page lists items.", "proof": {"kind": "drive", "not_drivable": None}}]}


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
    # What preparing the run's container raises, and the bases it was prepared at.
    unavailable: Exception | None = None
    prepared: list[str] = field(default_factory=list[str])

    def prepare_container(self, *, base, log):
        if self.unavailable is not None:
            raise self.unavailable
        self.prepared.append(base)

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
            "start": StartSettings(human_login=LOGIN, max_tokens=100, max_budget_usd="3"), "plan": PlanState(slice=DRIVEN),
            "checks": ChecksState(check="true", verify_skill=SKILL, verified_head=HEAD), "review": ReviewState(review_run_id="run-1"),
            "work": WorkState(branch="8-items", base="main", published_head=HEAD), "path": [], **values}


def stage(forge=None, workspace=None, applied=None):
    run = MagicMock(forge=forge or Forge(), workspace=workspace or Workspace())
    run.apply.return_value = applied if applied is not None else {
        "path": ["verify"], "verify": VerifyState(outcome="driven", tested_head=HEAD, criteria=[CRITERION])}
    run.call = partial(StageRun.call, run)  # The stage's own dispatch, so a test sees the DISPATCH it patches.
    return run


class VerifyTest(unittest.TestCase):
    def test_no_skill_is_gone_from_verify_and_the_top_router(self):
        """Doctor admits only a repository with a verify skill, so Verify always drives and has no no_skill route."""
        with self.assertRaises(ValidationError):
            VerifyState(outcome="no_skill")
        run = StageRun(workspace=Workspace(), forge=Forge(), check_log=None, max_rounds=3, dispatch=MagicMock(), apply=MagicMock())
        routes = {route for branch in verify.compile_stage(run).builder.branches.values() for spec in branch.values()
                  for route in (spec.ends or {})}
        self.assertEqual(routes, {"driven", "posted", "failed", "unverified", "head_moved", "cancelled", "halted"})
        graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(DryRun(), DryRun()))
        branch, = graph.builder.branches["verify"].values()
        self.assertEqual(dict(branch.ends or {}),
                         {"posted": "review", "failed": "implement", "head_moved": "implement", "cancelled": "__end__", "halted": "__end__"})

    def test_verify_follows_the_skill_at_the_pinned_base_whatever_the_pr_does(self):
        class Checkout(LiveWorkspace):
            def inspect(self):
                return WorktreeState(HEAD, True, "8-items")

            def prepare_container(self, *, base, log):
                pass

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

    def test_vf1_a_failed_criterion_posts_its_evidence_and_goes_to_implement_counting_once(self):
        run = stage()
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH") as dispatch:
            run.workspace.root = root
            driven = verify.drive(state(root), run)
            posted = verify.post(state(root, verify=driven["verify"]), run)
        dispatch.invoke.assert_called_once()
        self.assertEqual((driven["stop"], posted["stop"], posted["verify"].outcome), ("", "failed", "posted"))
        body, = run.forge.posted
        self.assertIn("- failed: The page lists items.\n  Observed: The list was empty.\n  Evidence: captures/items.png", body)
        self.assertIn(f"Tested head: {HEAD}.", body)
        # Implement gets the failed criteria with their evidence, and the round counts once toward the failed-check cap.
        self.assertEqual((posted["verify"].criteria, posted["checks"].verify_failures), ([CRITERION], 1))
        self.assertNotIn("halt", posted)

    def test_vf2_a_drivable_unverified_criterion_stops_and_a_not_drivable_one_does_not(self):
        reason = {"text": "The list survives a reload.", "proof": {"kind": "drive", "not_drivable": "The app has no persistence yet."}}
        plan = PlanState(slice={**DRIVEN, "criteria": [*DRIVEN["criteria"], reason]})
        unverified = {**CRITERION, "result": "unverified", "observation": "The app did not start.", "evidence": "No database in the container."}
        for criteria, halted in (([unverified, {**CRITERION, "criterion": reason["text"], "result": "unverified"}], True),
                                 ([{**CRITERION, "result": "passed"}, {**CRITERION, "criterion": reason["text"], "result": "unverified"}], False),
                                 ([{**CRITERION, "criterion": reason["text"], "result": "passed"}], True)):
            with self.subTest(criteria=criteria):
                run = stage()
                posted = verify.post(state(plan=plan, verify=VerifyState(outcome="driven", tested_head=HEAD, criteria=criteria)), run)
                # The evidence is posted either way.
                self.assertEqual(len(run.forge.posted), 1)
                if not halted:
                    self.assertEqual((posted["stop"], posted.get("halt")), ("posted", None))
                    continue
                self.assertEqual((posted["stop"], posted["halt"], posted["verify"].outcome), ("criterion_unverified",) * 2 + ("posted",))
                self.assertIn("The page lists items.", posted["halt_detail"])
                self.assertNotIn(reason["text"], posted["halt_detail"])
                if criteria[0] is unverified:
                    self.assertIn("Blocker: No database in the container.", posted["halt_detail"])
                else:
                    self.assertIn("the verifier reported no result for it", posted["halt_detail"])

    def test_vf2_review_never_runs_on_a_head_with_an_unverified_drivable_criterion(self):
        unverified = {**CRITERION, "result": "unverified"}
        script = ScriptHandler()

        def handler(node, model, state, instruction):
            outcome = script(node, model, state, instruction)
            if node == "verify":
                outcome.output["criteria"] = [{**unverified, "criterion": criterion["criterion"]} for criterion in outcome.output["criteria"]]
            return outcome
        result = walk(handler)
        self.assertEqual(result["stop"], "criterion_unverified", result.get("halt_detail"))
        self.assertNotIn("review", [call["node"] for call in script.calls])
        self.assertEqual(result["verify"].outcome, "posted")

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
        # An error after the comment landed adopts it, as the re-read shows, and routes on the failed criterion.
        landed = Forge(error=ForgeError("502"), lands=True)
        self.assertEqual(verify.post(driven, stage(landed))["stop"], "failed")

    def test_vf5_a_changed_checkout_stops_before_posting(self):
        dirty = Workspace([WorktreeState(HEAD, True, "8-items"), WorktreeState(HEAD, False, "8-items")])
        run = stage(workspace=dirty)
        with checkout(".claude/skills/verify-web/SKILL.md") as root, patch.object(verify, "DISPATCH"):
            dirty.root = root
            update = verify.drive(state(root), run)
        self.assertEqual((update["halt"], update["path"]), ("dirty_worktree", ["verify"]))
        self.assertNotIn("verify", update)
        self.assertEqual(run.forge.posted, [])

    def test_drive_runs_in_the_run_s_container_and_one_that_cannot_build_stops_with_env_unavailable(self):
        run = stage()
        with checkout(SKILL) as root, patch.object(verify, "DISPATCH") as dispatch:
            run.workspace.root = root
            self.assertEqual(verify.drive(state(root), run)["stop"], "")
            self.assertEqual(run.workspace.prepared, ["a" * 40])
            dispatch.invoke.assert_called_once()
        for failure, halt in ((ContainerUnavailable("Setup command 'npm ci' exited with 1."), "env_unavailable"),
                              (WorkspaceError("The pinned base tracks no contract."), "git_failed")):
            with self.subTest(halt=halt):
                run = stage(workspace=Workspace(unavailable=failure))
                with checkout(SKILL) as root, patch.object(verify, "DISPATCH") as dispatch:
                    run.workspace.root = root
                    update = verify.drive(state(root), run)
                self.assertEqual((update["halt"], update["path"]), (halt, ["verify"]))
                dispatch.invoke.assert_not_called()
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
                prompt = assignment(node, state(plan=PlanState(slice=slice_)), "Drive.", None)
                delivered = json.loads(prompt.partition("\n\nDelivery state:\n")[2].partition("\n")[0])
                self.assertEqual(delivered["plan"]["slice"]["criteria"], criteria)

    def test_vf6_a_contract_criterion_is_driven_against_the_related_service_and_its_capture_is_posted(self):
        """VF6: the verifier gets the assumptions, the contract criterion and each related service started from its pinned
        checkout. A wrong plan's contract criterion fails in the drive, and the posted evidence names the capture of the call."""
        from foreman.delivery import stage_text

        assumption = {"fact": "The engine serves a deal at /api/deals/{id}.",
                      "citation": {"repository": "org/engine", "path": "routes/deals.py"}}
        criterion = {"text": "The deal page shows the engine's deal.",
                     "proof": {"kind": "contract", "repository": "org/engine", "assumption": assumption["fact"]}}
        slice_ = {"title": "Show a deal", "stateful": False, "criteria": [criterion], "non_goals": [], "removed_tests": [],
                  "files": [], "screen": "", "properties": []}
        related = RelatedCheckout(repository="org/engine", commit="e" * 40, checkout="/state/foreman/runs/a/b/8.related/org/engine")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".foreman").mkdir()
            (root / ".foreman/contract.yaml").write_text(
                f"check: 'true'\nverify_skill: {SKILL}\ntest_report: junit.xml\nimage: node:22\nsetup: []\nsecrets: []\n"
                "start: {command: 'true', health: 'true', timeout_seconds: 60}\n"
                "production_check: {command: 'true', not_deployed_exit: 3, timeout_seconds: 60}\n"
                "mutation: {command: cat, sample_cap: 1}\n"
                "related_repositories: [{repository: org/engine, start: 'npm run serve'}]\n")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "c"], cwd=root, check=True)
            driving = state(str(root), related=[related], plan=PlanState(slice=slice_, assumptions=[assumption]))
            context, _, _ = STAGES["verify"].prepare(driving, Workspace(root=str(root)), Forge())
        instruction = " ".join((stage_text("verify") + context).split())
        for text in ("the plan's assumptions and the related services the contract starts",
                     "A contract criterion calls the related repository's real service started from its pinned checkout, or its "
                     "recorded contract at that commit, never a mock.",
                     f"org/engine at {'e' * 40}: {related.checkout}, started with: npm run serve"):
            self.assertIn(text, instruction)
        prompt = assignment("verify", driving, "Drive.", None)
        delivered = json.loads(prompt.partition("\n\nDelivery state:\n")[2].partition("\n")[0])
        self.assertEqual((delivered["plan"]["assumptions"], delivered["plan"]["slice"]["criteria"]), ([assumption], [criterion]))
        called = {"criterion": criterion["text"], "result": "failed", "evidence": "captures/engine-deal-call.har",
                  "observation": "GET /api/deals/7 on the engine started from its pinned checkout returned 404."}
        run = stage(applied={"path": ["verify"], "verify": VerifyState(outcome="driven", tested_head=HEAD, criteria=[called])})
        with checkout(SKILL) as root, patch.object(verify, "DISPATCH"):
            run.workspace.root = root
            driven = verify.drive(state(root), run)
            verify.post(state(root, verify=driven["verify"]), run)
        body, = run.forge.posted
        self.assertIn(f"- failed: {criterion['text']}\n  Observed: {called['observation']}\n  Evidence: {called['evidence']}", body)

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
