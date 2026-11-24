import tests

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.config import ModelSpec

CALL = Call("", "{}", False, Path("."))
SESSION = "01a1ff05-7685-7282-b515-828a0b63ee12"
FIXTURES = Path(__file__).with_name("fixtures")


def fixture_process(args, name, exit_code):
    folder = FIXTURES / args[0]
    stderr = folder / f"{name}.stderr.txt"
    suffix = "jsonl" if args[0] == "codex" else "json"
    return CompletedProcess(args, exit_code, (folder / f"{name}.{suffix}").read_text(),
                            stderr.read_text() if stderr.is_file() else "")


def claude_review_stream(envelope, reads=(), errors=()):
    """A Claude review's stream-json stdout: one top-level Bash call per command, then the result envelope."""
    events = []
    for index, command in enumerate((*reads, *errors)):
        use = f"toolu_{index:02}"
        events += [{"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                       {"type": "tool_use", "id": use, "name": "Bash", "input": {"command": command}}]}},
                   {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                       {"type": "tool_result", "tool_use_id": use, "content": "output", "is_error": command in errors}]}}]
    return "".join(json.dumps(event) + "\n" for event in (*events, envelope))


@contextmanager
def worker_process(completed=None):
    """Stand in for the worker's process group. Yields the Popen mock; its `prompt` is what the worker read on stdin."""
    completed = completed or CompletedProcess([], 0, "", "")

    def start(command, **kwargs):
        stdin = kwargs["stdin"]
        popen.prompt = None if stdin == subprocess.DEVNULL else stdin.read()
        kwargs["stdout"].write(completed.stdout)
        kwargs["stderr"].write(completed.stderr)
        return MagicMock(pid=-1, **{"wait.return_value": completed.returncode})

    with patch("foreman.process_group.subprocess.Popen", side_effect=start) as popen, \
         patch("foreman.process_group.os.killpg", side_effect=ProcessLookupError):
        yield popen

