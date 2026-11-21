import json
from pathlib import Path
from subprocess import CompletedProcess
import unittest

from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec
from foreman.stage_output import Contract, STAGE_OUTPUTS

FIXTURES = Path(__file__).with_name("fixtures")


def fixture_process(args, name, exit_code):
    folder = FIXTURES / args[0]
    stderr = folder / f"{name}.stderr.txt"
    suffix = "jsonl" if args[0] == "codex" else "json"
    return CompletedProcess(args, exit_code, (folder / f"{name}.{suffix}").read_text(),
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
            ("model_substitution_disabled", 1, "failed", False),
            ("missing_model_usage", 0, "failed", True),
            ("wrong_model", 0, "failed", True),
            ("zero_usage", 0, "failed", False),
        ), input_tokens=4, cached_tokens=3985, cost=0.037886699999999995)

    def test_grok_envelopes(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        self.assert_envelopes(adapter, "grok", (
            ("success", 0, "ran", True),
            ("is_error", 1, "failed", False),
            ("quota", 1, "quota", False),
            ("missing_model_usage", 0, "failed", True),
            ("zero_usage", 0, "failed", False),
            ("incomplete_usage", 0, "ran", False),
        ), input_tokens=18007, cached_tokens=9344, cost=0.01448196)

    def test_claude_quota_by_status_or_api_error(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        recorded = fixture_process(["claude"], "quota", 1)
        envelope = json.loads(recorded.stdout)
        for status, api_error in ((429, None), (None, "usage_limit_reached"), (None, "model_requires_usage_credits")):
            with self.subTest(status=status, api_error=api_error):
                envelope.update(api_error_status=status, api_error=api_error)
                process = CompletedProcess(recorded.args, 1, json.dumps(envelope), "")
                self.assertEqual(adapter.read(process).kind, "quota")

    def test_claude_model_substitution_is_a_failure(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        outcome = adapter.read(fixture_process(["claude"], "model_substitution_disabled", 1))
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("forbids model substitution", outcome.detail)

    def test_missing_or_empty_model_usage_fails_but_zero_token_usage_can_run(self):
        cases = (
            ("claude", ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high")),
             "modelUsage missing; cannot confirm claude-opus-5-5"),
            ("grok", GrokAdapter(ModelSpec.parse("grok-4.7-high")),
             "modelUsage missing; cannot confirm a model ran"),
        )
        for cli, adapter, detail in cases:
            with self.subTest(cli=cli):
                recorded = fixture_process([cli], "zero_usage", 0)
                envelope = json.loads(recorded.stdout)
                envelope.pop("modelUsage", None)
                for fields in ({}, {"modelUsage": {}}):
                    process = CompletedProcess(recorded.args, 0, json.dumps({**envelope, **fields}), "")
                    outcome = adapter.read(process)
                    self.assertEqual((outcome.kind, outcome.detail), ("failed", detail))
                success = fixture_process([cli], "success", 0)
                envelope["modelUsage"] = json.loads(success.stdout)["modelUsage"]
                process = CompletedProcess(recorded.args, 0, json.dumps(envelope), "")
                outcome = adapter.read(process)
                self.assertEqual(outcome.kind, "ran")
                self.assertIsNone(outcome.usage)

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

    def test_grok_accepts_the_recorded_usage_key_for_a_valid_cli_model(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        recorded = fixture_process(["grok"], "catalog_alias", 0)
        self.assertEqual(list(json.loads(recorded.stdout)["modelUsage"]), ["grok-4.7-build"])
        outcome = adapter.read(recorded)
        self.assertEqual((outcome.kind, outcome.output), ("ran", {"answer": "ok"}))


def codex_events():
    return [json.loads(line) for line in fixture_process(["codex"], "success", 0).stdout.splitlines()]


def codex_process(events, exit_code=0, stderr=""):
    return CompletedProcess(["codex"], exit_code, "\n".join(json.dumps(event, ensure_ascii=False) for event in events), stderr)


class RecordedTriage(Contract):
    triage: str
    slice: None
    human_question: str


class CodexAdapterTest(unittest.TestCase):
    adapter = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), RecordedTriage)

    def test_recorded_success(self):
        outcome = self.adapter.read(fixture_process(["codex"], "success", 0))
        self.assertEqual(outcome.kind, "ran")
        self.assertEqual(outcome.output, {"triage": "needs_human", "slice": None, "human_question": "Which items?"})
        self.assertEqual(outcome.session_id, "00000000-0000-4000-8000-000000000001")
        self.assertIsNotNone(outcome.usage)
        assert outcome.usage is not None
        self.assertEqual((outcome.usage.input_tokens, outcome.usage.output_tokens), (27979, 29))
        self.assertEqual((outcome.usage.cache_read_input_tokens, outcome.usage.cache_creation_input_tokens), (0, 0))
        self.assertIsNone(outcome.usage.total_cost_usd)

    def test_recorded_quota(self):
        outcome = self.adapter.read(fixture_process(["codex"], "quota", 1))
        self.assertEqual(outcome.kind, "quota")
        self.assertIn("Quota exceeded.", outcome.detail)

    def test_stderr_rate_limit_is_a_failure(self):
        process = codex_process([], exit_code=1, stderr='Prompt: Explain "rate limit" and HTTP 429.')
        outcome = self.adapter.read(process)
        self.assertEqual((outcome.kind, outcome.detail), ("failed", "Codex exited 1"))

    def test_invalid_model_is_a_failure(self):
        outcome = self.adapter.read(fixture_process(["codex"], "invalid-model", 1))
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("is not supported", outcome.detail)

    def test_schema_invalid_final_output_is_a_failure(self):
        events = codex_events()
        events[-2]["item"]["text"] = '{"triage":"needs_human","human_question":"Which items?"}'
        outcome = self.adapter.read(codex_process(events))
        self.assertEqual((outcome.kind, outcome.detail), ("failed", "slice: Field required"))
        self.assertIsNotNone(outcome.usage)

    def test_missing_and_zero_usage_are_unknown(self):
        for report in ({}, {"usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}}):
            with self.subTest(report=report):
                events = codex_events()
                events[-1] = {"type": "turn.completed", **report}
                outcome = self.adapter.read(codex_process(events))
                self.assertEqual(outcome.kind, "ran")
                self.assertIsNone(outcome.usage)

    def test_rerouted_model_is_a_failure(self):
        events = codex_events()
        message = "model rerouted: gpt-6.1-sol -> gpt-other (SyntheticReason)"
        events.insert(2, {"type": "item.completed", "item": {"id": "item_1", "type": "error", "message": message}})
        outcome = self.adapter.read(codex_process(events))
        self.assertEqual((outcome.kind, outcome.detail), ("failed", message))

    def test_recovers_from_a_retry_and_reads_the_last_completed_message(self):
        events = codex_events()
        events[2:2] = [
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "Checking."}},
        ]
        question = "Which\u2028items?"
        events[-2]["item"]["text"] = json.dumps({
            "triage": "needs_human", "slice": None, "human_question": question,
        }, ensure_ascii=False)
        outcome = self.adapter.read(codex_process(events))
        self.assertEqual(outcome.kind, "ran")
        self.assertEqual(outcome.output["human_question"], question)

    def test_recovered_quota_error_cannot_override_the_final_result(self):
        events = codex_events()
        events.insert(2, {"type": "error", "message": "rate limit exceeded: synthetic API limit"})
        failed = {"type": "turn.failed", "error": {"message": "Turn interrupted by synthetic test."}}
        for terminal, detail in ((events[-1], "Codex exited 1"), (failed, "Turn interrupted by synthetic test.")):
            with self.subTest(terminal=terminal):
                outcome = self.adapter.read(codex_process([*events[:-1], terminal], exit_code=1))
                self.assertEqual((outcome.kind, outcome.detail), ("failed", detail))

    def test_agent_message_alone_does_not_complete_the_turn(self):
        outcome = self.adapter.read(codex_process(codex_events()[:-1]))
        self.assertEqual((outcome.kind, outcome.detail), ("failed", "Codex did not complete the turn"))

    def test_codex_review_rejects_the_obsolete_verdict_contract(self):
        events = codex_events()
        events[-2]["item"]["text"] = json.dumps({"review": "changes. Add a check."})
        adapter = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"), STAGE_OUTPUTS["review"])
        outcome = adapter.read(codex_process(events))
        self.assertEqual(outcome.kind, "failed")
        self.assertTrue(outcome.detail.startswith("resolutions:"))

    def test_quota_formatters_as_terminal_failures(self):
        cases = json.loads((FIXTURES / "codex" / "quota-formatters.json").read_text())
        for case in cases:
            with self.subTest(formatter=case["formatter"]):
                message = case["message"]
                process = codex_process([{"type": "turn.failed", "error": {"message": message}}], exit_code=1)
                outcome = self.adapter.read(process)
                self.assertEqual((outcome.kind, outcome.detail), ("quota", message))

    def test_quota_near_misses_are_failures(self):
        messages = [
            "Quota exceeded for a local test budget.",
            "rate limit exceeded locally",
            "exceeded retry limit, last status: 503 Service Unavailable",
            "You’ve hit your usage limiter.",
            "You’ve hit your usage limit forensics failed.",
            "Your workspace is out of disk space.",
            "You hit your spend cap set in your workspace settings file is invalid.",
            "You hit your spend cap set by the owner of your workspace settings file is invalid.",
            "Reconnecting... 1/5 (rate limit exceeded: synthetic API limit)",
        ]
        for message in messages:
            with self.subTest(message=message):
                process = codex_process([{"type": "turn.failed", "error": {"message": message}}], exit_code=1)
                outcome = self.adapter.read(process)
                self.assertEqual((outcome.kind, outcome.detail), ("failed", message))

    def test_recorded_cached_usage_counts_input_once(self):
        outcome = self.adapter.read(fixture_process(["codex"], "cached-success", 0))
        self.assertEqual(outcome.kind, "ran")
        assert outcome.usage is not None
        self.assertEqual(outcome.usage.input_tokens, 40)
        self.assertEqual(outcome.usage.cache_read_input_tokens, 80)
        self.assertEqual(outcome.usage.output_tokens, 29)

    def test_cached_usage_clamps_and_missing_totals(self):
        for input_tokens, cached, expected in ((10, 20, (0, 20)), (10, -5, (10, 0))):
            with self.subTest(input_tokens=input_tokens, cached=cached):
                events = codex_events()
                events[-1]["usage"].update(input_tokens=input_tokens, cached_input_tokens=cached)
                usage = self.adapter.read(codex_process(events)).usage
                assert usage is not None
                self.assertEqual((usage.input_tokens, usage.cache_read_input_tokens), expected)
        events = codex_events()
        del events[-1]["usage"]["cached_input_tokens"]
        self.assertIsNone(self.adapter.read(codex_process(events)).usage)
