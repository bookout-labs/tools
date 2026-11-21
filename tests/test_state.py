import json
import operator
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Annotated, get_args, get_origin, get_type_hints
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.runner import checkpoint_state
from foreman.state import SCHEMA_VERSION, DeliveryState


FIXTURES = Path(__file__).with_name("fixtures") / "state"


class StateTests(unittest.TestCase):
    def test_channels_and_reducers_are_unchanged(self):
        hints = get_type_hints(DeliveryState, include_extras=True)
        self.assertEqual(sorted(set(hints) - {"schema_version"}), json.loads((FIXTURES / "channels.json").read_text()))
        reducers = {key: args[1] for key, hint in hints.items()
                    if get_origin(hint) is Annotated and (args := get_args(hint))}
        self.assertEqual(reducers, {"attempts": operator.add, "path": operator.add})

    def test_current_checkpoint_keeps_reducers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.sqlite"
            config = {"configurable": {"thread_id": "before-move"}}
            fixture_path = Path(directory) / "fixture.sqlite"
            shutil.copyfile(FIXTURES / "before-move.sqlite", fixture_path)
            with SqliteSaver.from_conn_string(str(fixture_path)) as fixture:
                original = {key: value for key, value in fixture.get(config)["channel_values"].items()
                            if key in DeliveryState.__annotations__}
            original["schema_version"] = SCHEMA_VERSION
            with SqliteSaver.from_conn_string(str(path)) as saver:
                graph = compile_delivery(ScriptHandler(), load_config(),
                                         run=LiveRun(forge=DryRun(), workspace=DryRun()),
                                         checkpointer=saver)
                graph.update_state(config, original, as_node="triage_call")
                before = graph.get_state(config)
                self.assertEqual(before.values, original)
                self.assertIsNotNone(checkpoint_state(before.values))
                self.assertEqual(before.next, ("done",))
                attempt = {**original["attempts"][0], "stage": "research"}
                graph.update_state(config, {"path": ["research"], "attempts": [attempt],
                                            "halt_detail": "after move"}, as_node="triage_call")
                after = graph.get_state(config).values
                self.assertEqual(after["path"], ["triage", "research"])
                self.assertEqual(after["attempts"], [*original["attempts"], attempt])
                self.assertEqual(after["halt_detail"], "after move")
                self.assertEqual(after["watch"], original["watch"])

    def test_watch_import_does_not_import_graph(self):
        subprocess.run([sys.executable, "-c", """
import sys
import foreman.watch
assert 'foreman.state' in sys.modules
assert 'foreman.delivery' not in sys.modules
"""], check=True)
