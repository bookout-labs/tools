import unittest

from dataclasses import replace
from pathlib import Path

from foreman.adapters import Outcome
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import DryRun, REQUIRED_NODES, compile_delivery, diagram, run_delivery, stage_text
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
from foreman.dry_run import ScriptHandler


SLICES = [
    {
        "title": "Board lists the deals",
        "acceptance": "The board shows the deals.",
        "depends_on": [],
        "screen": "docs/screens/deals.md",
        "properties": [{"name": "Deal external IDs are unique", "constraint": "deals_external_id_key"}],
    },
    {
        "title": "Board saves a move",
        "acceptance": "A move is stored.",
        "depends_on": ["Board lists the deals"],
        "screen": "",
        "properties": [],
    },
]
EMPTY_SLICE = {**SLICES[1], "depends_on": []}


HEAD_SHA = "a" * 40


def walk(handler, check="npm run check", config=None, *, slice=EMPTY_SLICE):
    return run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check=check,
        slice=slice,
        config=config,
    )


class DeliveryTest(unittest.TestCase):
    def test_passing_a_slice_starts_a_slice_run(self):
        result = walk(ScriptHandler(triage="spec"), slice=SLICES[1])
        self.assertEqual(result["issue_kind"], "slice")
        self.assertEqual(result["slice"], SLICES[1])
        self.assertEqual(result["stop"], "verified")

    def test_clear_change_skips_spec(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)

        def check_review_context(node, model, state, instruction):
            if node == "review":
                self.assertEqual(state["verify"], "pass")
                self.assertEqual(state["head_sha"], HEAD_SHA)
            return handler(node, model, state, instruction)

        result = walk(check_review_context)
        self.assertEqual(
            result["path"],
            ["triage", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["risk"]["level"], "low")
        self.assertEqual(result["merge"], "skipped")
        self.assertEqual(result["head_sha"], HEAD_SHA)
        self.assertEqual(result["models_used"]["implement"], "gpt-6.1-sol-high")
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertNotIn("verify", result["models_used"])

    def test_ready_spec_is_implemented(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="ready"))
        self.assertEqual(
            result["path"],
            ["triage", "spec", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_ready_spec_keeps_the_loaded_slice_proof_obligations(self):
        result = walk(ScriptHandler(triage="spec"), slice=SLICES[0])
        self.assertEqual(result["path"], ["triage", "spec", "implement", "verify", "proof", "done"])
        self.assertEqual(result["stop"], "insufficient_evidence")

    def test_triage_slice_rejects_a_dependency(self):
        result = walk(
            lambda *_: Outcome("ran", {
                "triage": "implement", "human_question": None, "slice": {**SLICES[0], "depends_on": ["Other"]},
            }),
            slice=None,
        )
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "invalid_output")
        self.assertEqual(result["halt_detail"], "slice.depends_on.0: unknown dependency Other")

    def test_a_parent_routes_spec_and_question_to_spec(self):
        for triage in ("spec", "question"):
            with self.subTest(triage=triage):
                result = walk(ScriptHandler(triage=triage, slices=SLICES), slice=None)
                self.assertEqual(result["path"], ["triage", "spec", "done"])
                self.assertEqual(result["stop"], "sliced")
                self.assertEqual(result["slices"], SLICES)

    def test_a_parent_stops_on_malformed_slices(self):
        first, second = SLICES
        missing_key = {key: value for key, value in first.items() if key != "screen"}
        cases = {
            "cycle": [{**first, "depends_on": [second["title"]]}, second],
            "unknown dependency": [{**first, "depends_on": ["Unknown"]}],
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

    def test_review_stops_when_no_other_family_remains(self):
        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], models=(ModelSpec.parse("gpt-6.1-terra-high"),))
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "done"])
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("gpt family", result["halt_detail"])
        self.assertNotIn("review", [call["node"] for call in handler.calls])

    def test_review_stops_when_the_builder_model_is_missing(self):
        config = load_config()
        config.nodes["implement"] = NodeConfig(role="check", models=())
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("implement model", result["halt_detail"])
        self.assertNotIn("review", [call["node"] for call in handler.calls])

    def test_open_spec_stops_for_a_person(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="needs_human"))
        self.assertEqual(result["path"], ["triage", "spec", "done"])
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
            ["triage", "implement", "verify", "review", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_review_excludes_every_builder_family(self):
        for review_models in (("claude-opus-5-5-high", "gpt-6.1-sol-high", "grok-4.7-high"),
                              ("claude-opus-5-5-high", "gpt-6.1-sol-high")):
            with self.subTest(review_models=tuple(ModelSpec.parse(model) for model in review_models)):
                config = load_config()
                config.nodes["review"] = replace(config.nodes["review"], models=tuple(ModelSpec.parse(model) for model in review_models))
                handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)

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
                    self.assertEqual(reviewers, ["claude-opus-5-5-high"])
                    self.assertEqual(result["stop"], "no_reviewer")
                    self.assertNotIn("merge", result["path"])
                self.assertEqual(result["implement_models"], ["gpt-6.1-sol-high", "claude-opus-5-5-high"])

    def test_review_cap_stops_without_arming(self):
        result = walk(ScriptHandler(reviews=[], head_sha=HEAD_SHA))
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertEqual(result["path"].count("verify"), 3)
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "review_cap")

    def test_verify_cap_stops_without_review(self):
        result = walk(ScriptHandler(verify="fail", head_sha=HEAD_SHA))
        self.assertEqual(result["path"].count("verify"), 3)
        self.assertNotIn("review", result["path"])
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "verify_cap")

    def test_merge_requires_the_latest_build_head(self):
        handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)

        def drop_second_head(node, model, state, instruction):
            output = handler(node, model, state, instruction)
            if node == "implement" and state.get("review") == "changes":
                output.output["head_sha"] = None
            return output

        result = walk(drop_second_head)
        self.assertIn("merge", result["path"])
        self.assertEqual(result["merge"], "skipped")
        self.assertIsNone(result["head_sha"])

    def test_needs_human_stops_at_triage(self):
        result = walk(ScriptHandler(triage="needs_human"))
        self.assertEqual(result["path"], ["triage", "done"])
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
        self.assertTrue(result["halt_detail"].startswith("risk:"))
        self.assertEqual(result["path"].count("review"), 1)
        self.assertNotIn("merge", result["path"])

    def test_non_string_risk_level_stops_as_invalid_output(self):
        handler = ScriptHandler()
        handler.risk["level"] = ["low"]
        result = walk(handler)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertTrue(result["halt_detail"].startswith("risk.level:"))

    def test_review_quota_never_falls_back_to_the_builder_family(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)
        offered = []

        def exhaust_review(node, model, state, instruction):
            if node == "review":
                offered.append(model.name)
                return Outcome("quota")
            return handler(node, model, state, instruction)

        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], other_family_from=None)
        result = walk(exhaust_review, config=config)
        self.assertEqual(offered, ["claude-opus-5-5-high", "grok-4.7-high"])
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("quota exhausted", result["halt_detail"])
        self.assertNotIn("merge", result["path"])

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if model and model.name == "claude-opus-5-5-high":
                return Outcome("quota")
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["models_used"]["triage"], "grok-4.7-high")
        self.assertEqual(result["models_used"]["review"], "grok-4.7-high")
        self.assertNotEqual(
            result["models_used"]["implement"].split("-", 1)[0],
            result["models_used"]["review"].split("-", 1)[0],
        )

    def test_every_model_exhausted_raises(self):
        def exhaust(node, model, state, instruction):
            return Outcome("quota")

        with self.assertRaises(QuotaExhausted):
            walk(exhaust)

    def test_node_instruction_comes_from_the_stage_file(self):
        handler = ScriptHandler(triage="spec")
        walk(handler)
        by_node = {call["node"]: call["instruction"] for call in handler.calls}
        self.assertEqual(by_node["triage"], stage_text("triage"))
        self.assertIn("Do not edit it.", by_node["implement"])
        self.assertIn("Do not add a test that restates the code.", by_node["implement"])
        self.assertIn("Alignment", by_node["review"])
        self.assertNotIn("insufficient_evidence", by_node["implement"])
        self.assertIn("formal method", by_node["review"])
        self.assertIn("Command: npm run check", by_node["verify"])
        self.assertNotIn("gpt-6.1-sol-high", " ".join(by_node.values()))

    def test_diagram_names_the_nodes(self):
        picture = diagram()
        for name in REQUIRED_NODES + ("merge", "done"):
            self.assertIn(name, picture)
        self.assertNotIn("claude-opus", picture)
        compile_delivery(FailoverDispatcher(ScriptHandler()), load_config(), run=DryRun())


if __name__ == "__main__":
    unittest.main()
