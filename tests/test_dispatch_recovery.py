"""Shared dispatch recovery across killed processes (DP2-DP4)."""

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt

from foreman.adapters import Call, Outcome, Session, Usage
from foreman.adapters.claude import ClaudeAdapter
from foreman.adapters.codex import CodexAdapter
from foreman.budget import claude_spend, work_tokens
from foreman.cli import run_exit_code
from foreman.config import ModelSpec, load_config
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun
from foreman.dispatch import DispatchRun, InvalidOutput
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.github import LiveForge
from foreman.runner import issue_lock, run_live_delivery
from foreman.state import SCHEMA_VERSION, CheckpointVersionError, StartSettings, checkpoint_serializer
from foreman.stage_output import DispatchPlan, TriageOutput, parse_output
from foreman.worker import CliWorker, assignment, run_worker_command
from foreman.workspace import LiveWorkspace, WorkspaceError, WorktreeState
from tests.skill_fixture import skill_fixture, verify_checkout
from test_delivery import EMPTY_SLICE, walk
from test_adapters import codex_events, codex_process, fixture_process
from publication_fixture import REPO, TOTALS, PublicationFixture, resume_id
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, refusal


SCRIPT = '''
import contextlib, json, os, sys
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from foreman.adapters import Outcome, Usage
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler
from foreman.usd import BudgetUsd
from foreman.runner import RunPresentation, run_live_delivery
from foreman.worker import run_worker_command
from foreman.workspace import WorktreeState
from test_runner import FakeGitHub, FakeWorkspace
mode, checkpoint, raw_pin, limit = sys.argv[1:]
script = ScriptHandler()
calls = []
workspace = FakeWorkspace(Path.cwd())
QUOTA = Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=9, output_tokens=1, total_cost_usd=0.1)
def handler(node, model, state, instruction):
    calls.append([node, model.name])
    if node == "triage" and mode == "entry":
        os._exit(19)
    if node == "triage" and mode in {"quota", "quota-uncommitted"} and len(calls) == 1:
        return Outcome("quota", usage=QUOTA)
    if node == "triage" and mode == "quota":
        os._exit(19)
    if node == "triage" and mode == "hold":
        # A worker that outlives its parent keeps the issue lock.
        run_worker_command(["sh", "-c", "echo $$ > " + sys.argv[2] + ".worker; exec sleep 60", "unused"], Path.cwd(), {})
    if node == "implement":
        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
    return script(node, model, state, instruction)
def crash(*args, **kwargs):
    os._exit(19)
pin = SkillPin.model_validate_json(raw_pin)
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
    with patch("foreman.dispatch.Attempt.record", side_effect=crash) if mode == "quota-uncommitted" else contextlib.nullcontext():
        with patch("foreman.delivery.stage_update", side_effect=crash) if mode == "success" else contextlib.nullcontext():
            forge = FakeGitHub()
            if limit:
                forge.settings["FOREMAN_MAX_BUDGET_USD"] = limit
            try:
                result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=Path(checkpoint),
                    dispatcher=handler, presentation=RunPresentation(), run=LiveRun(forge, workspace))
            except ValueError as exc:
                print(json.dumps({"error": str(exc), "calls": calls}))
                sys.exit(17)
print(json.dumps({"result": result, "calls": calls}, default=lambda value: value.model_dump(mode="json")))
'''


class IssueLockTests(unittest.TestCase):
    def test_one_writer_per_repository_and_issue_whatever_the_checkpoint(self):
        other = "from foreman.runner import issue_lock\nwith issue_lock({!r}): pass"
        with issue_lock("example/lock-test#8"):
            # Another process is refused. The holder itself may enter again, as foreman start does.
            refused = subprocess.run([sys.executable, "-c", other.format("example/lock-test#8")], capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("IssueLocked: Foreman is already running example/lock-test#8", refused.stderr)
            self.assertEqual(subprocess.run([sys.executable, "-c", other.format("example/lock-test#9")]).returncode, 0)
            with issue_lock("example/lock-test#8"):
                pass

    def test_processes_a_worker_leaves_behind_end_with_it_and_release_the_lock(self):
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / "leftover.pid"
            with issue_lock("example/lock-test#8"):
                # A build tool leaves a background process that inherited the lock, then the worker exits.
                done = run_worker_command(["sh", "-c", f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}", "worker"], Path(directory), {})
            self.assertEqual(done.returncode, 0)
            with issue_lock("example/lock-test#8"):
                pass
            pid, deadline = int(pidfile.read_text()), time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"leftover process {pid} outlived its worker")


class CodexQuotaResumeTests(PublicationFixture):
    def test_codex_quota_after_partial_edits_is_charged_its_rollout_and_resumes_its_session(self):
        # DP3 and D10: a Codex quota stop after edits is charged what its rollout logged and the next models run. The run
        # halts worker_unavailable, keeping that session as the builder's, and the call Ready makes resumes it on the
        # saved fingerprint.
        session = "01a1ff05-7685-7282-b515-828a0b63ee12"
        sol, terra = load_config().nodes["implement"].models[:2]
        home = Path(self.fixture.enter_context(TemporaryDirectory()))
        self.fixture.enter_context(patch.object(CodexAdapter, "env", {"CODEX_HOME": str(home)}))
        day = home / "sessions" / "2026" / "11" / "23"
        day.mkdir(parents=True)
        (day / f"rollout-2026-11-23T08-00-00-{session}.jsonl").write_text("\n".join(json.dumps(line) for line in (
            {"type": "event_msg", "payload": {"type": "task_started"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1200, "cached_input_tokens": 200, "cache_write_input_tokens": 0, "output_tokens": 30}}}})))
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}},
            {"type": "turn.failed", "error": {"message": "You’ve hit your usage limit. Try again later."}}))
        launches, commands = [], []
        launch = DispatchRun.launch

        def observed(run, state, reservation, model):
            if state["stage"] == "implement":
                launches.append((model.name, dict(state.get("sessions", {})), state.get("fingerprint"), reservation["fingerprint"]))
            return launch(run, state, reservation, model)

        def stop(model, state, instruction):
            if model != sol:
                return Outcome("quota", usage=SCRIPTED_USAGE)
            (self.checkout / "items.txt").write_text("partial\n")
            return CodexAdapter(sol).read(subprocess.CompletedProcess(["codex"], 1, stream, ""), Call("prompt", "{}", True, home))

        def resume(model, state, instruction):
            # The real worker turns the saved session into the Codex command, then finishes the tree.
            CliWorker(self.checkout, run=lambda args, cwd, env: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
                      scratch_root=Path(self.fixture.enter_context(TemporaryDirectory())), containers=lambda base: ("container-1", 1800))(
                "implement", model, state, instruction)
            self.edit()

        with patch.object(DispatchRun, "launch", observed):
            paused = self.walk(model_edit=stop)
            self.assertEqual((paused["halt"], paused["builder_session"]["model"]), ("worker_unavailable", sol.name))
            implement = [a for a in paused["attempts"] if a["stage"] == "implement"]
            self.assertEqual([a["model"] for a in implement[:2]], [sol.name, terra.name])
            self.assertEqual((implement[0]["outcome"], work_tokens([implement[0]])), ("quota", 1030))
            self.assertNotIn("work_charge", implement[0])
            (first, *fallbacks) = launches
            self.assertEqual(first[1], {})
            saved = fallbacks[0][2]
            self.assertNotEqual(saved, first[3])
            self.assertEqual([({name: held['id'] for name, held in entry[1].items()}, *entry[2:]) for entry in fallbacks],
                             [({sol.name: session}, saved, saved)] * len(fallbacks))
            resumed = self.walk(model_edit=resume)
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual(launches[len(fallbacks) + 1][::3], (sol.name, saved))
        self.assertEqual(commands[0][:3], ["codex", "exec", "resume"])
        self.assertEqual(commands[0][-2], session)


