import unittest

from foreman.config import load_config
from foreman.delivery import INSTRUCTIONS, compile_delivery, diagram, run_delivery
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
from foreman.dry_run import ScriptHandler


def walk(handler, check="npm run check"):
    return run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check=check,
    )


class DeliveryTest(unittest.TestCase):
    def test_ready_work_ends_verified(self):
        handler = ScriptHandler()
        result = walk(handler)
        self.assertEqual(
            result["path"],
            ["intake", "spec", "implement", "review", "verify", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["models_used"]["implement"], "gpt-6.1-sol-high")
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertNotIn("verify", result["models_used"])

    def test_one_change_request_loops_once(self):
        result = walk(ScriptHandler(reviews=["changes", "approve"]))
        self.assertEqual(
            result["path"],
            ["intake", "spec", "implement", "review", "implement", "review", "verify", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_review_cap_stops_without_verify(self):
        result = walk(ScriptHandler(reviews=[]))
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertNotIn("verify", result["path"])
        self.assertEqual(result["stop"], "review_cap")

    def test_needs_human_stops_at_intake(self):
        result = walk(ScriptHandler(intake="needs_human"))
        self.assertEqual(result["path"], ["intake", "done"])
        self.assertEqual(result["stop"], "needs_human")

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if model == "claude-opus-5-5-high":
                raise QuotaExhausted(model)
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["models_used"]["intake"], "grok-4.7-high")
        self.assertEqual(result["models_used"]["review"], "grok-4.7-high")

    def test_every_model_exhausted_raises(self):
        def exhaust(node, model, state, instruction):
            raise QuotaExhausted(model)

        with self.assertRaises(QuotaExhausted):
            walk(exhaust)

    def test_node_instruction_does_not_carry_the_model_list(self):
        handler = ScriptHandler()
        walk(handler)
        text = " ".join(call["instruction"] for call in handler.calls)
        self.assertNotIn("gpt-6.1-sol-high", text)
        self.assertIn("Do not edit it.", text)
        verify = next(call for call in handler.calls if call["node"] == "verify")
        self.assertIn("Command: npm run check", verify["instruction"])
        self.assertIn("ready or needs_human", INSTRUCTIONS["intake"])

    def test_diagram_names_the_nodes(self):
        picture = diagram()
        for name in ("intake", "spec", "implement", "review", "verify", "done"):
            self.assertIn(name, picture)
        self.assertNotIn("claude-opus", picture)
        compile_delivery(FailoverDispatcher(ScriptHandler()), load_config())


if __name__ == "__main__":
    unittest.main()
