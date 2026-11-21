import tests

import unittest
from dataclasses import replace

from foreman.adapters import Outcome
from foreman.cli import run_exit_code
from foreman.config import ModelSpec, load_config
from foreman.delivery import RESUMABLE_HALTS, spec_approved
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from test_delivery import EMPTY_SLICE, SLICES, walk


PROVENANCE = {"author": {"stage": "spec", "model": "gpt-author", "family": "gpt"}, "revision": "0" * 64}
CLEAR = {"findings": [], "notes": [], "resolutions": []}
GAP = {"id": "gap1", "kind": "scope", "slice": EMPTY_SLICE["title"], "section": "slice",
       "summary": "Keep the proof obligation.", "ruling_id": None}


class SpecGateTests(unittest.TestCase):
    def run_gate(self, output, *, item=None, triage="implement", config=None, legacy=False):
        script = ScriptHandler(triage=triage, slices=SLICES)
        self.checkers = []

        def handle(node, model, state, instruction):
            if node == "spec_check":
                self.checkers.append((model, state, instruction))
                if isinstance(output, dict) and output.get("findings"):
                    output["findings"] = [{**GAP, "slice": state["spec_candidate"]["slices"][0]["title"]}]
                return output if isinstance(output, Outcome) else Outcome("ran", output, usage=SCRIPTED_USAGE)
            return script(node, model, state, instruction)

        self.script = script
        return walk(handle, slice=item, config=config, legacy=legacy)

    def test_all_entry_paths_check_before_build_or_parent_completion(self):
        for item, triage in ((None, "implement"), (None, "spec"), ({**EMPTY_SLICE, "provenance": PROVENANCE}, "implement")):
            with self.subTest(item=item, triage=triage):
                result = self.run_gate(CLEAR, item=item, triage=triage)
                self.assertEqual(len(self.checkers), 1)
                self.assertTrue(spec_approved(result))
                self.assertEqual(result["review_rounds"], 0 if triage == "spec" else 1)
                self.assertEqual(len(result["spec_history"]["rounds"]), 1)
                self.assertEqual(result["stop"], "sliced" if triage == "spec" else "verified")
                self.assertNotIn("architect/SKILL.md", self.checkers[0][2])
                if triage != "spec":
                    self.assertLess(result["path"].index("spec_check"), result["path"].index("implement"))
                self.assertFalse(spec_approved({**result, "spec": "changed"}))
                self.assertFalse(spec_approved({**result, "spec_approval": {**result["spec_approval"], "revision": "f" * 64}}))

    def test_negative_paths_never_build_and_have_distinct_exit_codes(self):
        for item, triage in ((None, "implement"), (None, "spec"), ({**EMPTY_SLICE, "provenance": PROVENANCE}, "implement")):
            for output, halt, code in (({**CLEAR, "findings": [GAP]}, "spec_unsettled", 3),
                                       (Outcome("cli_missing", detail="missing"), "spec_check_unavailable", 1),
                                       (Outcome("quota"), "quota_exhausted", 75),
                                       ({}, "invalid_output", 1)):
                result = self.run_gate(output, item=item, triage=triage)
                self.assertEqual(result["halt"], halt)
                self.assertEqual(run_exit_code(result), code)
                self.assertNotIn("implement", result["path"])
                self.assertNotIn("publish", result["path"])
                self.assertTrue(result["halt_detail"])
                self.assertEqual(len(result.get("spec_history", {}).get("rounds", [])), int(halt == "spec_unsettled"))
                self.assertNotIn("spec_unsettled", RESUMABLE_HALTS)
                self.assertNotIn("spec_check_unavailable", RESUMABLE_HALTS)

    def test_failover_filters_every_model_using_the_actual_author(self):
        config = load_config()
        names = ("gpt-first", "claude-check", "gpt-last", "grok-check")
        config.nodes["review"] = replace(config.nodes["review"], models=tuple(map(ModelSpec.parse, names)))
        result = self.run_gate(Outcome("cli_missing"), item={**EMPTY_SLICE, "provenance": PROVENANCE}, config=config)
        self.assertEqual([m.name for m, _, _ in self.checkers], ["claude-check", "grok-check"])
        self.assertIn("gpt", result["halt_detail"])
        self.assertIn("grok-check", result["halt_detail"])
        config.nodes["review"] = replace(config.nodes["review"], models=(ModelSpec.parse("gpt-only"),))
        result = self.run_gate(CLEAR, item={**EMPTY_SLICE, "provenance": PROVENANCE}, config=config)
        self.assertEqual(self.checkers, [])
        self.assertEqual(result["halt"], "spec_check_unavailable")

    def test_legacy_slice_fails_closed_without_inferred_authorship(self):
        result = self.run_gate(CLEAR, item=EMPTY_SLICE, legacy=True)
        self.assertEqual(result["halt"], "invalid_output")
        self.assertEqual(self.checkers, [])
        self.assertNotIn("implement", result["path"])