class DispatchRecoveryTests(unittest.TestCase):
    def invoke(self, config, checkpoint, phase, limit="", wait=True):
        bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
        args = [sys.executable, "-c", bootstrap + SCRIPT, phase, str(checkpoint), config.pstack.model_dump_json(), limit]
        if not wait:
            return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return subprocess.run(args, capture_output=True, text=True)

    def test_each_crash_point_reconciles_before_any_repeat(self):
        # Crash in the worker, after a durable result, after a committed quota, and before a quota is recorded.
        with skill_fixture() as (config, _), TemporaryDirectory() as folder:
            for mode in ("entry", "success", "quota", "quota-uncommitted"):
                for limit in ("", "1"):
                    with self.subTest(mode=mode, limit=limit):
                        checkpoint = Path(folder) / (mode + limit) / "run.sqlite3"
                        crashed = self.invoke(config, checkpoint, mode, limit)
                        self.assertEqual(crashed.returncode, 19, crashed.stderr)
                        resumed = self.invoke(config, checkpoint, "recover", limit)
                        self.assertEqual(resumed.returncode, 0, resumed.stderr)
                        output = json.loads(resumed.stdout)
                        result, calls = output["result"], output["calls"]
                        triage = [a for a in result["attempts"] if a["stage"] == "triage"]
                        self.assertEqual(result["start"]["max_budget_usd"], limit or "3")
                        if mode in {"entry", "quota"}:
                            # The call may have launched: charge the worst case once and never relaunch it.
                            self.assertEqual(calls, [])
                            self.assertEqual(result["stop"], "budget_exceeded")
                            self.assertEqual(run_exit_code(result), 75)
                            self.assertIn("usage_unknown", result["halt_detail"])
                            self.assertEqual([a["outcome"] for a in triage], ["quota", "interrupted"] if mode == "quota" else ["interrupted"])
                            self.assertIn("work_charge", triage[-1])
                            self.assertEqual(result["plan"]["triage"], "")
                        else:
                            # A durable result is adopted with its observed usage; only later work runs.
                            self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                            relaunched = [model for node, model in calls if node == "triage"]
                            if mode == "quota-uncommitted":
                                # Only the next qualified model runs; the adopted quota attempt is not repeated.
                                self.assertEqual(relaunched, [triage[1]["model"]])
                                self.assertNotEqual(triage[0]["model"], triage[1]["model"])
                                self.assertEqual([a["outcome"] for a in triage], ["quota", "ran"])
                                self.assertEqual(triage[0]["usage"]["input"], 9)
                                self.assertNotIn("work_charge", triage[0])
                            else:
                                self.assertEqual(relaunched, [])
                                self.assertEqual([a["outcome"] for a in triage], ["ran"])
                        self.assertEqual(len({a["attempt_id"] for a in triage}), len(triage))
                        again = json.loads(self.invoke(config, checkpoint, "recover", limit).stdout)
                        self.assertEqual(again["calls"], [])
                        if mode in {"entry", "quota"}:
                            # A budget halt resumes, and with an unchanged cap it stops again before any worker call (item 19).
                            self.assertEqual((again["result"]["stop"], again["result"]["attempts"]), ("budget_exceeded", result["attempts"]))
                        else:
                            self.assertEqual(again["result"], result)

    def test_surviving_worker_blocks_a_second_writer(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "run.sqlite3"
            owner = self.invoke(config, checkpoint, "hold", wait=False)
            pid_file = Path(str(checkpoint) + ".worker")
            for _ in range(200):
                if pid_file.exists() and pid_file.read_text().strip():
                    break
                time.sleep(0.05)
            worker = int(pid_file.read_text())
            racer = self.invoke(config, checkpoint, "recover")
            self.assertEqual(racer.returncode, 17, racer.stderr)
            self.assertIn("already running", json.loads(racer.stdout)["error"])
            owner.kill()
            owner.wait()
            blocked = self.invoke(config, checkpoint, "recover")
            self.assertEqual(blocked.returncode, 17, blocked.stderr)
            self.assertEqual(json.loads(blocked.stdout)["calls"], [])
            os.kill(worker, signal.SIGKILL)
            for _ in range(200):
                try:
                    os.kill(worker, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            resumed = self.invoke(config, checkpoint, "recover")
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            result = json.loads(resumed.stdout)["result"]
            self.assertEqual(result["stop"], "budget_exceeded")
            self.assertEqual([a["outcome"] for a in result["attempts"] if a["stage"] == "triage"], ["interrupted"])


SESSIONS = [f"0000000{n}-0000-4000-8000-000000000000" for n in range(10)]
VALID = {"implementation": "diff", "no_change": None, "human_question": None}


def cli(answer, calls):
    """The real CliWorker over a scripted CLI. Each call records its stage, model, exact prompt and resumed session."""
    scratch = TemporaryDirectory()

    def dispatch(node, model, state, instruction):
        outcome = answer(node, model, state, instruction)

        class Adapter:
            env = {}

            def command(self, call):
                calls.append((node, model.name, call.prompt, resume_id(call)))
                return ["fixture"]

            def read(self, process, call):
                return outcome

        with patch.dict("foreman.worker.ADAPTERS", {model.family: lambda _: Adapter()}):
            return CliWorker(Path.cwd(), lambda *args: subprocess.CompletedProcess([], 0), scratch_root=Path(scratch.name),
                             containers=lambda base: ("container-1", 1800))(
                node, model, state, instruction)
    dispatch.scratch = scratch
    return dispatch


class RepairTurnTests(unittest.TestCase):
    """DP5: an invalid answer gets one repair turn in its own session with the exact validation error (item 1)."""

    def run_script(self, answer, *, slice=EMPTY_SLICE, checkout=None):
        calls = []
        dispatch = cli(answer, calls)
        with dispatch.scratch:
            return walk(dispatch, slice=slice, checkout=checkout), calls

    def test_one_invalid_answer_then_a_valid_one_costs_one_extra_call_in_the_same_session(self):
        script, triage = ScriptHandler(), []
        with self.assertRaises(InvalidOutput) as raised:
            parse_output(TriageOutput, {})
        error = str(raised.exception)

        def answer(node, model, state, instruction):
            triage.append(node)
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[len(triage)], totals=TOTALS)
            return replace(outcome, output={}, session_id=SESSIONS[0], totals=TOTALS) if triage == ["triage"] else outcome

        result, calls = self.run_script(answer)
        self.assertEqual(result["stop"], "merge_pending", result.get("halt_detail"))
        triage = [a for a in result["attempts"] if a["stage"] == "triage"]
        self.assertEqual([(a["outcome"], a["usage"]["input"]) for a in triage], [("ran", 1), ("ran", 1)])
        (first, repair), rest = calls[:2], calls[2:]
        self.assertEqual(first[0], "triage")
        self.assertIn("Delivery state:", first[2])
        self.assertEqual(repair, ("triage", first[1], error, SESSIONS[0]))
        self.assertTrue(all(prompt != error and session is None for _, _, prompt, session in rest))

    def test_a_second_invalid_answer_stops_with_both_calls_charged(self):
        script = ScriptHandler()

        def answer(node, model, state, instruction):
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[0], totals=TOTALS)
            return replace(outcome, output={}) if node == "triage" else outcome

        result, calls = self.run_script(answer)
        self.assertEqual(result["halt"], "invalid_output")
        self.assertTrue(result["halt_detail"].startswith(f"triage output from {calls[1][1]}: "))
        self.assertEqual([a["outcome"] for a in result["attempts"] if a["stage"] == "triage"], ["ran", "ran"])
        self.assertEqual(len(calls), 2)

    def test_a_failed_stage_check_gets_the_same_repair_turn(self):
        # Verify's own check of its answer: the tested head must be the published head.
        script, verify = ScriptHandler(), []

        def answer(node, model, state, instruction):
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[1], totals=TOTALS)
            if node == "verify" and not verify:
                verify.append(node)
                return replace(outcome, output={**outcome.output, "tested_head": "0" * 40}, session_id=SESSIONS[0], totals=TOTALS)
            return outcome

        with verify_checkout() as checkout:
            result, calls = self.run_script(answer, checkout=checkout)
        self.assertEqual(result["stop"], "merge_pending", result.get("halt_detail"))
        verify = [call for call in calls if call[0] == "verify"]
        self.assertEqual(len(verify), 2)
        self.assertEqual(verify[1][3], SESSIONS[0])
        self.assertEqual(verify[1][2], f"tested_head {'0' * 40} is not the published head {result['work'].published_head}.")

    def test_a_rejected_candidate_is_a_plan_revision_not_a_repair(self):
        # Spec's InvalidSlicePlan goes back to draft as a counted rejection, never as a repair turn.
        script = ScriptHandler(triage="plan", slices=[EMPTY_SLICE, EMPTY_SLICE])

        def answer(node, model, state, instruction):
            return replace(script(node, model, state, instruction), session_id=SESSIONS[0], totals=TOTALS)

        result, calls = self.run_script(answer, slice=None)
        spec = [call for call in calls if call[0] == "spec"]
        self.assertGreater(len(spec), 1)
        self.assertTrue(all("Delivery state:" in prompt and session is None for _, _, prompt, session in spec))
        self.assertTrue(result["plan"].rejected_revisions)

    def test_a_repair_call_on_quota_hands_the_original_instruction_to_a_fresh_session(self):
        # DP5: the next model's answer to the original call finishes the stage, or stops invalid_output with no repair.
        for valid in (True, False):
            with self.subTest(valid=valid):
                script = ScriptHandler()
                triage = []

                def answer(node, model, state, instruction):
                    outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[len(triage)], totals=TOTALS)
                    if node != "triage":
                        return outcome
                    triage.append(model.name)
                    if len(triage) == 1 or (len(triage) == 3 and not valid):
                        return replace(outcome, output={})
                    return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[5], totals=TOTALS) if len(triage) == 2 else outcome

                result, calls = self.run_script(answer)
                first, repair, fallback = [call for call in calls if call[0] == "triage"]
                self.assertEqual((repair[1], repair[3]), (first[1], SESSIONS[0]))
                self.assertNotEqual(fallback[1], first[1])
                self.assertEqual((fallback[2], fallback[3]), (first[2], None))
                if valid:
                    self.assertEqual(result["stop"], "merge_pending", result.get("halt_detail"))
                else:
                    self.assertEqual(result["halt"], "invalid_output")
                    self.assertEqual([a["model"] for a in result["attempts"] if a["stage"] == "triage"][-1], fallback[1])
                    self.assertEqual(len(calls), 3)

    def test_the_budget_an_invalid_answer_used_up_reserves_no_repair(self):
        script = ScriptHandler()

        def answer(node, model, state, instruction):
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[0], totals=TOTALS)
            if node == "triage":
                return replace(outcome, output={}, usage=Usage(input_tokens=DEFAULT_TOKEN_CAP, output_tokens=1,
                                                              cache_read_input_tokens=0, cache_creation_input_tokens=0))
            return outcome

        result, calls = self.run_script(answer)
        self.assertEqual(result["halt"], "budget_exceeded")
        self.assertEqual(len(calls), 1)

    def test_only_an_answer_gets_a_repair_turn(self):
        # A CLI failure carries no answer, even with a session ID, and an invalid answer without one is not an answer.
        for outcome in (Outcome("failed", session_id=SESSIONS[0], totals=TOTALS, usage=SCRIPTED_USAGE, detail="Codex exited 1"),
                        Outcome("ran", {}, usage=SCRIPTED_USAGE)):
            with self.subTest(kind=outcome.kind):
                script = ScriptHandler()
                result, calls = self.run_script(lambda node, *args: outcome if node == "triage" else script(node, *args))
                self.assertEqual((result["halt"], len(calls)), ("invalid_output", 1))


