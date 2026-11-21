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
from foreman.state import DeliveryState, SCHEMA_VERSION, CheckpointVersionError, checkpoint_serializer, require_schema_version
from foreman.config import ModelSpec, load_config
from foreman.worker import CliWorker
from owner_contract import OWNERS, assert_channels, historical_channels
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
        from foreman.delivery import HumanPrompt
        with self.assertNoLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            for owner in OWNERS:
                with self.subTest(owner=owner.name):
                    defaults = type(owner.value)()
                    for field in type(owner.value).model_fields:
                        self.assertNotEqual(getattr(owner.value, field), getattr(defaults, field), field)
                    self.assertEqual(serde.loads_typed(serde.dumps_typed(owner.value)), owner.value)
            self.assertIs(serde.loads_typed(serde.dumps_typed(Halt.quota_exhausted)), Halt.quota_exhausted)
        with self.assertLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            self.assertNotIsInstance(serde.loads_typed(serde.dumps_typed(HumanPrompt(stage="triage", question="Which?"))), HumanPrompt)
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "owners.sqlite")
            config = {"configurable": {"thread_id": "owners"}}
            with SqliteSaver.from_conn_string(path) as saver:
                saver.serde = serde
                graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(DryRun(), DryRun()), checkpointer=saver)
                graph.update_state(config, {**values, "schema_version": SCHEMA_VERSION}, as_node="done")
            with SqliteSaver.from_conn_string(path) as saver:
                saver.serde = serde
                restored = saver.get(config)["channel_values"]
                assert_channels(self, restored, values)

            def run(args, cwd, env):
                prompt = json.loads(args[-1].split("Delivery state:\n")[1])
                for owner in OWNERS:
                    with self.subTest(owner=owner.name):
                        self.assertEqual(prompt[owner.name], owner.value.model_dump(mode="json"))
                return worker_reply(args, {"triage": "implement"})

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
with SqliteSaver.from_conn_string(path) as saver:
    saver.serde = checkpoint_serializer()
    graph = compile_delivery(ScriptHandler(), load_config(),
                             run=LiveRun(DryRun(), DryRun()), checkpointer=saver)
    if phase == "write":
        values = json.loads(state_json)
        for owner in OWNERS:
            values[owner.name] = type(owner.value).model_validate(values[owner.name])
        graph.update_state(config, values, as_node=predecessor)
        if predecessor != "done":
            graph.invoke(None, config, durability="sync")
    saved = graph.get_state(config)
    assert checkpoint_state(saved.values) is not None
    for owner in OWNERS:
        assert isinstance(saved.values[owner.name], type(owner.value))
        assert saved.values[owner.name].model_dump(mode="json") == json.loads(state_json)[owner.name]
    print(json.dumps({"values": saved.values, "next": saved.next,
                      "interrupts": [item.value for item in saved.interrupts],
                      "result": snapshot_result(saved)}, sort_keys=True, default=lambda value: value.model_dump(mode="json")))
'''
        size = {"head": "a" * 40, "merge_base": "b" * 40, "lines": 401}
        cases = [
            ("triage", "triage_call", {}),
            ("spec", "spec_call", {}),
            ("review", "review_post", {"halt": "design_rethink", "review": "changes"}),
            ("pr_size", "spec_call", {"pr_size": size}),
            ("quota", "triage_call", {"halt": "quota_exhausted"}),
        ]
        cases += [(code.value, "done", {"halt": code.value, "stop": code.value}) for code in Halt]
        env = {**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true",
               "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")])}
        with TemporaryDirectory() as directory:
            for name, predecessor, extra in cases:
                with self.subTest(pause=name):
                    stage = "spec" if name == "pr_size" else "triage" if name == "quota" else name
                    plan = next(owner.value for owner in OWNERS if owner.name == "plan")
                    state = {"schema_version": SCHEMA_VERSION, **{owner.name: owner.value for owner in OWNERS}, "path": [stage], "human_question": "Which?", "halt_detail": "Waiting.", "attempts": [{"stage": "triage", "model": "gpt",
                                 "commit": "a" * 40, "methods": [], "outcome": "ran", "usage": {
                                     "input": 1, "output": 2, "cache_read": 0, "cache_creation": 0,
                                     "cost_usd": None}}], **extra}
                    if predecessor == "spec_call":
                        plan = plan.model_copy(update={"spec_gate": "needs_human" if name == "spec" else "ready", "spec_rejection": None})
                    elif name == "triage":
                        plan = plan.model_copy(update={"triage": "needs_human"})
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
                    if predecessor != "done":
                        self.assertEqual(len(outputs[1]["interrupts"]), 1)
                        payload = outputs[1]["interrupts"][0]
                        self.assertEqual(payload.get("stage", payload.get("reason")),
                                         "quota_exhausted" if name == "quota" else name)
