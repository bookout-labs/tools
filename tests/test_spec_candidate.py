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
from foreman.stage_output import CandidateSlice, SliceProvenance, SpecAuthor, SpecCandidate, StoredSlice, SpecOutput
from skill_fixture import skill_fixture
from foreman.skill_rules import git


SLICE = {
    "title": "Save", "kind": "feature", "criteria": [{"text": "Reject duplicate IDs", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": [], "stateful": True,
    "files": ["save.py"],
    "screen": "docs/screens/save.md", "properties": [{"name": "Unique ID", "constraint": "id_key"}],
    "depends_on": ["Database"], "blocker_numbers": [7],
}
# The engine route a synthetic app plan relies on, cited in the engine's pinned checkout, and the contract criterion it proves.
CITED = {"fact": "The engine serves a deal at /api/deals/{id}.", "citation": {"repository": "org/engine", "path": "routes/deals.py"}}
CONTRACT_CRITERION = {"text": "The deal page shows the engine's deal.",
                      "proof": {"kind": "contract", "repository": "org/engine", "assumption": CITED["fact"]}}
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
               "questions": [question], "assumptions": []}
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
                           ("non_goals", ["Changed"]), ("removed_tests", [{"test": "test_save", "reason": "Changed"}]),
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

    def redrafted(self, rejected, work_kind="feature"):
        """The rejection validate sends back for a rejected draft, then the run after a clean redraft."""
        script = ScriptHandler(triage="plan", slices=[rejected], work_kind=work_kind)
        nodes, rejections = [], []

        def handler(node, model, state, instruction):
            nodes.append(node)
            if node == "spec" and (rejection := state["plan"].spec_rejection):
                rejections.append(rejection)
                script.slices = [{**WORKER_SLICE, "criteria": [{"text": "Duplicate IDs are rejected.",
                                                                "proof": {"kind": "drive", "not_drivable": None}}]}]
            return script(node, model, state, instruction)
        with skill_fixture() as (config, scratch):
            result = run_delivery(handler, repo="org/repo", request="Save", config=config, scratch=scratch)
        self.assertEqual(nodes[:4], ["triage", "spec", "spec", "spec_check"])
        self.assertEqual(result["plan"].spec_failures, 1)
        return rejections

    def test_a_criterion_without_its_proof_returns_the_draft_to_draft_before_any_checker_call(self):
        """SP3, proof half: a missing proof, or a not-drivable mark without its reason, is a plan defect, not a repair turn."""
        for criterion, detail in (
                ({"text": "Duplicate IDs are rejected."},
                 "slices.0.criteria.0.proof: each criterion names its proof by test, drive or contract."),
                ({"text": "Duplicate IDs are rejected.", "proof": {"kind": "drive", "not_drivable": " "}},
                 "slices.0.criteria.0.proof.drive.not_drivable: a criterion marked not drivable gives the reason.")):
            with self.subTest(detail=detail):
                rejections = self.redrafted({**WORKER_SLICE, "criteria": [criterion]})
                self.assertEqual(rejections, [{"kind": "criterion_proof", "detail": detail}])

    def test_validate_rejects_a_base_met_criterion_proven_by_test_or_contract(self):
        """A refactor or perf slice's criteria already hold at the base, so only a drive proves them."""
        for work_kind in ("refactoring", "perf_issue"):
            for proof in ({"kind": "test"}, {"kind": "contract", "repository": "org/engine", "assumption": "The engine saves."}):
                kind = proof["kind"]
                with self.subTest(work_kind=work_kind, kind=kind):
                    rejected = {**WORKER_SLICE, "criteria": [{"text": "Saving stays as fast.", "proof": proof}]}
                    self.assertEqual(self.redrafted(rejected, work_kind), [{"kind": "criterion_proof", "detail": (
                        f"Save proves criterion 1 by {kind}, but the base already meets a {work_kind} slice's criteria, "
                        "so prove it by drive.")}])

    def cited_run(self, assumptions, criteria):
        """A synthetic app run whose plan relies on a route of its related engine, pinned in a real checkout. It returns the
        nodes called, the rejections validate sent back and the result after a clean redraft."""
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from unittest.mock import patch
        from foreman.dry_run import DRY_CONTRACT
        from foreman.workspace import RelatedCheckout

        with TemporaryDirectory() as directory:
            engine = Path(directory)
            git(engine, "init", "--quiet")
            (engine / "routes").mkdir()
            (engine / "routes" / "deals.py").write_text("ROUTE = '/api/deals/{id}'\n")
            git(engine, "add", ".")
            git(engine, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Routes")
            pinned = RelatedCheckout(repository="org/engine", commit=git(engine, "rev-parse", "HEAD"), checkout=str(engine))
            slice_ = {**WORKER_SLICE, "criteria": criteria}
            script = ScriptHandler(triage="plan", slices=[slice_], assumptions=assumptions)
            nodes, rejections = [], []

            def handler(node, model, state, instruction):
                nodes.append(node)
                if node == "spec" and (rejection := state["plan"].spec_rejection):
                    rejections.append(rejection)
                    script.assumptions = [CITED]
                    script.slices = [{**slice_, "criteria": [CONTRACT_CRITERION]}]
                return script(node, model, state, instruction)
            contract = DRY_CONTRACT + b"related_repositories: [{repository: org/engine, start: 'true'}]\n"
            with skill_fixture() as (config, scratch), patch.object(DryRun, "check_out_related", return_value=pinned):
                result = run_delivery(handler, repo="org/app", request="Show a deal", contract=contract, config=config, scratch=scratch)
        return nodes, rejections, result

    def test_a_plan_cites_each_outside_fact_and_an_uncited_one_is_rejected_before_any_checker_call(self):
        """SP4: the draft cites the engine route file in the pinned related checkout, and the candidate carries it. Seeded
        with the uncited wrong assumption, validate rejects it as one rejected digest before any checker call."""
        nodes, rejections, result = self.cited_run([CITED], [CONTRACT_CRITERION])
        self.assertEqual((nodes[:3], rejections), (["triage", "spec", "spec_check"], []))
        self.assertEqual(result["plan"].spec_candidate["assumptions"], [CITED])
        nodes, rejections, result = self.cited_run([{"fact": "The engine serves /deals/{id}.", "citation": None}], [CONTRACT_CRITERION])
        self.assertEqual(nodes[:4], ["triage", "spec", "spec", "spec_check"])
        self.assertEqual(rejections, [{"kind": "assumption_citation", "detail": (
            "The assumption 'The engine serves /deals/{id}.' cites no file. Cite a tracked file in a related repository "
            "checkout, or ask a grounded question instead.")}])
        self.assertEqual(result["plan"].spec_failures, 1)

    def test_a_citation_names_a_tracked_regular_file_in_a_pinned_related_checkout(self):
        """rules.paths: a citation outside the pinned related repositories, or of a path its pin does not track as a regular
        file, is rejected like an uncited one."""
        for citation, shown in (({"repository": "org/other", "path": "routes/deals.py"}, "is not a related repository pinned"),
                                ({"repository": "org/engine", "path": "routes"}, "routes, which is a directory at"),
                                ({"repository": "org/engine", "path": "routes/missing.py"}, "routes/missing.py, which is absent at"),
                                ({"repository": "org/engine", "path": "../deals.py"}, "not a relative path")):
            with self.subTest(citation=citation):
                _, rejections, result = self.cited_run([{**CITED, "citation": citation}], [CONTRACT_CRITERION])
                self.assertEqual([rejection["kind"] for rejection in rejections], ["assumption_citation"])
                self.assertIn(shown, rejections[0]["detail"])
                self.assertEqual(result["plan"].spec_failures, 1)

    def test_a_contract_criterion_names_its_repository_and_the_assumption_it_proves(self):
        """A contract criterion names a pinned related repository and one of the plan's listed assumptions."""
        for proof in ({"kind": "contract", "repository": "org/other", "assumption": CITED["fact"]},
                      {"kind": "contract", "repository": "org/engine", "assumption": "An unlisted fact."}):
            with self.subTest(proof=proof):
                _, rejections, _ = self.cited_run([CITED], [{**CONTRACT_CRITERION, "proof": proof}])
                self.assertEqual(rejections, [{"kind": "criterion_proof", "detail": (
                    "Save proves criterion 1 by contract, so it names a related repository pinned for this run and the plan's "
                    "assumption it proves.")}])
        rejections = self.redrafted({**WORKER_SLICE, "criteria": [{"text": "The deal loads.", "proof": {"kind": "contract"}}]})
        self.assertEqual([rejection["kind"] for rejection in rejections], ["criterion_proof"])
        self.assertIn("a contract criterion names the related repository and the assumption it proves", rejections[0]["detail"])

    def test_a_slice_stores_and_renders_its_criteria_non_goals_and_removed_tests(self):
        """F1: the drafter's removed or renamed tests, with their reasons, travel with the slice into its issue body."""
        child = StoredSlice.model_validate({
            **SLICE, "non_goals": ["Bulk import."],
            "criteria": [{"text": "Duplicate IDs are rejected.", "proof": {"kind": "test"}},
                         {"text": "The save screen shows the error.", "proof": {"kind": "drive", "not_drivable": "No display in CI."}}],
            "removed_tests": [{"test": "tests/test_save.py::test_overwrites_duplicates", "reason": "Duplicates are now rejected."}],
        })
        body = render_slice_body("", child)
        self.assertIn("Acceptance criteria:\n1. Duplicate IDs are rejected. Proof: test.\n"
                      "2. The save screen shows the error. Proof: drive, not drivable: No display in CI.", body)
        self.assertIn("Non-goals:\n- Bulk import.", body)
        self.assertIn("Removed or renamed tests:\n- tests/test_save.py::test_overwrites_duplicates: Duplicates are now rejected.", body)
        self.assertEqual(_load_slice(body).record(), child.record())

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
