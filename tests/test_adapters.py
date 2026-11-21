import json
from pathlib import Path
from subprocess import CompletedProcess
import unittest

from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec
from foreman.stage_output import STAGE_OUTPUTS, parse_output

FIXTURES = Path(__file__).with_name("fixtures")


def fixture_process(args, name, exit_code):
    folder = FIXTURES / args[0]
    stderr = folder / f"{name}.stderr.txt"
    return CompletedProcess(args, exit_code, (folder / f"{name}.json").read_text(),
                            stderr.read_text() if stderr.is_file() else "")


class NativeAdaptersTest(unittest.TestCase):
    def assert_envelopes(self, adapter, cli, cases, *, input_tokens, cached_tokens, cost):
        for name, exit_code, kind, has_usage in cases:
            with self.subTest(cli=cli, fixture=name):
                outcome = adapter.read(fixture_process([cli], name, exit_code))
                self.assertEqual(outcome.kind, kind)
                if not has_usage:
                    self.assertIsNone(outcome.usage)
                else:
                    self.assertEqual(outcome.usage.input_tokens, input_tokens)
                    self.assertEqual(outcome.usage.cache_read_input_tokens, cached_tokens)
                    self.assertEqual(outcome.usage.total_cost_usd, cost)
                if kind == "ran":
                    self.assertEqual(outcome.output, {
                        "triage": "needs_human", "slice": None, "human_question": "Which items?",
                    })
                    self.assertEqual(outcome.session_id, "4d8f5dbc-5b69-4be1-a541-402e6f92cafe")
                if name == "wrong_model":
                    self.assertIn("modelUsage reported", outcome.detail)

    def test_claude_envelopes(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        self.assert_envelopes(adapter, "claude", (
            ("success", 0, "ran", True),
            ("is_error", 1, "failed", False),
            ("quota", 1, "quota", True),
            ("missing_model_usage", 0, "ran", True),
            ("wrong_model", 0, "failed", True),
            ("zero_usage", 0, "ran", False),
        ), input_tokens=4, cached_tokens=3985, cost=0.037886699999999995)

    def test_grok_envelopes(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        self.assert_envelopes(adapter, "grok", (
            ("success", 0, "ran", True),
            ("is_error", 1, "failed", False),
            ("quota", 1, "quota", False),
            ("missing_model_usage", 0, "ran", True),
            ("wrong_model", 0, "failed", True),
            ("zero_usage", 0, "ran", False),
            ("incomplete_usage", 0, "ran", False),
        ), input_tokens=18007, cached_tokens=9344, cost=0.01448196)

    def test_grok_other_quota_messages(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        for message in (
            "You’ve hit your team’s API rate limit. Ask a team admin to purchase more credits for higher limits, "
            "or try again later. See https://docs.x.ai/developers/rate-limits#rate-limit-tiers",
            "You’ve reached your free Grok Build usage limit for now. Get SuperGrok for much higher limits, "
            "or try again later: https://grok.com/supergrok?referrer=grok-build",
        ):
            with self.subTest(message=message):
                process = CompletedProcess(["grok"], 1, json.dumps({"type": "error", "message": message}), "")
                self.assertEqual(adapter.read(process).kind, "quota")

    def test_grok_does_not_classify_a_partial_quota_message(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        message = "You’ve hit the rate limit for your plan."
        process = CompletedProcess(["grok"], 1, json.dumps({"type": "error", "message": message}), "")
        outcome = adapter.read(process)
        self.assertEqual((outcome.kind, outcome.detail), ("failed", message))

    def test_grok_requires_a_successful_stop_reason(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        reply = fixture_process(["grok"], "success", 0)
        envelope = json.loads(reply.stdout)
        for stop_reason in ("max_tokens", "unknown", None):
            with self.subTest(stop_reason=stop_reason):
                process = CompletedProcess(reply.args, 0, json.dumps({**envelope, "stopReason": stop_reason}), "")
                outcome = adapter.read(process)
                self.assertEqual((outcome.kind, outcome.detail),
                                 ("failed", f"Grok stopReason: {stop_reason or 'missing'}"))

    def test_grok_requires_only_the_reported_canonical_model(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        reply = fixture_process(["grok"], "success", 0)
        envelope = json.loads(reply.stdout)
        envelope["modelUsage"]["grok-4.6"] = {}
        outcome = adapter.read(CompletedProcess(reply.args, 0, json.dumps(envelope), ""))
        self.assertEqual(outcome.kind, "failed")


class CodexCompatibilityTest(unittest.TestCase):
    def read(self, stage, output):
        adapter = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), STAGE_OUTPUTS[stage])
        recorded = fixture_process(["codex"], "success", 0)
        return adapter.read(CompletedProcess(recorded.args, 0, json.dumps(output), recorded.stderr))

    def test_codex_recorded_envelope(self):
        adapter = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), STAGE_OUTPUTS["triage"])
        outcome = adapter.read(fixture_process(["codex"], "success", 0))
        self.assertEqual(outcome.kind, "ran")
        self.assertEqual(outcome.output, {"triage": "needs_human", "slice": None, "human_question": "Which items?"})
        self.assertIsNone(outcome.usage)

    def test_codex_quota_fixtures(self):
        adapter = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), STAGE_OUTPUTS["triage"])
        messages = (FIXTURES / "codex" / "quota.stderr.txt").read_text().splitlines()
        for message in messages:
            for prefix in ("", "ERROR: "):
                with self.subTest(message=message, prefix=prefix):
                    process = CompletedProcess(["codex"], 1, "", f"{prefix}{message}\n")
                    self.assertEqual(adapter.read(process).kind, "quota")

    def test_combines_sections_and_fills_nested_nullable_fields(self):
        outcome = self.read("implement", {"implementation": {"Changes": "Built.", "Tests": "Passed."},
                                          "browser_proof": {"trace": "trace.zip"}})
        self.assertEqual(outcome.kind, "ran")
        proof = parse_output(STAGE_OUTPUTS["implement"], outcome.output).model_dump()
        self.assertEqual(proof["implementation"], "Changes\nBuilt.\n\nTests\nPassed.")
        self.assertEqual(proof["browser_proof"], {"trace": "trace.zip", "screen": None, "passed": None})

    def test_normalizes_punctuated_verdicts(self):
        for raw, verdict, note in (
            ("approve.", "approve", None),
            ("changes.", "changes", None),
            ("changes. Add a check.", "changes", "Add a check."),
        ):
            with self.subTest(raw=raw):
                outcome = self.read("review", {"review": raw})
                self.assertEqual(outcome.output, {"review": verdict, "review_note": note})

    def test_rejects_two_change_notes(self):
        outcome = self.read("review", {"review": "changes. Add a check.", "review_note": "Update the docs."})
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("inline change note conflicts with review_note", outcome.detail)
