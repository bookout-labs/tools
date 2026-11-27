import tests

import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import TypeAdapter, ValidationError

from foreman.adapters import Call, NativeUsage, NativeUsageTokens, Outcome
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.adapters.grok import GrokAdapter
from foreman.budget import claude_spend
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import LiveRun, compile_delivery, run_delivery, run_tokens
from foreman.dry_run import DryRun
from foreman.runner import checkpoint_state
from foreman.state import checkpoint_serializer
from foreman.worker import CliWorker
from skill_fixture import skill_fixture
from test_adapters import fixture_process
from container_double import container_route


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
            if cli == "grok":
                cases += [("incomplete_usage", 0), ("zero_tokens_with_cost", 0), ("partial_side", 0)]
            for fixture, code in cases:
                if cli == "codex" and fixture == "zero_usage":
                    continue
                with self.subTest(family=family, fixture=fixture):
                    outcome = adapter.read(fixture_process([cli], fixture, code), call)
                    if outcome.usage is None:
                        # Claude and Grok keep the report's rows even when it charges nothing known (#446 T2-6).
                        if cli == "codex":
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
                        self.assertEqual(details["entries"]["claude-haiku-5-5"]["cache_creation_input_tokens"], 0)
        cached = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")).read(
            fixture_process(["codex"], "cached_success", 0), call)
        self.assertEqual(cached.native_usage["entries"]["turn"]["input_tokens"], 40)
        self.assertEqual(cached.native_usage["entries"]["turn"]["cache_read_input_tokens"], 80)

    def test_missing_and_malformed_optional_counts_are_not_invented(self):
        adapter = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        source = fixture_process(["claude"], "success", 0)
        for field, key in (("cacheReadInputTokens", "cache_read_input_tokens"),
                           ("cacheCreationInputTokens", "cache_creation_input_tokens")):
            for value in (None, 0, "bad", -1):
                with self.subTest(field=field, value=value):
                    envelope = json.loads(source.stdout)
                    row = envelope["modelUsage"]["claude-opus-5-5"]
                    if value is None:
                        del row[field]
                    else:
                        row[field] = value
                    outcome = adapter.read(CompletedProcess([], 0, json.dumps(envelope), ""),
                                           Call("", "{}", False, Path(".")))
                    if value is None or value == 0:
                        self.assertEqual(outcome.native_usage["entries"]["claude-opus-5-5"][key], value)
                    else:
                        self.assertIsNone(outcome.native_usage)
                        self.assertIsNone(outcome.usage)

    def test_missing_and_malformed_native_cost_is_unknown(self):
        for cli, model, cls, entry in (
            ("claude", "claude-opus-5-5-high", ClaudeAdapter, "claude-opus-5-5"),
            ("grok", "grok-4.7-high", GrokAdapter, "grok-4.7-build"),
        ):
            for value in (None, "bad", -1):
                with self.subTest(cli=cli, value=value):
                    envelope = json.loads(fixture_process([cli], "success", 0).stdout)
                    row = envelope["modelUsage"][entry]
                    if value is None:
                        del row["costUSD"]
                    else:
                        row["costUSD"] = value
                    outcome = cls(ModelSpec.parse(model)).read(
                        CompletedProcess([], 0, json.dumps(envelope), ""), Call("", "{}", False, Path(".")))
                    self.assertIsNotNone(outcome.usage)
                    self.assertIsNone(outcome.native_usage["entries"][entry]["total_cost_usd"])

    def test_codex_missing_cache_write_and_cost_are_unknown(self):
        source = fixture_process(["codex"], "success", 0)
        events = [json.loads(line) for line in source.stdout.splitlines()]
        del events[-1]["usage"]["cache_write_input_tokens"]
        outcome = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")).read(
            CompletedProcess([], 0, "\n".join(json.dumps(event) for event in events), ""),
            Call("", "{}", False, Path(".")))
        self.assertIsNotNone(outcome.usage)
        self.assertIsNone(outcome.native_usage["entries"]["turn"]["cache_creation_input_tokens"])
        self.assertIsNone(outcome.native_usage["entries"]["turn"]["total_cost_usd"])

    def test_native_usage_records_reject_extra_keys(self):
        tokens = dict(input_tokens=1, output_tokens=2, cache_read_input_tokens=None,
                      cache_creation_input_tokens=None, total_cost_usd=None)
        native = dict(family="claude", aggregation="cli_model_totals", entries={"model": tokens})
        for contract, record in ((NativeUsageTokens, tokens), (NativeUsage, native)):
            with self.subTest(contract=contract), self.assertRaises(ValidationError):
                TypeAdapter(contract).validate_python({**record, "unexpected": 1})

    def test_prompt_bytes_do_not_replace_unknown_usage(self):
        with skill_fixture() as (config, scratch):
            result = run_delivery(lambda *_: Outcome("ran", prompt_bytes=2658),
                                  repo="Example/Engine", request="List items", config=config, scratch=scratch)
        self.assertEqual(result["stop"], "budget_exceeded")
        self.assertIn("usage_unknown", result["halt_detail"])
        self.assertEqual(result["attempts"][0]["prompt_bytes"], 2658)
        self.assertIsNone(result["attempts"][0]["usage"])

    def test_final_prompt_bytes_and_projection_without_metric_replay(self):
        prompts = []
        def run(args, cwd, env):
            prompts.append(args[-1])
            return fixture_process(args, "success", 0)
        worker = CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=container_route)
        model = ModelSpec.parse("claude-opus-5-5-high")
        state = {"attempts": [{"stage": "triage", "prompt_bytes": 999, "native_usage": {"secret": "not input"}}]}
        first = worker("triage", model, state, "Read café 🐈.")
        # The attempt records are in no procedure's inputs; an answer is in triage's.
        second = worker("triage", model, {}, "Read café 🐈.")
        third = worker("triage", model, {"human_answers": ["Yes."]}, "Read café 🐈.")
        self.assertEqual(first.prompt_bytes, len(prompts[0].encode("utf-8")))
        self.assertGreater(first.prompt_bytes, len(prompts[0]))
        self.assertEqual(first.prompt_bytes, second.prompt_bytes)
        self.assertNotEqual(first.prompt_bytes, third.prompt_bytes)
        self.assertIn("prompt_bytes", state["attempts"][0])
        self.assertNotIn("native_usage", prompts[0])
        self.assertNotIn("output schema:", prompts[0])
        codex = CliWorker(Path("."), lambda args, cwd, env: fixture_process(args, "cached_success", 0), scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=container_route)(
            "triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "Read.")
        self.assertEqual(codex.native_usage["entries"]["turn"]["input_tokens"], 40)
        self.assertIsNotNone(codex.prompt_bytes)
        def missing(*args):
            raise FileNotFoundError()
        unavailable = CliWorker(Path("."), missing, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=container_route)("triage", model, {}, "Read café 🐈.")
        self.assertEqual(unavailable.prompt_bytes, second.prompt_bytes)
        self.assertIsNone(unavailable.usage)
        self.assertIsNone(unavailable.native_usage)
        blocked = worker("triage", ModelSpec.parse("grok-4.7-high"), {}, "Read.")
        self.assertEqual(blocked.kind, "cli_missing")
        self.assertIsNotNone(blocked.prompt_bytes)
        self.assertIsNone(blocked.native_usage)

    def test_worker_never_reads_the_allowance_and_the_cli_still_enforces_it(self):
        calls = []
        def run(args, cwd, env):
            calls.append(args)
            return fixture_process(args, "success", 0)
        state = {"dispatch_plan": {"budget_usd": "0.35", "models": ["claude-opus-5-5-high"], "instruction": "Triage.",
                                   "methods": [], "target": None, "scope": None},
                 "run": {"repo": "Example/Engine"},
                 "start": {"human_login": "fixture-owner", "max_tokens": 8000000, "max_budget_usd": "3"},
                 "attempts": [{"stage": "spec", "usage": {"cost_usd": 2.65}, "dollar_charge": "2.65"}]}
        CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=container_route)("triage", ModelSpec.parse("claude-opus-5-5-high"), state, "Read.")
        prompt = calls[0][-1]
        for allowance in ("budget_usd", "max_tokens", "max_budget_usd", "fixture-owner", "cost_usd", "dollar_charge", "0.35", "2.65"):
            self.assertNotIn(allowance, prompt)
        self.assertIn("Example/Engine", prompt)
        self.assertEqual(calls[0][calls[0].index("--max-budget-usd") + 1], "0.35")
        self.assertEqual(state["dispatch_plan"]["budget_usd"], "0.35")

    def test_real_attempt_seam_checkpoint_and_unchanged_ledgers(self):
        config = load_config()
        config.nodes["triage"] = NodeConfig("coordinator", (ModelSpec.parse("claude-opus-5-5-high"),))
        config.nodes["spec"] = config.nodes["triage"]
        prompts = []
        def run(args, cwd, env):
            prompts.append(args[-1])
            envelope = json.loads(fixture_process(args, "success", 0).stdout)
            envelope["structured_output"] = (
                {"triage": "plan", "work_kind": "feature", "slice": None, "human_question": None}
                if len(prompts) == 1 else
                {"spec": "Needs item definitions.", "spec_gate": "needs_human", "slices": [],
                 "questions": [{"question": "Which items?", "why_code_unanswered": "No policy in code.", "checked_paths": []}]}
            )
            return CompletedProcess(args, 0, json.dumps(envelope), "")
        worker = CliWorker(Path("."), run, scratch_root=Path(self.enterContext(TemporaryDirectory())), containers=container_route)
        with skill_fixture(config) as (config, scratch), TemporaryDirectory() as directory:
            result = run_delivery(worker, repo="Example/Engine", request="List items", config=config, scratch=scratch)
            self.assertEqual([attempt["stage"] for attempt in result["attempts"]], ["triage", "spec"])
            self.assertEqual(len(prompts), 2)
            self.assertEqual(result["stop"], "needs_human")
            for metric in ("prompt_bytes", "native_usage", "cli_model_totals", "claude-haiku-5-5"):
                self.assertNotIn(metric, prompts[1])
            for recorded, prompt in zip(result["attempts"], prompts):
                self.assertEqual(recorded["prompt_bytes"], len(prompt.encode("utf-8")))
                self.assertEqual(recorded["native_usage"]["aggregation"], "cli_model_totals")
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
                graph.update_state(thread, result, as_node="research")
                self.assertEqual(graph.get_state(thread).values["attempts"], result["attempts"])
            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint.sqlite")) as restored:
                restored.serde = checkpoint_serializer()
                validated = checkpoint_state(restored.get(thread)["channel_values"])
                self.assertIsNotNone(validated)
                self.assertEqual(validated["attempts"], result["attempts"])
