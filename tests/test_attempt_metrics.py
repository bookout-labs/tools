import tests

import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Call
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.budget import claude_spend
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import LiveRun, compile_delivery, run_delivery, run_tokens
from foreman.dry_run import DryRun
from foreman.state import checkpoint_serializer
from foreman.worker import CliWorker
from skill_fixture import skill_fixture
from test_adapters import fixture_process


class AttemptMetricsTests(unittest.TestCase):
    def test_native_details_only_report_proven_aggregation(self):
        call = Call("", "{}", False, Path("."))
        for family, model, cls in (("claude", "claude-opus-5-5-high", ClaudeAdapter),
                                   ("gpt", "gpt-6.1-sol-high", CodexAdapter),
                                   ("grok", "grok-4.7-high", GrokAdapter)):
            cli = "codex" if family == "gpt" else family
            adapter = cls(ModelSpec.parse(model))
            cases = [("success", 0), ("quota", 1), ("zero_usage", 0)]
            if cli == "claude":
                cases += [("wrong_model", 0), ("max_budget", 1)]
            for fixture, code in cases:
                if cli == "codex" and fixture == "zero_usage":
                    continue
                with self.subTest(family=family, fixture=fixture):
                    outcome = adapter.read(fixture_process([cli], fixture, code), call)
                    if outcome.usage is None:
                        self.assertIsNone(outcome.native_usage)
                        continue
                    details = outcome.native_usage
                    self.assertEqual(details["family"], family)
                    self.assertEqual(details["aggregation"], "turn_total" if cli == "codex" else "cli_model_totals")
                    self.assertEqual(sum(row["input_tokens"] for row in details["entries"].values()),
                                     outcome.usage.input_tokens)
                    if cli == "claude" and fixture == "success":
                        self.assertEqual(details["entries"]["claude-haiku-5-5"]["input_tokens"], 1477)
                        self.assertEqual(details["entries"]["claude-haiku-5-5"]["cache_read_input_tokens"], 0)
        cached = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")).read(
            fixture_process(["codex"], "cached_success", 0), call)
        self.assertEqual(cached.native_usage["entries"]["turn"]["input_tokens"], 40)
        self.assertEqual(cached.native_usage["entries"]["turn"]["cache_read_input_tokens"], 80)

    def test_missing_and_malformed_optional_counts_are_not_invented(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        source = fixture_process(["claude"], "success", 0)
        for value in (None, "bad", -1):
            envelope = json.loads(source.stdout)
            row = envelope["modelUsage"]["claude-opus-5-5"]
            if value is None:
                del row["cacheReadInputTokens"]
            else:
                row["cacheReadInputTokens"] = value
            outcome = adapter.read(CompletedProcess([], 0, json.dumps(envelope), ""), Call("", "{}", False, Path(".")))
            if value is None:
                self.assertIsNone(outcome.native_usage["entries"]["claude-opus-5-5"]["cache_read_input_tokens"])
            else:
                self.assertIsNone(outcome.native_usage)
                self.assertIsNone(outcome.usage)

    def test_final_prompt_bytes_and_projection_without_metric_replay(self):
        prompts = []
        def run(args, cwd, env):
            prompts.append(args[-1])
            return fixture_process(args, "success", 0)
        worker = CliWorker(Path("."), run)
        model = ModelSpec.parse("claude-opus-5-5-high")
        state = {"attempts": [{"stage": "triage", "prompt_bytes": 999, "native_usage": {"secret": "not input"}}]}
        first = worker("triage", model, state, "Read café 🐈.")
        second = worker("triage", model, {"attempts": [{"stage": "triage"}]}, "Read café 🐈.")
        third = worker("triage", model, {}, "Read café 🐈.")
        self.assertEqual(first.prompt_bytes, len(prompts[0].encode("utf-8")))
        self.assertGreater(first.prompt_bytes, len(prompts[0]))
        self.assertEqual(first.prompt_bytes, second.prompt_bytes)
        self.assertNotEqual(first.prompt_bytes, third.prompt_bytes)
        self.assertIn("prompt_bytes", state["attempts"][0])
        self.assertNotIn("native_usage", prompts[0])
        self.assertNotIn("output schema:", prompts[0])
        codex = CliWorker(Path("."), lambda args, cwd, env: fixture_process(args, "cached_success", 0))(
            "triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "Read.")
        self.assertEqual(codex.native_usage["entries"]["turn"]["input_tokens"], 40)
        self.assertIsNotNone(codex.prompt_bytes)
        def missing(*args):
            raise FileNotFoundError()
        unavailable = CliWorker(Path("."), missing)("triage", model, {}, "Read café 🐈.")
        self.assertEqual(unavailable.prompt_bytes, third.prompt_bytes)
        self.assertIsNone(unavailable.usage)
        self.assertIsNone(unavailable.native_usage)
        blocked = worker("triage", ModelSpec.parse("grok-4.7-high"), {}, "Read.")
        self.assertEqual(blocked.kind, "cli_missing")
        self.assertIsNotNone(blocked.prompt_bytes)
        self.assertIsNone(blocked.native_usage)

    def test_real_attempt_seam_checkpoint_and_unchanged_ledgers(self):
        config = load_config()
        config.nodes["triage"] = NodeConfig("coordinator", (ModelSpec.parse("claude-opus-5-5-high"),))
        worker = CliWorker(Path("."), lambda args, cwd, env: fixture_process(args, "success", 0))
        with skill_fixture(config) as (config, scratch), TemporaryDirectory() as directory:
            result = run_delivery(worker, repo="Example/Engine", request="List items", check="true",
                                  config=config, scratch=scratch)
            attempt = result["attempts"][0]
            self.assertGreater(attempt["prompt_bytes"], 0)
            self.assertEqual(attempt["native_usage"]["family"], "claude")
            bare = {key: value for key, value in attempt.items() if key not in ("prompt_bytes", "native_usage")}
            self.assertEqual(run_tokens([attempt]), run_tokens([bare]))
            self.assertEqual(run_tokens([attempt]), 9799)
            self.assertEqual(claude_spend([attempt]), claude_spend([bare]))
            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint.sqlite")) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(worker, config, run=LiveRun(DryRun(), DryRun()), checkpointer=saver)
                thread = {"configurable": {"thread_id": "metrics"}}
                graph.update_state(thread, result, as_node="done")
                self.assertEqual(graph.get_state(thread).values["attempts"], [attempt])
            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint.sqlite")) as restored:
                restored.serde = checkpoint_serializer()
                self.assertEqual(restored.get(thread)["channel_values"]["attempts"], [attempt])