class BuilderSessionTests(PublicationFixture):
    """The builder's answers over a real checkout: its repair turn on its own edits, and its one conversation (D5, item 2)."""

    def plan(self, state):
        return DispatchPlan.model_validate(state["dispatch_plan"])

    def builder(self, answers):
        """A builder that writes, and maybe commits, what each turn's answer says, and records what it was sent."""
        seen = []

        def edit(model, state, instruction):
            seen.append((model.name, self.plan(state), assignment("implement", state, instruction, None)))
            content, output, session, commit = answers[len(seen) - 1]
            if content is not None:
                (self.checkout / "items.txt").write_text(content)
            if commit:
                self.git("add", "items.txt")
                self.git("commit", "-m", "Worker commit")
            return Outcome("ran", output, usage=SCRIPTED_USAGE, session_id=session, totals=TOTALS)
        return edit, seen

    def test_an_invalid_builder_answer_is_repaired_on_the_checkout_it_left(self):
        for commit in (False, True):
            with self.subTest(commit=commit):
                self.reset_fixture()
                edit, seen = self.builder([("one\n", {}, SESSIONS[0], commit), (None, VALID, SESSIONS[1], False)])
                result = self.walk(model_edit=edit)
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                (model, plan, _), (repaired_model, repair, _) = seen
                self.assertEqual((plan.repair, resume_id(plan)), (None, None))
                self.assertEqual((repaired_model, resume_id(repair)), (model, SESSIONS[0]))
                self.assertTrue(repair.repair)
                self.assertEqual(self.git("show", f"{self.remote_head()}:items.txt"), "one")

    def test_a_repair_call_stopped_by_quota_never_resumes_the_invalid_answer(self):
        # Every qualified builder hits quota after the repair call, so the run halts worker_unavailable. Ready starts fresh.
        sol = load_config().nodes["implement"].models[0]
        quota = Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[4], totals=TOTALS)
        seen = []

        def edit(model, state, instruction):
            seen.append((model.name, self.plan(state)))
            if len(seen) == 1:
                self.edit()
                return Outcome("ran", {}, usage=SCRIPTED_USAGE, session_id=SESSIONS[0], totals=TOTALS)
            return quota

        paused = self.walk(model_edit=edit)
        self.assertEqual(paused["stop"], "worker_unavailable")
        self.assertEqual((seen[1][0], resume_id(seen[1][1])), (sol.name, SESSIONS[0]))
        self.assertTrue(all(plan.repair is None for _, plan in seen[2:]))

        # DP3 run 3: Ready makes the call again from its original instructions, and its invalid answer gets its own repair.
        halted = len(seen)

        def ready(model, state, instruction):
            seen.append((model.name, self.plan(state), assignment("implement", state, instruction, None)))
            if len(seen) == halted + 1:
                return Outcome("ran", {}, usage=SCRIPTED_USAGE, session_id=SESSIONS[6], totals=TOTALS)
            return Outcome("ran", VALID, usage=SCRIPTED_USAGE)

        result = self.walk(model_edit=ready)
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        (name, again, prompt), (repaired, repair, _) = seen[halted:]
        self.assertEqual((name, again.repair, resume_id(again)), (sol.name, None, None))
        self.assertIn("Delivery state:", prompt)
        self.assertEqual((repaired, resume_id(repair)), (sol.name, SESSIONS[6]))
        self.assertTrue(repair.repair)

    def test_a_resumed_builder_whose_repair_hit_quota_starts_fresh_after_ready(self):
        # A Codex builder resumes in place, so its invalid answer leaves the saved session and checkout as they were.
        # Ready after the repair's quota stop never resumes that session with the original instruction.
        sol, check = load_config().nodes["implement"].models[0], "grep -qx two items.txt"
        seen = []

        def edit(model, state, instruction):
            seen.append((model.name, self.plan(state), assignment("implement", state, instruction, None)))
            if len(seen) == 1:
                self.edit()
                return Outcome("ran", VALID, usage=SCRIPTED_USAGE, session_id=SESSIONS[0], totals=TOTALS)
            if len(seen) == 2:
                return Outcome("ran", {}, usage=SCRIPTED_USAGE, session_id=SESSIONS[0], totals=TOTALS)
            return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[4], totals=TOTALS)

        self.reset_fixture(check=check)
        paused = self.walk(model_edit=edit)
        self.assertEqual(paused["stop"], "worker_unavailable", paused.get("halt_detail"))
        self.assertEqual([(name, resume_id(plan), bool(plan.repair)) for name, plan, _ in seen[:3]],
                         [(sol.name, None, False), (sol.name, SESSIONS[0], False), (sol.name, SESSIONS[0], True)])

        def ready(model, state, instruction):
            seen.append((model.name, self.plan(state), assignment("implement", state, instruction, None)))
            (self.checkout / "items.txt").write_text("two\n")
            return Outcome("ran", VALID, usage=SCRIPTED_USAGE, session_id=SESSIONS[5], totals=TOTALS)

        result = self.walk(model_edit=ready)
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        name, plan, prompt = seen[-1]
        self.assertEqual((name, plan.repair, resume_id(plan)), (sol.name, None, None))
        self.assertIn("Delivery state:", prompt)

    def test_ready_resumes_the_builder_only_with_the_model_that_holds_its_conversation(self):
        # DP3 run 1: Sol stops on quota holding S1 and every builder is out. After Ready Sol is still out, so its call
        # resumes S1 and stops again, and the next model chosen starts a fresh session.
        sol, terra = load_config().nodes["implement"].models[:2]
        seen = []

        def edit(model, state, instruction):
            seen.append((model.name, resume_id(self.plan(state))))
            return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[1] if model == sol else None, totals=TOTALS)

        paused = self.walk(model_edit=edit)
        self.assertEqual((paused["stop"], paused["builder_session"]["model"]), ("worker_unavailable", sol.name))
        halted = len(seen)

        def ready(model, state, instruction):
            seen.append((model.name, resume_id(self.plan(state))))
            if model == sol:
                return Outcome("quota", usage=SCRIPTED_USAGE)
            self.edit()
            return Outcome("ran", VALID, usage=SCRIPTED_USAGE)

        result = self.walk(model_edit=ready)
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        self.assertEqual(seen[halted:], [(sol.name, SESSIONS[1]), (terra.name, None)])

    def test_a_failed_check_at_the_saved_fingerprint_resumes_the_builder(self):
        # DP6: an accepted answer then a failed check at a matching fingerprint resumes; a changed fingerprint starts fresh.
        for check, resumed in (("grep -qx two items.txt", SESSIONS[0]),
                               ("grep -qx two items.txt || { echo touched >> items.txt; false; }", None)):
            with self.subTest(resumed=resumed):
                self.reset_fixture(check=check)
                edit, seen = self.builder([("one\n", VALID, SESSIONS[0], False), ("two\n", VALID, SESSIONS[1], False)])
                result = self.walk(model_edit=edit)
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                self.assertEqual([resume_id(plan) for _, plan, _ in seen], [None, resumed])
                self.assertEqual(result["builder_session"]["session"]["id"], SESSIONS[1])

    def test_dp6_the_resumed_builder_call_is_charged_its_own_spend(self):
        # #446: the resumed call is a fork whose Claude report is the conversation's running total (the Opus capture).
        capture = json.loads((Path(__file__).with_name("fixtures") / "claude" / "resume-totals.json").read_text())
        model = ModelSpec.parse(capture["model"])
        turns = iter(((capture["parent"], SESSIONS[0], "one\n"), (capture["fork"], SESSIONS[1], "two\n")))

        def edit(model, state, instruction):
            report, session, content = next(turns)
            (self.checkout / "items.txt").write_text(content)
            text = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": session,
                               "structured_output": VALID, "total_cost_usd": report["total_cost_usd"], "modelUsage": report["modelUsage"]})
            call = Call("", "{}", True, Path("."), resume=self.plan(state).resume)
            return ClaudeAdapter(model).read(subprocess.CompletedProcess(["claude"], 0, text, ""), call)

        self.reset_fixture(check="grep -qx two items.txt")
        result = self.walk(model_edit=edit, family="claude")
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        built = [attempt for attempt in result["attempts"] if attempt["stage"] == "implement" and attempt["outcome"] == "ran"]
        self.assertEqual([attempt["model"] for attempt in built], [model.name, model.name])
        first, repair = (attempt["usage"] for attempt in built)
        self.assertEqual((repair["input"], repair["output"], repair["cache_read"], repair["cache_creation"]), (2, 53, 6085, 437))
        self.assertEqual((first["input"], first["output"], first["cache_read"], first["cache_creation"]), (1174, 69, 0, 6085))
        # The run's spend is the sum of the calls' own spends, the conversation's total once.
        self.assertAlmostEqual(float(claude_spend(built)), capture["fork"]["total_cost_usd"], places=12)

    def test_a_review_finding_resumes_the_conversation_after_commit_and_prepare(self):
        # Accepted edit, commit, review P1, prepare, edit: the repair resumes the session the latest answer returned.
        for publish_failed in (False, True):
            with self.subTest(publish_failed=publish_failed):
                self.reset_fixture()
                self.create_error = publish_failed
                edit, seen = self.builder([("one\n", VALID, SESSIONS[0], False), ("one\ntwo\n", VALID, SESSIONS[1], False)])
                result = self.walk(model_edit=edit, reviews=["changes", "approve"])
                if publish_failed:
                    self.assertEqual(result["halt"], "publish_failed")
                    self.create_error = False
                    result = self.walk(model_edit=edit, reviews=["changes", "approve"])
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                (first, repair) = seen
                self.assertEqual((first[0], resume_id(first[1])), (repair[0], None))
                self.assertEqual(resume_id(repair[1]), SESSIONS[0])
                # The saved session is Foreman's to resume and never reaches the worker's prompt.
                self.assertNotIn(SESSIONS[0], repair[2])

    def test_a_resume_adopts_only_the_edits_the_builder_returned(self):
        # IM1: a budget stop lands after the builder returned and before edit marked the attempt built, so the resume
        # meets the checkout before any builder call. Edits the halt does not own stop it with the tree preserved.
        spent = Usage(input_tokens=5000, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        for edited in (True, False):
            with self.subTest(edited=edited):
                self.reset_fixture()
                builds = []

                def edit(model, state, instruction):
                    builds.append(model.name)
                    self.edit()
                    return Outcome("ran", VALID, usage=spent)

                self.settings["FOREMAN_MAX_TOKENS"] = "1000"
                halted = self.walk(model_edit=edit)
                self.assertEqual(halted["halt"], "budget_exceeded")
                if edited:
                    (self.checkout / "items.txt").write_text("edited after the halt\n")
                del self.settings["FOREMAN_MAX_TOKENS"]
                result = self.walk(model_edit=edit)
                if edited:
                    self.assertEqual(result["halt"], "dirty_worktree", result.get("halt_detail"))
                    self.assertEqual(len(builds), 1)
                    self.assertEqual((self.checkout / "items.txt").read_text(), "edited after the halt\n")
                    self.assertEqual(self.created, [])
                else:
                    self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                    self.assertEqual(self.git("show", f"{self.remote_head()}:items.txt"), "one")

    def test_a_resume_that_calls_no_builder_is_refused_at_prepare_for_a_checkout_moved_off_the_attempt(self):
        # IM1: publish fails after the check passed, so the resume calls no builder. The same tree on another branch,
        # or at a head that does not descend from the attempt's, is refused at prepare before any later step runs.
        for moved in ("branch", "head"):
            with self.subTest(moved=moved):
                self.reset_fixture()
                edit, seen = self.builder([("one\n", VALID, None, False)])
                self.create_error = True
                halted = self.walk(model_edit=edit)
                self.assertEqual(halted["halt"], "publish_failed", halted.get("halt_detail"))
                self.create_error = False
                if moved == "branch":
                    self.git("switch", "-c", "elsewhere")
                else:
                    self.git("reset", "--soft", self.git("commit-tree", "HEAD^{tree}", "-m", "Elsewhere"))
                before = LiveWorkspace(self.checkout).inspect()
                result = self.walk(model_edit=edit)
                self.assertEqual(result["halt"], "dirty_worktree", result.get("halt_detail"))
                self.assertEqual(result["path"][result["path"].index("prepare"):], ["prepare"])
                self.assertEqual(len(seen), 1)
                self.assertEqual(LiveWorkspace(self.checkout).inspect(), before)
                self.assertEqual(self.created, [])

    def test_a_resumed_halt_after_the_builder_returned_continues_only_on_the_edits_it_returned(self):
        # Dispatch's check of the returned worktree fails once: a resumable git_failed after the call returned.
        # The halt owns the fingerprint the call returned and resume never recomputes it, so a later edit is dirty.
        original = DispatchRun.fingerprint
        for edited in (True, False):
            with self.subTest(edited=edited):
                self.reset_fixture()
                failed, builds = [], []

                def inspect(run, tree=None):
                    if run.edits and not failed and sys._getframe(1).f_code.co_name == "reconcile":
                        failed.append(True)
                        raise WorkspaceError("The index is locked.")
                    return original(run, tree)

                def edit(model, state, instruction):
                    builds.append(model.name)
                    self.edit()
                    return Outcome("ran", VALID, usage=SCRIPTED_USAGE)

                with patch.object(DispatchRun, "fingerprint", inspect):
                    halted = self.walk(model_edit=edit)
                self.assertEqual(halted["halt"], "git_failed")
                self.assertIn("The index is locked.", halted["halt_detail"])
                workspace = LiveWorkspace(self.checkout)
                owned = DispatchRun(edit, workspace, None, "", True, lambda: {}, lambda *args: None,
                                    StartSettings(human_login="fixture-owner", max_tokens=1, max_budget_usd="1")).fingerprint
                self.assertEqual(halted["work"].owned, owned())
                if edited:
                    (self.checkout / "items.txt").write_text("edited after the halt\n")
                result = self.walk(model_edit=edit)
                if edited:
                    self.assertEqual(result["halt"], "dirty_worktree")
                    self.assertEqual(len(builds), 1)
                    self.assertEqual((self.checkout / "items.txt").read_text(), "edited after the halt\n")
                else:
                    self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                    self.assertEqual(self.git("show", f"{self.remote_head()}:items.txt"), "one")

    def test_ready_after_worker_unavailable_refuses_edits_made_while_halted(self):
        # DP3: every builder hits quota after partial edits, so the run halts worker_unavailable with those edits owned.
        # Ready continues them only on the saved fingerprint; an edit made while halted is refused.
        for edited in (True, False):
            with self.subTest(edited=edited):
                self.reset_fixture()
                builds = []

                def edit(model, state, instruction):
                    builds.append(model.name)
                    if len(builds) == 1:
                        (self.checkout / "items.txt").write_text("partial\n")
                    return Outcome("quota", usage=SCRIPTED_USAGE)

                paused = self.walk(model_edit=edit)
                self.assertEqual(paused["stop"], "worker_unavailable", paused.get("halt_detail"))
                halted = len(builds)
                if edited:
                    (self.checkout / "items.txt").write_text("edited while halted\n")

                def ready(model, state, instruction):
                    builds.append(model.name)
                    self.edit()
                    return Outcome("ran", VALID, usage=SCRIPTED_USAGE)

                result = self.walk(model_edit=ready)
                if edited:
                    self.assertEqual(result["halt"], "dirty_worktree", result.get("halt_detail"))
                    self.assertEqual(len(builds), halted)
                    self.assertEqual((self.checkout / "items.txt").read_text(), "edited while halted\n")
                    self.assertEqual(self.created, [])
                else:
                    self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                    self.assertEqual(self.git("show", f"{self.remote_head()}:items.txt"), "one")

    def test_a_quota_session_is_never_resumed_on_a_tree_a_later_call_changed(self):
        # D5 and DP3: Sol stops on quota holding S1, then Terra edits the tree and the run halts. Whether Terra's invalid
        # answer gets a repair that stops on quota (worker_unavailable) or Terra exhausts its budget, Sol's first call
        # after Ready starts fresh on Terra's edits.
        sol = load_config().nodes["implement"].models[0]
        for ending, stop in (("repair_quota", "worker_unavailable"), ("budget_exhausted", "budget_exceeded")):
            with self.subTest(ending=ending):
                self.reset_fixture()
                seen = []

                def edit(model, state, instruction):
                    seen.append((model.name, resume_id(self.plan(state))))
                    if len(seen) == 1:
                        return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[1], totals=TOTALS)
                    if len(seen) > 2:
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    (self.checkout / "items.txt").write_text("written by the second model\n")
                    if ending == "budget_exhausted":
                        return Outcome("budget_exhausted", usage=SCRIPTED_USAGE, session_id=SESSIONS[2], totals=TOTALS, detail="cap")
                    return Outcome("ran", {}, usage=SCRIPTED_USAGE, session_id=SESSIONS[2], totals=TOTALS)

                halted = self.walk(model_edit=edit)
                self.assertEqual(halted["halt"], stop, halted.get("halt_detail"))
                calls = len(seen)

                def ready(model, state, instruction):
                    seen.append((model.name, resume_id(self.plan(state))))
                    self.edit()
                    return Outcome("ran", VALID, usage=SCRIPTED_USAGE)

                result = self.walk(model_edit=ready)
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                self.assertEqual(seen[calls], (sol.name, None))


