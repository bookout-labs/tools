import tests
import contextlib
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Overwrite

from foreman.adapters import Outcome, Usage
from foreman.budget import CostUnknown, Exhausted, Remaining, dollar_admission
from foreman.cli import _print_run
from foreman.delivery import LiveRun, compile_delivery, run_delivery, run_tokens
from foreman.dry_run import DryRun, ScriptHandler, SCRIPTED_USAGE
from foreman.runner import run_live_delivery, snapshot_result
from foreman.state import ReviewState, PlanState, ChecksState, RunState, StartSettings, checkpoint_serializer, SCHEMA_VERSION
from foreman.usd import BudgetUsd
from foreman.doctor import NotReady, operator_variables
from foreman.workspace import WorkspaceError
from tests.skill_fixture import skill_fixture
from test_runner import FakeGitHub, FakeWorkspace, planning_answer_env

THREAD = {"configurable": {"thread_id": "a/b#8"}}
NEEDS_HUMAN = {"triage": "needs_human", "work_kind": "feature", "slice": None, "human_question": "Which?"}


def usd(text: str) -> BudgetUsd:
    return BudgetUsd(Decimal(text))


def attempt(model, cost=None, *, outcome="ran", usage=True):
    return {"stage": "triage", "model": model, "commit": "a", "methods": [], "outcome": outcome,
            "usage": {"input": 1, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": cost} if usage else None}


def printed(result) -> str:
    with contextlib.redirect_stdout(io.StringIO()) as output:
        _print_run(result)
    return output.getvalue()


class DollarAdmissionTests(unittest.TestCase):
    def test_claude_spend_against_the_limit(self):
        limit = usd("1")
        cases = (
            ([attempt("claude-opus-5-5-high", 0.8), attempt("claude-opus-5-5-high", 0.1)], Remaining(Decimal("0.1"))),
            ([attempt("claude-opus-5-5-high", 1.0)], Exhausted()),
            ([attempt("claude-opus-5-5-high", 1.05)], Exhausted()),
            ([attempt("claude-opus-5-5-high", usage=False, outcome="quota")], CostUnknown()),
            ([attempt("claude-opus-5-5-high", None)], CostUnknown()),
            ([attempt("claude-opus-5-5-high", usage=False, outcome="cli_missing")], Remaining(Decimal("1"))),
            ([attempt("gpt-6.1-sol-high", 5.0), attempt("grok-4.7-high", 5.0)], Remaining(Decimal("1"))),
        )
        for attempts, expected in cases:
            with self.subTest(attempts=attempts):
                self.assertEqual(dollar_admission(limit, attempts), expected)


class DollarAllowanceTests(unittest.TestCase):
    def test_fresh_process_cli_resume_preserves_allowance_before_dispatch(self):
        script = r'''
from dataclasses import replace
from pathlib import Path
import json
import os
import subprocess
import sys
from unittest.mock import patch
from foreman.cli import main
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun
from test_runner import FakeGitHub, FakeWorkspace, fixture_process, worker_reply, quota_with_usage

checkpoint, raw_pin, phase = sys.argv[1:4]
def worker(args, cwd, env):
    with Path("workers.log").open("a") as log:
        log.write(json.dumps(args) + "\n")
    if phase == "needs_human":
        return worker_reply(args, {"triage": "needs_human", "human_question": "Which items?"})
    process = quota_with_usage(args)
    if args[0] == "claude":
        envelope = json.loads(process.stdout)
        envelope["total_cost_usd"] = 0.01
        for index, row in enumerate(envelope["modelUsage"].values()):
            row["costUSD"] = 0.01 if index == 0 else 0.0
        return subprocess.CompletedProcess(args, 1, json.dumps(envelope), "")
    return process

pin = SkillPin.model_validate_json(raw_pin)
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
    sys.exit(main(sys.argv[4:], worker_runner=worker, checkpoint=Path(checkpoint),
                  live_run=LiveRun(FakeGitHub(), FakeWorkspace(Path.cwd()))))
'''
        bootstrap = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
                     f"sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n")
        for pause in ("needs_human", "quota_exhausted"):
            for stored in ("1.5", None):
                with self.subTest(pause=pause, stored=stored), skill_fixture() as (config, _), TemporaryDirectory() as folder:
                    root = Path(folder)
                    checkpoint = root / "allowance.sqlite"
                    checkout = root / "checkout"
                    checkout.mkdir()
                    log = checkout / "workers.log"

                    def invoke(phase, *flags, expected, env=None, settings):
                        # The fake forge in the subprocess serves these repository variables.
                        env = {**(env or os.environ), "FOREMAN_FIXTURE_SETTINGS": json.dumps(settings)}
                        process = subprocess.run(
                            [sys.executable, "-c", bootstrap + script, str(checkpoint), config.pstack.model_dump_json(),
                             phase, "run", "delivery", "--repo", "a/b", "--issue", "8", *flags],
                            cwd=checkout, text=True, capture_output=True, timeout=30, env=env,
                        )
                        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
                        return process

                    def saved():
                        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                            saver.serde = checkpoint_serializer()
                            return saver.get(THREAD)["channel_values"]

                    def saved_attempts():
                        """A quota pause keeps its attempts in the nested dispatch checkpoint."""
                        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                            saver.serde = checkpoint_serializer()
                            graph = compile_delivery(ScriptHandler(), config, run=LiveRun(FakeGitHub(), FakeWorkspace(checkout)),
                                                     checkpointer=saver)
                            return snapshot_result(graph.get_state(THREAD, subgraphs=True))["attempts"]

                    settings = {"FOREMAN_HUMAN_LOGIN": "fixture-owner", **({"FOREMAN_MAX_BUDGET_USD": "1.50"} if stored else {})}
                    allowance = stored or "3"
                    first = invoke(pause, expected=3 if pause == "needs_human" else 75, settings=settings)
                    self.assertIn("paused " + pause, first.stdout)
                    self.assertEqual(saved()["start"].max_budget_usd, allowance)
                    before = log.read_text()
                    attempts = saved_attempts()
                    answer = ("--answer", "Only open items.") if pause == "needs_human" else ()
                    relayed = planning_answer_env(first.stdout, "Only open items.") if answer else None
                    # The operator changes the cap while the run waits. The invocation that continues it reads no variable.
                    resumed = invoke("quota", *answer, expected=75, env=relayed, settings={**settings, "FOREMAN_MAX_BUDGET_USD": "2"})
                    self.assertIn("restored ", resumed.stdout)
                    self.assertIn("paused quota_exhausted", resumed.stdout)
                    new_calls = [json.loads(line) for line in log.read_text()[len(before):].splitlines()]
                    claude = next(args for args in new_calls if args[0] == "claude")
                    prior_costs = [Decimal(repr(a["usage"]["cost_usd"])) for a in attempts if a["model"].startswith("claude-")]
                    self.assertEqual(claude.count("--max-budget-usd"), 1)
                    self.assertEqual(Decimal(claude[claude.index("--max-budget-usd") + 1]), Decimal(allowance) - sum(prior_costs))
                    self.assertTrue(all("--max-budget-usd" not in args for args in new_calls if args[0] != "claude"))
                    self.assertEqual(saved()["start"].max_budget_usd, allowance)
                    self.assertIn("claude spend", resumed.stdout)
                    self.assertIn(f"of ${allowance}", resumed.stdout)

    def test_the_repository_cap_accepts_only_finite_positive_amounts(self):
        for raw in ("0", "-1", "abc", "NaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw), self.assertRaisesRegex(NotReady, "FOREMAN_MAX_BUDGET_USD"):
                operator_variables("a/b", {"FOREMAN_HUMAN_LOGIN": "fixture-owner", "FOREMAN_MAX_BUDGET_USD": raw})
        for raw, stored in (("1", "1"), ("1.50", "1.5"), ("1e-4", "0.0001")):
            with self.subTest(raw=raw):
                read = operator_variables("a/b", {"FOREMAN_HUMAN_LOGIN": "fixture-owner", "FOREMAN_MAX_BUDGET_USD": raw})
                self.assertEqual(read.start.max_budget_usd, stored)

    def test_run_delivery_stores_canonical_text_and_defaults_to_the_drawn_cap(self):
        bounded = run_delivery(ScriptHandler(), repo="a/b", request="x", max_budget_usd=usd("1.50"))
        self.assertEqual(bounded["start"].max_budget_usd, "1.5")
        # A dry run gets the cap a live run gets with no variable set.
        default = run_delivery(ScriptHandler(), repo="a/b", request="x")
        self.assertEqual(default["start"].max_budget_usd, "3")
        self.assertIn("claude spend $0.0 of $3", printed(default))

    def test_printed_spend_counts_only_claude_costs(self):
        base = {"review": ReviewState(), "run": RunState(repo="a/b"), "start": StartSettings(human_login="fixture-owner", max_tokens=20_000_000, max_budget_usd="1"), "checks": ChecksState(check=""), "path": ["research"], "stop": "answered", "plan": PlanState()}
        counted = [attempt("claude-opus-5-5-high", 0.1), attempt("claude-opus-5-5-high", 0.2),
                   attempt("claude-opus-5-5-high", usage=False, outcome="cli_missing"),
                   attempt("gpt-6.1-sol-high", 5.0), attempt("grok-4.7-high", 5.0)]
        self.assertIn("claude spend $0.3 of $1", printed({**base, "attempts": counted}))
        for missing in (attempt("claude-opus-5-5-high", None), attempt("claude-opus-5-5-high", usage=False, outcome="quota")):
            with self.subTest(missing=missing):
                self.assertIn("claude spend unknown of $1", printed({**base, "attempts": [*counted, missing]}))
        # A refused new run read no caps, so it reports no spend against them.
        refused = {key: value for key, value in base.items() if key != "start"}
        self.assertNotIn("claude spend", printed({**refused, "stop": "not_ready", "attempts": []}))

    def test_a_continuing_invocation_keeps_the_stored_allowance(self):
        for pause in ("needs_human", "quota_exhausted"):
            for stored in ("1.5", None):
                with self.subTest(pause=pause, stored=stored), skill_fixture() as (config, _), TemporaryDirectory() as folder, \
                     patch("foreman.runner.load_config", return_value=config):
                    calls = []

                    def handler(node, model, state, instruction):
                        calls.append(node)
                        if pause == "needs_human" and len(calls) == 1:
                            return Outcome("ran", NEEDS_HUMAN, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=0.1))
                        return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=0.0))

                    forge = FakeGitHub()
                    if stored:
                        forge.settings["FOREMAN_MAX_BUDGET_USD"] = "1.50"
                    kwargs = dict(repo="a/b", issue=8, checkpoint=Path(folder) / "allowance.sqlite",
                                  run=LiveRun(forge, FakeWorkspace(Path(folder))), dispatcher=handler)
                    state = run_live_delivery(**kwargs)
                    if pause == "needs_human":
                        forge.answer(state["halt_detail"], "Only open items.")
                    self.assertEqual(state["stop"], pause)
                    self.assertEqual(state["start"].max_budget_usd, stored or "3")
                    for changed in ("2", "1.5"):
                        forge.settings["FOREMAN_MAX_BUDGET_USD"] = changed
                        before = len(calls)
                        answer = "Only open items." if state["stop"] == "needs_human" else None
                        state = run_live_delivery(**kwargs, answer=answer)
                        self.assertGreater(len(calls), before)
                        self.assertEqual(state["start"].max_budget_usd, stored or "3")

    def test_resumable_checkpoints_without_a_stored_allowance_need_a_new_run(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder, patch("foreman.runner.load_config", return_value=config):
            calls = []

            def quota(node, model, state, instruction):
                calls.append(node)
                return Outcome("quota", usage=SCRIPTED_USAGE)

            root = Path(folder)
            kwargs = dict(repo="a/b", issue=8, run=LiveRun(FakeGitHub(), FakeWorkspace(root)),
                          dispatcher=quota)
            first = run_live_delivery(**kwargs, checkpoint=root / "modern.sqlite")
            self.assertEqual(first["stop"], "quota_exhausted")
            dispatched = len(calls)
            snapshots = (first, {**first, "stop": "needs_human", "halt": None},
                         *({**first, "stop": halt, "halt": halt} for halt in ("env_unavailable", "git_failed")))
            for snapshot in snapshots:
                with self.subTest(stop=snapshot["stop"]):
                    legacy = root / f"{snapshot['stop']}.sqlite"
                    with SqliteSaver.from_conn_string(str(legacy)) as saver:
                        saver.serde = checkpoint_serializer()
                        graph = compile_delivery(quota, config, run=LiveRun(forge=DryRun(), workspace=DryRun()), checkpointer=saver)
                        graph.update_state(THREAD, {**snapshot, "schema_version": 3})
                    with self.assertRaisesRegex(ValueError, "schema_version.*Start a new run"):
                        run_live_delivery(**kwargs, checkpoint=legacy)
            self.assertEqual(len(calls), dispatched)

            historical = root / "historical.sqlite"
            with SqliteSaver.from_conn_string(str(historical)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(quota, config, run=kwargs["run"], checkpointer=saver)
                graph.update_state(THREAD, {"review": ReviewState(review="changes"), "stop": "P1", "schema_version": 5, "run": RunState(repo="a/b", request="x"), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "checks": ChecksState(preflight="exit 1"), "path": ["triage"],
                    "pstack_pin": config.pstack.model_dump(), "matt_skills_pin": config.matt_skills.model_dump()}, as_node="review")
                graph.update_state(THREAD, {"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(repo="a/b", request="x"), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "halt": "env_unavailable", "stop": "env_unavailable",
                                            "path": ["implement"], "plan": PlanState()}, as_node="research")
            with patch("foreman.runner.compile_delivery", wraps=compile_delivery) as compile_graph:
                with self.assertRaisesRegex(ValueError, "schema_version.*Start a new run"):
                    run_live_delivery(**kwargs, checkpoint=historical)
                compile_graph.assert_called_once()
            self.assertEqual(len(calls), dispatched)

            finished = root / "finished.sqlite"
            with SqliteSaver.from_conn_string(str(finished)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(ScriptHandler(), config, run=LiveRun(forge=DryRun(), workspace=DryRun()), checkpointer=saver)
                graph.update_state(THREAD, {"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(repo="a/b"), "start": StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), "checks": ChecksState(check=""), "path": ["research"], "stop": "answered", "plan": PlanState()}, as_node="research")
            output = printed(run_live_delivery(**kwargs, checkpoint=finished))
            self.assertIn("done answered", output)
            self.assertIn("claude spend $0 of $3", output)

    def test_halt_replay_keeps_attempts_recorded_by_the_halted_call(self):
        class GitFailsOnce(FakeWorkspace):
            """Git status fails only after a worker ran, so the halted call has a charged attempt."""
            fail, ran = True, False

            def fingerprint(self):
                if self.fail and self.ran:
                    raise WorkspaceError("git status failed")
                return super().fingerprint()

        for case in ("replay", "non_prefix"):
            with self.subTest(case=case), skill_fixture() as (config, _), TemporaryDirectory() as folder, \
                 patch("foreman.runner.load_config", return_value=config):
                root = Path(folder)
                workspace = GitFailsOnce(root)
                calls = []
                claude_cost = iter((0.3, 0.1))

                def handler(node, model, state, instruction):
                    calls.append(node)
                    if node == "triage":
                        return Outcome("ran", {**NEEDS_HUMAN, "triage": "supplied_slice", "human_question": None},
                                       usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.2))
                    if node == "spec_check":
                        return ScriptHandler()(node, model, state, instruction)
                    workspace.ran = True
                    cost = next(claude_cost) if model.family == "claude" else 5.0
                    return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=0, total_cost_usd=cost))

                checkpoint = root / "replay.sqlite"
                forge = FakeGitHub()
                forge.settings["FOREMAN_MAX_BUDGET_USD"] = "1"
                kwargs = dict(repo="a/b", issue=8, checkpoint=checkpoint, run=LiveRun(forge, workspace), dispatcher=handler)
                halted = run_live_delivery(**kwargs)
                self.assertEqual(halted["stop"], "git_failed")
                implement = [a for a in halted["attempts"] if a["stage"] == "implement"]
                self.assertEqual([a["usage"]["cost_usd"] for a in implement], [5.0])
                workspace.fail = False
                dispatched = len(calls)
                if case == "non_prefix":
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saver.serde = checkpoint_serializer()
                        graph = compile_delivery(ScriptHandler(), config, run=LiveRun(forge=DryRun(), workspace=DryRun()), checkpointer=saver)
                        graph.update_state(THREAD, {"attempts": Overwrite(implement)}, as_node="research")
                    with self.assertRaisesRegex(ValueError, "non-prefix attempt history"):
                        run_live_delivery(**kwargs)
                    self.assertEqual(len(calls), dispatched)
                    continue
                resumed = run_live_delivery(**kwargs)
                self.assertEqual(resumed["stop"], "quota_exhausted")
                self.assertEqual(resumed["attempts"][:len(halted["attempts"])], halted["attempts"])
                # The replay tries each implement model once more; every quota attempt reports one token.
                retried = [a for a in resumed["attempts"][len(halted["attempts"]):] if a["stage"] == "implement"]
                self.assertEqual(len({a["model"] for a in retried}), len(retried))
                self.assertEqual(run_tokens(resumed["attempts"]), run_tokens(halted["attempts"]) + len(retried))
                self.assertIn("claude spend $0.5 of $1", printed(resumed))
