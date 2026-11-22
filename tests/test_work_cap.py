import tests
import contextlib
import io
import json
from pathlib import Path
from subprocess import CompletedProcess
import unittest

from foreman.adapters import Call, Outcome, Usage
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.cli import _print_run
from foreman.delivery import run_delivery, run_tokens
from test_adapters import fixture_process
from test_dispatch_budget import configured, GPT, CLAUDE
from skill_fixture import skill_fixture
QUESTION = dict(triage="needs_human", work_kind="feature", slice=None, human_question="Which?")


class WorkCapTests(unittest.TestCase):
    def walk(self, outcome, cap=200_000):
        calls = []
        def handler(*args):
            calls.append(args[1].name)
            return outcome
        with skill_fixture(configured(models=(GPT, CLAUDE))) as (config, scratch):
            result = run_delivery(handler, repo="a/b", request="x", max_tokens=cap,
                                  max_budget_usd=None, config=config, scratch=scratch)
        return result, calls

    def test_engine_replay_and_fresh_threshold(self):
        usage = Usage(input_tokens=143086, output_tokens=26309,
                      cache_read_input_tokens=3460352, cache_creation_input_tokens=0)
        for kind, cap, stop in (("ran", 200000, "needs_human"), ("ran", 169396, "needs_human"),
                                ("ran", 169395, "budget_exceeded"), ("ran", 169394, "budget_exceeded"),
                                ("quota", 169395, "budget_exceeded"), ("failed", 169395, "budget_exceeded")):
            with self.subTest(cap=cap):
                result, calls = self.walk(Outcome(kind, QUESTION, usage=usage), cap)
                self.assertEqual(result["stop"], stop)
                self.assertEqual(len(calls), 1)
                self.assertEqual(run_tokens(result["attempts"]), 3629747)
                if stop == "budget_exceeded":
                    self.assertEqual(result["plan"].triage, "")
                    self.assertIn(f"Measured work overshoot: {169395-cap}", result["halt_detail"])

    def test_reports_known_subtotals_and_bucket_availability(self):
        envelope = json.loads(fixture_process(["claude"], "success", 0).stdout)
        cases = [(Outcome(kind, QUESTION, usage=u), raw, known, "unknown")
                 for kind in ("ran", "failed", "quota", "budget_exhausted")
                 for u, raw, known in ((None, 0, 0), (Usage(input_tokens=4, output_tokens=2), 6, 6),
                                       (Usage(input_tokens=4, output_tokens=2, total_cost_usd=0.1), 6, 6))]
        cases.append((Outcome("ran", QUESTION, usage=Usage(input_tokens=4, output_tokens=2, cache_creation_input_tokens=0)), 6, 6, "6"))
        codex = CodexAdapter(GPT).read(fixture_process(["codex"], "cached_success", 0), Call("", "{}", False, Path(".")))
        cases.append((Outcome("ran", QUESTION, usage=codex.usage, native_usage=codex.native_usage), 149, 69, "69"))
        for field, value, raw, known, work in (("cacheCreationInputTokens", None, 5713, 1728, "unknown"),
                                              ("cacheReadInputTokens", None, 5814, 5814, "5814"),
                                              ("cacheCreationInputTokens", "bad", 0, 0, "unknown"), ("cacheCreationInputTokens", -1, 0, 0, "unknown")):
            omitted = json.loads(json.dumps(envelope))
            row = omitted["modelUsage"]["claude-haiku-5-5"]
            row.pop(field) if value is None else row.update({field: value})
            outcome = ClaudeAdapter(CLAUDE).read(CompletedProcess([], 0, json.dumps(omitted), ""), Call("", "{}", False, Path(".")))
            cases.append((Outcome("ran", QUESTION, usage=outcome.usage, native_usage=outcome.native_usage), raw, known, work))
        for outcome, raw, known, work in cases:
            result, calls = self.walk(outcome)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["stop"], "usage_unknown" if work == "unknown" else "needs_human")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                _print_run(result)
            report = output.getvalue()
            self.assertIn(f"raw tokens {raw} (known subtotal)", report)
            coverage = "partial; unknown attempts 1" if work == "unknown" else "complete; unknown attempts 0"
            self.assertIn(f"work tokens used / work cap: {known} / 200000; {coverage}", report)
            self.assertIn(f"work={work}", report)
            usage = outcome.usage
            for bucket in ("input", "output", "cache_read", "cache_creation"):
                value = getattr(usage, f"{bucket}_input_tokens" if bucket.startswith("cache") else f"{bucket}_tokens", None)
                self.assertIn(f"{bucket} tokens {value or 0}; available {int(value is not None)}/1", report)
