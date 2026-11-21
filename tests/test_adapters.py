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
    return CompletedProcess(args, exit_code, (FIXTURES / args[0] / f"{name}.json").read_text(), "")


class NativeAdaptersTest(unittest.TestCase):
    def assert_envelopes(self, adapter, cli, cases):
        for name, exit_code, kind, input_tokens in cases:
            with self.subTest(cli=cli, fixture=name):
                outcome = adapter.read(fixture_process([cli], name, exit_code))
                self.assertEqual(outcome.kind, kind)
                if input_tokens is None:
                    self.assertIsNone(outcome.usage)
                else:
                    self.assertEqual(outcome.usage.input_tokens, input_tokens)
                    self.assertEqual(outcome.usage.cache_read_input_tokens, 8)
                    self.assertEqual(outcome.usage.total_cost_usd, 0.0001)
                if kind == "ran":
                    self.assertEqual(outcome.output, {
                        "triage": "needs_human", "slice": None, "human_question": "Which items?",
                    })
                    self.assertEqual(outcome.session_id, "4d8f5dbc-5b69-4be1-a541-402e6f92cafe")
                if name == "wrong_model":
                    self.assertIn(f"requested {adapter.model.model_id}, modelUsage reported", outcome.detail)

    def test_claude_envelopes(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"), STAGE_OUTPUTS["triage"])
        self.assert_envelopes(adapter, "claude", (
            ("success", 0, "ran", 4),
            ("is_error", 1, "failed", None),
            ("quota", 1, "quota", 4),
            ("missing_model_usage", 0, "ran", 4),
            ("wrong_model", 0, "failed", 4),
            ("zero_usage", 0, "ran", None),
        ))

    def test_grok_envelopes(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"), STAGE_OUTPUTS["triage"])
        self.assert_envelopes(adapter, "grok", (
            ("success", 0, "ran", 4),
            ("is_error", 1, "failed", None),
            ("quota", 1, "quota", None),
            ("missing_model_usage", 0, "ran", 4),
            ("wrong_model", 0, "failed", 4),
            ("zero_usage", 0, "ran", None),
            ("incomplete_usage", 0, "ran", None),
        ))

    def test_grok_other_quota_messages(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"), STAGE_OUTPUTS["triage"])
        for message in (
            "You’ve hit your team’s API rate limit. Ask a team admin to purchase more credits for higher limits, "
            "or try again later. See https://docs.x.ai/developers/rate-limits#rate-limit-tiers",
            "You’ve reached your free Grok Build usage limit for now. Get SuperGrok for much higher limits, "
            "or try again later: https://grok.com/supergrok?referrer=grok-build",
            "subscription:free-usage-exhausted: Free usage exhausted",
        ):
            with self.subTest(message=message):
                process = CompletedProcess(["grok"], 1, json.dumps({"type": "error", "message": message}), "")
                self.assertEqual(adapter.read(process).kind, "quota")

    def test_grok_does_not_classify_a_partial_quota_message(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"), STAGE_OUTPUTS["triage"])
        message = "You’ve hit the rate limit for your plan."
        process = CompletedProcess(["grok"], 1, json.dumps({"type": "error", "message": message}), "")
        self.assertEqual(adapter.read(process).kind, "failed")


class CodexCompatibilityTest(unittest.TestCase):
    def read(self, stage, output):
        adapter = CodexAdapter(ModelSpec.parse("gpt-test"), STAGE_OUTPUTS[stage])
        return adapter.read(CompletedProcess(["codex"], 0, json.dumps(output), "model: gpt-test\n"))

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
