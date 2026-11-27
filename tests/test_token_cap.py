import tests
import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Outcome, Usage
from foreman.cli import _print_run, run_exit_code
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun, compile_delivery, run_delivery, run_tokens
from foreman.dry_run import DryRun, ScriptHandler
from foreman.runner import run_live_delivery, snapshot_result
from foreman.state import ReviewState, PlanState, ChecksState, RunState, StartSettings, checkpoint_serializer, CheckpointVersionError
from foreman.doctor import NotReady, operator_variables
from tests.skill_fixture import skill_fixture
from test_runner import FakeGitHub, FakeWorkspace


class TokenCapTests(unittest.TestCase):
    def test_persisted_history_boundary_and_quota_fallback(self):
        cases = ((90, (("ran", 9),), "needs_human"), (90, (("ran", 10),), "budget_exceeded"),
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
                                          "human_question": "Which?"}, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=tokens, output_tokens=0, total_cost_usd=0.0))
                dispatch = handler
                with SqliteSaver.from_conn_string(checkpoint) as saver:
                    saver.serde = checkpoint_serializer()
                    graph = compile_delivery(dispatch, config, run=LiveRun(forge=DryRun(), workspace=DryRun()), checkpointer=saver)
                    graph.update_state(invocation, {"review": ReviewState(), "run": RunState(repo="a/b", issue=8, request="x", base_sha=DryRun.head), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "checks": ChecksState(check=""), "path": [],
                        "plan": PlanState(issue_kind="parent"), "attempts": [{"stage": "spec",
                        "model": "prior", "commit": "a", "methods": [], "outcome": "ran",
                        "usage": {"input": prior, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": None}}]}, as_node="__start__")
                with SqliteSaver.from_conn_string(checkpoint) as saver:
                    saver.serde = checkpoint_serializer()
                    graph = compile_delivery(dispatch, config, run=LiveRun(forge=DryRun(), workspace=DryRun()), checkpointer=saver)
                    graph.invoke(None, invocation, durability="sync")
                    result = snapshot_result(graph.get_state(invocation, subgraphs=True))
                total = prior + sum(tokens for _, tokens in replies)
                self.assertEqual(result.get("stop"), expected)
                self.assertEqual(len(calls), len(replies))
                self.assertEqual(run_tokens(result["attempts"]), total)
                self.assertEqual(len(result["attempts"]), 1 + len(replies))
                if expected == "budget_exceeded":
                    self.assertEqual(result["plan"].triage, "")
                    self.assertIn(f"Run used {total} work tokens; cap is 100. Last attempt: triage", result["halt_detail"])
                    self.assertEqual(run_exit_code(result), 75)

    def test_reached_cap_never_calls_handler_and_unknown_never_applies_output(self):
        with skill_fixture() as (config, scratch):
            calls = []
            def unknown(node, model, state, instruction):
                calls.append(node)
                return Outcome("ran", {"triage": "supplied_slice"})
            result = run_delivery(unknown, repo="a/b", request="x", config=config, scratch=scratch)
            # D10: unknown usage is charged at the remaining allowance and its output is not applied.
            self.assertEqual(result["stop"], "budget_exceeded")
            self.assertIn("usage_unknown", result["halt_detail"])
            self.assertEqual(result["plan"].triage, "")
            self.assertIsNone(result["attempts"][0]["usage"])
            self.assertEqual(run_exit_code(result), 75)
            graph = compile_delivery(unknown, config, run=LiveRun(forge=DryRun(), workspace=DryRun()))
            saved = {"review": ReviewState(), **result, "halt": None, "stop": "", "path": [], "run": RunState(issue=8, base_sha=DryRun.head), "start": StartSettings(human_login="fixture-owner", max_tokens=1, max_budget_usd="3"), "attempts": [{**result["attempts"][0], "usage": {"input": 1, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": None}}]}
            stopped = graph.invoke(saved)
            self.assertEqual(stopped["stop"], "budget_exceeded")
            self.assertEqual(calls, ["triage"])

    def test_ready_after_worker_unavailable_reads_the_cap_and_keeps_the_quota_history(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            checkpoint = Path(folder) / "resume.sqlite"
            calls = []
            def quota(node, model, state, instruction):
                calls.append(node)
                return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=0.0))
            forge = FakeGitHub()
            forge.settings["FOREMAN_MAX_TOKENS"] = "100"
            kwargs = dict(repo="a/b", issue=8, checkpoint=checkpoint, run=LiveRun(forge, FakeWorkspace(Path(folder))))
            first = run_live_delivery(**kwargs, dispatcher=quota)
            self.assertEqual(first["stop"], "worker_unavailable")
            # Ready after the halt is a start, so it reads the caps again (#379).
            forge.settings["FOREMAN_MAX_TOKENS"] = "101"
            second = run_live_delivery(**kwargs, dispatcher=quota)
            self.assertEqual((second["halt"], second["start"].max_tokens), ("worker_unavailable", 101))
            self.assertEqual(second["attempts"][:len(first["attempts"])], first["attempts"])
            self.assertEqual(len(second["attempts"]), 2 * len(first["attempts"]))
    def test_positive_tokens_default_and_done_log(self):
        for raw in ("0", "-1", "abc"):
            with self.subTest(raw=raw), self.assertRaisesRegex(NotReady, "FOREMAN_MAX_TOKENS"):
                operator_variables("a/b", {"FOREMAN_HUMAN_LOGIN": "fixture-owner", "FOREMAN_MAX_TOKENS": raw})
        result = run_delivery(ScriptHandler(), repo="a/b", request="x", max_tokens=1)
        self.assertEqual(result["stop"], "budget_exceeded")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            _print_run(result)
        self.assertIn("work tokens used / work cap: 2 / 1", output.getvalue())
        self.assertIn("overshoot", output.getvalue())
        self.assertEqual(run_delivery(ScriptHandler(), repo="a/b", request="x")["start"].max_tokens, DEFAULT_TOKEN_CAP)

    def test_finished_legacy_checkpoint_is_refused(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            checkpoint = Path(folder) / "finished.sqlite"
            with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(ScriptHandler(), config, run=LiveRun(forge=DryRun(), workspace=DryRun()), checkpointer=saver)
                graph.update_state({"configurable": {"thread_id": "a/b#8"}},
                                   {"review": ReviewState(), "run": RunState(repo="a/b"), "checks": ChecksState(check=""), "path": ["research"], "stop": "answered", "plan": PlanState()}, as_node="research")
            with self.assertRaisesRegex(CheckpointVersionError, "Start a new run"):
                run_live_delivery(repo="a/b", issue=8, checkpoint=checkpoint, dispatcher=ScriptHandler(),
                                  run=LiveRun(FakeGitHub(), FakeWorkspace(Path(folder))))
