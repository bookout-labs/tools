import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from foreman.dispatch import InvalidOutput
from foreman.github import IssueComment
from foreman.spec_check import AcceptedSpecCheck, SpecHistory, SpecRound, accept
from foreman.stage_output import PlanState, SpecAuthor, SpecCandidate, SpecCheckOutput, parse_output
from foreman.delivery import run_delivery, stage_text
from foreman.dry_run import ScriptHandler
from foreman.skill_rules import git
from foreman.stage_output import InvalidSlicePlan, Slice
from foreman.stages import STAGES, clean_slices
from foreman.state import RunState, StartSettings
from foreman.steps import StageRun, spec
from foreman.workspace import LiveWorkspace, RelatedCheckout
from skill_fixture import skill_fixture
from test_spec_candidate import SLICE, WORKER_SLICE


FINDING = dict(id="f1", kind="contract", slice="Save", section="slice", summary="Keep IDs unique.", ruling_id=None)


def output(findings=(), notes=()):
    return parse_output(SpecCheckOutput, dict(findings=list(findings), notes=list(notes), human_question=None))


def candidate(**changes):
    raw = dict(slices=[SLICE], spec="Save with a unique ID.", work_kind="feature",
               author=SpecAuthor(stage="spec", model="gpt-test", family="gpt"))
    return SpecCandidate.model_validate({**raw, **changes})


def pinned_tree(root):
    """A tree with a regular file, a directory, a file symlink, a directory symlink and a submodule."""
    git(root, "init", "-q")
    (root / "pkg").mkdir()
    (root / "items.py").touch()
    (root / "pkg" / "mod.py").touch()
    os.symlink("items.py", root / "link.py")
    os.symlink("pkg", root / "linked")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Tree")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{git(root, 'rev-parse', 'HEAD')},sub")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Submodule")
    return git(root, "rev-parse", "HEAD")


