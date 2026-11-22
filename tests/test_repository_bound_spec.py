"""Exercise repository admission and candidate-bound approval with two local Git trees."""

from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.adapters import Outcome
from foreman.delivery import LiveRun
from foreman.dry_run import CLEAR, SCRIPTED_USAGE, ScriptHandler
from foreman.github import IssueComment, IssueBlocker
from foreman.runner import AnswerError, run_live_delivery
from foreman.skill_rules import git
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_skills
from test_runner import EMPTY_SLICE, FakeGitHub, FakeWorkspace, slice_body


TARGET = "Fixture/Index"
OTHER = "Fixture/Tool"
EDIT = {**EMPTY_SLICE, "uses": [{"name": "index", "exists": True, "path": "index.py"}]}


class Checkout(FakeWorkspace):
    has_tracked_file = LiveWorkspace.has_tracked_file
    validate_origin = LiveWorkspace.validate_origin

    def pin_base(self, base):
        return git(self.root, "rev-parse", "HEAD")

    def create_branch(self, branch, base, *, pinned=""):
        super().create_branch(branch, base, pinned=pinned)
        self.current = type(self.current)(pinned, True, branch)


class Forge(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.rulings = []
        self.builds = []

    def issue_comment_records(self, repo, number):
        return list(self.rulings)

    def open_pull_request(self, repo, head, base, title, body):
        self.builds.append((repo, head, base))

    def arm_auto_merge(self, *args):
        receipt = super().arm_auto_merge(*args)
        return receipt.model_copy(update={"status": "MERGED"})

    def ruling(self, body, login="fixture-owner"):
        self.rulings.append(IssueComment.model_validate({"user": {"login": login}, "body": body}))


class RepositoryBoundSpecTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.target = root / "index"
        self.tool = root / "tool"
        for repo, folder in ((TARGET, self.target), (OTHER, self.tool)):
            folder.mkdir()
            git(folder, "init", "--quiet", "-b", "main")
            git(folder, "remote", "add", "origin", f"https://github.com/{repo}")
            (folder / "index.py").write_text(f"OWNER = {repo!r}\n")
            (folder / "link.py").symlink_to("index.py")
            if folder == self.tool:
                (folder / "tool.py").touch()
            git(folder, "add", ".")
            git(folder, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture base")
        self.base = git(self.target, "rev-parse", "HEAD")
        self.forge = Forge()
        self.workspace = Checkout(self.target)
        self.forge.base = self.base
        self.forge.body = slice_body(EDIT)
        self.checkpoint = root / "run.sqlite3"
        self.script = ScriptHandler(slice=EDIT)
        self.calls = []

    def invoke(self, check=None, *, answer=None, draft=None):
        def dispatch(stage, model, state, instruction):
            self.calls.append(stage)
            if stage == "spec" and draft:
                return Outcome("ran", draft(), usage=SCRIPTED_USAGE)
            if stage == "spec_check" and check:
                return Outcome("ran", check(model, state, instruction), usage=SCRIPTED_USAGE)
            if stage == "implement":
                self.workspace.current = type(self.workspace.current)(self.base, False, self.workspace.current.branch)
            return self.script(stage, model, state, instruction)

        return run_live_delivery(repo=TARGET, issue=8, check="exit 0", dispatcher=dispatch,
            run=LiveRun(self.forge, self.workspace, skills=fixture_skills, human_login="fixture-owner"),
            checkpoint=self.checkpoint, answer=answer)

    def test_admission_rejects_foreign_issue_and_open_parent_blocker_before_workers(self):
        with patch.object(self.forge, "load_issue", return_value={"repo": OTHER, "number": 8,
                "title": "Foreign issue", "body": self.forge.body, "labels": ["slice"]}):
            self.assertEqual(self.invoke()["halt"], "invalid_output")
        self.assertEqual(self.calls, [])
        self.checkpoint = self.checkpoint.with_name("blocked.sqlite3")
        self.forge.labels = ["factory"]
        blocker = IssueBlocker(number=7, title="Prerequisite", repository_url=f"https://api.github.com/repos/{TARGET}",
            html_url=f"https://github.com/{TARGET}/issues/7", state="open")
        with patch.object(self.forge, "blockers", return_value=[blocker]):
            self.assertEqual(self.invoke()["stop"], "blocked")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.forge.builds, [])

    def test_explicit_foreign_slice_stops_before_independent_check(self):
        self.forge.body = slice_body({**EDIT, "repository": OTHER})
        result = self.invoke()
        self.assertEqual(result["halt"], "invalid_output")
        self.assertIn("foreign_repository", result["halt_detail"])
        self.assertNotIn("spec_check", self.calls)
        self.assertEqual(self.forge.builds, [])
        self.assertFalse(any("slice" in labels for _, _, labels in self.forge.created))

    def test_tree_uses_only_the_pinned_tracked_regular_files(self):
        (self.target / "untracked.py").touch()
        self.assertTrue(self.workspace.has_tracked_file(self.base, "index.py"))
        self.assertTrue(LiveWorkspace(self.tool).has_tracked_file(git(self.tool, "rev-parse", "HEAD"), "tool.py"))
        for path in ("tool.py", "untracked.py", "link.py", "./index.py", "../index.py", str(self.target / "index.py")):
            with self.subTest(path=path):
                self.assertFalse(self.workspace.has_tracked_file(self.base, path))

    def test_checker_rejects_implied_foreign_intent_and_correction_merges(self):
        self.forge.body = slice_body({**EDIT, "acceptance": "Update the tool's index."})
        self.script.slices = [EDIT]
        revisions = []

        def check(model, state, instruction):
            self.assertIn(str(self.target), instruction)
            self.assertIn(self.base, instruction)
            self.assertIn(TARGET, instruction)
            candidate = state["plan"].spec_candidate
            self.assertEqual(candidate["target"], {"repo": TARGET.lower(), "issue": 8, "base": self.base})
            self.assertNotEqual(model.family, candidate["author"]["family"])
            self.assertEqual(self.forge.builds, [])
            self.assertEqual(self.forge.created, [])
            revisions.append(candidate["revision"])
            if len(revisions) == 1:
                return {**CLEAR.model_dump(), "findings": [{**FINDING, "kind": "scope",
                    "slice": EDIT["title"], "summary": "Acceptance refers to the tool repository."}]}
            self.forge.ruling(f"foreman stop {revisions[0]}")
            self.forge.ruling(f"foreman stop {revisions[1]}", login="untrusted")
            self.forge.ruling(f"we're good {revisions[1]}")
            return CLEAR.model_dump()

        result = self.invoke(check)
        self.assertNotEqual(*revisions)
        self.assertEqual(result["plan"].spec_failures, 1)
        self.assertEqual(result["plan"].human_approval, revisions[-1])
        self.assertEqual(result["merge"].receipt["status"], "MERGED")
        self.assertEqual(result["merge"].receipt["source_head"], result["checks"].verified_head)
        self.assertEqual(self.calls.count("implement"), 1)

    def test_authenticated_stop_blocks_build_and_child_writes(self):
        for entry in ("stored", "drafted"):
            with self.subTest(entry=entry):
                self.checkpoint = self.checkpoint.with_name(f"{entry}.sqlite3")
                if entry == "drafted":
                    self.forge.labels = ["factory"]
                    self.script.triage = "spec"
                    self.script.slices = [EDIT]

                def check(model, state, instruction):
                    self.forge.ruling(f"foreman stop {state['plan'].spec_candidate['revision']}")
                    return CLEAR.model_dump()

                result = self.invoke(check)
                self.assertEqual(result["stop"], "cancelled")
                self.assertEqual(self.forge.builds, [])
                self.assertEqual(self.forge.created, [])
                self.assertNotIn("implement", self.calls)

    def test_same_rejection_counts_once_and_distinct_defects_reach_the_cap(self):
        self.forge.labels = ["factory"]
        self.script.triage = "spec"
        invalid = {**EDIT, "uses": [{"name": "other", "exists": True, "path": "tool.py"}]}
        first = {"spec": "First draft", "spec_gate": "ready", "slices": [invalid], "questions": []}
        plans = iter([first, first, {**first, "spec": "Second draft"}, {**first, "spec": "Third draft"}])
        result = self.invoke(draft=lambda: next(plans))
        self.assertEqual(result["halt"], "spec_cap")
        self.assertEqual(result["plan"].spec_failures, 3)
        self.assertEqual(self.calls.count("spec"), 4)
        self.assertNotIn("spec_check", self.calls)
        self.assertFalse(any("slice" in labels for _, _, labels in self.forge.created))

    def test_malformed_checker_output_cannot_allocate_a_build(self):
        result = self.invoke(lambda *_: {"findings": [], "notes": [], "verdict": "clear"})
        self.assertEqual(result["halt"], "invalid_output")
        self.assertNotIn("implement", self.calls)
        self.assertEqual(self.forge.builds, [])

    def test_stale_or_spoofed_answer_cannot_resume_the_checker(self):
        def ask(model, state, instruction):
            if state.get("human_answers"):
                self.assertEqual(state["human_answers"], ["Preserve index order"])
                return CLEAR.model_dump()
            return {**CLEAR.model_dump(), "human_question": "What order should the index use?"}

        paused = self.invoke(ask)
        self.assertEqual(paused["stop"], "needs_human")
        revision = re.search(r"foreman answer ([0-9a-f-]{71})", paused["halt_detail"])[1]
        self.forge.ruling(f"foreman answer {'0' * 64}\nPreserve index order")
        self.forge.ruling(f"foreman answer {revision}\nPreserve index order", login="untrusted")
        with self.assertRaises(AnswerError):
            self.invoke(ask, answer="Preserve index order")
        self.assertEqual(self.forge.builds, [])
        self.forge.ruling(f"foreman answer {revision}\nPreserve index order")
        self.assertEqual(self.invoke(ask)["merge"].receipt["status"], "MERGED")


FINDING = {"id": "scope", "kind": "scope", "section": "slice", "ruling_id": None}
