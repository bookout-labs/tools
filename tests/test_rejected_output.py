import tests

import unittest

from foreman.adapters import Outcome, Usage
from foreman.config import load_config
from foreman.delivery import MODEL_STAGES
from foreman.dispatch import InvalidOutput
from foreman.dry_run import ScriptHandler
from foreman.review_context import SCRIPTED_REVIEW_CONTEXT
from foreman.stage_output import STAGE_OUTPUTS, parse_output
from test_delivery import walk


class RejectedStageOutputTest(unittest.TestCase):
    def test_every_dispatched_stage_names_the_returning_model_and_keeps_usage(self):
        usage = Usage(input_tokens=11, output_tokens=7, cache_read_input_tokens=5,
                      cache_creation_input_tokens=3, total_cost_usd=0.125)
        for stage in MODEL_STAGES:
            for kind in ("ran", "failed"):
                for fallback in (None, "quota", "cli_missing"):
                    with self.subTest(stage=stage, kind=kind, fallback=fallback):
                        config = load_config()
                        script = ScriptHandler(triage="question" if stage == "research" else "spec" if stage == "spec" else "implement")
                        calls = []
                        diagnostic = "adapter could not parse JSON"
                        if kind == "ran":
                            with self.assertRaises(InvalidOutput) as raised:
                                parse_output(STAGE_OUTPUTS[stage], {}, context=SCRIPTED_REVIEW_CONTEXT)
                            diagnostic = str(raised.exception)

                        def worker(node, model, state, instruction):
                            if node == stage:
                                calls.append(model.name if model else None)
                                if fallback and len(calls) == 1:
                                    return Outcome(fallback)
                                return Outcome(kind, {}, usage=usage, detail="" if kind == "ran" else diagnostic)
                            return script(node, model, state, instruction)

                        result = walk(worker, config=config)
                        expected_model = calls[-1] or "no model"
                        self.assertEqual(result["halt"], "invalid_output")
                        self.assertEqual(result["halt_detail"], f"{stage} output from {expected_model}: {diagnostic}")
                        self.assertIsNone(result["pending_dispatch"])
                        attempts = [a for a in result["attempts"] if a["stage"] == stage]
                        self.assertEqual([a["model"] for a in attempts], calls)
                        self.assertEqual([a["outcome"] for a in attempts], [fallback, kind] if fallback else [kind])
                        self.assertEqual(attempts[-1]["usage"], {
                            "input": 11, "output": 7, "cache_read": 5, "cache_creation": 3, "cost_usd": 0.125,
                        })
                        self.assertNotIn(stage, result.get("models_used", {}))
                        if fallback:
                            self.assertEqual(len(calls), 2)
                            self.assertNotEqual(calls[0], calls[1])
