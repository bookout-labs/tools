import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Outcome, Usage
from foreman.cli import _print_run, main, run_exit_code
from foreman.delivery import DEFAULT_TOKEN_CAP, DryRun, LiveRun, compile_delivery, run_delivery, run_tokens
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.runner import run_live_delivery
from foreman.skill_rules import SkillMissing
from tests.skill_fixture import skill_fixture
from test_runner import FakeGitHub, FakeWorkspace


class TokenCapTests(unittest.TestCase):
    def test_persisted_history_boundary_and_quota_fallback(self):
        cases = ((90, (("ran", 9),), None), (90, (("ran", 10),), "budget_exceeded"),
                 (90, (("ran", 11),), "budget_exceeded"), (90, (("quota", 10),), "budget_exceeded"),
                 (95, (("quota", 3), ("ran", 2)), "budget_exceeded"))
        for prior, replies, expected in cases:
            with self.subTest(prior=prior, replies=replies), skill_fixture() as (config, scratch), TemporaryDirectory() as folder:
                checkpoint = str(Path(folder) / "cap.sqlite")
                invocation = {"configurable": {"thread_id": "cap"}}
                calls, outcomes = [], iter(replies)
                def handler(node, model, state, instruction):
                    calls.append(model.name)
                    kind, tokens = next(outcomes)
                    return Outcome(kind, {"triage": "needs_human", "work_kind": "feature", "slice": None,
                                          "human_question": "Which?"}, usage=Usage(input_tokens=tokens, output_tokens=0))
                dispatch = FailoverDispatcher(handler)
                with SqliteSaver.from_conn_string(checkpoint) as saver:
                    graph = compile_delivery(dispatch, config, run=DryRun(), checkpointer=saver)
                    graph.update_state(invocation, {"repo": "a/b", "check": "", "path": ["triage"], "request": "x",
                        "issue_kind": "parent", "token_cap": 100, "attempts": [{"stage": "spec",
                        "model": "prior", "commit": "a", "methods": [], "outcome": "ran",
                        "usage": {"input": prior, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": None}}]}, as_node="needs_human")
                with SqliteSaver.from_conn_string(checkpoint) as saver:
                    graph = compile_delivery(dispatch, config, run=DryRun(), checkpointer=saver)
                    graph.invoke(None, invocation, durability="sync")
                    result = graph.get_state(invocation).values
                total = prior + sum(tokens for _, tokens in replies)
                self.assertEqual(result.get("stop"), expected)
                self.assertEqual(len(calls), len(replies))
                self.assertEqual(run_tokens(result["attempts"]), total)
                self.assertEqual(len(result["attempts"]), 1 + len(replies))
                if expected == "budget_exceeded":
                    self.assertNotIn("triage", result)
                    self.assertIn(f"Run used {total} tokens; cap is 100. Last attempt: triage", result["halt_detail"])
                    self.assertEqual(run_exit_code(result), 1)

    def test_reached_cap_never_calls_handler_and_unknown_never_applies_output(self):
        with skill_fixture() as (config, scratch):
            calls = []
            def unknown(node, model, state, instruction):
                calls.append(node)
                return Outcome("ran", {"triage": "implement"})
            result = run_delivery(FailoverDispatcher(unknown), repo="a/b", request="x", config=config, scratch=scratch)
            self.assertEqual(result["stop"], "usage_unknown")
            self.assertNotIn("triage", result)
            self.assertIsNone(result["attempts"][0]["usage"])
            self.assertEqual(run_exit_code(result), 1)
            graph = compile_delivery(FailoverDispatcher(unknown), config, run=DryRun())
            saved = {**result, "halt": None, "stop": "", "path": [], "token_cap": 1,
                     "attempts": [{**result["attempts"][0], "usage": {"input": 1, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": None}}]}
            stopped = graph.invoke(saved)
            self.assertEqual(stopped["stop"], "budget_exceeded")
            self.assertEqual(calls, ["triage"])

    def test_stored_cap_resume_quota_history_and_legacy_rejection(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            checkpoint = Path(folder) / "resume.sqlite"
            calls = []
            def quota(node, model, state, instruction):
                calls.append(node)
                return Outcome("quota", usage=Usage(input_tokens=1, output_tokens=0))
            kwargs = dict(repo="a/b", issue=8, check="exit 0", checkpoint=checkpoint,
                          run=LiveRun(FakeGitHub(), FakeWorkspace(Path(folder))))
            first = run_live_delivery(**kwargs, max_tokens=100, dispatcher=FailoverDispatcher(quota))
            self.assertEqual(first["stop"], "quota_exhausted")
            with self.assertRaisesRegex(ValueError, "stored token cap"):
                run_live_delivery(**kwargs, max_tokens=101, dispatcher=FailoverDispatcher(quota))
            second = run_live_delivery(**kwargs, dispatcher=FailoverDispatcher(quota))
            self.assertEqual(second["token_cap"], 100)
            self.assertEqual(second["attempts"][:len(first["attempts"])], first["attempts"])
            self.assertEqual(len(second["attempts"]), 2 * len(first["attempts"]))
            for snapshot in (first, {**second, "stop": "needs_human", "halt": None},
                             *({**second, "stop": halt, "halt": halt} for halt in
                               ("env_unavailable", "implement_quota", "git_failed", "publish_failed"))):
                legacy = checkpoint.with_name(str(snapshot["stop"]) + ".sqlite")
                with SqliteSaver.from_conn_string(str(legacy)) as saver:
                    graph = compile_delivery(FailoverDispatcher(quota), config, run=DryRun(), checkpointer=saver)
                    graph.update_state({"configurable": {"thread_id": "a/b#8"}}, {key: value for key, value in snapshot.items() if key != "token_cap"})
                with self.assertRaisesRegex(ValueError, "unknown usage history.*new run"):
                    run_live_delivery(**{**kwargs, "checkpoint": legacy}, dispatcher=FailoverDispatcher(quota))

    def test_legacy_unfinished_checkpoint_with_missing_skill(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            def quota(*args):
                return Outcome("quota", usage=Usage(input_tokens=1, output_tokens=0))
            kwargs = dict(repo="a/b", issue=8, check="exit 0",
                          run=LiveRun(FakeGitHub(), FakeWorkspace(Path(folder))))
            first = run_live_delivery(**kwargs, checkpoint=Path(folder) / "a.sqlite", dispatcher=FailoverDispatcher(quota))
            legacy = Path(folder) / "legacy.sqlite"
            with SqliteSaver.from_conn_string(str(legacy)) as saver:
                graph = compile_delivery(FailoverDispatcher(quota), config, run=DryRun(), checkpointer=saver)
                graph.update_state({"configurable": {"thread_id": "a/b#8"}},
                                   {key: value for key, value in first.items() if key != "token_cap"})
            with patch("foreman.delivery.export_skills", side_effect=SkillMissing(config.pstack)) as export:
                with self.assertRaisesRegex(ValueError, "unknown usage history.*new run"):
                    run_live_delivery(**kwargs, checkpoint=legacy, dispatcher=FailoverDispatcher(quota))
                export.assert_not_called()

    def test_cli_positive_tokens_default_and_done_log(self):
        for raw in ("0", "-1", "abc"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["run", "delivery", "--repo", "a/b", "--dry-run", "--request", "x", "--max-tokens", raw])
        result = run_delivery(FailoverDispatcher(ScriptHandler()), repo="a/b", request="x", max_tokens=1)
        self.assertEqual(result["stop"], "budget_exceeded")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            _print_run(result)
        self.assertIn("tokens 2 of 1", output.getvalue())
        self.assertIn("overshoot", output.getvalue())
        self.assertEqual(run_delivery(FailoverDispatcher(ScriptHandler()), repo="a/b", request="x")["token_cap"], DEFAULT_TOKEN_CAP)

    def test_resume_checks_the_historical_snapshot_it_will_invoke(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            checkpoint = Path(folder) / "historical.sqlite"
            invocation = {"configurable": {"thread_id": "a/b#8"}}
            live = LiveRun(FakeGitHub(), FakeWorkspace(Path(folder)))
            with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                graph = compile_delivery(FailoverDispatcher(ScriptHandler()), config, run=live, checkpointer=saver)
                graph.update_state(invocation, {"repo": "a/b", "check": "exit 0", "request": "x", "path": ["triage"],
                    "triage": "implement", "preflight": "exit 1", "pstack_pin": config.pstack.model_dump()}, as_node="prepare")
                graph.update_state(invocation, {"token_cap": 100, "max_budget_usd": None, "halt": "env_unavailable", "stop": "env_unavailable",
                                                "path": ["implement", "done"]}, as_node="done")
            with self.assertRaisesRegex(ValueError, "unknown usage history"):
                run_live_delivery(repo="a/b", issue=8, check="exit 0", checkpoint=checkpoint, run=live,
                                  dispatcher=FailoverDispatcher(ScriptHandler()))

    def test_finished_legacy_checkpoint_can_be_printed(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            checkpoint = Path(folder) / "finished.sqlite"
            with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                graph = compile_delivery(None, config, run=DryRun(), checkpointer=saver)
                graph.update_state({"configurable": {"thread_id": "a/b#8"}},
                                   {"repo": "a/b", "check": "", "path": ["done"], "stop": "answered"}, as_node="done")
            result = run_live_delivery(repo="a/b", issue=8, check="", checkpoint=checkpoint,
                                       run=LiveRun(FakeGitHub(), FakeWorkspace(Path(folder))))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                _print_run(result)
            self.assertIn("done answered", output.getvalue())
            self.assertNotIn("tokens", output.getvalue())
