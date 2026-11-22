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
from foreman.budget import work_tokens
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
                self.assertEqual(work_tokens(result["attempts"]), 169395)
                if stop == "budget_exceeded":
                    self.assertEqual(result["plan"].triage, "")
                    self.assertIn(f"Measured work overshoot: {169395-cap}", result["halt_detail"])

    def test_normalized_codex_and_claude_side_model_count_once(self):
        call = Call("", "{}", False, Path("."))
        codex = CodexAdapter(GPT).read(fixture_process(["codex"], "cached_success", 0), call)
        claude = ClaudeAdapter(CLAUDE).read(fixture_process(["claude"], "success", 0), call)
        for outcome, expected in ((codex, 69), (claude, 5814)):
            with self.subTest(expected=expected):
                result, _ = self.walk(Outcome("ran", QUESTION, usage=outcome.usage,
                                             native_usage=outcome.native_usage), expected)
                self.assertEqual(result["stop"], "budget_exceeded")
                self.assertEqual(work_tokens(result["attempts"]), expected)

    def test_unknown_work_never_falls_back(self):
        for kind in ("ran", "failed", "quota", "budget_exhausted"):
            for usage in (None, Usage(input_tokens=2, output_tokens=1),
                          Usage(input_tokens=2, output_tokens=1, total_cost_usd=0.1)):
                with self.subTest(kind=kind, usage=usage):
                    result, calls = self.walk(Outcome(kind, QUESTION, usage=usage))
                    self.assertEqual(result["stop"], "usage_unknown")
                    self.assertEqual(len(calls), 1)

    def test_native_omission_or_bad_count_is_unknown(self):
        source = fixture_process(["claude"], "success", 0)
        for value in (None, "bad", -1):
            envelope = json.loads(source.stdout)
            row = envelope["modelUsage"]["claude-haiku-5-5" if value is None else "claude-opus-5-5"]
            if value is None:
                del row["cacheCreationInputTokens"]
            else:
                row["cacheCreationInputTokens"] = value
            outcome = ClaudeAdapter(CLAUDE).read(
                CompletedProcess([], 0, json.dumps(envelope), ""), Call("", "{}", False, Path(".")))
            result, calls = self.walk(outcome)
            if value is None:
                self.assertEqual(run_tokens(result["attempts"]), 9799)
            self.assertEqual(result["stop"], "usage_unknown")
            self.assertEqual(len(calls), 1)

    def test_reports_known_subtotals_and_bucket_availability(self):
        for usage, coverage in ((None, "partial"), (Usage(input_tokens=4, output_tokens=2), "partial"),
                                (Usage(input_tokens=4, output_tokens=2, cache_creation_input_tokens=0), "complete")):
            result, _ = self.walk(Outcome("ran", QUESTION, usage=usage))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                _print_run(result)
            report = output.getvalue()
            self.assertIn("raw tokens", report)
            self.assertIn("work tokens used / work cap:", report)
            self.assertIn(coverage, report)
            for bucket in ("input", "output", "cache_read", "cache_creation"):
                self.assertIn(f"{bucket} tokens", report)
