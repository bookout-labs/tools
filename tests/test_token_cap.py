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
from tests.skill_fixture import skill_fixture
from test_runner import FakeGitHub, FakeWorkspace


class TokenCapTests(unittest.TestCase):
    def test_persisted_history_boundary_and_quota_fallback(self):
        for kind, tokens, expected in (("ran", 9, None), ("ran", 10, "budget_exceeded"),
                                       ("ran", 11, "budget_exceeded"), ("quota", 10, "budget_exceeded")):
            with self.subTest(kind=kind, tokens=tokens), skill_fixture() as (config, scratch), TemporaryDirectory() as folder:
                checkpoint = str(Path(folder) / "cap.sqlite")
                invocation = {"configurable": {"thread_id": "cap"}}
                calls = []
                def handler(node, model, state, instruction):
                    calls.append(model.name)
                    return Outcome(kind, {"triage": "needs_human", "work_kind": "feature", "slice": None,
                                          "human_question": "Which?"}, usage=Usage(input_tokens=tokens, output_tokens=0))
                dispatch = FailoverDispatcher(handler)
                with SqliteSaver.from_conn_string(checkpoint) as saver:
                    graph = compile_delivery(dispatch, config, run=DryRun(), checkpointer=saver)
                    graph.update_state(invocation, {"repo": "a/b", "check": "", "path": ["triage"], "request": "x",
                        "issue_kind": "parent", "token_cap": 100, "usage_version": 1, "attempts": [{"stage": "spec",
                        "model": "prior", "commit": "a", "methods": [], "outcome": "ran",
                        "usage": {"input": 90, "output": 0, "cost_usd": None}}]}, as_node="needs_human")
                with SqliteSaver.from_conn_string(checkpoint) as saver:
                    graph = compile_delivery(dispatch, config, run=DryRun(), checkpointer=saver)
                    graph.invoke(None, invocation, durability="sync")
                    result = graph.get_state(invocation).values
                self.assertEqual(result.get("stop"), expected)
                self.assertEqual(len(calls), 1)
                self.assertEqual(run_tokens(result["attempts"]), 90 + tokens)
                self.assertEqual(len(result["attempts"]), 2)
                if expected == "budget_exceeded":
                    self.assertNotIn("triage", result)
                    self.assertIn(f"Run used {90 + tokens} tokens; cap is 100. Last attempt: triage", result["halt_detail"])
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
                     "attempts": [{**result["attempts"][0], "usage": {"input": 1, "output": 0, "cost_usd": None}}]}
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
                             {**second, "stop": "env_unavailable", "halt": "env_unavailable"}):
                with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                    graph = compile_delivery(FailoverDispatcher(quota), config, run=DryRun(), checkpointer=saver)
                    graph.update_state({"configurable": {"thread_id": "a/b#8"}}, {**snapshot, "usage_version": 0})
                with self.assertRaisesRegex(ValueError, "unknown usage history.*new run"):
                    run_live_delivery(**kwargs, dispatcher=FailoverDispatcher(quota))

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
                    "triage": "implement", "preflight": "exit 1", "token_cap": 100, "usage_version": 0,
                    "pstack_pin": config.pstack.model_dump()}, as_node="triage")
                graph.invoke(None, invocation, durability="sync")
                self.assertEqual(graph.get_state(invocation).values["halt"], "env_unavailable")
                graph.update_state(invocation, {"usage_version": 1}, as_node="done")
            with self.assertRaisesRegex(ValueError, "unknown usage history"):
                run_live_delivery(repo="a/b", issue=8, check="exit 0", checkpoint=checkpoint, run=live,
                                  dispatcher=FailoverDispatcher(ScriptHandler()))
