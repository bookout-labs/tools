import tests

from foreman.state import ReviewState, PlanState, ChecksState, RunState, StartSettings, SCHEMA_VERSION
from foreman.dry_run import DryRun, SCRIPTED_USAGE

import json
import unittest
from dataclasses import replace

from pydantic import ValidationError

from foreman.adapters import Outcome
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import LiveRun, DEFAULT_TOKEN_CAP, compile_delivery, run_delivery
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.github import render_slice_body
from foreman.runner import checkpoint_state
from foreman.stage_output import load_stored_slice as _load_slice
from foreman.stage_output import CandidateSlice, InvalidSlicePlan, SliceProvenance, SpecAuthor, SpecCandidate, StoredSlice, SpecOutput, TriageOutput, parse_output
from foreman.contracts import InvalidOutput
from skill_fixture import skill_fixture
from foreman.skill_rules import git


SLICE = {
    "title": "Save", "criteria": [{"text": "Reject duplicate IDs", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": [], "stateful": True,
    "files": ["save.py"],
    "screen": "docs/screens/save.md", "properties": [{"name": "Unique ID", "constraint": "id_key"}],
    "depends_on": ["Database"], "blocker_numbers": [7],
}
# The worker's slice names no blocker numbers, and its plan has no other slice to depend on.
WORKER_SLICE = {**{key: value for key, value in SLICE.items() if key != "blocker_numbers"}, "depends_on": []}


class CandidateTests(unittest.TestCase):
    def candidate(self):
        return SpecCandidate(
            slices=[CandidateSlice.model_validate(SLICE)], spec="Save with a unique ID.", work_kind="feature",
            author=SpecAuthor(stage="spec", model="gpt-test", family="gpt"),
        )

    def test_grounded_question_contract_and_checkpoint(self):
        from foreman.dispatch import InvalidOutput
        from foreman.stages import Acceptance, accept_spec
        from foreman.workspace import LiveWorkspace
        from tempfile import TemporaryDirectory
        from pathlib import Path

        question = {"question": "Which policy?", "why_code_unanswered": "Code describes mechanics only.",
                    "checked_paths": ["policy.py"]}
        raw = {"spec": "Open policy decision", "spec_gate": "needs_human", "slices": None,
               "questions": [question]}
        for changes in ({"questions": []}, {"spec_gate": "ready"},
                        {"questions": [{**question, "question": " "}]},
                        {"questions": [{**question, "why_code_unanswered": " "}]},
                        {"questions": [{**question, "checked_paths": [" "]}]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                SpecOutput.model_validate({**raw, **changes})
        with self.assertRaises(ValidationError):
            SpecOutput.model_validate({k: v for k, v in raw.items() if k != "questions"})
        candidate = self.candidate()
        state = {"review": ReviewState(), "schema_version": SCHEMA_VERSION,
                 "run": RunState(repo="org/repo"), "start": StartSettings(human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"), "checks": ChecksState(check=""),
                 "path": [], "plan": candidate.update(PlanState(), single=False)}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "--quiet")
            git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-qm", "Empty tree")
            state["run"] = state["run"].model_copy(update={"base_sha": git(root, "rev-parse", "HEAD")})
            workspace = LiveWorkspace(root, "org/repo")
            with self.assertRaisesRegex(InvalidOutput, "checked_paths"):
                accept_spec(SpecOutput.model_validate(raw), Acceptance(state, workspace=workspace))
            (root / "policy.py").write_text("# Mechanics only\n")
            git(root, "add", "policy.py")
            git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-qm", "Tracked policy")
            state["run"] = state["run"].model_copy(update={"base_sha": git(root, "rev-parse", "HEAD")})
            update = accept_spec(SpecOutput.model_validate(raw), Acceptance(state, workspace=workspace))
            restored = checkpoint_state(json.loads(json.dumps({**state, **update}, default=lambda x: x.model_dump())))
            self.assertIsNone(restored["plan"].spec_candidate)
            self.assertEqual(restored["plan"].spec, raw["spec"])
            for text in (question["question"], question["why_code_unanswered"], "policy.py"):
                self.assertIn(text, update["human_question"])
            empty = SpecOutput.model_validate({**raw, "questions": [{**question, "checked_paths": []}]})
            accept_spec(empty, Acceptance(state, workspace=workspace))

    def test_revision_binds_all_content_and_ignores_mapping_order(self):
        self.assertEqual(SpecCandidate.from_record(json.loads(self.candidate().model_dump_json())), self.candidate())
        with self.assertRaises(ValidationError):
            SpecCandidate.model_validate({**self.candidate().model_dump(exclude={"revision"}), "approval": "clear"})
        raw = self.candidate().model_dump(exclude={"revision"})
        expected = SpecCandidate.model_validate(dict(reversed(list(raw.items())))).revision
        self.assertEqual(expected, self.candidate().revision)
        for key, value in (("repo", "org/other"), ("issue", 2), ("base", "b" * 40)):
            target = {"repo": "org/repo", "issue": 1, "base": "a" * 40}
            self.assertNotEqual(SpecCandidate.model_validate({**raw, "target": target}).revision,
                SpecCandidate.model_validate({**raw, "target": {**target, key: value}}).revision)
        for key, value in (("spec", "Changed"), ("work_kind", "refactoring"),
                           ("author", {"stage": "triage", "model": "claude-test", "family": "claude"})):
            self.assertNotEqual(SpecCandidate.model_validate({**raw, key: value}).revision, expected)
        for key, value in (("screen", ""), ("properties", []), ("criteria", [{"text": "Changed", "proof": {"kind": "test"}}]),
                           ("non_goals", ["Changed"]), ("removed_tests", [{"file": "save_test.py", "name": None, "reason": "Changed"}]),
                           ("stateful", False), ("files", []), ("depends_on", []), ("blocker_numbers", [])):
            changed = {**raw, "slices": [{**SLICE, key: value}]}
            self.assertNotEqual(SpecCandidate.model_validate(changed).revision, expected)

    def test_optional_stored_provenance_round_trips_through_issue_and_checkpoint(self):
        candidate = self.candidate()
        provenance = SliceProvenance(author=candidate.author, revision=candidate.revision)
        child = StoredSlice.model_validate({**SLICE, "work_kind": "bug_fix", "provenance": provenance.record()})
        restored = _load_slice(render_slice_body("Save safely.", child))
        self.assertEqual(restored.provenance, provenance)
        self.assertEqual(restored.work_kind, "bug_fix")
        with self.assertRaises(ValidationError):
            StoredSlice.model_validate({**SLICE, "provenance": {"author": candidate.author.record(), "revision": "bad"}})
        state = checkpoint_state({"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(repo="org/repo"), "start": StartSettings(human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"), "checks": ChecksState(check=""), "path": [], "plan": PlanState(slice=restored.record())})
        self.assertEqual(state["plan"].slice["provenance"], provenance.record())
        self.assertNotIn("provenance", StoredSlice.model_validate({**SLICE, "provenance": None}).record())

    def test_the_issue_body_renders_the_criteria_non_goals_and_removed_tests(self):
        """F1: the removed or renamed tests are stored with the slice and rendered in its body, beside its proofs and non-goals."""
        child = StoredSlice.model_validate({
            **SLICE, "non_goals": ["Existing IDs are not migrated."],
            "criteria": [*SLICE["criteria"], {"text": "Saving stays fast.", "proof": {"kind": "drive", "not_drivable": "No load fixture."}}],
            "removed_tests": [{"file": "tests/test_save.py", "name": "test_duplicate_ids_pass", "reason": "The issue makes duplicates an error."},
                              {"file": "tests/test_legacy_save.py", "name": None, "reason": "The issue retires the legacy save."}]})
        body = render_slice_body("Save safely.", child)
        self.assertIn("Criteria:\n0. Reject duplicate IDs (proof: test)\n1. Saving stays fast. (proof: drive, not drivable: No load fixture.)", body)
        self.assertIn("Non-goals:\n- Existing IDs are not migrated.", body)
        self.assertIn("Removed or renamed tests:\n- tests/test_save.py test_duplicate_ids_pass: The issue makes duplicates an error.\n"
                      "- tests/test_legacy_save.py: The issue retires the legacy save.", body)
        self.assertEqual(_load_slice(body), child)

    def test_actual_accepted_failover_authors(self):
        for stage in ("triage", "spec"):
            with self.subTest(stage=stage):
                config = load_config()
                nodes = {**config.nodes, stage: NodeConfig("coordinator", tuple(map(ModelSpec.parse, ("gpt-test", "claude-test"))))}
                script = ScriptHandler(triage="supplied_slice" if stage == "triage" else "plan",
                                       slices=[WORKER_SLICE])

                def handler(node, model, state, instruction):
                    if node == stage and model.name == "gpt-test":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    return script(node, model, state, instruction)
                with skill_fixture(replace(config, nodes=nodes)) as (configured, scratch):
                    result = run_delivery(handler, repo="org/repo", request="Save", config=configured, scratch=scratch)
                candidate = SpecCandidate.from_record(result["plan"].spec_candidate)
                self.assertEqual(candidate.author.record(), {"stage": stage, "model": "claude-test", "family": "claude"})
                attempts = [a for a in result["attempts"] if a["stage"] == stage]
                self.assertEqual([a["outcome"] for a in attempts], ["quota", "ran"])
                stored = result["plan"].slice if stage == "triage" else result["plan"].slices[0]
                self.assertEqual(stored["provenance"], {"revision": candidate.revision, "author": candidate.author.record()})
                self.assertIn("implement" if stage == "triage" else "spec_check", result["path"])

    def test_inherited_slice_author_survives_child_triage_and_spec(self):
        candidate = self.candidate()
        provenance = {"author": candidate.author.record(), "revision": candidate.revision}
        supplied = {**SLICE, "provenance": provenance, "depends_on": [], "blocker_numbers": []}
        for triage in ("supplied_slice", "plan"):
            with skill_fixture() as (config, scratch):
                result = run_delivery(ScriptHandler(triage=triage), repo="org/repo", request="Save",
                                      slice=supplied, config=config, scratch=scratch)
            bound = SpecCandidate.from_record(result["plan"].spec_candidate)
            self.assertEqual(bound.author.record(), provenance["author"])
            self.assertEqual(result["plan"].slice["provenance"]["revision"], bound.revision)
            self.assertEqual(bound.target, {"repo": "org/repo", "issue": 0, "base": "<sha>"})

    def test_a_seam_field_returns_the_draft_to_draft_before_any_checker_call(self):
        """SP3, seam half: the defect is a plan revision, with no repair turn and no checker call."""
        for field in ("builds", "uses"):
            with self.subTest(field=field):
                script = ScriptHandler(triage="plan", slices=[{**WORKER_SLICE, field: []}])
                nodes, rejections = [], []

                def handler(node, model, state, instruction):
                    nodes.append(node)
                    if node == "spec" and (rejection := state["plan"].spec_rejection):
                        rejections.append(rejection)
                        script.slices = [WORKER_SLICE]
                    return script(node, model, state, instruction)
                with skill_fixture() as (config, scratch):
                    result = run_delivery(handler, repo="org/repo", request="Save", config=config, scratch=scratch)
                self.assertEqual(rejections, [{"kind": "seam_field", "detail": f"slices.0.{field}: a slice has no builds or uses seams."}])
                self.assertEqual(nodes[:4], ["triage", "spec", "spec", "spec_check"])
                self.assertEqual(result["plan"].spec_failures, 1)
                self.assertEqual(result["plan"].spec_candidate["slices"][0]["files"], ["save.py"])

    def test_a_criterion_without_its_proof_returns_the_draft_to_draft_before_any_checker_call(self):
        """SP3, criterion half: a slice with no criterion, a criterion with no proof or an unknown proof kind, or a
        not-drivable mark with a blank reason is one typed plan defect, counted once, with no repair turn or checker call."""
        text = "Reject duplicate IDs"
        for criteria in ([], [{"text": text}], [{"text": text, "proof": {"kind": "review"}}],
                         [{"text": text, "proof": {"kind": "drive", "not_drivable": " "}}],
                         [{"text": text, "proof": {"kind": "test", "not_drivable": "No app."}}]):
            with self.subTest(criteria=criteria):
                script = ScriptHandler(triage="plan", slices=[{**WORKER_SLICE, "criteria": criteria}])
                nodes, rejections = [], []

                def handler(node, model, state, instruction):
                    nodes.append(node)
                    if node == "spec" and (rejection := state["plan"].spec_rejection):
                        rejections.append(rejection)
                        script.slices = [WORKER_SLICE]
                    return script(node, model, state, instruction)
                with skill_fixture() as (config, scratch):
                    result = run_delivery(handler, repo="org/repo", request="Save", config=config, scratch=scratch)
                self.assertEqual([rejection["kind"] for rejection in rejections], ["criterion_proof"])
                self.assertIn("slices.0.criteria", rejections[0]["detail"])
                self.assertIn("each proven by test, drive or contract", rejections[0]["detail"])
                self.assertEqual(nodes[:4], ["triage", "spec", "spec", "spec_check"])
                self.assertEqual(result["plan"].spec_failures, 1)

    def test_a_triage_slice_without_a_proof_fails_the_triage_answer(self):
        """A supplied slice from triage is one answer, so its schema defect gets triage's repair turn, never a half-formed validate."""
        answer = {"work_kind": "feature", "triage": "supplied_slice", "human_question": None,
                  "slice": {**WORKER_SLICE, "criteria": [{"text": "Reject duplicate IDs"}]}}
        with self.assertRaises(InvalidOutput) as raised:
            parse_output(TriageOutput, answer)
        self.assertNotIsInstance(raised.exception, InvalidSlicePlan)
        self.assertIn("slice.criteria.0.proof", str(raised.exception))
        self.assertEqual(parse_output(TriageOutput, {**answer, "slice": WORKER_SLICE}).slice.criteria, WORKER_SLICE["criteria"])

    def test_checkpoint_rejects_divergent_candidate_copies(self):
        candidate = self.candidate()
        state = {"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(repo="org/repo"), "start": StartSettings(human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"), "checks": ChecksState(check=""), "path": [], "plan": candidate.update(PlanState(), single=True)}
        self.assertIsNotNone(checkpoint_state(state))
        for field, value in (("criteria", [{"text": "Changed", "proof": {"kind": "test"}}]), ("provenance", {"author": candidate.author.record(), "revision": "0" * 64}),
                             ("provenance", {"author": {"stage": "spec", "model": "other", "family": "gpt"}, "revision": candidate.revision})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                checkpoint_state({**state, "plan": state["plan"].model_copy(update={"slice": {**state["plan"].slice, field: value}})})
        for key, value in (("work_kind", "bug_fix"), ("spec", "Changed")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                checkpoint_state({**state, "plan": state["plan"].model_copy(update={key: value})})
        for revision in (None, "", "bad", "0" * 64):
            raw = {**candidate.record(), "revision": revision}
            if revision is None:
                del raw["revision"]
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                checkpoint_state({**state, "plan": state["plan"].model_copy(update={"spec_candidate": raw})})
        with self.assertRaises(ValidationError):
            candidate.spec = "Changed"

    def test_failed_author_does_not_produce_a_candidate(self):
        with skill_fixture() as (config, scratch):
            result = run_delivery(lambda *_: Outcome("failed", detail="Malformed output", usage=SCRIPTED_USAGE),
                                  repo="org/repo", request="Save", config=config, scratch=scratch)
        self.assertEqual(result["halt"], "invalid_output")
        self.assertIsNone(result["plan"].spec_candidate)


    def test_without_methods_records_dispatch_author_independently_of_attempts(self):
        result = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=DryRun(), workspace=DryRun(), start=StartSettings(
            human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"))).invoke(
            {"review": ReviewState(), "run": RunState(repo="org/repo", issue=0, base_branch="main", base_sha="<sha>"), "start": StartSettings(human_login="fixture-owner", max_tokens=DEFAULT_TOKEN_CAP, max_budget_usd="3"), "checks": ChecksState(check=""), "path": [], "plan": PlanState(issue_kind="parent")})
        self.assertNotIn("skill_attempts", result)
        self.assertEqual(result["plan"].spec_candidate["author"]["model"], result["models_used"]["triage"])