class NativeAdaptersTest(unittest.TestCase):
    def test_claude_budget_stop_takes_precedence_and_keeps_usage(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        for code in (0, 1):
            recorded = fixture_process(["claude"], "max_budget", code)
            original = json.loads(recorded.stdout)
            for changes in ({}, {"is_error": False}, {"api_error": "usage_limit_reached", "api_error_status": 429},
                            {"api_error": "other_error"}, {"structured_output": {"triage": "implement"}},
                            {"errors": []}):
                with self.subTest(code=code, changes=changes):
                    process = CompletedProcess(recorded.args, code, json.dumps({**original, **changes}), "")
                    outcome = adapter.read(process, CALL)
                    self.assertEqual(outcome.kind, "budget_exhausted")
                    self.assertEqual(outcome.session_id, "4d8f5dbc-5b69-4be1-a541-402e6f92cafe")
                    self.assertTrue(outcome.detail)
                    self.assertEqual(outcome.output, {})
                    self.assertEqual((outcome.usage.input_tokens, outcome.usage.output_tokens), (1481, 247))
                    self.assertAlmostEqual(outcome.usage.total_cost_usd, 0.0378867)

    def assert_envelopes(self, adapter, cli, cases):
        for name, exit_code, kind, expected in cases:
            has_usage = expected is not None
            with self.subTest(cli=cli, fixture=name):
                outcome = adapter.read(fixture_process([cli], name, exit_code), CALL)
                self.assertEqual(outcome.kind, kind)
                if not has_usage:
                    self.assertIsNone(outcome.usage)
                else:
                    self.assertEqual(outcome.usage.input_tokens, expected[0])
                    self.assertEqual(outcome.usage.cache_read_input_tokens, expected[1])
                    if expected[2] is None:
                        self.assertIsNone(outcome.usage.total_cost_usd)
                    else:
                        self.assertAlmostEqual(outcome.usage.total_cost_usd, expected[2])
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
            ("success", 0, "ran", (1481, 3985, 0.037886699999999995)),
            ("is_error", 1, "failed", None),
            ("quota", 1, "quota", (1477, 0, None)),
            ("model_substitution_disabled", 1, "failed", None),
            ("missing_model_usage", 0, "failed", None),
            ("wrong_model", 0, "failed", (1477, 0, None)),
            ("zero_usage", 0, "failed", None),
        ))

    def test_grok_envelopes(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        self.assert_envelopes(adapter, "grok", (
            ("success", 0, "ran", (18007, 9344, 0.01448196)),
            ("is_error", 1, "failed", None),
            ("quota", 1, "quota", None),
            ("missing_model_usage", 0, "failed", None),
            ("zero_usage", 0, "failed", None),
            ("incomplete_usage", 0, "ran", None),
        ))

    def test_claude_quota_by_status_or_api_error(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        recorded = fixture_process(["claude"], "quota", 1)
        envelope = json.loads(recorded.stdout)
        for status, api_error in ((429, None), (None, "usage_limit_reached"), (None, "model_requires_usage_credits")):
            with self.subTest(status=status, api_error=api_error):
                envelope.update(api_error_status=status, api_error=api_error)
                process = CompletedProcess(recorded.args, 1, json.dumps(envelope), "")
                self.assertEqual(adapter.read(process, CALL).kind, "quota")

    def test_claude_model_substitution_is_a_failure(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        outcome = adapter.read(fixture_process(["claude"], "model_substitution_disabled", 1), CALL)
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
                    outcome = adapter.read(process, CALL)
                    self.assertEqual((outcome.kind, outcome.detail), ("failed", detail))
                success = fixture_process([cli], "success", 0)
                envelope["modelUsage"] = json.loads(success.stdout)["modelUsage"]
                for row in envelope["modelUsage"].values():
                    for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
                        row[key] = 0
                process = CompletedProcess(recorded.args, 0, json.dumps(envelope), "")
                outcome = adapter.read(process, CALL)
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
                self.assertEqual(adapter.read(process, CALL).kind, "quota")

    def test_grok_does_not_classify_a_partial_quota_message(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        message = "You’ve hit the rate limit for your plan."
        process = CompletedProcess(["grok"], 1, json.dumps({"type": "error", "message": message}), "")
        outcome = adapter.read(process, CALL)
        self.assertEqual((outcome.kind, outcome.detail), ("failed", message))

    def test_grok_requires_a_successful_stop_reason(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        reply = fixture_process(["grok"], "success", 0)
        envelope = json.loads(reply.stdout)
        for stop_reason in ("max_tokens", "unknown", None):
            with self.subTest(stop_reason=stop_reason):
                process = CompletedProcess(reply.args, 0, json.dumps({**envelope, "stopReason": stop_reason}), "")
                outcome = adapter.read(process, CALL)
                self.assertEqual((outcome.kind, outcome.detail),
                                 ("failed", f"Grok stopReason: {stop_reason or 'missing'}"))

    def test_grok_accepts_the_recorded_usage_key_for_a_valid_cli_model(self):
        adapter = GrokAdapter(ModelSpec.parse("grok-4.7-high"))
        recorded = fixture_process(["grok"], "catalog_alias", 0)
        self.assertEqual(list(json.loads(recorded.stdout)["modelUsage"]), ["grok-4.7-build"])
        outcome = adapter.read(recorded, CALL)
        self.assertEqual((outcome.kind, outcome.output), ("ran", {"answer": "ok"}))


def codex_events():
    return [json.loads(line) for line in fixture_process(["codex"], "success", 0).stdout.splitlines()]


def codex_process(events, exit_code=0, stderr=""):
    return CompletedProcess(["codex"], exit_code, "\n".join(json.dumps(event, ensure_ascii=False) for event in events), stderr)


STARTED = {"type": "event_msg", "payload": {"type": "task_started"}}


def rollout_count(total, cached, output):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": total, "cached_input_tokens": cached, "cache_write_input_tokens": 0, "output_tokens": output}}}}


class CodexAdapterTest(unittest.TestCase):
    adapter = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"))

    def test_recorded_success(self):
        outcome = self.adapter.read(fixture_process(["codex"], "success", 0), CALL)
        self.assertEqual(outcome.kind, "ran")
        self.assertEqual(outcome.output, {"triage": "needs_human", "slice": None, "human_question": "Which items?"})
        self.assertEqual(outcome.session_id, "00000000-0000-4000-8000-000000000001")
        self.assertIsNotNone(outcome.usage)
        assert outcome.usage is not None
        self.assertEqual((outcome.usage.input_tokens, outcome.usage.output_tokens), (27979, 29))
        self.assertEqual((outcome.usage.cache_read_input_tokens, outcome.usage.cache_creation_input_tokens), (0, 0))
        self.assertIsNone(outcome.usage.total_cost_usd)

    def test_recorded_quota(self):
        outcome = self.adapter.read(fixture_process(["codex"], "quota", 1), CALL)
        self.assertEqual(outcome.kind, "quota")
        self.assertIn("Quota exceeded.", outcome.detail)

    def test_quota_charges_zero_only_without_model_items(self):
        # DP3: absent usage counts as zero only when the stream proves no paid work began.
        message = {"type": "turn.failed", "error": {"message": "You’ve hit your usage limit. Try again later."}}
        item = {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}}
        for events, usage in (([message], (0, 0)), ([item, message], None)):
            with self.subTest(events=len(events)), TemporaryDirectory() as home, patch.dict("os.environ", {"CODEX_HOME": home}):
                outcome = self.adapter.read(codex_process([{"type": "thread.started", "thread_id": SESSION}, *events], exit_code=1), CALL)
                self.assertEqual(outcome.kind, "quota")
                self.assertEqual(outcome.usage and (outcome.usage.input_tokens, outcome.usage.output_tokens), usage)

    def quota_after_model_items(self, rollout):
        events = [{"type": "thread.started", "thread_id": SESSION},
                  {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}},
                  {"type": "turn.failed", "error": {"message": "You’ve hit your usage limit. Try again later."}}]
        with TemporaryDirectory() as home, patch.dict("os.environ", {"CODEX_HOME": home}):
            day = Path(home, "sessions", "2026", "11", "23")
            day.mkdir(parents=True)
            (day / f"rollout-2026-11-23T08-00-00-{SESSION}.jsonl").write_text("\n".join(json.dumps(line) for line in rollout))
            outcome = self.adapter.read(codex_process(events, exit_code=1), CALL)
        self.assertEqual(outcome.kind, "quota")
        return outcome.usage

    def test_quota_after_model_items_is_charged_what_the_rollout_logged_for_the_turn(self):
        # DP3: a resumed session's rollout is cumulative, so the stopped turn is charged its own responses only.
        rollout = [{"type": "session_meta", "payload": {"id": SESSION}},
                   STARTED, rollout_count(1000, 0, 10), {"type": "event_msg", "payload": {"type": "task_complete"}},
                   STARTED, rollout_count(2500, 900, 40), rollout_count(4200, 2400, 75),
                   {"type": "event_msg", "payload": {"type": "token_count", "info": None}}]
        usage = self.quota_after_model_items(rollout)
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.cache_read_input_tokens, usage.cache_creation_input_tokens,
                          usage.output_tokens), (800, 2400, 0, 65))

    def test_quota_after_model_items_stays_unknown_when_the_rollout_does_not_show_the_turn(self):
        # D10: model items prove paid work, so a rollout without the stopped turn's complete count charges the worst case.
        missing_field = rollout_count(1000, 0, 10)
        del missing_field["payload"]["info"]["total_token_usage"]["cache_write_input_tokens"]
        for name, rollout in (("empty file", []),
                              ("no count after the start", [STARTED, rollout_count(1000, 0, 10),
                                                            {"type": "event_msg", "payload": {"type": "task_complete"}},
                                                            STARTED]),
                              ("missing field", [STARTED, missing_field])):
            with self.subTest(name):
                self.assertIsNone(self.quota_after_model_items(rollout))

    def test_every_call_keeps_its_rollout(self):
        with TemporaryDirectory() as directory:
            for edit in (False, True):
                command = self.adapter.command(Call("prompt", "{}", edit, Path(directory)))
                self.assertNotIn("--ephemeral", command)

    def test_stderr_rate_limit_is_a_failure(self):
        process = codex_process([], exit_code=1, stderr='Prompt: Explain "rate limit" and HTTP 429.')
        outcome = self.adapter.read(process, CALL)
        self.assertEqual((outcome.kind, outcome.detail), ("failed", "Codex exited 1"))

    def test_invalid_model_is_a_failure(self):
        outcome = self.adapter.read(fixture_process(["codex"], "invalid-model", 1), CALL)
        self.assertEqual(outcome.kind, "failed")
        self.assertIn("is not supported", outcome.detail)

    def test_schema_validation_is_left_to_delivery(self):
        events = codex_events()
        events[-2]["item"]["text"] = '{"triage":"needs_human","human_question":"Which items?"}'
        outcome = self.adapter.read(codex_process(events), CALL)
        self.assertEqual(outcome.kind, "ran")
        self.assertEqual(outcome.output, {"triage": "needs_human", "human_question": "Which items?"})
        self.assertIsNotNone(outcome.usage)

    def test_missing_and_zero_usage_are_unknown(self):
        for report in ({}, {"usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}}):
            with self.subTest(report=report):
                events = codex_events()
                events[-1] = {"type": "turn.completed", **report}
                outcome = self.adapter.read(codex_process(events), CALL)
                self.assertEqual(outcome.kind, "ran")
                self.assertIsNone(outcome.usage)

    def test_rerouted_model_is_a_failure(self):
        events = codex_events()
        message = "model rerouted: gpt-6.1-sol -> gpt-other (SyntheticReason)"
        events.insert(2, {"type": "item.completed", "item": {"id": "item_1", "type": "error", "message": message}})
        outcome = self.adapter.read(codex_process(events), CALL)
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
        outcome = self.adapter.read(codex_process(events), CALL)
        self.assertEqual(outcome.kind, "ran")
        self.assertEqual(outcome.output["human_question"], question)

    def test_recovered_quota_error_cannot_override_the_final_result(self):
        events = codex_events()
        events.insert(2, {"type": "error", "message": "rate limit exceeded: synthetic API limit"})
        failed = {"type": "turn.failed", "error": {"message": "Turn interrupted by synthetic test."}}
        for terminal, detail in ((events[-1], "Codex exited 1"), (failed, "Turn interrupted by synthetic test.")):
            with self.subTest(terminal=terminal):
                outcome = self.adapter.read(codex_process([*events[:-1], terminal], exit_code=1), CALL)
                self.assertEqual((outcome.kind, outcome.detail), ("failed", detail))

    def test_agent_message_alone_does_not_complete_the_turn(self):
        outcome = self.adapter.read(codex_process(codex_events()[:-1]), CALL)
        self.assertEqual((outcome.kind, outcome.detail), ("failed", "Codex did not complete the turn"))

    def test_quota_formatters_as_terminal_failures(self):
        cases = json.loads((FIXTURES / "codex" / "quota-formatters.json").read_text())
        for case in cases:
            with self.subTest(formatter=case["formatter"]):
                message = case["message"]
                process = codex_process([{"type": "turn.failed", "error": {"message": message}}], exit_code=1)
                outcome = self.adapter.read(process, CALL)
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
                outcome = self.adapter.read(process, CALL)
                self.assertEqual((outcome.kind, outcome.detail), ("failed", message))

    def test_recorded_cached_usage_counts_input_once(self):
        outcome = self.adapter.read(fixture_process(["codex"], "cached_success", 0), CALL)
        self.assertEqual(outcome.kind, "ran")
        assert outcome.usage is not None
        self.assertEqual(outcome.usage.input_tokens, 40)
        self.assertEqual(outcome.usage.cache_read_input_tokens, 80)
        self.assertEqual(outcome.usage.output_tokens, 29)

    def test_cached_usage_clamps_and_missing_totals(self):
        for input_tokens, cached in ((10, 20), (10, -5)):
            events = codex_events()
            events[-1]["usage"].update(input_tokens=input_tokens, cached_input_tokens=cached)
            self.assertIsNone(self.adapter.read(codex_process(events), CALL).usage)
        events = codex_events()
        del events[-1]["usage"]["cached_input_tokens"]
        self.assertIsNone(self.adapter.read(codex_process(events), CALL).usage)
