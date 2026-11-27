"""Dollar limits at the delivery and subprocess boundaries."""

import tests

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from foreman.adapters import Outcome, Usage
from foreman.budget import claude_spend
from foreman.cli import run_exit_code
from foreman.config import ModelSpec, load_config
from foreman.delivery import MODEL_STAGES, LiveRun, compile_delivery
from foreman.dry_run import CLEAR, DryRun, ScriptHandler
from foreman.runner import run_live_delivery, snapshot_result
from foreman.state import ReviewState, PlanState, ChecksState, RunState, StartSettings, checkpoint_serializer
from foreman.usd import BudgetUsd
from foreman.worker import CliWorker
from foreman.workspace import WorkState
from test_dollar_allowance import NEEDS_HUMAN, attempt
from skill_fixture import fixture_skills, skill_fixture, verify_checkout
from test_runner import APPROVE, EMPTY_SLICE, FakeGitHub, FakeWorkspace, fixture_process, worker_reply


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


def walk(handler, *, config=None, prior=0.8, limit="1", max_tokens=1_000_000, parent=False, checkout=""):
    saver = InMemorySaver(serde=checkpoint_serializer())
    dry = DryRun(checkout=Path(checkout) if checkout else None)
    start = StartSettings(human_login="fixture-owner", max_tokens=max_tokens, max_budget_usd=limit)
    graph = compile_delivery(dry.builder(handler), config or configured(), run=LiveRun(forge=dry, workspace=dry, start=start), checkpointer=saver).with_config({"configurable": {"thread_id": "budget"}})
    graph.invoke({"review": ReviewState(),
        "run": RunState(issue=dry.issue_placeholder, repo="a/b", base_branch="main", base_sha=dry.head, request="x", workspace=checkout), "start": start, "checks": ChecksState(check="exit 0", verify_skill=".claude/skills/verify-web/SKILL.md"), "path": [], "plan": PlanState(issue_kind="parent") if parent else PlanState(issue_kind="slice", slice=EMPTY_SLICE),

        "attempts": [attempt(CLAUDE.name, prior)],
    })
    return snapshot_result(graph.get_state(graph.config, subgraphs=True))


