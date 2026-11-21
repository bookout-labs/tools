import tests

import unittest

from dataclasses import replace
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from foreman.adapters import Outcome, Usage
from foreman.config import PSTACK_PIN, ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery, stage_text
from unittest.mock import patch
from foreman.dry_run import DryRun, ScriptHandler
from skill_fixture import FixtureSkills, skill_fixture


BOARD = {"name": "deals-board", "exists": False, "path": None}
MOVE = {"name": "deal-move", "exists": False, "path": None}
SLICES = [
    {
        "title": "Board lists the deals",
        "acceptance": "The board shows the deals.",
        "builds": [BOARD],
        "uses": [],
        "screen": "docs/screens/deals.md",
        "properties": [{"name": "Deal external IDs are unique", "constraint": "deals_external_id_key"}],
        "stateful": False,
    },
    {
        "title": "Board saves a move",
        "acceptance": "A move is stored.",
        "builds": [MOVE],
        "uses": [BOARD],
        "screen": "",
        "properties": [],
        "stateful": False,
    },
]
EMPTY_SLICE = {**SLICES[1], "builds": [], "uses": []}


def walk(handler, check="npm run check", config=None, *, slice=EMPTY_SLICE, commands=None):
    with skill_fixture(config) as (fixture_config, scratch):
        return run_delivery(
            handler, repo="DoctorCRE/doctorcre-engine", request="Add a field",
            check=check, slice=slice, config=fixture_config, scratch=scratch, commands=commands,
        )