class RealAdapterResumeTests(unittest.TestCase):
    """The real Claude and Codex adapters resume a read-only call's session, and a review repair needs no second diff read."""

    DIFF = "git --no-replace-objects diff --no-color a...b --"

    def review(self, model, process, repair):
        argv = []
        plan = {"models": [model.name], "instruction": "", "methods": [], "target": None, "resume": Session(id=SESSIONS[0], totals=TOTALS),
                "scope": {"command": self.DIFF, "inline": []}, "repair": repair}
        with TemporaryDirectory() as scratch:
            outcome = CliWorker(Path.cwd(), lambda args, *_: argv.append(args) or process, scratch_root=Path(scratch),
                                containers=lambda base: ("container-1", 1800))("review", model, {"dispatch_plan": plan}, "")
        return outcome, argv

    def test_a_review_repair_resumes_its_session(self):
        error = "findings.0.severity: Input should be 'P0', 'P1' or 'P2'"
        envelope = json.loads(fixture_process(["claude"], "success", 0).stdout)
        events = codex_events()
        events[-2]["item"]["text"] = json.dumps(APPROVE)
        for model, process in ((ModelSpec.parse("claude-opus-5-5-high"),
                                subprocess.CompletedProcess(["claude"], 0, json.dumps(envelope), "")),
                               (ModelSpec.parse("gpt-6.1-sol-high"), codex_process(events))):
            with self.subTest(family=model.family):
                outcome, argv = self.review(model, process, error)
                self.assertEqual(outcome.kind, "ran", outcome.detail)
                if model.family == "claude":
                    resume = argv[0].index("--resume")
                    self.assertEqual(argv[0][resume:resume + 3], ["--resume", SESSIONS[0], "--fork-session"])
                else:
                    self.assertEqual((argv[0][:3], argv[0][-2:]), (["codex", "exec", "resume"], [SESSIONS[0], error]))

    def test_a_read_only_call_resumes_its_saved_session(self):
        # Quota-interrupted calls resume for every procedure, so the adapters no longer gate resume on edit.
        with TemporaryDirectory() as folder:
            call = Call("finish", "{}", False, Path(folder), resume=Session(id=SESSIONS[0], totals=TOTALS))
            claude = ClaudeAdapter(ModelSpec.parse("claude-opus-5-5-high")).command(call)
            codex = CodexAdapter(ModelSpec.parse("gpt-6.1-sol-high")).command(call)
        resume = claude.index("--resume")
        self.assertEqual(claude[resume:resume + 3], ["--resume", SESSIONS[0], "--fork-session"])
        self.assertEqual((codex[:3], codex[-2:]), (["codex", "exec", "resume"], [SESSIONS[0], "finish"]))