class DispatchBudgetTests(unittest.TestCase):
    def test_every_dispatching_stage_recomputes_the_budget_for_each_claude_attempt(self):
        for stage in MODEL_STAGES:
            with self.subTest(stage=stage):
                script = ScriptHandler(triage="question" if stage == "research" else "plan", slices=[EMPTY_SLICE])
                calls = []

                def handler(node, model, state, instruction):
                    if node == stage:
                        calls.append((model.name, state["dispatch_plan"]["budget_usd"]))
                        if len(calls) == 1:
                            return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=0.1))
                    reply = script(node, model, state, instruction)
                    cost = 0.01 if model and model.family == "claude" else 5.0
                    return replace(reply, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=cost))

                with verify_checkout() as checkout:
                    result = walk(handler, config=configured(stage), parent=stage in {"research", "spec"},
                                  checkout=str(checkout) if stage == "verify" else "")
                self.assertEqual(result["stop"], {"research": "answered", "spec": "children_filed"}.get(stage, "merge_pending"))
                self.assertEqual(calls, [(CLAUDE.name, "0.2"), (NEXT_CLAUDE.name, "0.1")])
                self.assertTrue(getattr(result["plan"], stage) if stage in {"triage", "spec"} else result["implementation" if stage == "implement" else stage])

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

        result = walk(CliWorker(Path.cwd(), runner, scratch_root=Path(self.enterContext(TemporaryDirectory()))))
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

                reply = CliWorker(Path.cwd(), other_runner, scratch_root=Path(self.enterContext(TemporaryDirectory())))("triage", model,
                    {"dispatch_plan": {"models": [], "instruction": "x", "methods": [], "target": None, "scope": None, "budget_usd": "0.1"}}, "x")
                if model == GROK:
                    self.assertEqual((reply.kind, received), ("cli_missing", []))
                    continue
                self.assertEqual(reply.kind, "ran")
                self.assertEqual(len(received), 1)
                self.assertNotIn("--max-budget-usd", received[0])

    def test_bounded_dry_run_records_remainders_and_zero_cost(self):
        script = ScriptHandler()
        result = walk(script, config=load_config())
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual([call["budget_usd"] for call in script.calls], ["0.2"] * 4)
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
                                       usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0,
                                                   total_cost_usd=cost if len(calls) == 1 else 0.0))

                    result = walk(handler)
                    self.assertEqual(result["stop"], expected)
                    self.assertEqual(len(calls), count)
                    self.assertEqual(claude_spend(result["attempts"]), Decimal("0.8") + Decimal(str(cost)))
                    if expected == "budget_exceeded":
                        self.assertEqual(run_exit_code(result), 75)
                        self.assertEqual(result["plan"].triage, "")
                        self.assertEqual(result["halt_detail"], {
                            0.2: "Run Claude spend: $1.0. Dollar allowance: $1.",
                            0.25: "Run Claude spend: $1.05. Dollar allowance: $1.",
                        }[cost])
                    else:
                        self.assertTrue(result["halt_detail"].startswith("Which?"))

    def test_spent_or_unknown_allowance_never_starts_a_candidate(self):
        for prior, expected in ((1.0, "budget_exceeded"), (1.05, "budget_exceeded"), (None, "budget_exceeded")):
            with self.subTest(prior=prior):
                def unexpected(*args, **kwargs):
                    self.fail("a candidate ran without a known positive allowance")
                result = walk(unexpected, prior=prior, config=configured(models=(GPT, GROK, CLAUDE)))
                self.assertEqual(result["stop"], expected)
                self.assertEqual(run_exit_code(result), 75)
                self.assertEqual(result["plan"].triage, "")
                self.assertEqual(len(result["attempts"]), 1)

    def test_unknown_claude_cost_stops_the_run_at_its_dollar_cap(self):
        for kind, has_usage in (("ran", True), ("quota", True), ("failed", True), ("quota", False), ("failed", False)):
            with self.subTest(kind=kind, has_usage=has_usage):
                calls = []

                def handler(node, model, state, instruction):
                    calls.append(model.name)
                    return Outcome(kind, NEEDS_HUMAN,
                                   usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0) if has_usage else None)

                result = walk(handler)
                # D10: unknown usage is charged at the remaining allowance, so the run stops.
                self.assertEqual(result["stop"], "invalid_output" if kind == "failed" else "budget_exceeded")
                self.assertEqual(len(calls), 1)
                if has_usage and kind != "failed":
                    self.assertIn("usage_unknown", result["halt_detail"])
                    self.assertIn("Dollar allowance: $1.", result["halt_detail"])
                    self.assertEqual(run_exit_code(result), 75)
                    self.assertEqual(result["plan"].triage, "")

    def test_zero_cost_and_missing_cli_allow_the_next_candidate(self):
        for kind in ("quota", "cli_missing"):
            with self.subTest(kind=kind):
                calls = []

                def handler(node, model, state, instruction):
                    calls.append(state["dispatch_plan"]["budget_usd"])
                    usage = Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=7, output_tokens=0, total_cost_usd=0.0)
                    return Outcome(kind if len(calls) == 1 else "ran", NEEDS_HUMAN,
                                   usage=None if kind == "cli_missing" and len(calls) == 1 else usage)

                result = walk(handler)
                self.assertEqual(result["stop"], "needs_human")
                self.assertEqual(calls, ["0.2", "0.2"])
                self.assertEqual(claude_spend(result["attempts"]), Decimal("0.8"))

    def test_stop_precedence_and_budget_stop_detail(self):
        cases = (
            (Outcome("budget_exhausted"), 100, "budget_exceeded", "usage_unknown"),
            (Outcome("budget_exhausted", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=99, output_tokens=0)), 100, "budget_exceeded", "usage_unknown"),
            (Outcome("budget_exhausted", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=99, output_tokens=0, total_cost_usd=0.1)),
             100, "budget_exceeded", "spend: $0.9"),
            (Outcome("ran", NEEDS_HUMAN), 100, "budget_exceeded",
             "claude-opus-5-5-high was charged the remaining allowance of 99 work tokens. An in-flight attempt can overshoot the cap. usage_unknown:"),
            (Outcome("ran", NEEDS_HUMAN, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=99, output_tokens=0, total_cost_usd=0.2)),
             100, "budget_exceeded", "Run used 100 work tokens; cap is 100"),
        )
        for reply, cap, expected, detail in cases:
            with self.subTest(reply=reply):
                received = []

                def handler(*args, **kwargs):
                    received.append(args[2]["dispatch_plan"]["budget_usd"])
                    return reply

                result = walk(handler, max_tokens=cap)
                self.assertEqual(received, ["0.2"])
                self.assertEqual(result["stop"], expected)
                self.assertIn(detail, result["halt_detail"])
                self.assertEqual(result["plan"].triage, "")
                self.assertEqual(run_exit_code(result), 75)

    def test_mixed_providers_spend_only_the_claude_allowance(self):
        script = ScriptHandler()
        calls = []

        def handler(node, model, state, instruction):
            budget_usd = state["dispatch_plan"]["budget_usd"]
            calls.append((node, model.name if model else None, budget_usd))
            if model in (GPT, GROK) and node == "triage":
                return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=5.0))
            reply = script(node, model, state, instruction)
            return replace(reply, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0,
                                              total_cost_usd=0.01 if model == CLAUDE else 5.0))

        result = walk(handler, config=configured(models=(GPT, GROK, CLAUDE)))
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual([model for _, model, _ in calls[:3]], [GPT.name, GROK.name, CLAUDE.name])
        self.assertEqual([budget for _, _, budget in calls[:3]], ["0.2"] * 3)
        self.assertEqual(calls[-1], ("review", CLAUDE.name, "0.19"))
        self.assertEqual(claude_spend(result["attempts"]), Decimal("0.82"))


