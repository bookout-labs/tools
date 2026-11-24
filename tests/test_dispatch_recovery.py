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

from foreman.adapters import Call, Outcome, Usage
from foreman.adapters.codex import CodexAdapter
from foreman.budget import work_tokens
from foreman.cli import run_exit_code
from foreman.config import load_config
from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun
from foreman.dispatch import DispatchRun, InvalidOutput
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.github import LiveForge
from foreman.runner import issue_lock, run_live_delivery
from foreman.stage_output import DispatchPlan, TriageOutput, parse_output
from foreman.worker import CliWorker, assignment, run_worker_command
from foreman.workspace import LiveWorkspace, WorktreeState
from tests.skill_fixture import skill_fixture, verify_checkout
from test_delivery import EMPTY_SLICE, walk
from test_publication import REPO, PublicationFixture
from test_runner import FakeGitHub, FakeWorkspace


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
            try:
                result = run_live_delivery(repo="Example/Engine", issue=8, check="exit 0", checkpoint=Path(checkpoint),
                    dispatcher=handler, presentation=RunPresentation(),
                    max_budget_usd=BudgetUsd(Decimal(limit)) if limit else None, run=LiveRun(FakeGitHub(), workspace, human_login="fixture-owner"))
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
        # DP3 and D10: a Codex quota stop after edits is charged what its rollout logged, the next models run,
        # and after the quota wake the same model resumes its saved session on the saved fingerprint.
        session = "01a1ff05-7685-7282-b515-828a0b63ee12"
        sol, terra = load_config().nodes["implement"].models[:2]
        home = Path(self.fixture.enter_context(TemporaryDirectory()))
        self.fixture.enter_context(patch.dict("os.environ", {"CODEX_HOME": str(home)}))
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
            CliWorker(self.checkout, run=lambda args, cwd, env: commands.append(args) or subprocess.CompletedProcess(args, 0, "", ""))(
                "implement", model, state, instruction)
            self.edit()

        with patch.object(DispatchRun, "launch", observed):
            paused = self.walk(model_edit=stop)
            self.assertEqual(paused["stop"], "quota_exhausted")
            implement = [a for a in paused["attempts"] if a["stage"] == "implement"]
            self.assertEqual([a["model"] for a in implement[:2]], [sol.name, terra.name])
            self.assertEqual((implement[0]["outcome"], work_tokens([implement[0]])), ("quota", 1030))
            self.assertNotIn("work_charge", implement[0])
            (first, *fallbacks) = launches
            self.assertEqual(first[1], {})
            saved = fallbacks[0][2]
            self.assertNotEqual(saved, first[3])
            self.assertEqual([entry[1:] for entry in fallbacks], [({sol.name: session}, saved, saved)] * len(fallbacks))
            resumed = self.walk(model_edit=resume)
        self.assertEqual(resumed["stop"], "merged")
        self.assertEqual(launches[len(fallbacks) + 1], (sol.name, {sol.name: session}, saved, saved))
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
                        self.assertEqual(result["run"]["max_budget_usd"], limit or None)
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
                calls.append((node, model.name, call.prompt, call.resume_session))
                return ["fixture"]

            def read(self, process, call):
                return outcome

        with patch.dict("foreman.worker.ADAPTERS", {model.family: lambda _: Adapter()}):
            return CliWorker(Path.cwd(), lambda *args: subprocess.CompletedProcess([], 0), scratch_root=Path(scratch.name))(
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
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[len(triage)])
            return replace(outcome, output={}, session_id=SESSIONS[0]) if triage == ["triage"] else outcome

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
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[0])
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
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[1])
            if node == "verify" and not verify:
                verify.append(node)
                return replace(outcome, output={**outcome.output, "tested_head": "0" * 40}, session_id=SESSIONS[0])
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
            return replace(script(node, model, state, instruction), session_id=SESSIONS[0])

        result, calls = self.run_script(answer, slice=None)
        spec = [call for call in calls if call[0] == "spec"]
        self.assertGreater(len(spec), 1)
        self.assertTrue(all("Delivery state:" in prompt and session is None for _, _, prompt, session in spec))
        self.assertTrue(result["plan"].rejected_revisions)

    def test_a_repair_call_on_quota_hands_the_original_instruction_to_a_fresh_session(self):
        script = ScriptHandler()
        triage = []

        def answer(node, model, state, instruction):
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[len(triage)])
            if node != "triage":
                return outcome
            triage.append(model.name)
            if len(triage) == 1:
                return replace(outcome, output={})
            return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[5]) if len(triage) == 2 else outcome

        result, calls = self.run_script(answer)
        self.assertEqual(result["stop"], "merge_pending", result.get("halt_detail"))
        first, repair, fallback = [call for call in calls if call[0] == "triage"]
        self.assertEqual((repair[1], repair[3]), (first[1], SESSIONS[0]))
        self.assertNotEqual(fallback[1], first[1])
        self.assertEqual((fallback[2], fallback[3]), (first[2], None))

    def test_the_budget_an_invalid_answer_used_up_reserves_no_repair(self):
        script = ScriptHandler()

        def answer(node, model, state, instruction):
            outcome = replace(script(node, model, state, instruction), session_id=SESSIONS[0])
            if node == "triage":
                return replace(outcome, output={}, usage=Usage(input_tokens=DEFAULT_TOKEN_CAP, output_tokens=1,
                                                              cache_read_input_tokens=0, cache_creation_input_tokens=0))
            return outcome

        result, calls = self.run_script(answer)
        self.assertEqual(result["halt"], "budget_exceeded")
        self.assertEqual(len(calls), 1)

    def test_only_an_answer_gets_a_repair_turn(self):
        # A CLI failure carries no answer, even with a session ID, and an invalid answer without one is not an answer.
        for outcome in (Outcome("failed", session_id=SESSIONS[0], usage=SCRIPTED_USAGE, detail="Codex exited 1"),
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
            seen.append((model.name, self.plan(state), assignment("implement", model, state, instruction, None)))
            content, output, session, commit = answers[len(seen) - 1]
            if content is not None:
                (self.checkout / "items.txt").write_text(content)
            if commit:
                self.git("add", "items.txt")
                self.git("commit", "-m", "Worker commit")
            return Outcome("ran", output, usage=SCRIPTED_USAGE, session_id=session)
        return edit, seen

    def test_an_invalid_builder_answer_is_repaired_on_the_checkout_it_left(self):
        for commit in (False, True):
            with self.subTest(commit=commit):
                self.reset_fixture()
                edit, seen = self.builder([("one\n", {}, SESSIONS[0], commit), (None, VALID, SESSIONS[1], False)])
                result = self.walk(model_edit=edit)
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                (model, plan, _), (repaired_model, repair, _) = seen
                self.assertEqual((plan.repair, plan.resume_session), (None, None))
                self.assertEqual((repaired_model, repair.resume_session), (model, SESSIONS[0]))
                self.assertTrue(repair.repair)
                self.assertEqual(self.git("show", f"{self.remote_head()}:items.txt"), "one")

    def test_a_repair_call_stopped_by_quota_never_resumes_the_invalid_answer(self):
        # Every qualified builder hits quota after the repair call, so the run waits. The wake starts fresh.
        sol = load_config().nodes["implement"].models[0]
        quota = Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[4])
        seen = []

        def edit(model, state, instruction):
            seen.append((model.name, self.plan(state)))
            if len(seen) == 1:
                self.edit()
                return Outcome("ran", {}, usage=SCRIPTED_USAGE, session_id=SESSIONS[0])
            return quota

        paused = self.walk(model_edit=edit)
        self.assertEqual(paused["stop"], "quota_exhausted")
        self.assertEqual((seen[1][0], seen[1][1].resume_session), (sol.name, SESSIONS[0]))
        self.assertTrue(all(plan.repair is None for _, plan in seen[2:]))

        def wake(model, state, instruction):
            seen.append((model.name, self.plan(state)))
            return Outcome("ran", VALID, usage=SCRIPTED_USAGE)

        result = self.walk(model_edit=wake)
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        self.assertEqual((seen[-1][0], seen[-1][1].repair, seen[-1][1].resume_session), (sol.name, None, None))

    def test_a_failed_check_at_the_saved_fingerprint_resumes_the_builder(self):
        # DP6: an accepted answer then a failed check at a matching fingerprint resumes; a changed fingerprint starts fresh.
        for check, resumed in (("grep -qx two items.txt", SESSIONS[0]),
                               ("grep -qx two items.txt || { echo touched >> items.txt; false; }", None)):
            with self.subTest(resumed=resumed):
                self.reset_fixture()
                edit, seen = self.builder([("one\n", VALID, SESSIONS[0], False), ("two\n", VALID, SESSIONS[1], False)])
                result = self.walk(model_edit=edit, check=check)
                self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
                self.assertEqual([plan.resume_session for _, plan, _ in seen], [None, resumed])
                self.assertEqual(result["builder_session"]["session"], SESSIONS[1])

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
                self.assertEqual((first[0], first[1].resume_session), (repair[0], None))
                self.assertEqual(repair[1].resume_session, SESSIONS[0])
                # The saved session is Foreman's to resume and never reaches the worker's prompt.
                self.assertNotIn(SESSIONS[0], repair[2])

    def test_a_budget_stop_after_the_builder_returned_keeps_its_edits_owned(self):
        # Fable's T19 ruling: the halted checkpoint owns the fingerprint dispatch returned, so a later edit is still dirty.
        workspace = LiveWorkspace(self.checkout)
        script = ScriptHandler()
        spent = Usage(input_tokens=5000, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)

        def handler(node, model, state, instruction):
            if node == "implement":
                self.edit()
                return Outcome("ran", VALID, usage=spent)
            return script(node, model, state, instruction)

        result = run_live_delivery(repo=REPO, issue=8, check="test -f items.txt", checkpoint=self.checkpoint, max_tokens=1000,
                                   run=LiveRun(LiveForge(), workspace, human_login="fixture-owner"), dispatcher=handler)
        self.assertEqual(result["halt"], "budget_exceeded")
        fingerprint = DispatchRun(handler, workspace, None, "", True, lambda: {}, lambda *args: None).fingerprint
        self.assertEqual(result["work"].owned, fingerprint())
        (self.checkout / "items.txt").write_text("edited after the halt\n")
        self.assertNotEqual(result["work"].owned, fingerprint())