class OtherProcedureSessionTests(unittest.TestCase):
    def test_other_procedures_start_fresh_when_ready_makes_the_interrupted_call_again(self):
        # Every triage model, or on a second run every verify.drive model, stops on quota with a session, so the run halts
        # worker_unavailable. Only the builder keeps a conversation (D5), so the call Ready makes starts fresh with the
        # first model in order (DP3 run 2).
        for stage in ("triage", "verify"):
            with self.subTest(stage=stage), TemporaryDirectory() as folder, skill_fixture() as (config, _), \
                    patch("foreman.runner.load_config", return_value=config):
                script, seen, ready, github = ScriptHandler(), [], [], FakeGitHub()
                workspace = FakeWorkspace(Path(folder))

                def handler(node, model, state, instruction):
                    if node == stage:
                        seen.append((model.name, resume_id(DispatchPlan.model_validate(state["dispatch_plan"]))))
                        if not ready:
                            return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[len(seen)], totals=TOTALS)
                    if node == "implement":
                        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
                    return replace(script(node, model, state, instruction), session_id=SESSIONS[9], totals=TOTALS)

                def walk_live():
                    return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=Path(folder) / "run.sqlite3",
                                             dispatcher=handler, run=LiveRun(github, workspace))

                self.assertEqual(walk_live()["halt"], "worker_unavailable")
                github.ready_again(8)
                ready.append(True)
                result = walk_live()
                models = load_config().nodes[stage].models
                self.assertEqual(seen, [(model.name, None) for model in [*models, models[0]]])
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))



