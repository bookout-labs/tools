import tests

from foreman.state import RunState, checkpoint_serializer, SCHEMA_VERSION

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Call, Outcome, Usage
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.cli import run_exit_code
from foreman.config import ModelSpec
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import ScriptHandler
from foreman.runner import checkpoint_state
from foreman.usd import BudgetUsd, usd_text
from tests.skill_fixture import skill_fixture
from test_runner import EMPTY_SLICE, FakeGitHub, FakeWorkspace


class DollarBudgetTests(unittest.TestCase):
    def test_canonical_fixed_point_budget_text(self):
        for amount, expected in (("1.50", "1.5"), ("100", "100"), ("0.1", "0.1"), ("0.0001", "0.0001")):
            with self.subTest(amount=amount):
                self.assertEqual(usd_text(BudgetUsd(Decimal(amount))), expected)
        for amount in ("0", "-1", "NaN", "Infinity"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                BudgetUsd(Decimal(amount))

    def test_only_claude_receives_the_optional_dollar_budget(self):
        directory = self.enterContext(TemporaryDirectory())
        call = Call(prompt="Do work", schema="{}", edit=True, temp_dir=Path(directory))
        budgeted = replace(call, budget_usd=BudgetUsd(Decimal("0.1")))
        claude = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high"))
        argv = claude.command(budgeted)
        self.assertEqual(argv.count("--max-budget-usd"), 1)
        index = argv.index("--max-budget-usd")
        self.assertEqual(argv[index + 1], "0.1")
        self.assertEqual(argv[:index] + argv[index + 2:], claude.command(call))
        codex = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high"))
        self.assertEqual(codex.command(budgeted), codex.command(call))

    def test_budget_stop_halts_without_fallback_output_or_publication(self):
        for stage in ("triage", "implement"):
            with self.subTest(stage=stage), skill_fixture() as (config, scratch), TemporaryDirectory() as folder:
                root = Path(folder)
                workspace, forge = FakeWorkspace(root), FakeGitHub()
                calls = []
                script = ScriptHandler()
                usage = Usage(input_tokens=7, output_tokens=3, total_cost_usd=0.1)

                def handler(node, model, state, instruction):
                    calls.append((node, model.name))
                    if node != stage:
                        return script(node, model, state, instruction)
                    if node == "implement":
                        (root / "partial.txt").write_text("unfinished edits\n")
                        workspace.current = replace(workspace.current, clean=False)
                    return Outcome("budget_exhausted", usage=usage)

                self.assertGreater(len(config.nodes[stage].models), 1)
                with patch.object(workspace, "commit", wraps=workspace.commit) as commit, \
                     patch.object(workspace, "push", wraps=workspace.push) as push, \
                     patch.object(forge, "open_pull_request", wraps=forge.open_pull_request) as publish:
                    with SqliteSaver.from_conn_string(str(root / "budget.sqlite")) as saver:
                        saver.serde = checkpoint_serializer()
                        invocation = {"configurable": {"thread_id": stage}}
                        graph = compile_delivery(handler, config, run=LiveRun(forge, workspace),
                                                 scratch=scratch, checkpointer=saver)
                        graph.invoke({"schema_version": SCHEMA_VERSION, "run": RunState(repo="a/b", issue=8, token_cap=100, request="x"), "check": "exit 0", "path": [],
                                        "issue_kind": "slice", "slice": EMPTY_SLICE},
                                     invocation, durability="sync")
                        snapshot = graph.get_state(invocation)
                        state = checkpoint_state(snapshot.values)
                        self.assertEqual(snapshot.next, ())
                    commit.assert_not_called()
                    push.assert_not_called()
                    publish.assert_not_called()
                self.assertEqual([node for node, _ in calls], ["triage"] if stage == "triage" else ["triage", "implement"])
                self.assertEqual(calls[-1][1], config.nodes[stage].models[0].name)
                attempts = [attempt for attempt in state["attempts"] if attempt["stage"] == stage]
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["outcome"], "budget_exhausted")
                self.assertEqual(attempts[0]["usage"], {"input": 7, "output": 3, "cache_read": 0,
                                                        "cache_creation": 0, "cost_usd": 0.1})
                self.assertIsNone(state["pending_dispatch"])
                self.assertEqual((state["halt"], state["stop"]), ("budget_exceeded", "budget_exceeded"))
                self.assertIn("dollar", state["halt_detail"])
                self.assertEqual(run_exit_code(state), 1)
                self.assertNotIn(stage, state)
                self.assertNotIn(stage, state.get("models_used", {}))
                self.assertNotIn("commit", state["path"])
                self.assertNotIn("publish", state["path"])
                self.assertEqual(forge.merge_calls, [])
                if stage == "implement":
                    self.assertEqual((root / "partial.txt").read_text(), "unfinished edits\n")
                    self.assertNotIn("implementation", state)