class DeliveryTest(unittest.TestCase):
    def test_graph_receives_the_skill_source_as_an_input(self):
        source = FixtureSkills()

        with TemporaryDirectory() as directory:
            result = run_delivery(
                ScriptHandler(), repo="Example/Engine",
                request="List items", scratch=Path(directory), skills=source,
            )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(source.pins, [PSTACK_PIN])
        self.assertTrue(result["attempts"])

    def test_passing_a_slice_starts_a_slice_run(self):
        result = walk(ScriptHandler(triage="spec"), slice=SLICES[1])
        self.assertEqual(result["issue_kind"], "slice")
        self.assertEqual(result["slice"], SLICES[1])
        self.assertEqual(result["stop"], "verified")

    def test_clear_change_skips_spec(self):
        handler = ScriptHandler()

        def check_review_context(node, model, state, instruction):
            if node == "review":
                self.assertEqual(state["verify"], "pass")
                self.assertEqual(state["head_sha"], state["verified_head"])
            return handler(node, model, state, instruction)

        result = walk(check_review_context)
        self.assertEqual(
            result["path"],
            ["triage", "implement", "publish", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["risk"]["level"], "low")
        self.assertEqual(result["merge"], "skipped")
        self.assertEqual(result["head_sha"], result["verified_head"])
        self.assertEqual(result["models_used"]["implement"], "gpt-6.1-sol-high")
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertNotIn("verify", result["models_used"])

    def test_ready_spec_is_implemented(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="ready"))
        self.assertEqual(
            result["path"],
            ["triage", "spec", "implement", "publish", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_ready_spec_keeps_the_loaded_slice_proof_obligations(self):
        result = walk(ScriptHandler(triage="spec"), slice=SLICES[0])
        self.assertEqual(result["path"], ["triage", "spec", "implement", "publish", "verify", "proof", "done"])
        self.assertEqual(result["stop"], "insufficient_evidence")

    def test_worker_cannot_declare_depends_on(self):
        result = walk(
            lambda *_: Outcome("ran", {
                "triage": "implement", "work_kind": "feature", "human_question": None, "slice": {**SLICES[0], "depends_on": ["Other"]},
            }, usage=Usage(input_tokens=1, output_tokens=1)),
            slice=None,
        )
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "invalid_output")
        self.assertTrue(result["halt_detail"].startswith("triage output from claude-opus-5-5-high: slice.depends_on:"))

    def test_a_parent_routes_spec_and_question_to_spec(self):
        for triage in ("spec", "question"):
            with self.subTest(triage=triage):
                result = walk(ScriptHandler(triage=triage, slices=SLICES[::-1]), slice=None)
                self.assertEqual(result["path"], ["triage", "spec", "done"])
                self.assertEqual(result["stop"], "sliced")
                self.assertEqual(result["slices"], [
                    {**SLICES[0], "depends_on": [], "blocker_numbers": [], "provenance": {"author": result["spec_candidate"]["author"], "revision": result["spec_candidate"]["revision"]}},
                    {**SLICES[1], "depends_on": [SLICES[0]["title"]], "blocker_numbers": [], "provenance": {"author": result["spec_candidate"]["author"], "revision": result["spec_candidate"]["revision"]}},
                ])

    def test_single_slice_parents_skip_seam_ordering(self):
        consumer = SLICES[1]
        triaged = walk(ScriptHandler(slice=consumer), slice=None)
        self.assertEqual(triaged["stop"], "verified")
        specified = walk(ScriptHandler(triage="spec", slices=[consumer]), slice=None)
        self.assertEqual(specified["slices"], [{**consumer, "depends_on": [], "blocker_numbers": [], "provenance": {"author": specified["spec_candidate"]["author"], "revision": specified["spec_candidate"]["revision"]}}])

    def test_invalid_slice_plan_uses_a_round_and_returns_the_typed_reason_to_spec(self):
        first, second = SLICES
        cases = {
            "dependency_cycle": [{**first, "uses": [MOVE]}, second],
            "dangling_seam": [{**first, "builds": []}, second],
            "duplicate_builder": [first, {**second, "builds": [BOARD]}],
        }
        for kind, invalid in cases.items():
            with self.subTest(kind=kind):
                handler = ScriptHandler(triage="spec", slices=invalid)

                def repair(node, model, state, instruction):
                    if rejection := state.get("spec_rejection"):
                        self.assertEqual(rejection["kind"], kind)
                        self.assertIn(BOARD["name"] if kind != "dependency_cycle" else first["title"], rejection["detail"])
                        handler.slices = SLICES
                    return handler(node, model, state, instruction)

                result = walk(repair, slice=None)
                self.assertEqual(result["path"], ["triage", "spec", "spec", "done"])
                self.assertEqual(result["review_rounds"], 0)
                self.assertEqual(result["spec_failures"], 1)
                self.assertEqual(result["stop"], "sliced")
                self.assertIsNone(result["spec_rejection"])

    def test_a_parent_stops_on_malformed_slices(self):
        first = SLICES[0]
        missing_key = {key: value for key, value in first.items() if key != "screen"}
        cases = {
            "missing key": [missing_key],
            "extra key": [{**first, "branch": "extra"}],
            "outside screens": [{**first, "screen": "docs/system.md"}],
            "traversal": [{**first, "screen": "docs/screens/../system.md"}],
            "double dot": [{**first, "screen": "docs/screens/deals..draft.md"}],
            "backslash": [{**first, "screen": r"docs/screens/other\deals.md"}],
            "nested screen": [{**first, "screen": "docs/screens/sub/deals.md"}],
            "blank constraint": [{**first, "properties": [{"name": "Deal external IDs are unique", "constraint": " "}]}],
            "missing name": [{**first, "properties": [{"constraint": "deals_external_id_key"}]}],
        }
        for case, slices in cases.items():
            with self.subTest(case=case):
                result = walk(ScriptHandler(triage="spec", slices=slices), slice=None)
                self.assertEqual(result["path"], ["triage", "spec", "done"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"])

    def test_review_can_use_the_exact_builder_model(self):
        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], models=(ModelSpec.parse("gpt-6.1-sol-high"),))
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["models_used"]["review"], result["models_used"]["implement"])

    def test_review_stops_when_the_builder_model_is_missing(self):
        config = load_config()
        config.nodes["implement"] = NodeConfig(role="check", models=())
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["stop"], "publication_model_missing")
        self.assertIn("implement model", result["halt_detail"])
        self.assertNotIn("review", [call["node"] for call in handler.calls])

    def test_model_selection_stops_when_its_dependency_is_missing(self):
        config = load_config()
        config.nodes["research"] = replace(config.nodes["research"], other_family_from="implement")
        result = walk(ScriptHandler(triage="question"), config=config)
        self.assertEqual(result["stop"], "model_dependency_missing")

    def test_open_spec_stops_for_a_person(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="needs_human"))
        self.assertEqual(result["path"], ["triage", "spec"])
        self.assertEqual(result["stop"], "needs_human")
        self.assertNotIn("implement", result["path"])

    def test_question_is_answered_without_a_change(self):
        result = walk(ScriptHandler(triage="question"))
        self.assertEqual(result["path"], ["triage", "research", "done"])
        self.assertEqual(result["stop"], "answered")

    def test_one_change_request_loops_once(self):
        result = walk(ScriptHandler(reviews=["changes", "approve"]))
        self.assertEqual(
            result["path"],
            ["triage", "implement", "publish", "verify", "review", "implement", "publish", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_review_prefers_models_outside_every_builder_family(self):
        for review_models in (("claude-opus-5-5-high", "gpt-6.1-sol-high", "grok-4.7-high"),
                              ("claude-opus-5-5-high", "gpt-6.1-sol-high")):
            with self.subTest(review_models=tuple(ModelSpec.parse(model) for model in review_models)):
                config = load_config()
                config.nodes["review"] = replace(config.nodes["review"], models=tuple(ModelSpec.parse(model) for model in review_models))
                handler = ScriptHandler(reviews=["changes", "approve"])

                def switch_builder(node, model, state, instruction):
                    if node == "implement" and state.get("review") == "changes" and model and model.family == "gpt":
                        return Outcome("quota")
                    return handler(node, model, state, instruction)

                result = walk(switch_builder, config=config)
                reviewers = [call["model"] for call in handler.calls if call["node"] == "review"]
                self.assertEqual(result["models_used"]["implement"], "claude-opus-5-5-high")
                if "grok-4.7-high" in review_models:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high", "grok-4.7-high"])
                    self.assertEqual(result["stop"], "verified")
                else:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high"] * 2)
                    self.assertEqual(result["stop"], "verified")
                self.assertEqual(result["implement_models"], ["gpt-6.1-sol-high", "claude-opus-5-5-high"])

    def test_review_cap_stops_without_arming(self):
        result = walk(ScriptHandler(reviews=[]))
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertEqual(result["path"].count("verify"), 3)
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "review_cap")

    def test_verify_cap_stops_without_review(self):
        with patch.object(DryRun, "run", return_value=1):
            result = walk(ScriptHandler())
        self.assertEqual(result["path"].count("verify"), 3)
        self.assertNotIn("review", result["path"])
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "verify_cap")

    def test_needs_human_stops_at_triage(self):
        result = walk(ScriptHandler(triage="needs_human"))
        self.assertEqual(result["path"], ["triage"])
        self.assertEqual(result["stop"], "needs_human")

    def test_review_without_risk_is_rejected(self):
        handler = ScriptHandler()

        def drop_risk(node, model, state, instruction):
            raw = handler(node, model, state, instruction)
            if node == "review":
                raw.output.pop("risk")
            return raw

        result = walk(drop_risk)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("review output from claude-opus-5-5-high: risk:", result["halt_detail"])
        self.assertEqual(result["path"].count("review"), 1)
        self.assertNotIn("merge", result["path"])

    def test_non_string_risk_level_stops_as_invalid_output(self):
        handler = ScriptHandler()
        handler.risk["level"] = ["low"]
        result = walk(handler)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("review output from claude-opus-5-5-high: risk.level:", result["halt_detail"])

    def test_review_exhausts_the_whole_configured_list(self):
        handler = ScriptHandler()
        offered = []

        def exhaust_review(node, model, state, instruction):
            if node == "review":
                offered.append(model.name)
                return Outcome("quota")
            return handler(node, model, state, instruction)

        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], other_family_from=None)
        result = walk(exhaust_review, config=config)
        self.assertEqual(offered, ["claude-opus-5-5-high", "gpt-6.1-sol-high"])
        self.assertEqual(result["stop"], "reviewer_unavailable")
        self.assertIn("quota", result["halt_detail"])
        self.assertNotIn("merge", result["path"])

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if node == "triage" and model and model.name == "claude-opus-5-5-high":
                return Outcome("quota")
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["models_used"]["triage"], "grok-4.7-high")
        triage = [item for item in result["attempts"] if item["stage"] == "triage"]
        self.assertEqual([item["model"] for item in triage], ["claude-opus-5-5-high", "grok-4.7-high"])
        self.assertEqual([item["outcome"] for item in triage], ["quota", "ran"])
        self.assertEqual([item["methods"] for item in triage], [["poteto-mode/SKILL.md#L110-L110"]] * 2)
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertEqual(result["stop"], "verified")

    def test_every_model_exhausted_halts(self):
        def exhaust(node, model, state, instruction):
            return Outcome("quota")

        result = walk(exhaust)
        self.assertEqual(result["stop"], "quota_exhausted")

    def test_node_instruction_comes_from_the_stage_file(self):
        handler = ScriptHandler(triage="spec")
        walk(handler)
        by_node = {call["node"]: call["instruction"] for call in handler.calls}
        self.assertTrue(by_node["triage"].startswith(stage_text("triage")))
        for instruction in by_node.values():
            self.assertNotIn("Talk through the work with the person.", instruction)
            self.assertNotIn("The factory label triggers the delivery graph.", instruction)
        self.assertIn("Do not edit it.", by_node["implement"])
        self.assertIn("Do not add a test that restates the code.", by_node["implement"])
        self.assertIn("review-quality.md", by_node["review"])
        self.assertNotIn("insufficient_evidence", by_node["implement"])
        self.assertIn("merge-risk.md", by_node["review"])
        self.assertNotIn("verify", by_node)
        self.assertNotIn("gpt-6.1-sol-high", " ".join(by_node.values()))

    def test_work_kind_selects_implement_and_research_methods(self):
        for kind in ("feature", "bug_fix", "refactoring", "perf_issue"):
            with self.subTest(kind=kind):
                built = walk(ScriptHandler(work_kind=kind))
                methods = next(record["methods"] for record in built["attempts"] if record["stage"] == "implement")
                self.assertEqual("tdd/SKILL.md" in methods, kind == "bug_fix")
                self.assertEqual("benchmark-checklist/SKILL.md" in methods, kind == "perf_issue")
                self.assertIn("principle-prove-it-works/SKILL.md", methods)

                researched = walk(ScriptHandler(triage="question", work_kind=kind))
                research = researched["attempts"][-1]["methods"]
                self.assertIn("how/SKILL.md#L17-L17", research)
                self.assertEqual("why/SKILL.md" in research, kind == "bug_fix")

    def test_spec_uses_statechart_only_for_a_stored_stateful_slice(self):
        for item in (None, EMPTY_SLICE, {**EMPTY_SLICE, "stateful": True}):
            with self.subTest(item=item):
                result = walk(ScriptHandler(triage="spec", slices=[{**EMPTY_SLICE, "stateful": True}]), slice=item)
                methods = next(record["methods"] for record in result["attempts"] if record["stage"] == "spec")
                self.assertEqual("statechart/SKILL.md" in methods, item is not None and item["stateful"])

    def test_local_proof_methods_keep_independent_conditions(self):
        for screen in ("", SLICES[0]["screen"]):
            for properties in ([], SLICES[0]["properties"]):
                with self.subTest(screen=screen, properties=properties):
                    result = walk(ScriptHandler(), slice={**EMPTY_SLICE, "screen": screen, "properties": properties})
                    methods = next(record["methods"] for record in result["attempts"] if record["stage"] == "implement")
                    self.assertEqual("foreman/skills/browser-proof.md" in methods, bool(screen))
                    self.assertEqual("foreman/skills/formal-proof.md" in methods, bool(properties))

    def test_worker_gets_absolute_read_only_method_paths(self):
        script = ScriptHandler(triage="spec")

        def read_methods(node, model, state, instruction):
            paths = re.findall(r"^(?:Read |Use only lines \d+ through \d+ of )(/.*?\.md) ", instruction, re.M)
            if node != "verify":
                self.assertTrue(paths)
            for path in paths:
                method = Path(path)
                text = method.read_text().strip()
                self.assertTrue(text)
                if text.startswith("Synthetic method."):
                    self.assertEqual(method.stat().st_mode & 0o222, 0)
            return script(node, model, state, instruction)

        result = walk(read_methods)
        self.assertEqual(result["stop"], "verified")
        self.assertEqual([item["stage"] for item in result["attempts"]], ["triage", "spec", "implement", "review"])


if __name__ == "__main__":
    unittest.main()
