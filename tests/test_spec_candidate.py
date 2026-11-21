import tests

import json
import unittest
from dataclasses import replace

from pydantic import TypeAdapter, ValidationError

from foreman.adapters import Outcome
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.github import render_slice_body
from foreman.runner import _load_slice, checkpoint_state
from foreman.stage_output import (
    CandidateSlice, SliceProvenance, SpecAuthor, SpecCandidate, SpecCandidateRecord, StoredSlice,
)
from skill_fixture import skill_fixture


SLICE = {
    "title": "Save", "acceptance": "Reject duplicate IDs", "stateful": True,
    "builds": [{"name": "save", "exists": False, "path": None}], "uses": [],
    "screen": "docs/screens/save.md", "properties": [{"name": "Unique ID", "constraint": "id_key"}],
    "depends_on": ["Database"], "blocker_numbers": [7],
}


class CandidateTests(unittest.TestCase):
    def candidate(self):
        return SpecCandidate(
            slices=[CandidateSlice.model_validate(SLICE)], spec="Save with a unique ID.", work_kind="feature",
            author=SpecAuthor(stage="spec", model="gpt-test", family="gpt"),
        )

    def test_candidate_round_trip_and_checkpoint(self):
        candidate = self.candidate()
        raw = json.loads(candidate.model_dump_json())
        self.assertEqual(SpecCandidate.model_validate(raw), candidate)
        record = candidate.record()
        self.assertEqual(TypeAdapter(SpecCandidateRecord).validate_python(raw), record)
        state = checkpoint_state({"repo": "org/repo", "check": "true", "path": [], "spec_candidate": record})
        self.assertEqual(state["spec_candidate"], record)
        self.assertEqual(record["slices"][0], SLICE)
        with self.assertRaises(ValidationError):
            checkpoint_state({**state, "spec_candidate": {**record, "spec": "Changed"}})
        self.assertNotIn("approval", record)
        with self.assertRaises(ValidationError):
            SpecCandidate.model_validate({**raw, "approval": "clear"})
        with self.assertRaises(ValidationError):
            SpecCandidate.model_validate({**raw, "spec": "Changed"})

    def test_revision_binds_all_content_and_ignores_mapping_order(self):
        raw = self.candidate().model_dump(exclude={"revision"})
        expected = SpecCandidate.model_validate(dict(reversed(list(raw.items())))).revision
        self.assertEqual(expected, self.candidate().revision)
        for key, value in (("spec", "Changed"), ("work_kind", "refactoring"),
                           ("author", {"stage": "triage", "model": "claude-test", "family": "claude"})):
            self.assertNotEqual(SpecCandidate.model_validate({**raw, key: value}).revision, expected)
        for key, value in (("screen", ""), ("properties", []), ("acceptance", "Changed"),
                           ("stateful", False), ("builds", []), ("depends_on", []), ("blocker_numbers", [])):
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
        state = checkpoint_state({"repo": "org/repo", "check": "", "path": [], "slice": restored.record()})
        self.assertEqual(state["slice"]["provenance"], provenance.record())
        legacy = StoredSlice.model_validate(SLICE)
        self.assertIsNone(legacy.provenance)
        self.assertNotIn("provenance", legacy.record())

    def test_actual_accepted_failover_authors(self):
        for stage in ("triage", "spec"):
            with self.subTest(stage=stage):
                config = load_config()
                nodes = {**config.nodes, stage: NodeConfig("coordinator", tuple(map(ModelSpec.parse, ("gpt-test", "claude-test"))))}
                script = ScriptHandler(triage="implement" if stage == "triage" else "spec",
                                       slices=[{k: v for k, v in SLICE.items() if k not in {"depends_on", "blocker_numbers"}}])
                def handler(node, model, state, instruction):
                    if node == stage and model.name == "gpt-test":
                        return Outcome("quota")
                    return script(node, model, state, instruction)
                with skill_fixture(replace(config, nodes=nodes)) as (configured, scratch):
                    result = run_delivery(FailoverDispatcher(handler), repo="org/repo", request="Save", config=configured, scratch=scratch)
                candidate = SpecCandidate.model_validate(result["spec_candidate"])
                self.assertEqual(candidate.author.record(), {"stage": stage, "model": "claude-test", "family": "claude"})
                attempts = [a for a in result["skill_attempts"] if a["stage"] == stage]
                self.assertEqual([a["outcome"] for a in attempts], ["quota", "ran"])
                stored = result["slice"] if stage == "triage" else result["slices"][0]
                self.assertEqual(stored["provenance"]["revision"], candidate.revision)
                self.assertEqual(stored["provenance"]["author"], candidate.author.record())
                self.assertIn("implement" if stage == "triage" else "done", result["path"])

    def test_inherited_slice_author_is_not_replaced_by_child_triage(self):
        candidate = self.candidate()
        provenance = {"author": candidate.author.record(), "revision": candidate.revision}
        with skill_fixture() as (config, scratch):
            result = run_delivery(FailoverDispatcher(ScriptHandler()), repo="org/repo", request="Save",
                                  slice={**SLICE, "provenance": provenance}, config=config, scratch=scratch)
        self.assertEqual(result["slice"]["provenance"], provenance)
        self.assertNotIn("spec_candidate", result)

    def test_failed_author_does_not_produce_a_candidate(self):
        with skill_fixture() as (config, scratch):
            result = run_delivery(FailoverDispatcher(lambda *_: Outcome("failed", detail="Malformed output")),
                                  repo="org/repo", request="Save", config=config, scratch=scratch)
        self.assertEqual(result["halt"], "invalid_output")
        self.assertNotIn("spec_candidate", result)