class SpecCheckTests(unittest.TestCase):
    def test_common_validator_rejects_unsafe_file_lists_and_bad_dependencies(self):
        """SC4: a listed path must be a tracked regular file or absent at the pinned base, and dependencies name sibling
        titles without a cycle. A path under a tracked symlink or submodule resolves through it, so it is not absent."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = pinned_tree(root)
            workspace = LiveWorkspace(root)

            def clean(*slices):
                return clean_slices([Slice.model_validate({**WORKER_SLICE, **item}) for item in slices], workspace, base)

            self.assertEqual(clean({"files": ["items.py", "new.py", "pkg/mod.py", "pkg/new.py", "fresh/new.py", "items.py/new.py"]})[0]["files"],
                             ["items.py", "new.py", "pkg/mod.py", "pkg/new.py", "fresh/new.py", "items.py/new.py"])
            for path in ("pkg", "link.py", "linked", "linked/mod.py", "sub", "sub/mod.py", "/items.py",
                         str(root / "items.py"), "../items.py", "pkg/../items.py", "./items.py", "pkg//mod.py"):
                with self.subTest(path=path), self.assertRaises(InvalidSlicePlan) as raised:
                    clean({"files": [path]})
                self.assertEqual(raised.exception.reason["kind"], "file_path")
                self.assertIn(path, raised.exception.reason["detail"])
            first, second = {"title": "First"}, {"title": "Second", "depends_on": ["First"]}
            self.assertEqual([item["title"] for item in clean(second, first)], ["First", "Second"])
            for slices, kind in (((first, second, {"title": "Third", "depends_on": ["Fourth"]}), "unknown_dependency"),
                                 (({"title": "First", "depends_on": ["First"]},), "unknown_dependency"),
                                 (({**first, "depends_on": ["Second"]}, second), "dependency_cycle")):
                with self.subTest(kind=kind, slices=slices), self.assertRaises(InvalidSlicePlan) as raised:
                    clean(*slices)
                self.assertEqual(raised.exception.reason["kind"], kind)

    def test_the_last_allowed_rejection_of_a_file_list_or_cycle_reports_spec_cap(self):
        """SC4: within one start, each rejected draft is a plan revision, and the last one allowed halts spec_cap with no check."""
        cyclic = [{**WORKER_SLICE, "title": "First", "files": [], "depends_on": ["Second"]},
                  {**WORKER_SLICE, "title": "Second", "files": [], "depends_on": ["First"]}]
        for kind, slices in (("file_path", [{**WORKER_SLICE, "files": ["linked/mod.py"]}]), ("dependency_cycle", cyclic)):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                pinned_tree(root)
                script = ScriptHandler(triage="plan", slices=slices)
                nodes = []

                def handler(node, model, state, instruction):
                    nodes.append(node)
                    script.spec = f"Draft {nodes.count('spec')}"
                    return script(node, model, state, instruction)
                with skill_fixture() as (config, scratch):
                    result = run_delivery(handler, repo="org/repo", request="Save", config=config, scratch=scratch, checkout=root)
                self.assertEqual(result["halt"], "spec_cap")
                self.assertEqual(result["plan"].spec_failures, config.max_review_rounds)
                self.assertEqual(result["plan"].spec_rejection["kind"], kind)
                self.assertEqual(nodes, ["triage"] + ["spec"] * config.max_review_rounds)

    def first(self):
        return accept(SpecHistory(), output([FINDING]), candidate())

    def test_a_cleared_plan_proceeds_without_the_human_and_a_stop_ends_the_run_first(self):
        """SC5: the model's clearance alone moves a validated plan on. The human's stop, read at the spec entry, ends the run
        cancelled before any check, build or filing."""
        target = {"repo": "a/b", "issue": 8, "base": "a" * 40}
        checked = candidate(target=target)
        plan = checked.update(PlanState(triage="supplied_slice"), single=True)
        state = {"run": RunState(repo="a/b", issue=8, base_sha="a" * 40), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "path": [],
                 "plan": plan.model_copy(update={"spec_history": {"rounds": accept(SpecHistory(), output(), checked).history.rounds}})}
        for comments, stop in (([], None), (["foreman stop"], "cancelled")):
            with self.subTest(stop=stop):
                forge = Mock(**{"ready_labeled_at.return_value": None, "issue_comment_records.return_value": [
                    IssueComment.model_validate({"user": {"login": "fixture-owner"}, "body": body}) for body in comments]})
                run = StageRun(workspace=Mock(), forge=forge, check_log=None, max_rounds=3, start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"),
                               dispatch=Mock(side_effect=AssertionError("no worker call")), apply=Mock())
                ended = spec.compile_stage(run).invoke(state)
                self.assertEqual((ended.get("stop"), ended.get("halt"), ended.get("path")), (stop, None, ["spec"] if stop else []))
                self.assertTrue(SpecHistory.cleared(ended["plan"]))
                self.assertEqual({call[0] for call in forge.method_calls}, {"issue_comment_records", *(["ready_labeled_at"] if stop else [])})
                self.assertNotIn("human_approval", PlanState.model_fields)

    def test_a_slice_whose_changes_belong_in_a_related_repository_stops_before_the_check(self):
        """SC1 and SC2: a related repository is a read root, never a write scope. Validate stops a slice scoped to it with
        foreign_repository before any paid check, and the checker reads the related checkout at its pin to catch the
        implied case, including a path that exists in both repositories."""
        related = RelatedCheckout(repository="a/api", commit="c" * 40, checkout="/state/foreman/runs/a/b/8.related/a/api")
        state = {"run": RunState(repo="a/b", issue=8, base_sha="a" * 40), "related": [related], "path": [],
                 "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"),
                 "plan": PlanState(triage="supplied_slice", slice={**SLICE, "depends_on": [], "blocker_numbers": [], "repository": "a/api"})}
        forge = Mock(**{"ready_labeled_at.return_value": None, "issue_comment_records.return_value": []})
        dispatch = Mock(side_effect=AssertionError("no worker call"))
        run = StageRun(workspace=Mock(), forge=forge, check_log=None, max_rounds=3, start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), dispatch=dispatch, apply=Mock())
        ended = spec.compile_stage(run).invoke(state)
        self.assertEqual((ended["halt"], ended["halt_detail"].split(":")[0], ended["path"]), ("invalid_output", "foreign_repository", ["spec"]))
        dispatch.assert_not_called()
        workspace = Mock(**{"root.resolve.return_value": "/state/foreman/runs/a/b/8"})
        instruction = " ".join((stage_text("spec_check") + STAGES["spec_check"].prepare(state, workspace, forge)[0]).split())
        for text in ("the pinned target checkout and the related repository checkouts",
                     "Confirm each slice's changes belong to the target repository, including when a matching path exists in "
                     "Foreman's repository or a related repository.",
                     f"a/api at {'c' * 40}: {related.checkout}"):
            self.assertIn(text, instruction)

    def test_native_fixture_and_contradictions(self):
        raw = json.loads(Path("tests/fixtures/spec-check.json").read_text())
        checked = parse_output(SpecCheckOutput, raw)
        self.assertEqual(accept(SpecHistory(), checked, candidate()).verdict, "gaps")
        self.assertEqual(accept(SpecHistory(), output(), candidate()).verdict, "clear")
        for verdict in ("clear", "gaps"):
            with self.assertRaises(InvalidOutput):
                parse_output(SpecCheckOutput, {**raw, "verdict": verdict})
        for summary in ("", " \t"):
            with self.assertRaises(InvalidOutput):
                output([{**FINDING, "summary": summary}])
        self.assertEqual(output([{**FINDING, "summary": "  Valid.  "}]).findings[0].summary, "Valid.")

    def test_clear_approval_is_bound_and_round_history_roundtrips(self):
        accepted = accept(SpecHistory(), output(notes=[FINDING]), candidate())
        self.assertEqual(accepted.verdict, "clear")
        self.assertTrue(accepted.approval.matches(candidate()))
        self.assertFalse(accepted.approval.matches(candidate(spec="Changed")))
        self.assertEqual(SpecHistory.model_validate_json(accepted.history.model_dump_json()), accepted.history)
        self.assertEqual(accepted.history.rounds[0].notes[0].id, "f1")
        self.assertEqual(AcceptedSpecCheck.model_validate_json(accepted.model_dump_json()), accepted)
        for history, approval in ((self.first().history, accepted.approval),
                                  (accepted.history, None),
                                  (accepted.history, {"revision": "0" * 64, "round": 1}),
                                  (accepted.history, {"revision": candidate().revision, "round": 2})):
            with self.subTest(approval=approval), self.assertRaisesRegex(ValueError, "approval must match"):
                AcceptedSpecCheck(history=history, approval=approval)
            raw = dict(history=json.loads(history.model_dump_json()), approval=approval.model_dump() if hasattr(approval, "model_dump") else approval)
            with self.assertRaisesRegex(ValueError, "approval must match"):
                AcceptedSpecCheck.model_validate_json(json.dumps(raw))

    def test_rounds_store_only_the_checked_candidate_and_its_report(self):
        raw = self.first().history.model_dump()
        for extra in ("resolutions", "interdiff"):
            entry = {**raw["rounds"][0], extra: {}}
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
                SpecRound(**entry)

    def test_targets_missing_renamed_and_duplicate_titles(self):
        for payload in (output([{**FINDING, "slice": "Missing"}]), output(notes=[{**FINDING, "slice": "Missing"}])):
            with self.assertRaisesRegex(InvalidOutput, "unknown candidate slice"):
                accept(SpecHistory(), payload, candidate())
        renamed = candidate(slices=[{**SLICE, "title": "Renamed"}])
        with self.assertRaisesRegex(InvalidOutput, "unknown candidate slice"):
            accept(self.first().history, output([FINDING]), renamed)
        accepted = accept(self.first().history, output([{**FINDING, "slice": "Renamed"}]), renamed)
        self.assertEqual(accepted.verdict, "gaps")
        self.assertEqual(accept(accepted.history, output(), renamed).verdict, "clear")
        with self.assertRaisesRegex(ValueError, "titles must be unique"):
            candidate(slices=[SLICE, SLICE])

    def test_a_plan_level_finding_names_no_slice(self):
        # App #1067 run b: a clear verdict whose one note concerns the plan's spec, not a slice.
        plan_note = dict(id="N1", kind="proof-obligation", section="spec", summary="Name the proof for the whole plan.", ruling_id=None)
        accepted = accept(SpecHistory(), output(notes=[plan_note]), candidate())
        self.assertEqual(accepted.verdict, "clear")
        self.assertTrue(accepted.approval.matches(candidate()))
        self.assertEqual(SpecHistory.model_validate_json(accepted.history.model_dump_json()), accepted.history)
        work_kind = dict(plan_note, id="f2", section="work_kind")
        self.assertEqual(accept(SpecHistory(), output([work_kind]), candidate()).verdict, "gaps")
        # The schema itself rules out an invented slice on a plan finding and a missing one on a slice finding.
        for bad in ({**plan_note, "slice": "plan"}, {**FINDING, "section": "spec"}, {k: v for k, v in FINDING.items() if k != "slice"}):
            with self.subTest(bad=bad), self.assertRaises(InvalidOutput):
                output(notes=[bad])
        shapes = SpecCheckOutput.model_json_schema()["$defs"]
        self.assertEqual(shapes["PlanFinding"]["properties"]["section"]["enum"], ["spec", "work_kind"])
        self.assertNotIn("slice", shapes["PlanFinding"]["properties"])
        self.assertIn("slice", shapes["SliceFinding"]["required"])

    def test_ids_are_unique_within_a_round_only(self):
        for payload in (dict(findings=[FINDING, FINDING]), dict(findings=[FINDING], notes=[FINDING])):
            with self.subTest(payload=payload), self.assertRaisesRegex(InvalidOutput, "unique within a round"):
                accept(SpecHistory(), output(**payload), candidate())
        # Engine #1225: round 3 repeated an earlier note's ID. Each round reports on its own candidate.
        noted = accept(SpecHistory(), output(notes=[FINDING]), candidate())
        changed = candidate(spec="Changed")
        second = accept(noted.history, output([FINDING]), changed)
        self.assertEqual(second.verdict, "gaps")
        for payload in (output(notes=[FINDING]), output([{**FINDING, "summary": "Different gap."}])):
            with self.subTest(payload=payload):
                third = accept(second.history, payload, candidate(spec="Again"))
                self.assertEqual(len(third.history.rounds), 3)
        self.assertEqual(accept(second.history, output(notes=[FINDING]), changed).verdict, "clear")

    def test_verdict_and_clearance_follow_the_latest_check_of_a_revision(self):
        gaps = self.first()
        self.assertEqual([f.id for f in gaps.findings], ["f1"])
        self.assertFalse(gaps.history.clears(candidate().revision))
        cleared = accept(gaps.history, output(), candidate())
        self.assertTrue(cleared.history.clears(candidate().revision))
        self.assertTrue(cleared.approval.matches(candidate()))
        reopened = accept(cleared.history, output([FINDING]), candidate())
        self.assertFalse(reopened.history.clears(candidate().revision))
        self.assertFalse(cleared.history.clears(candidate(spec="Unchecked").revision))
        plan = PlanState(spec_candidate=candidate().record(), spec_history={"rounds": cleared.history.rounds})
        self.assertTrue(SpecHistory.cleared(plan))
        self.assertFalse(SpecHistory.cleared(plan.model_copy(update={"spec_candidate": None})))

    def test_settled_rulings_turn_findings_into_notes_and_unknown_rulings_fail(self):
        past = SpecHistory(settled=[dict(id="design:1", text="Use a unique ID.")])
        current = candidate()
        accepted = accept(past, output([{**FINDING, "ruling_id": "design:1"}]), current)
        self.assertEqual(accepted.verdict, "clear")
        self.assertEqual(accepted.history.rounds[0].notes[0].id, "f1")
        self.assertEqual(accepted.history.settled, past.settled)
        for payload in (output([{**FINDING, "ruling_id": "unknown"}]), output(notes=[{**FINDING, "ruling_id": "unknown"}]),
                        output([{**FINDING, "ruling_id": "finding:f1"}])):
            with self.subTest(payload=payload), self.assertRaisesRegex(InvalidOutput, "unknown settled ruling"):
                accept(self.first().history, payload, current)

    def test_candidate_snapshot_is_preserved(self):
        current = candidate()
        accepted = accept(SpecHistory(), output(), current)
        checked_revision = accepted.history.rounds[0].candidate["revision"]
        current.slices[0].acceptance = "Changed after acceptance"
        self.assertFalse(accepted.approval.matches(current))
        self.assertEqual(accepted.history.rounds[0].candidate["revision"], checked_revision)
        raw = json.loads(accepted.history.model_dump_json())
        raw["rounds"][0]["candidate"]["spec"] = "Tampered"
        with self.assertRaises(ValueError):
            SpecHistory.model_validate(raw)

