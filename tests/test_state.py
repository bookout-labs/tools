import tests
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
from pydantic import ValidationError

from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.runner import checkpoint_state
from foreman.state import ReviewState, PlanState, ChecksState, RunState, SCHEMA_VERSION, DeliveryState, checkpoint_serializer, extend_attempts


FIXTURES = Path(__file__).with_name("fixtures") / "state"


class StateTests(unittest.TestCase):
    def test_human_answer_rejects_unknown_fields_and_stages(self):
        answer = {"stage": "spec", "question": "Keep?", "answer": "Yes"}
        self.assertEqual(PlanState(human_qa=[answer]).human_qa, [answer])
        for invalid in ({**answer, "stage": "pr_size"}, {**answer, "extra": True}):
            with self.subTest(answer=invalid), self.assertRaises(ValidationError):
                PlanState(human_qa=[invalid])

    def test_owner_channels_and_reducers(self):
        hints = get_type_hints(DeliveryState, include_extras=True)
        self.assertEqual(sorted(set(hints) - {"schema_version"}), json.loads((FIXTURES / "channels.json").read_text()))
        reducers = {key: args[1] for key, hint in hints.items()
                    if get_origin(hint) is Annotated and (args := get_args(hint))}
        self.assertEqual(reducers, {"attempts": extend_attempts, "path": operator.add})
        first, second = [{"attempt_id": "1"}], [{"attempt_id": "2"}]
        # A nested stage returns the attempts it was given and its own; a plain update appends.
        self.assertEqual(extend_attempts(first, [*first, *second]), [*first, *second])
        self.assertEqual(extend_attempts(first, second), [*first, *second])

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
            original["review"] = ReviewState()
            original["plan"] = PlanState(triage="needs_human",
                human_qa=[{"stage": "spec", "question": "Keep?", "answer": "Yes"}])
            original["checks"] = ChecksState(check="true")
            original["run"] = RunState(repo="a/b", token_cap=100)
            with SqliteSaver.from_conn_string(str(path)) as saver:
                saver.serde = checkpoint_serializer()
                graph = compile_delivery(ScriptHandler(), load_config(),
                                         run=LiveRun(forge=DryRun(), workspace=DryRun()),
                                         checkpointer=saver)
                graph.update_state(config, original, as_node="triage")
                before = graph.get_state(config)
                self.assertEqual(before.values, original)
                self.assertIsNotNone(checkpoint_state(before.values))
                self.assertEqual(before.next, ("done",))
                attempt = {**original["attempts"][0], "stage": "research"}
                graph.update_state(config, {"path": ["research"], "attempts": [attempt],
                                            "halt_detail": "after move"}, as_node="triage")
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