class WorkerUnavailableTests(unittest.TestCase):
    def test_ready_makes_the_interrupted_call_again_and_charges_each_attempt_once(self):
        """DP3: every builder is out of quota, so the run stops worker_unavailable under Stopped and its status names
        each model tried. Ready while every model is still out stops the same way and charges nothing. Ready once one
        has capacity makes the interrupted call as one new attempt, and the earlier attempts are not charged again."""
        nothing = Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=0, output_tokens=0, total_cost_usd=0.0)
        with TemporaryDirectory() as folder, skill_fixture() as (config, _), patch("foreman.runner.load_config", return_value=config):
            script, github, workspace = ScriptHandler(), FakeGitHub(), FakeWorkspace(Path(folder))
            capacity = []

            def handler(node, model, state, instruction):
                if node == "implement":
                    if not capacity:
                        return Outcome("quota", usage=nothing)
                    workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
                return script(node, model, state, instruction)

            def start():
                return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=Path(folder) / "run.sqlite3",
                                         dispatcher=handler, run=LiveRun(github, workspace))

            models = [model.name for model in load_config().nodes["implement"].models]
            halted = start()
            self.assertEqual((halted["halt"], github.edited[8][-1:]), ("worker_unavailable", ["Stopped"]))
            status = github.status["Example/Engine", 8]
            self.assertIn("worker_unavailable", status)
            self.assertIn("Adding Ready makes the interrupted call again", status)
            for name in models:
                self.assertIn(f"{name}: quota.", status)

            github.ready_again(8)
            again = start()
            self.assertEqual(again["halt"], "worker_unavailable")
            self.assertEqual(again["attempts"][:len(halted["attempts"])], halted["attempts"])
            added = again["attempts"][len(halted["attempts"]):]
            self.assertEqual([(a["model"], a["outcome"]) for a in added], [(name, "quota") for name in models])
            self.assertEqual(work_tokens(added), 0)

            github.ready_again(8)
            capacity.append(True)
            result = start()
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        self.assertEqual(result["attempts"][:len(again["attempts"])], again["attempts"])
        built = [a for a in result["attempts"][len(again["attempts"]):] if a["stage"] == "implement"]
        self.assertEqual([(a["model"], a["outcome"]) for a in built], [(models[0], "ran")])
        ids = [a["attempt_id"] for a in result["attempts"]]
        self.assertEqual(len(ids), len(set(ids)))


