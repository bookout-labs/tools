import json
from subprocess import CompletedProcess
import unittest

from pydantic import TypeAdapter, ValidationError

from foreman.adapters import Usage
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.stage_output import AttemptUsage, AttemptUsageRecord, STAGE_OUTPUTS
from foreman.config import ModelSpec
from foreman.delivery import run_tokens, run_delivery
from foreman.dry_run import ScriptHandler
from tests.test_adapters import fixture_process


class UsageTests(unittest.TestCase):
    def test_claude_invalid_costs_keep_the_token_ledger(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        recorded = fixture_process(["claude"], "success", 0)
        for location in ("row", "total"):
            for invalid in (-1, float("nan"), float("inf"), "NaN", None, "missing", True):
                with self.subTest(location=location, invalid=invalid):
                    envelope = json.loads(recorded.stdout)
                    container = next(iter(envelope["modelUsage"].values())) if location == "row" else envelope
                    key = "costUSD" if location == "row" else "total_cost_usd"
                    if invalid == "missing":
                        container.pop(key)
                    else:
                        container[key] = invalid
                    outcome = adapter.read(CompletedProcess(recorded.args, 0, json.dumps(envelope), ""))
                    self.assertEqual(outcome.kind, "ran")
                    usage = outcome.usage
                    self.assertEqual((usage.input_tokens, usage.output_tokens,
                                      usage.cache_read_input_tokens, usage.cache_creation_input_tokens),
                                     (1481, 247, 3985, 4086))
                    self.assertIsNone(usage.total_cost_usd)

    def test_claude_costs_are_corroborated_and_counted_once_including_zero(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        recorded = fixture_process(["claude"], "success", 0)
        for delta, expected in ((0, 0.0378867), (0.0000005, 0.0378867), (0.001, None)):
            with self.subTest(delta=delta):
                envelope = json.loads(recorded.stdout)
                envelope["total_cost_usd"] += delta
                usage = adapter.read(CompletedProcess(recorded.args, 0, json.dumps(envelope), "")).usage
                if expected is None:
                    self.assertIsNone(usage.total_cost_usd)
                else:
                    self.assertAlmostEqual(usage.total_cost_usd, expected, places=12)
        envelope = json.loads(recorded.stdout)
        envelope["total_cost_usd"] = 0
        for row in envelope["modelUsage"].values():
            row["costUSD"] = 0
        usage = adapter.read(CompletedProcess(recorded.args, 0, json.dumps(envelope), "")).usage
        self.assertEqual(usage.total_cost_usd, 0.0)
        self.assertEqual(usage.input_tokens, 1481)
        envelope["total_cost_usd"] = 1e308
        for row in envelope["modelUsage"].values():
            row["costUSD"] = 1e308
        usage = adapter.read(CompletedProcess(recorded.args, 0, json.dumps(envelope), "")).usage
        self.assertIsNone(usage.total_cost_usd)
        self.assertEqual(usage.input_tokens, 1481)

    def test_cost_boundaries_reject_negative_and_nonfinite_dollars(self):
        attempt = {"input": 1, "output": 2, "cache_read": 0, "cache_creation": 0}
        for cost in (-1, float("inf"), float("-inf"), float("nan")):
            with self.subTest(cost=cost):
                with self.assertRaises(ValidationError):
                    Usage(input_tokens=1, output_tokens=2, total_cost_usd=cost)
                with self.assertRaises(ValidationError):
                    AttemptUsage(**attempt, cost_usd=cost)
                with self.assertRaises(ValidationError):
                    TypeAdapter(AttemptUsageRecord).validate_python({**attempt, "cost_usd": cost}, strict=True)

    def test_captured_totals_and_cost(self):
        for cli, adapter, fixture, total in (
            ("claude", ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high")), "success", 9799),
            ("grok", GrokAdapter(ModelSpec.parse("grok-4.7-high")), "success", 27669),
            ("codex", CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), STAGE_OUTPUTS["triage"]), "cached_success", 149),
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
            for name in ("negative_side", "partial_side", "fractional_side", "zero_tokens_with_cost"):
                with self.subTest(cli=cli, name=name):
                    result = adapter.read(fixture_process([cli], name, 0))
                    self.assertIsNone(result.usage)
                    self.assertEqual(result.kind, "ran")

    def test_all_attempts_and_required_cache(self):
        result = run_delivery(ScriptHandler(), repo="a/b", request="x")
        self.assertEqual([a["stage"] for a in result["attempts"]], ["triage", "implement", "review"])
        self.assertEqual(run_tokens(result["attempts"]), 6)
        attempt = {"stage": "triage", "model": None, "commit": "x", "methods": [], "outcome": "quota",
                   "usage": {"input": 3, "output": 4, "cache_read": 0, "cache_creation": 0, "cost_usd": 0.2}}
        self.assertEqual(run_tokens([attempt]), 7)
        attempt["usage"] = None
        self.assertEqual(run_tokens([attempt]), 0)

    def test_bad_ledger_preserves_quota_and_requires_all_costs(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        process = fixture_process(["claude"], "quota", 1)
        envelope = json.loads(process.stdout)
        row = next(iter(envelope["modelUsage"].values()))
        for invalid in (-1, 1.5, True):
            row["inputTokens"] = invalid
            self.assertEqual(adapter.read(CompletedProcess(process.args, 1, json.dumps(envelope), "")).kind, "quota")
            self.assertIsNone(adapter.read(CompletedProcess(process.args, 1, json.dumps(envelope), "")).usage)
        row["inputTokens"] = 1
        row["extra"] = "ignored"
        envelope["modelUsage"]["side"] = {"inputTokens": 1, "outputTokens": 1}
        usage = adapter.read(CompletedProcess(process.args, 1, json.dumps(envelope), "")).usage
        self.assertIsNotNone(usage)
        self.assertIsNone(usage.total_cost_usd)