class OtherProcedureSessionTests(unittest.TestCase):
    def test_other_procedures_resume_only_their_repair_or_quota_interrupted_call(self):
        # Triage resumes its quota-interrupted session after the wait, then every later call starts fresh.
        with TemporaryDirectory() as folder, skill_fixture() as (config, _), patch("foreman.runner.load_config", return_value=config):
            script, seen, wakes = ScriptHandler(), [], []
            workspace = FakeWorkspace(Path(folder))

            def handler(node, model, state, instruction):
                if node == "triage":
                    seen.append((model.name, DispatchPlan.model_validate(state["dispatch_plan"]).resume_session))
                    if not wakes:
                        return Outcome("quota", usage=SCRIPTED_USAGE, session_id=SESSIONS[len(seen)])
                if node == "implement":
                    workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
                return replace(script(node, model, state, instruction), session_id=SESSIONS[9])

            def walk_live():
                return run_live_delivery(repo="Example/Engine", issue=8, check="exit 0", checkpoint=Path(folder) / "run.sqlite3",
                                         dispatcher=handler, run=LiveRun(FakeGitHub(), workspace, human_login="fixture-owner"))

            self.assertEqual(walk_live()["stop"], "quota_exhausted")
            wakes.append(True)
            result = walk_live()
        models = load_config().nodes["triage"].models
        self.assertEqual(seen, [(model.name, None) for model in models] + [(models[0].name, SESSIONS[1])])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