class BudgetResumeTests(unittest.TestCase):
    """DP7 and DP8: a run stopped on its budget resumes at the interrupted step once the operator raises the cap."""

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        config = self.enterContext(skill_fixture())[0]
        claude = replace(config, nodes={name: replace(node, models=(CLAUDE,)) for name, node in config.nodes.items()})
        self.enterContext(patch("foreman.runner.load_config", return_value=claude))
        self.forge, self.workspace = FakeGitHub(), FakeWorkspace(self.root)
        self.calls = []

    def handler(self, costs, tokens=1):
        def reply(node, model, state, instruction):
            self.calls.append((node, state["dispatch_plan"]["budget_usd"]))
            if node == "implement":
                (self.root / "partial.txt").write_text("builder edits\n")
                self.workspace.current = replace(self.workspace.current, clean=False)
            output = {"triage": {"triage": "supplied_slice", "work_kind": "feature", "slice": None, "human_question": None},
                      "spec_check": CLEAR.model_dump(), "implement": {"implementation": "built", "no_change": None, "human_question": None},
                      "verify": {"tested_head": WorkState.model_validate(state.get("work") or {}).published_head, "criteria": [{
                          "criterion": 0, "result": "passed", "observation": "The scripted drive used the change.",
                          "evidence": "captures/scripted.txt"}]},
                      "review": APPROVE}[node]
            spent = len([call for call in self.calls if call[0] == node]) - 1
            return Outcome("ran", output, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=tokens,
                                                      output_tokens=0, total_cost_usd=costs[node][spent]))
        return reply

    def deliver(self, handler, *, max_budget_usd=None, max_tokens=None, preflight=None):
        """One invocation with the repository's variables set as given. Only a start reads them."""
        self.forge.settings = {"FOREMAN_HUMAN_LOGIN": "fixture-owner"}
        for name, value in (("FOREMAN_MAX_BUDGET_USD", max_budget_usd), ("FOREMAN_MAX_TOKENS", max_tokens), ("FOREMAN_PREFLIGHT", preflight)):
            if value is not None:
                self.forge.settings[name] = str(value.amount if isinstance(value, BudgetUsd) else value)
        return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=self.root / "runs.sqlite3",
                                 run=LiveRun(self.forge, self.workspace, skills=fixture_skills), dispatcher=handler)

    def test_a_call_near_the_cap_is_charged_once_and_a_raised_cap_resumes_it_on_the_builders_edits(self):
        costs = {"triage": [0.1], "spec_check": [2.65], "implement": [2.94, 0.5], "verify": [0.1], "review": [0.1]}
        stopped = self.deliver(self.handler(costs), max_budget_usd=BudgetUsd(Decimal("3")))
        # DP8: the last $0.25 admits the builder with that CLI cap, its $2.94 is charged once and no further call starts.
        self.assertEqual(stopped["stop"], "budget_exceeded")
        self.assertEqual(self.calls, [("triage", "3"), ("spec_check", "2.9"), ("implement", "0.25")])
        self.assertEqual(claude_spend(stopped["attempts"]), Decimal("5.69"))
        self.assertEqual(run_exit_code(stopped), 75)
        # A cap below what the run spent stops it at the first reservation, before any paid call.
        for limits in ({"max_budget_usd": "2"}, {"max_budget_usd": "3", "max_tokens": 1}):
            with self.subTest(limits=limits):
                lowered = self.deliver(self.handler(costs), **limits)
                self.assertEqual((lowered["stop"], lowered["attempts"]), ("budget_exceeded", stopped["attempts"]))
                self.assertEqual(len(self.calls), 3)
        again = self.deliver(self.handler(costs), max_budget_usd=BudgetUsd(Decimal("3")))
        self.assertEqual((again["stop"], again["attempts"]), ("budget_exceeded", stopped["attempts"]))
        self.assertEqual(len(self.calls), 3)
        self.forge.ready_again(8)
        # DP7: Ready again after the operator raises the cap resumes the builder's step on the edits it made.
        resumed = self.deliver(self.handler(costs), max_budget_usd=BudgetUsd(Decimal("10")))
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(self.calls[3:], [("implement", "4.31"), ("verify", "3.81"), ("review", "3.71")])
        self.assertEqual(resumed["start"].max_budget_usd, "10")
        self.assertEqual(resumed["attempts"][:len(stopped["attempts"])], stopped["attempts"])
        self.assertEqual([a["usage"]["cost_usd"] for a in resumed["attempts"] if a["stage"] == "implement"], [2.94, 0.5])
        self.assertEqual((self.root / "partial.txt").read_text(), "builder edits\n")

    def test_a_raised_token_cap_resumes_and_a_lower_one_stops_before_any_call(self):
        costs = {"triage": [0.0], "spec_check": [0.0], "implement": [0.0, 0.0], "verify": [0.0], "review": [0.0]}
        stopped = self.deliver(self.handler(costs, tokens=40), max_tokens=100)
        self.assertEqual(stopped["stop"], "budget_exceeded")
        self.assertEqual([node for node, _ in self.calls], ["triage", "spec_check", "implement"])
        lowered = self.deliver(self.handler(costs, tokens=40), max_tokens=99)
        self.assertEqual((lowered["stop"], lowered["start"].max_tokens, len(self.calls)), ("budget_exceeded", 99, 3))
        resumed = self.deliver(self.handler(costs, tokens=40), max_tokens=1000)
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(resumed["start"].max_tokens, 1000)
        self.assertEqual([node for node, _ in self.calls[3:]], ["implement", "verify", "review"])

    def test_a_raised_cap_resumes_a_budget_halt_in_triage_with_the_issue_it_admitted(self):
        costs = {"triage": [3.5, 0.1], "spec_check": [0.1], "implement": [0.1], "verify": [0.1], "review": [0.1]}
        stopped = self.deliver(self.handler(costs), max_budget_usd=BudgetUsd(Decimal("3")))
        self.assertEqual((stopped["stop"], stopped["path"][-1:]), ("budget_exceeded", ["triage"]))
        self.forge.body += "\n\nEdited after the halt."
        self.forge.ready_again(8)
        resumed = self.deliver(self.handler(costs), max_budget_usd=BudgetUsd(Decimal("10")))
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual((resumed["run"], resumed["start"].max_budget_usd), (stopped["run"], "10"))
        self.assertEqual([node for node, _ in self.calls], ["triage", "triage", "spec_check", "implement", "verify", "review"])

    def test_a_resume_from_any_halt_takes_the_caps_it_read_and_keeps_its_preflight(self):
        costs = {"triage": [0.1], "spec_check": [0.1], "implement": [0.1], "review": [0.1]}
        stopped = self.deliver(self.handler(costs), max_budget_usd=BudgetUsd(Decimal("3")), preflight="exit 1")
        self.assertEqual(stopped["stop"], "env_unavailable")
        calls = list(self.calls)
        resumed = self.deliver(self.handler(costs), max_budget_usd="10", max_tokens=30_000_000, preflight="exit 0")
        self.assertEqual((resumed["stop"], resumed["start"].max_budget_usd, resumed["start"].max_tokens), ("env_unavailable", "10", 30_000_000))
        self.assertEqual(resumed["checks"].preflight, "exit 1")
        self.assertEqual(self.calls, calls)
