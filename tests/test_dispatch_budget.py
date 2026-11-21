"""Dollar limits at the delivery and subprocess boundaries."""

import tests

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from subprocess import CompletedProcess
import unittest

from langgraph.checkpoint.memory import InMemorySaver
from foreman.runner import snapshot_result
from foreman.adapters import Outcome, Usage
from foreman.budget import claude_spend
from foreman.cli import run_exit_code
from foreman.config import ModelSpec, load_config
from foreman.delivery import MODEL_STAGES, LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.worker import CliWorker
from test_dollar_allowance import NEEDS_HUMAN, attempt
from test_runner import EMPTY_SLICE, fixture_process, worker_reply


CLAUDE = ModelSpec.parse("claude-opus-5-5-high")
NEXT_CLAUDE = ModelSpec.parse("claude-opus-5-5-max")
GPT = ModelSpec.parse("gpt-6.1-sol-high")
GROK = ModelSpec.parse("grok-4.7-high")


def configured(stage="triage", models=(CLAUDE, NEXT_CLAUDE)):
    config = load_config()
    nodes = {name: replace(node, models=(GPT,) if name != "verify" else ())
             for name, node in config.nodes.items()}
    nodes["review"] = replace(nodes["review"], models=(GROK,) if stage == "implement" else (CLAUDE,))
    nodes[stage] = replace(nodes[stage], models=models)
    return replace(config, nodes=nodes)


def walk(handler, *, config=None, prior=0.8, limit="1", token_cap=1_000_000):
    saver = InMemorySaver()
    dry = DryRun()
    graph = compile_delivery(handler, config or configured(), run=LiveRun(forge=dry, workspace=dry), checkpointer=saver).with_config({"configurable": {"thread_id": "budget"}})
    graph.invoke({
        "issue": dry.issue_placeholder, "repo": "a/b", "request": "x", "check": "exit 0", "path": [], "issue_kind": "slice", "slice": EMPTY_SLICE,
        "token_cap": token_cap, "max_budget_usd": limit,
        "attempts": [attempt(CLAUDE.name, prior)],
    })
    return snapshot_result(graph.get_state(graph.config), saver)


