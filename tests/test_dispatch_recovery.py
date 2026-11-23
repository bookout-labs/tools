"""Shared dispatch recovery across killed processes (DP2-DP4)."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from foreman.cli import run_exit_code
from tests.skill_fixture import skill_fixture


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
                    max_budget_usd=BudgetUsd(Decimal(limit)) if limit else None, run=LiveRun(FakeGitHub(), workspace))
            except ValueError as exc:
                print(json.dumps({"error": str(exc), "calls": calls}))
                sys.exit(17)
print(json.dumps({"result": result, "calls": calls}, default=lambda value: value.model_dump(mode="json")))
'''


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
                            self.assertEqual(run_exit_code(result), 1)
                            self.assertIn("usage_unknown", result["halt_detail"])
                            self.assertEqual([a["outcome"] for a in triage], ["quota", "interrupted"] if mode == "quota" else ["interrupted"])
                            self.assertIn("work_charge", triage[-1])
                            self.assertEqual(result["plan"]["triage"], "")
                        else:
                            # A durable result is adopted with its observed usage; only later work runs.
                            self.assertEqual(result["stop"], "verified", result.get("halt_detail"))
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
                        again = self.invoke(config, checkpoint, "recover", limit)
                        self.assertEqual(json.loads(again.stdout)["result"], result)
                        self.assertEqual(json.loads(again.stdout)["calls"], [])

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