class QuotaPauseUpgradeTests(unittest.TestCase):
    def test_a_run_an_older_release_paused_for_capacity_is_refused_on_ready(self):
        """Schema 25 paused a run quota_exhausted inside dispatch. This release has no such pause, so Ready on that
        checkpoint refuses it, naming the stored and expected versions and the issue thread, and leaves it unchanged."""
        with TemporaryDirectory() as folder, skill_fixture() as (config, _), patch("foreman.runner.load_config", return_value=config):
            script, github, workspace = ScriptHandler(), FakeGitHub(), FakeWorkspace(Path(folder))
            checkpoint = Path(folder) / "run.sqlite3"

            def handler(node, model, state, instruction):
                if node == "implement":
                    interrupt({"reason": "quota_exhausted", "stage": "implement", "attempts": [],
                               "detail": "No qualified worker available for implement."})
                return script(node, model, state, instruction)

            def start():
                return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=handler,
                                         run=LiveRun(github, workspace))

            with self.assertRaises(InvalidOutput):  # The pause is saved, and this release cannot read it.
                start()
            with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                saver.serde = checkpoint_serializer()
                saved = saver.get_tuple({"configurable": {"thread_id": "example/engine#8", "checkpoint_ns": ""}})
                saved.checkpoint["channel_values"]["schema_version"] = 25
                saver.put(saved.config, saved.checkpoint, saved.metadata, {})
            github.ready_again(8)
            before = checkpoint.read_bytes()
            with self.assertRaises(CheckpointVersionError) as raised:
                start()
            self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(str(raised.exception), refusal("example/engine#8", 25))
