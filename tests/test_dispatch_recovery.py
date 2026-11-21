import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver
from tests.skill_fixture import skill_fixture


SCRIPT = '''
import contextlib, json, os, sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from foreman.adapters import Outcome, Usage
from foreman.config import PstackPin, load_config
from foreman.delivery import LiveRun
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.runner import RunPresentation, run_live_delivery
from test_runner import FakeGitHub, FakeWorkspace
mode, checkpoint, raw_pin = sys.argv[1:]
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
        return Outcome("quota", usage=Usage(input_tokens=9, output_tokens=1))
    if mode == "quota":
        if calls == 1:
            return Outcome("quota", usage=Usage(input_tokens=9, output_tokens=1))
        os._exit(19)
    return script(node, model, state, instruction)
def crash(*args, **kwargs):
    os._exit(19)
original_run = FailoverDispatcher.run
def uncommitted(*args, **kwargs):
    original_run(*args, **kwargs)
    os._exit(19)
pin = PstackPin.model_validate_json(raw_pin)
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin)):
    with patch("foreman.dispatch.FailoverDispatcher.run", new=uncommitted) if mode == "quota-uncommitted" else contextlib.nullcontext():
        with patch("foreman.delivery.stage_update", side_effect=crash) if mode == "success" else contextlib.nullcontext():
            result = run_live_delivery(repo="Example/Engine", issue=8, check="exit 0", checkpoint=Path(checkpoint),
                dispatcher=FailoverDispatcher(handler), presentation=RunPresentation(), run=LiveRun(FakeGitHub(), FakeWorkspace(Path.cwd())))
print(json.dumps(result))
'''


class DispatchRecoveryTests(unittest.TestCase):
    def test_fresh_process_crash_after_success_or_quota_never_replays(self):
        with skill_fixture() as (config, _), TemporaryDirectory() as folder:
            for mode in ("entry", "success", "quota", "quota-uncommitted"):
                with self.subTest(mode=mode):
                    checkpoint = Path(folder) / (mode + ".sqlite")
                    bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
                    def invoke(phase):
                        return subprocess.run([sys.executable, "-c", bootstrap + SCRIPT, phase, str(checkpoint),
                                               config.pstack.model_dump_json()], capture_output=True, text=True)
                    crashed = invoke(mode)
                    self.assertEqual(crashed.returncode, 19, crashed.stderr)
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})["channel_values"]
                        self.assertIsNotNone(saved["pending_dispatch"])
                        if mode == "quota":
                            self.assertEqual(saved["attempts"][0]["outcome"], "quota")
                            self.assertEqual(saved["attempts"][0]["usage"]["input"], 9)
                    resumed = invoke("recover")
                    self.assertEqual(resumed.returncode, 0, resumed.stderr)
                    result = json.loads(resumed.stdout)
                    self.assertEqual(result["stop"], "usage_unknown")
                    self.assertIsNone(result["pending_dispatch"])
                    self.assertNotIn("triage", result)
                    self.assertEqual(len(result["attempts"]), 2 if mode == "quota" else 1)
                    again = invoke("recover")
                    self.assertEqual(json.loads(again.stdout), result)
