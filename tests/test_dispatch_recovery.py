import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from foreman.state import checkpoint_serializer
from foreman.cli import run_exit_code
from tests.skill_fixture import skill_fixture


SCRIPT = '''
import contextlib, json, os, sys
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from foreman.adapters import Outcome, Usage
from foreman.config import PstackPin, load_config
from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler
from foreman.usd import BudgetUsd
from foreman.runner import RunPresentation, run_live_delivery
from test_runner import FakeGitHub, FakeWorkspace
mode, checkpoint, raw_pin, limit = sys.argv[1:]
script = ScriptHandler()
calls = 0
def handler(node, model, state, instruction):
    global calls
    calls += 1
    if mode == "recover":
        raise AssertionError("recovery dispatched a model")
    if mode == "entry":
        os._exit(19)
    if mode == "quota-uncommitted":
        return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=9, output_tokens=1, total_cost_usd=0.1))
    if mode == "quota":
        if calls == 1:
            return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=9, output_tokens=1, total_cost_usd=0.1))
        os._exit(19)
    return script(node, model, state, instruction)
def crash(*args, **kwargs):
    os._exit(19)
pin = PstackPin.model_validate_json(raw_pin)
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin)):
    with patch("foreman.delivery.Attempt.record", side_effect=crash) if mode == "quota-uncommitted" else contextlib.nullcontext():
        with patch("foreman.delivery.stage_update", side_effect=crash) if mode == "success" else contextlib.nullcontext():
            result = run_live_delivery(repo="Example/Engine", issue=8, check="exit 0", checkpoint=Path(checkpoint),
                dispatcher=handler, presentation=RunPresentation(),
                max_budget_usd=BudgetUsd(Decimal(limit)) if limit else None, run=LiveRun(FakeGitHub(), FakeWorkspace(Path.cwd())))
print(json.dumps(result, default=lambda value: value.model_dump(mode="json")))
'''


class DispatchRecoveryTests(unittest.TestCase):
    def test_fresh_process_crash_after_success_or_quota_never_replays(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder:
            for mode, limit in ((mode, limit) for mode in ("entry", "success", "quota", "quota-uncommitted")
                                for limit in ("", "1")):
                with self.subTest(mode=mode, limit=limit):
                    checkpoint = Path(folder) / (mode + limit + ".sqlite")
                    bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
                    def invoke(phase):
                        return subprocess.run([sys.executable, "-c", bootstrap + SCRIPT, phase, str(checkpoint),
                                               config.pstack.model_dump_json(), limit], capture_output=True, text=True)
                    crashed = invoke(mode)
                    self.assertEqual(crashed.returncode, 19, crashed.stderr)
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saver.serde = checkpoint_serializer()
                        saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})["channel_values"]
                        self.assertIsNotNone(saved["pending_dispatch"])
                        if mode == "quota":
                            self.assertEqual(saved["attempts"][0]["outcome"], "quota")
                            self.assertEqual(saved["attempts"][0]["usage"]["input"], 9)
                    resumed = invoke("recover")
                    self.assertEqual(resumed.returncode, 0, resumed.stderr)
                    result = json.loads(resumed.stdout)
                    self.assertEqual(result["stop"], "usage_unknown")
                    self.assertEqual(run_exit_code(result), 1)
                    self.assertEqual(result["run"]["max_budget_usd"], limit or None)
                    self.assertEqual(result["attempts"][-1]["outcome"], "interrupted")
                    self.assertIsNone(result["pending_dispatch"])
                    self.assertEqual(result["plan"]["triage"], "")
                    self.assertEqual(len(result["attempts"]), 2 if mode == "quota" else 1)
                    again = invoke("recover")
                    self.assertEqual(json.loads(again.stdout), result)
