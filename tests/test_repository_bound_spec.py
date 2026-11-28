"""Exercise repository admission and candidate-bound approval with two local Git trees."""

from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.adapters import Outcome
from foreman.delivery import LiveRun
from foreman.dry_run import CLEAR, SCRIPTED_USAGE, ScriptHandler
from foreman.github import IssueBlocker
from foreman.runner import AnswerError, issue_thread, run_live_delivery, saved_workspace
from foreman.skill_rules import git
from foreman.spec_check import SpecHistory
from foreman.stage_output import SpecCandidate, StoredSlice, load_stored_slice
from foreman.steps import spec as spec_step
from foreman.workspace import LiveWorkspace
from skill_fixture import fixture_skills
from publication_fixture import VERIFY_SKILL_PATH
from test_runner import EMPTY_SLICE, FakeGitHub, FakeWorkspace, slice_body


TARGET = "Fixture/Index"
OTHER = "Fixture/Tool"
EDIT = {**EMPTY_SLICE, "files": ["index.py"]}
# A drafted plan with one slice is built by its own run, so a plan that files children has a second slice.
SECOND = {**EDIT, "title": "Second edit"}


class Checkout(FakeWorkspace):
    tracked_path = LiveWorkspace.tracked_path
    contains_commit = LiveWorkspace.contains_commit
    validate_origin = LiveWorkspace.validate_origin

    def pin_base(self, base):
        return git(self.root, "rev-parse", "HEAD")

    def create_branch(self, branch, base, *, pinned=""):
        super().create_branch(branch, base, pinned=pinned)
        self.current = type(self.current)(pinned, True, branch)


