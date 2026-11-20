import unittest

from foreman.config import load_config, parse_config
from foreman.delivery import REQUIRED_NODES, compile_delivery, diagram, run_delivery, stage_text
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
from foreman.dry_run import ScriptHandler


SLICES = [
    {
        "title": "Board lists the deals",
        "acceptance": "The board shows the deals.",
        "depends_on": [],
    },
    {
        "title": "Board saves a move",
        "acceptance": "A move is stored.",
        "depends_on": ["Board lists the deals"],
    },
]


def walk(handler, check="npm run check", issue_kind="slice"):
    return run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check=check,
        issue_kind=issue_kind,
    )


class DeliveryTest(unittest.TestCase):
    def test_clear_change_skips_spec(self):
        handler = ScriptHandler()
        result = walk(handler)
        self.assertEqual(
            result["path"],
            ["triage", "implement", "review", "verify", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["risk"]["level"], "low")
        self.assertEqual(result["models_used"]["implement"], "gpt-6.1-sol-high")
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertNotIn("verify", result["models_used"])

    def test_ready_spec_is_implemented(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="ready"))
        self.assertEqual(
            result["path"],
            ["triage", "spec", "implement", "review", "verify", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_a_parent_stops_after_spec_files_slices(self):
        result = walk(
            ScriptHandler(triage="spec", spec_gate="ready", slices=SLICES),
            issue_kind="parent",
        )
        self.assertEqual(result["path"], ["triage", "spec", "done"])
        self.assertEqual(result["stop"], "sliced")
        self.assertEqual(
            [item["title"] for item in result["slices"]],
            ["Board lists the deals", "Board saves a move"],
        )
        self.assertNotIn("implement", result["path"])

    def test_a_parent_rejects_a_dependency_cycle(self):
        cycle = [
            {"title": "One", "acceptance": "First.", "depends_on": ["Two"]},
            {"title": "Two", "acceptance": "Second.", "depends_on": ["One"]},
        ]
        with self.assertRaises(ValueError):
            walk(ScriptHandler(triage="spec", spec_gate="ready", slices=cycle), issue_kind="parent")

    def test_review_stops_when_no_other_family_remains(self):
        config = parse_config(
            {
                "max_review_rounds": 3,
                "nodes": {
                    "triage": {"role": "coordinator", "models": ["claude-opus-5-5-high"]},
                    "research": {"role": "coordinator", "models": ["claude-opus-5-5-high"]},
                    "spec": {"role": "coordinator", "models": ["claude-opus-5-5-high"]},
                    "implement": {"role": "builder", "models": ["gpt-6.1-sol-high"]},
                    "review": {
                        "role": "verifier",
                        "other_family_from": "implement",
                        "models": ["gpt-6.1-terra-high"],
                    },
                    "verify": {"role": "check", "models": []},
                },
            }
        )
        result = run_delivery(
            FailoverDispatcher(ScriptHandler()),
            repo="DoctorCRE/doctorcre-engine",
            request="Add a field",
            check="npm run check",
            config=config,
        )
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertNotIn("verify", result["path"])

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
            ["triage", "implement", "review", "implement", "review", "verify", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_review_cap_stops_without_verify(self):
        result = walk(ScriptHandler(reviews=[]))
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertNotIn("verify", result["path"])
        self.assertEqual(result["stop"], "review_cap")

    def test_needs_human_stops_at_triage(self):
        result = walk(ScriptHandler(triage="needs_human"))
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "needs_human")

    def test_review_without_risk_is_rejected(self):
        handler = ScriptHandler()

        def drop_risk(node, model, state, instruction):
            raw = handler(node, model, state, instruction)
            if node == "review":
                raw = {"review": raw["review"]}
            return raw

        with self.assertRaises(ValueError):
            walk(drop_risk)

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if model == "claude-opus-5-5-high":
                raise QuotaExhausted(model)
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
            raise QuotaExhausted(model)

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
        for name in REQUIRED_NODES + ("done",):
            self.assertIn(name, picture)
        self.assertNotIn("claude-opus", picture)
        compile_delivery(FailoverDispatcher(ScriptHandler()), load_config())


if __name__ == "__main__":
    unittest.main()