class DispatchBudgetTests(unittest.TestCase):
    def test_every_dispatching_stage_recomputes_the_budget_for_each_claude_attempt(self):
        for stage in MODEL_STAGES:
            with self.subTest(stage=stage):
                script = ScriptHandler(triage="question" if stage == "research" else "spec")
                calls = []

                def handler(node, model, state, instruction):
                    if node == stage:
                        calls.append((model.name, state["dispatch_plan"]["budget_usd"]))
                        if len(calls) == 1:
                            return Outcome("quota", usage=Usage(input_tokens=1, output_tokens=0, total_cost_usd=0.1))
                    reply = script(node, model, state, instruction)
                    cost = 0.01 if model and model.family == "claude" else 5.0
                    return replace(reply, usage=Usage(input_tokens=1, output_tokens=0, total_cost_usd=cost))

                result = walk(handler, config=configured(stage))
                self.assertEqual(result["stop"], "answered" if stage == "research" else "verified")
                self.assertEqual(calls, [(CLAUDE.name, "0.2"), (NEXT_CLAUDE.name, "0.1")])
                self.assertIn(stage if stage != "implement" else "implementation", result)

    def test_cli_worker_sends_one_exact_remainder_flag_only_to_claude(self):
        argv = []

        def runner(args, cwd, env):
            argv.append(args)
            cost = 0.1 if len(argv) == 1 else 0.01
            process = (fixture_process(args, "quota", 1) if len(argv) == 1
                       else worker_reply(args, NEEDS_HUMAN))
            envelope = json.loads(process.stdout)
            envelope["total_cost_usd"] = cost
            for index, row in enumerate(envelope["modelUsage"].values()):
                row["costUSD"] = cost if index == 0 else 0.0
            return CompletedProcess(process.args, process.returncode, json.dumps(envelope), process.stderr)

        result = walk(CliWorker(Path.cwd(), runner))
        self.assertEqual(result["stop"], "needs_human")
        self.assertEqual(len(argv), 2)
        for args, expected in zip(argv, ("0.2", "0.1")):
            self.assertEqual(args.count("--max-budget-usd"), 1)
            self.assertEqual(args[args.index("--max-budget-usd") + 1], expected)
        for model in (GPT, GROK):
            with self.subTest(model=model.name):
                received = []

                def other_runner(args, cwd, env):
                    received.append(args)
                    return worker_reply(args, NEEDS_HUMAN)

                reply = CliWorker(Path.cwd(), other_runner)("triage", model,
                    {"dispatch_plan": {"models": [], "instruction": "x", "methods": [], "target": None, "scope": None, "budget_usd": "0.1"}}, "x")
                self.assertEqual(reply.kind, "ran")
                self.assertEqual(len(received), 1)
                self.assertNotIn("--max-budget-usd", received[0])

    def test_bounded_dry_run_records_remainders_and_zero_cost(self):
        script = ScriptHandler()
        result = walk(script, config=load_config())
        self.assertEqual(result["stop"], "verified")
        self.assertEqual([call["budget_usd"] for call in script.calls], ["0.2"] * 3)
        self.assertEqual(claude_spend(result["attempts"]), Decimal("0.8"))
        self.assertTrue(all(a["usage"]["cost_usd"] == 0.0 for a in result["attempts"][1:]))

    def test_quota_or_success_at_the_limit_stops_before_fallback_or_output(self):
        for kind in ("quota", "ran"):
            for cost, expected, count in ((0.1, "needs_human", 2 if kind == "quota" else 1),
                                          (0.2, "budget_exceeded", 1), (0.25, "budget_exceeded", 1)):
                with self.subTest(kind=kind, cost=cost):
                    calls = []

                    def handler(node, model, state, instruction):
                        calls.append(state["dispatch_plan"]["budget_usd"])
                        return Outcome(kind if len(calls) == 1 else "ran", NEEDS_HUMAN,
                                       usage=Usage(input_tokens=1, output_tokens=0,
                                                   total_cost_usd=cost if len(calls) == 1 else 0.0))

                    result = walk(handler)
                    self.assertEqual(result["stop"], expected)
                    self.assertEqual(len(calls), count)
                    self.assertEqual(claude_spend(result["attempts"]), Decimal("0.8") + Decimal(str(cost)))
                    if expected == "budget_exceeded":
                        self.assertEqual(run_exit_code(result), 1)
                        self.assertNotIn("triage", result)
                        self.assertIsNone(result["pending_dispatch"])
                        self.assertEqual(result["halt_detail"], {
                            0.2: "Run Claude spend: $1.0. Dollar allowance: $1.",
                            0.25: "Run Claude spend: $1.05. Dollar allowance: $1.",
                        }[cost])
                    else:
                        self.assertEqual(result["triage"], "needs_human")

    def test_spent_or_unknown_allowance_never_starts_a_candidate(self):
        for prior, expected in ((1.0, "budget_exceeded"), (1.05, "budget_exceeded"), (None, "usage_unknown")):
            with self.subTest(prior=prior):
                def unexpected(*args, **kwargs):
                    self.fail("a candidate ran without a known positive allowance")
                result = walk(unexpected, prior=prior, config=configured(models=(GPT, GROK, CLAUDE)))
                self.assertEqual(result["stop"], expected)
                self.assertEqual(run_exit_code(result), 1)
                self.assertNotIn("triage", result)
                self.assertEqual(len(result["attempts"]), 1)

    def test_unknown_claude_cost_stops_bounded_attempts_but_preserves_unbounded_behavior(self):
        for kind, has_usage in (("ran", True), ("quota", True), ("failed", True), ("quota", False), ("failed", False)):
            for limit in ("1", None):
                with self.subTest(kind=kind, has_usage=has_usage, limit=limit):
                    calls = []

                    def handler(node, model, state, instruction):
                        calls.append(model.name)
                        if len(calls) > 1:
                            return Outcome("ran", NEEDS_HUMAN, usage=Usage(input_tokens=1, output_tokens=0))
                        return Outcome(kind, NEEDS_HUMAN,
                                       usage=Usage(input_tokens=1, output_tokens=0) if has_usage else None)

                    result = walk(handler, limit=limit)
                    expected = "usage_unknown" if limit else ("invalid_output" if kind == "failed" else "needs_human")
                    self.assertEqual(result["stop"], expected)
                    self.assertEqual(len(calls), 2 if limit is None and kind == "quota" else 1)
                    if limit:
                        self.assertIn("without a dollar cost", result["halt_detail"])
                        self.assertIn("Dollar allowance: $1.", result["halt_detail"])
                        self.assertEqual(run_exit_code(result), 1)
                        self.assertNotIn("triage", result)

    def test_zero_cost_and_missing_cli_allow_the_next_candidate(self):
        for kind in ("quota", "cli_missing"):
            with self.subTest(kind=kind):
                calls = []

                def handler(node, model, state, instruction):
                    calls.append(state["dispatch_plan"]["budget_usd"])
                    usage = Usage(input_tokens=7, output_tokens=0, total_cost_usd=0.0)
                    return Outcome(kind if len(calls) == 1 else "ran", NEEDS_HUMAN,
                                   usage=None if kind == "cli_missing" and len(calls) == 1 else usage)

                result = walk(handler)
                self.assertEqual(result["stop"], "needs_human")
                self.assertEqual(calls, ["0.2", "0.2"])
                self.assertEqual(claude_spend(result["attempts"]), Decimal("0.8"))

    def test_stop_precedence_and_budget_stop_detail(self):
        cases = (
            (Outcome("budget_exhausted"), 100, "budget_exceeded", "spend: unknown"),
            (Outcome("budget_exhausted", usage=Usage(input_tokens=99, output_tokens=0)), 100, "budget_exceeded", "spend: unknown"),
            (Outcome("budget_exhausted", usage=Usage(input_tokens=99, output_tokens=0, total_cost_usd=0.1)),
             100, "budget_exceeded", "spend: $0.9"),
            (Outcome("ran", NEEDS_HUMAN), 100, "usage_unknown", "without a usage report"),
            (Outcome("ran", NEEDS_HUMAN, usage=Usage(input_tokens=99, output_tokens=0, total_cost_usd=0.2)),
             100, "budget_exceeded", "Run used 100 tokens; cap is 100"),
        )
        for reply, cap, expected, detail in cases:
            with self.subTest(reply=reply):
                received = []

                def handler(*args, **kwargs):
                    received.append(args[2]["dispatch_plan"]["budget_usd"])
                    return reply

                result = walk(handler, token_cap=cap)
                self.assertEqual(received, ["0.2"])
                self.assertEqual(result["stop"], expected)
                self.assertIn(detail, result["halt_detail"])
                self.assertNotIn("triage", result)
                self.assertEqual(run_exit_code(result), 1)

    def test_mixed_providers_spend_only_the_claude_allowance(self):
        script = ScriptHandler()
        calls = []

        def handler(node, model, state, instruction):
            budget_usd = state["dispatch_plan"]["budget_usd"]
            calls.append((node, model.name if model else None, budget_usd))
            if model in (GPT, GROK) and node == "triage":
                return Outcome("quota", usage=Usage(input_tokens=1, output_tokens=0, total_cost_usd=5.0))
            reply = script(node, model, state, instruction)
            return replace(reply, usage=Usage(input_tokens=1, output_tokens=0,
                                              total_cost_usd=0.01 if model == CLAUDE else 5.0))

        result = walk(handler, config=configured(models=(GPT, GROK, CLAUDE)))
        self.assertEqual(result["stop"], "verified")
        self.assertEqual([model for _, model, _ in calls[:3]], [GPT.name, GROK.name, CLAUDE.name])
        self.assertEqual([budget for _, _, budget in calls[:3]], ["0.2"] * 3)
        self.assertEqual(calls[-1], ("review", CLAUDE.name, "0.19"))
        self.assertEqual(claude_spend(result["attempts"]), Decimal("0.82"))