class Forge(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.builds = []

    def open_pull_request(self, repo, head, base, title, body):
        self.builds.append((repo, head, base))
        super().open_pull_request(repo, head, base, title, body)



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
            else:
                (folder / VERIFY_SKILL_PATH).parent.mkdir(parents=True)
                (folder / VERIFY_SKILL_PATH).write_text("# Verify the index\n")
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

    def invoke(self, check=None, *, answer=None, draft=None, issue=8, review=None):
        def dispatch(stage, model, state, instruction):
            self.calls.append(stage)
            if stage == "spec" and draft:
                return Outcome("ran", {"assumptions": [], **draft()}, usage=SCRIPTED_USAGE)
            if stage == "spec_check" and check:
                return Outcome("ran", check(model, state, instruction), usage=SCRIPTED_USAGE)
            if stage == "review" and review:
                review(instruction)
            if stage == "implement":
                self.workspace.current = type(self.workspace.current)(self.base, False, self.workspace.current.branch)
            return self.script(stage, model, state, instruction)

        if saved_workspace(self.checkpoint, issue_thread(TARGET, issue)) is None:
            self.forge.ready_again(issue)
        return run_live_delivery(repo=TARGET, issue=issue, dispatcher=dispatch,
            run=LiveRun(self.forge, self.workspace, skills=fixture_skills),
            checkpoint=self.checkpoint, answer=answer)

    def test_admission_rejects_foreign_issue_and_open_parent_blocker_before_workers(self):
        with patch.object(self.forge, "load_issue", return_value={"repo": OTHER, "number": 8,
                "title": "Foreign issue", "body": self.forge.body, "labels": ["slice"]}):
            self.assertEqual(self.invoke()["halt"], "invalid_output")
        self.assertEqual(self.calls, [])
        self.checkpoint = self.checkpoint.with_name("blocked.sqlite3")
        self.forge.labels = ["Ready"]
        blocker = IssueBlocker(number=7, title="Prerequisite", repository_url=f"https://api.github.com/repos/{TARGET}",
            html_url=f"https://github.com/{TARGET}/issues/7", state="open")
        with patch.object(self.forge, "blockers", return_value=[blocker]):
            self.assertEqual(self.invoke()["stop"], "blocked")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.forge.builds, [])

    def test_explicit_foreign_slice_stops_before_independent_check(self):
        self.script.triage = "question"
        self.forge.body = slice_body({**EDIT, "repository": OTHER})
        result = self.invoke()
        self.assertEqual(result["halt"], "invalid_output")
        self.assertIn("foreign_repository", result["halt_detail"])
        self.assertNotIn("spec_check", self.calls)
        self.assertEqual(self.forge.builds, [])
        self.assertFalse(any("slice" in labels for _, _, labels in self.forge.created))

    def test_tree_uses_only_the_pinned_tracked_regular_files(self):
        (self.target / "untracked.py").touch()
        self.assertEqual(self.workspace.tracked_path(self.base, "index.py"), "file")
        self.assertEqual(LiveWorkspace(self.tool).tracked_path(git(self.tool, "rev-parse", "HEAD"), "tool.py"), "file")
        for path, kind in (("tool.py", "absent"), ("untracked.py", "absent"), ("link.py", "symlink"), ("link.py/index.py", "symlink"),
                           ("./index.py", None), ("../index.py", None), (str(self.target / "index.py"), None)):
            with self.subTest(path=path):
                self.assertEqual(self.workspace.tracked_path(self.base, path), kind)

    def test_supplied_file_list_fails_in_the_pinned_tree_before_checking(self):
        folder = self.target / "tracked"
        folder.mkdir()
        (folder / "file.py").touch()
        git(self.target, "add", "tracked/file.py")
        git(self.target, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Add tracked directory")
        self.base = self.forge.base = git(self.target, "rev-parse", "HEAD")
        self.script.triage = "question"
        self.script.reviews = ["approve"] * 3
        for path in ("link.py", "tracked", "../index.py"):
            with self.subTest(path=path):
                self.checkpoint = self.checkpoint.with_name(f"{path.replace('/', '-')}.sqlite3")
                self.workspace = Checkout(self.target)
                self.calls.clear()
                self.forge.body = slice_body({**EDIT, "files": ["index.py", path]})
                result = self.invoke(draft=lambda: {"spec": "Correction", "spec_gate": "ready", "slices": [EDIT], "questions": []})
                self.assertEqual(self.calls[:3], ["triage", "spec", "spec_check"])
                self.assertEqual(result["plan"].spec_failures, 1)
                self.assertEqual(result["plan"].spec_candidate["slices"][0]["files"], ["index.py"])
                self.assertEqual(result["stop"], "merged")

    def test_filed_child_with_one_merged_blocker_passes_validate(self):
        """Admit owns a filed slice's blockers, so validate keeps its stored record and reads no blocker."""
        blocker = StoredSlice.model_validate({**EMPTY_SLICE, "title": "Earlier sibling"})
        number = self.forge.create_issue(TARGET, blocker.title, "", ["slice"], child=blocker, marker="")
        supplied = {**EDIT, "depends_on": [blocker.title], "blocker_numbers": [number]}
        self.forge.body = slice_body(supplied)
        self.forge.dependencies[8] = [number]
        self.forge.closed.add(number)
        self.forge.merged.add(number)
        # Spec never reads the blocker's body, so one that holds no slice record changes nothing.
        self.forge.bodies[TARGET, number] = "Edited by hand."
        self.script.reviews = ["approve"]

        def check(_model, state, instruction):
            self.assertEqual(state["plan"].spec_candidate["slices"], [supplied])
            return CLEAR.model_dump()

        result = self.invoke(check)
        self.assertEqual(self.calls[:3], ["triage", "spec_check", "implement"])
        self.assertEqual(result["plan"].spec_failures, 0)
        self.assertEqual(result["plan"].spec_candidate["slices"], [supplied])
        self.assertEqual(result["stop"], "merged")

    def test_changed_origin_on_resume_stops_before_another_dispatch(self):
        ask = lambda *_: {**CLEAR.model_dump(), "human_question": "What sort order?"}
        self.assertEqual(self.invoke(ask)["stop"], "needs_human")
        before = self.calls[:]
        git(self.target, "remote", "set-url", "origin", f"https://github.com/{OTHER}")
        self.assertEqual(self.invoke(ask)["halt"], "git_failed")
        self.assertEqual(self.calls, before)
        self.assertEqual(self.forge.builds, [])

    def test_checker_rejects_implied_foreign_intent_and_correction_merges(self):
        self.forge.body = slice_body({**EDIT, "criteria": [{"text": "Update the tool's index.", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": []})
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
            # The digest phrases are gone: neither stops nor approves, and the model's clearance moves the plan on.
            self.forge.ruling(f"foreman stop {revisions[0]}")
            self.forge.ruling(f"foreman approve {revisions[1]}")
            self.forge.ruling(f"we're good {revisions[1]}")
            self.forge.ruling("foreman stop", login="untrusted")
            return CLEAR.model_dump()

        result = self.invoke(check)
        self.assertNotEqual(*revisions)
        self.assertEqual(result["plan"].spec_failures, 1)
        self.assertNotIn("human_approval", result["plan"].model_dump())
        self.assertEqual(result["stop"], "merged")
        self.assertIn(result["checks"].verified_head, [call[3] for call in self.forge.merge_calls])
        self.assertEqual(self.calls.count("implement"), 1)

    def test_authenticated_stop_blocks_build_and_child_writes(self):
        """SC5: a stop the human posts while the plan is checked ends the run at implement's entry, before any build or filing."""
        for entry in ("stored", "drafted"):
            with self.subTest(entry=entry):
                self.checkpoint = self.checkpoint.with_name(f"{entry}.sqlite3")
                self.forge.rulings.clear()
                if entry == "drafted":
                    self.forge.labels = ["Ready"]
                    self.script.triage = "plan"
                    self.script.slices = [EDIT]

                def check(model, state, instruction):
                    self.forge.ruling("foreman stop")
                    return CLEAR.model_dump()

                result = self.invoke(check)
                self.assertEqual((result["stop"], result["path"][-1]), ("cancelled", "prepare"))
                self.assertEqual(self.forge.builds, [])
                self.assertEqual(self.forge.created, [])
                self.assertNotIn("implement", self.calls)

    def test_distinct_defects_reach_the_cap(self):
        self.forge.labels = ["Ready"]
        self.script.triage = "plan"
        invalid = {**EDIT, "files": ["link.py"]}
        first = {"spec": "First draft", "spec_gate": "ready", "slices": [invalid], "questions": []}
        plans = iter([first, {**first, "spec": "Second draft"}, {**first, "spec": "Third draft"}])
        result = self.invoke(draft=lambda: next(plans))
        self.assertEqual(result["halt"], "spec_cap")
        self.assertEqual(result["plan"].spec_failures, 3)
        self.assertEqual(self.calls.count("spec"), 3)
        self.assertNotIn("spec_check", self.calls)
        self.assertFalse(any("slice" in labels for _, _, labels in self.forge.created))

    def test_unchanged_invalid_draft_stops_instead_of_drafting_until_recursion_limit(self):
        self.forge.labels = ["Ready"]
        self.script.triage = "plan"
        invalid = {**EDIT, "files": ["link.py"]}
        draft = {"spec": "Same draft", "spec_gate": "ready", "slices": [invalid], "questions": []}
        result = self.invoke(draft=lambda: draft)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertEqual(result["plan"].spec_failures, 1)
        self.assertEqual(self.calls, ["triage", "spec", "spec"])
        self.assertFalse(any("slice" in labels for _, _, labels in self.forge.created))
        self.assertEqual(self.forge.builds, [])

    def test_checker_cannot_return_both_a_question_and_blocking_findings(self):
        output = {**CLEAR.model_dump(), "human_question": "Preserve existing order?",
                  "findings": [{**FINDING, "slice": EDIT["title"], "summary": "Clarify repository ownership."}]}
        verdicts = iter([output, CLEAR.model_dump()])
        corrected = {**EDIT, "criteria": [{"text": "Update the target index.", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": []}
        result = self.invoke(lambda *_: next(verdicts), draft=lambda: {
            "spec": "Correction", "spec_gate": "ready", "slices": [corrected], "questions": [],
        })
        self.assertEqual(result["stop"], "invalid_output")
        self.assertEqual(self.calls, ["triage", "spec_check"])
        self.assertEqual(self.forge.builds, [])

    def test_changed_draft_requires_a_fresh_answer_to_the_same_question(self):
        self.forge.labels = ["Ready"]
        self.script.triage = "plan"
        question = {"question": "Use the proposed order?", "why_code_unanswered": "The issue does not specify order.",
                    "checked_paths": ["index.py"]}
        initial = {"spec": "Proposed change", "spec_gate": "needs_human", "slices": [EDIT, SECOND], "questions": [question]}
        changed = {**initial, "slices": [{**EDIT, "criteria": [{"text": "Changed scope for the proposed order.", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": []}, SECOND]}
        steps = iter([initial, changed, {**changed, "spec_gate": "ready", "questions": []}])
        draft = lambda: next(steps)
        first = self.invoke(draft=draft)
        marker_a = re.search(r"foreman answer [0-9a-f-]{71}", first["halt_detail"])[0]
        self.forge.ruling(marker_a + "\nNo")
        self.forge.ruling(marker_a + "\nYes")
        second = self.invoke(draft=draft)
        marker_b = re.search(r"foreman answer [0-9a-f-]{71}", second["halt_detail"])[0]
        self.assertNotEqual(marker_a, marker_b)
        waiting = self.invoke(draft=draft)
        self.assertEqual(waiting["stop"], "needs_human")
        self.assertEqual(self.calls, ["triage", "spec", "spec"])
        self.forge.ruling(marker_b + "\nUse a stable sort")
        result = self.invoke(draft=draft)
        self.assertEqual([qa["answer"] for qa in result["plan"].human_qa], ["Yes", "Use a stable sort"])
        self.assertEqual(result["stop"], "children_filed")

    def test_the_checker_reads_the_humans_answer_to_the_drafts_question(self):
        self.forge.labels = ["Ready"]
        self.script.triage = "plan"
        question = {"question": "Use the proposed order?", "why_code_unanswered": "The issue does not specify order.",
                    "checked_paths": ["index.py"]}
        asked = {"spec": "Proposed change", "spec_gate": "needs_human", "slices": [EDIT, SECOND], "questions": [question]}
        drafts = iter([asked, {**asked, "spec_gate": "ready", "questions": []}])
        seen = []

        def check(_model, state, _instruction):
            seen.append((state["human_answers"], [(qa["stage"], qa["answer"]) for qa in state["plan"].human_qa]))
            scoped.append([qa.get("candidate") == state["plan"].spec_candidate["revision"] for qa in state["plan"].human_qa])
            return CLEAR.model_dump()

        scoped: list[list[bool]] = []
        first = self.invoke(check, draft=lambda: next(drafts))
        self.assertEqual(first["stop"], "needs_human")
        self.forge.ruling(re.search(r"foreman answer [0-9a-f-]{71}", first["halt_detail"])[0] + "\nUse a stable sort")
        result = self.invoke(check, draft=lambda: next(drafts))
        self.assertEqual(seen, [(["Use a stable sort"], [("spec", "Use a stable sort")])])
        # The draft's answer is saved scoped to the candidate and choice (target.yaml spec gate).
        self.assertEqual(scoped, [[True]])
        self.assertEqual(self.calls, ["triage", "spec", "spec", "spec_check"])
        self.assertEqual(result["stop"], "children_filed")
        child = load_stored_slice(self.forge.load_issue(TARGET, 101)["body"])
        ruling, = child.settled_rulings
        self.assertTrue(ruling.text.startswith("Use the proposed order?\n"))
        self.assertTrue(ruling.text.endswith("\nAnswer: Use a stable sort"))

    def test_checker_answer_settles_the_issue_for_later_candidates(self):
        question = "Should closed items stay hidden?"
        ruling = {"id": "spec:1", "text": f"{question}\nAnswer: Keep them hidden"}
        for later in ("same", "revised"):
            with self.subTest(later=later):
                self.checkpoint = self.checkpoint.with_name(f"settled-{later}.sqlite3")
                self.calls.clear()
                self.forge = Forge()
                self.forge.base, self.forge.body, self.forge.labels = self.base, slice_body(EDIT), ["Ready"]
                self.script.triage = "plan"
                drafts = iter([{"spec": "First", "spec_gate": "ready", "slices": [EDIT, SECOND], "questions": []},
                               {"spec": "Second", "spec_gate": "ready", "questions": [],
                                "slices": [{**EDIT, "criteria": [{"text": "Keep closed items hidden in the index.", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": []}, SECOND]}])
                seen = []

                def check(_model, state, _instruction):
                    plan = state["plan"]
                    seen.append((plan.spec_candidate["revision"], SpecHistory.model_validate(plan.spec_history)))
                    if len(seen) == 1:
                        return {**CLEAR.model_dump(), "human_question": question}
                    if len(seen) == 2 and later == "revised":
                        return {**CLEAR.model_dump(), "findings": [{**FINDING, "slice": EDIT["title"],
                                                                   "summary": "Acceptance must name the hidden items."}]}
                    return CLEAR.model_dump()

                first = self.invoke(check, draft=lambda: next(drafts))
                self.assertEqual(first["stop"], "needs_human")
                self.forge.ruling(re.search(r"foreman answer [0-9a-f-]{71}", first["halt_detail"])[0] + "\nKeep them hidden")
                result = self.invoke(check, draft=lambda: next(drafts))
                self.assertEqual(seen[0][1].settled, [])
                self.assertEqual([[r.model_dump() for r in history.settled] for _, history in seen[1:]], [[ruling]] * (len(seen) - 1))
                self.assertTrue(all(not history.rounds for _, history in seen))
                self.assertEqual(len({revision for revision, _ in seen}), 1 if later == "same" else 2)
                self.assertEqual(result["plan"].spec_failures, 0 if later == "same" else 1)
                self.assertEqual(self.calls.count("spec_check"), 2 if later == "same" else 3)
                self.assertNotEqual(result.get("halt"), "spec_cap")
                self.assertEqual(result["stop"], "children_filed")
                if later == "same":
                    def child_check(model, state, instruction):
                        inherited = SpecHistory.model_validate(state["plan"].spec_history).settled
                        self.assertEqual([item.record() for item in inherited], [ruling])
                        return CLEAR.model_dump()

                    passes = []
                    child = self.invoke(child_check, issue=101, review=passes.append)
                    self.assertEqual(child["stop"], "merged")
                    self.assertEqual(len(passes), 3)
                    for instruction in passes:
                        self.assertIn('"id": "spec:1"', instruction)
                        self.assertIn("Answer: Keep them hidden", instruction)

    def test_verdict_for_another_scope_blocks_both_effects(self):
        for entry in ("stored", "drafted"):
            with self.subTest(entry=entry):
                self.checkpoint = self.checkpoint.with_name(f"stale-{entry}.sqlite3")
                self.calls.clear()
                if entry == "drafted":
                    self.forge.labels = ["Ready"]
                    self.script.triage = "plan"
                    self.script.slices = [EDIT]

                real, guards = spec_step.check, []

                def stale(state, run):
                    # The saved verdict cleared a plan bound to another pinned base.
                    guards.append(entry)
                    plan = state["plan"]
                    approved = SpecCandidate.from_record(plan.spec_candidate)
                    changed = approved.model_copy(update={"target": {**approved.target, "base": "0" * 40}})
                    self.assertNotEqual(changed.revision, approved.revision)
                    return real({**state, "plan": changed.update(plan, single=entry == "stored")}, run)

                with patch.object(spec_step, "check", stale):
                    result = self.invoke()
                self.assertEqual(guards, [entry])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertEqual(self.forge.builds, [])
                self.assertFalse(any("slice" in labels for _, _, labels in self.forge.created))
                self.assertNotIn("implement", self.calls)
                self.assertFalse({"spec_approval", "spec_failures"} & result["plan"].model_dump().keys())

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
        self.assertEqual(self.invoke(ask)["stop"], "merged")


FINDING = {"id": "scope", "kind": "scope", "section": "slice", "ruling_id": None}
