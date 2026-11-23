"""Shared owner contracts across persistence, transport, and suspension."""

import tests
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel
from typing import get_type_hints

from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.outcomes import Halt
from foreman.runner import run_live_delivery
from foreman.state import DeliveryState, PlanState, ReviewState, SCHEMA_VERSION, CheckpointVersionError, checkpoint_serializer, require_schema_version
from foreman.config import ModelSpec, load_config
from foreman.worker import CliWorker
from owner_contract import COUNTERS, OWNERS, assert_channels, historical_channels
from test_runner import worker_reply


class CheckpointBoundaryTests(unittest.TestCase):
    def test_old_checkpoint_is_refused_before_validation_or_execution(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "old.sqlite"
            shutil.copyfile(Path(__file__).parent / "fixtures/state/before-move.sqlite", checkpoint)
            for version in (None, *range(SCHEMA_VERSION), SCHEMA_VERSION + 1, True, "1", str(SCHEMA_VERSION)):
                with self.subTest(version=version):
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saver.serde = checkpoint_serializer()
                        saved = saver.get_tuple({"configurable": {"thread_id": "before-move"}})
                        values = saved.checkpoint["channel_values"]
                        values.clear()
                        values.update(historical_channels(version))
                        saver.put({"configurable": {"thread_id": "a/b#8", "checkpoint_ns": ""}}, saved.checkpoint, saved.metadata, {})
                    before = checkpoint.read_bytes()
                    with patch("foreman.runner.compile_delivery") as compile_graph, \
                         patch("foreman.runner.checkpoint_state") as validate, \
                         patch("foreman.runner.load_config") as config, \
                         patch("foreman.runner.require_schema_version", wraps=require_schema_version) as refuse:
                        with self.assertRaisesRegex(CheckpointVersionError, "schema_version.*Start a new run") as raised:
                            run_live_delivery(repo="a/b", issue=8, check="", checkpoint=checkpoint,
                                              dispatcher=ScriptHandler(), run=LiveRun(DryRun(), DryRun()))
                        self.assertIn(str(checkpoint), str(raised.exception))
                        self.assertIn("a/b#8", str(raised.exception))
                        self.assertIn("XDG_STATE_HOME", str(raised.exception))
                        if version is not None and (type(version) is not int or version > SCHEMA_VERSION):
                            self.assertIn("written by an incompatible release", str(raised.exception))
                        if version is None:
                            self.assertIn("schema_version missing", str(raised.exception))
                        refuse.assert_called_once()
                        self.assertEqual(refuse.call_args.args[0], values)
                        stored_version = refuse.call_args.args[0].get("schema_version")
                        self.assertEqual(stored_version, version)
                        self.assertIs(type(stored_version), type(version))
                        compile_graph.assert_not_called()
                        validate.assert_not_called()
                        config.assert_not_called()
                    self.assertEqual(checkpoint.read_bytes(), before)

    def test_full_owner_channels_round_trip(self):
        model_channels = {name for name, hint in get_type_hints(DeliveryState).items()
                          if isinstance(hint, type) and issubclass(hint, BaseModel)}
        self.assertEqual({owner.name for owner in OWNERS}, model_channels)
        serde = checkpoint_serializer()
        values = {owner.name: owner.value for owner in OWNERS}
        from foreman.stage_output import HumanPrompt
        with self.assertNoLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            for owner in OWNERS:
                with self.subTest(owner=owner.name):
                    defaults = type(owner.value)()
                    for field in type(owner.value).model_fields:
                        self.assertNotEqual(getattr(owner.value, field), getattr(defaults, field), field)
                    self.assertEqual(serde.loads_typed(serde.dumps_typed(owner.value)), owner.value)
            self.assertIs(serde.loads_typed(serde.dumps_typed(Halt.budget_exceeded)), Halt.budget_exceeded)
        with self.assertLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            self.assertNotIsInstance(serde.loads_typed(serde.dumps_typed(HumanPrompt(stage="triage", question="Which?"))), HumanPrompt)
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "owners.sqlite")
            config = {"configurable": {"thread_id": "owners"}}
            with SqliteSaver.from_conn_string(path) as saver:
                saver.serde = serde
                graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(DryRun(), DryRun()), checkpointer=saver)
                graph.update_state(config, {**values, "schema_version": SCHEMA_VERSION}, as_node="research")
            with SqliteSaver.from_conn_string(path) as saver:
                saver.serde = serde
                restored = saver.get(config)["channel_values"]
                assert_channels(self, restored, values)

            def run(args, cwd, env):
                prompt = json.loads(args[-1].split("Delivery state:\n")[1])
                for owner in OWNERS:
                    with self.subTest(owner=owner.name):
                        self.assertEqual(prompt[owner.name], owner.value.model_dump(mode="json"))
                return worker_reply(args, {"triage": "supplied_slice"})

            result = CliWorker(Path(directory), run)("triage", ModelSpec.parse("gpt-6.1-sol-high"), restored, "Read.")
            self.assertEqual(result.kind, "ran")

    def test_every_pause_survives_a_fresh_process(self):
        script = '''
import tests
import json
from pathlib import Path
import sys
from langgraph.checkpoint.sqlite import SqliteSaver
from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.runner import checkpoint_state, snapshot_result
from foreman.state import checkpoint_serializer
from owner_contract import OWNERS

path, phase, state_json, predecessor = sys.argv[1:]
config = {"configurable": {"thread_id": "pause"}}
script = ScriptHandler(triage="needs_human", spec_gate="needs_human")

def ask(node, model, state, instruction):
    # Every worker asks, so each stage pauses at its own gate.
    result = script(node, model, state, instruction)
    if node == "review":
        result.output["human_question"] = "Which?"
    return result

with SqliteSaver.from_conn_string(path) as saver:
    saver.serde = checkpoint_serializer()
    # The checkout sits at the owners' checked head on their branch.
    graph = compile_delivery(ask, load_config(),
                             run=LiveRun(DryRun(), DryRun(head="b" * 40, branch="8-change")), checkpointer=saver)
    if phase == "write":
        values = json.loads(state_json)
        for owner in OWNERS:
            values[owner.name] = type(owner.value).model_validate(values[owner.name])
        graph.update_state(config, values, as_node=predecessor)
        if predecessor != "merge":
            graph.invoke(None, config, durability="sync")
    saved = graph.get_state(config, subgraphs=True)
    assert checkpoint_state(saved.values) is not None
    for owner in OWNERS:
        assert isinstance(saved.values[owner.name], type(owner.value))
        assert saved.values[owner.name].model_dump(mode="json") == json.loads(state_json)[owner.name], owner.name
    print(json.dumps({"values": saved.values, "next": saved.next,
                      "interrupts": [item.value for item in saved.interrupts],
                      "result": snapshot_result(saved)}, sort_keys=True, default=lambda value: value.model_dump(mode="json")))
'''
        cases = [
            ("triage", "__start__", {}),
            ("spec", "triage", {}),
            ("review", "implement", {}),
        ]
        # The shared quota wait is an interrupt inside dispatch; test_review_availability resumes it in fresh processes.
        cases += [(code.value, "merge", {"halt": code.value, "stop": code.value}) for code in Halt]
        env = {**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true",
               "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")])}
        with TemporaryDirectory() as directory:
            for name, predecessor, extra in cases:
                with self.subTest(pause=name):
                    plan = next(owner.value for owner in OWNERS if owner.name == "plan")
                    state = {"schema_version": SCHEMA_VERSION, **{owner.name: owner.value for owner in OWNERS}, "path": [name], "human_question": "Which?", "halt_detail": "Waiting.", "attempts": [{"stage": "triage", "model": "gpt",
                                 "commit": "a" * 40, "methods": [], "outcome": "ran", "usage": {
                                     "input": 1, "output": 2, "cache_read": 0, "cache_creation": 0,
                                     "cost_usd": None}}], **extra}
                    if name == "spec":
                        plan = PlanState(triage="plan", issue_kind="parent", work_kind="feature")
                    elif name == "triage":
                        plan = plan.model_copy(update={"triage": ""})
                    elif name == "review":
                        # The checked head has no review yet, so the reviewer runs and asks.
                        state["review"] = ReviewState()
                        state["models_used"] = {"implement": "gpt-6.1-sol-high"}
                    state["plan"] = plan
                    checkpoint = str(Path(directory) / f"{name}.sqlite")
                    outputs = []
                    for phase in ("write", "read"):
                        process = subprocess.run([sys.executable, "-c", script, checkpoint, phase,
                                                  json.dumps(state, default=lambda value: value.model_dump(mode="json")), predecessor],
                                                 text=True, capture_output=True, env=env, check=False)
                        self.assertEqual(process.returncode, 0, process.stderr)
                        self.assertEqual(process.stderr, "")
                        outputs.append(json.loads(process.stdout))
                    self.assertEqual(*outputs)
                    if predecessor != "merge":
                        self.assertEqual(len(outputs[1]["interrupts"]), 1, outputs[1]["result"].get("halt_detail"))
                        payload = outputs[1]["interrupts"][0]
                        self.assertEqual(payload.get("stage", payload.get("reason")),
                                         "quota_exhausted" if name == "quota" else name)


    def test_caps_survive_fresh_process_resume(self):
        from skill_fixture import fixture_pin
        script = r'''
import tests
import json
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch
from foreman.adapters import Outcome
from foreman.config import SkillPin, load_config
from foreman.delivery import LiveRun
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE
from foreman.runner import run_live_delivery
from foreman.workspace import WorktreeState
from owner_contract import COUNTERS
from test_runner import FakeGitHub, FakeWorkspace, BRANCH, SLICE, slice_body

root, pin, stage, phase = sys.argv[1:]
root = Path(root)
_, owner, field, stop = next(row for row in COUNTERS if row[0] == stage)
workspace = FakeWorkspace(root)
workspace.current = WorktreeState(workspace.current.head, True, BRANCH)
forge = FakeGitHub(["factory"] if stage == "spec" else ["slice"])
handler = ScriptHandler(triage="plan" if stage == "spec" else "supplied_slice", reviews=["changes"] * 3, slices=[SLICE])
calls = []
def count(state):
    value = getattr(state[owner], field)
    return len(value) if stage == "review" else value

def worker(node, model, state, instruction):
    if phase == "pause" and count(state) >= 1:
        return Outcome("quota", usage=SCRIPTED_USAGE)
    calls.append(node)
    if node == "implement":
        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
    result = handler(node, model, state, instruction)
    if stage == "spec" and node == "spec":
        result.output["slices"] = [result.output["slices"][0]] * 2
        result.output["spec"] += f" {count(state)}"
    return result

config = replace(load_config(), pstack=SkillPin.model_validate_json(pin), matt_skills=SkillPin.model_validate_json(pin).model_copy(update={"directory": "skills"}))
with patch("foreman.runner.load_config", return_value=config), patch.object(
    workspace, "run", side_effect=lambda command, **kwargs: 1 if stage == "check" and command == "check" else 0,
):
    result = run_live_delivery(repo="Example/Engine", issue=8, check="check", preflight="env",
        checkpoint=root / "run.sqlite", run=LiveRun(forge, workspace), dispatcher=worker)
assert count(result) == (1 if phase == "pause" else config.max_review_rounds), result
assert result["stop"] == ("quota_exhausted" if phase == "pause" else stop), result
assert not any(key in result for key in ("review_rounds", "verify_failures", "spec_failures"))
print(json.dumps({"count": count(result), "calls": calls, "path": result["path"]}))
'''
        env = {**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true",
               "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")])}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pin = fixture_pin(root / "skills")
            for stage, _, _, _ in COUNTERS:
                with self.subTest(cap=stage):
                    workspace = root / stage
                    workspace.mkdir()
                    results = []
                    for phase in ("pause", "resume", "terminal"):
                        process = subprocess.run([sys.executable, "-c", script, str(workspace),
                                                  pin.model_dump_json(), stage, phase],
                                                 capture_output=True, text=True, env=env)
                        self.assertEqual(process.returncode, 0, process.stderr)
                        results.append(json.loads(process.stdout))
                    self.assertEqual(results[0]["count"], 1)
                    self.assertEqual(results[1]["count"], load_config().max_review_rounds)
                    self.assertEqual(results[2]["calls"], [])
                    self.assertEqual(results[1]["path"], results[2]["path"])
