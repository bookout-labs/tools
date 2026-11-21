import json
from subprocess import CompletedProcess
import unittest

from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.stage_output import Contract, STAGE_OUTPUTS
from foreman.config import ModelSpec
from foreman.delivery import run_tokens, run_delivery
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from tests.test_adapters import fixture_process


class UsageTests(unittest.TestCase):
    def test_captured_totals_and_cost(self):
        for cli, adapter, fixture, total in (
            ("claude", ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high")), "success", 9799),
            ("grok", GrokAdapter(ModelSpec.parse("grok-4.7-high")), "success", 27669),
            ("codex", CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), STAGE_OUTPUTS["triage"]), "cached-success", 149),
        ):
            with self.subTest(cli=cli):
                process = fixture_process([cli], fixture, 0)
                usage = adapter.read(process).usage
                self.assertIsNotNone(usage)
                self.assertEqual(usage.input_tokens + usage.output_tokens +
                                 (usage.cache_read_input_tokens or 0) + (usage.cache_creation_input_tokens or 0), total)
                if cli != "codex":
                    self.assertAlmostEqual(usage.total_cost_usd, json.loads(process.stdout)["total_cost_usd"])

    def test_malformed_and_zero_ledgers(self):
        for cli, adapter in (("claude", ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))),
                             ("grok", GrokAdapter(ModelSpec.parse("grok-4.7-high")))):
            for name in ("negative-side", "partial-side", "fractional-side", "zero-cost"):
                with self.subTest(cli=cli, name=name):
                    result = adapter.read(fixture_process([cli], name, 0))
                    self.assertIsNone(result.usage)
                    self.assertEqual(result.kind, "ran" if name == "zero-cost" else "failed")

    def test_all_attempts_and_optional_cache(self):
        result = run_delivery(FailoverDispatcher(ScriptHandler()), repo="a/b", request="x")
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "implement", "verify", "review"])
        self.assertEqual(run_tokens(result["attempts"]), 8)
        attempt = {"stage": "triage", "model": None, "commit": "x", "methods": [], "outcome": "quota",
                   "usage": {"input": 3, "output": 4, "cost_usd": 0.2}}
        self.assertEqual(run_tokens([attempt]), 7)
        attempt["usage"] = None
        self.assertEqual(run_tokens([attempt]), 0)
