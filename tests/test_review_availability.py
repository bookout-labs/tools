"""Reviewer eligibility and checkpoint recovery across independent processes."""
import tests

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.config import load_config, order_for
from skill_fixture import fixture_pin


SCRIPT = r'''
import json
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch
from foreman.adapters import Outcome
from foreman.cli import run_exit_code
from foreman.config import PstackPin, load_config
from foreman.delivery import LiveRun
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.runner import run_live_delivery
from foreman.workspace import WorktreeState
from test_runner import FakeGitHub, FakeWorkspace, BRANCH

root, pin, author, outcomes = sys.argv[1:]
root = Path(root)
workspace = FakeWorkspace(root)
workspace.current = WorktreeState(workspace.current.head, True, BRANCH)
forge = FakeGitHub()
script = ScriptHandler()
queue = iter(json.loads(outcomes))
calls = []
def worker(node, model, state, instruction):
    if node == "implement":
        builder = ("claude" if state.get("review") == "changes" else "gpt") if author == "mixed" else author
        if model.family != builder:
            return Outcome("quota")
        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
    if node == "review":
        calls.append(model.name)
        kind = next(queue)
        if kind in {"quota", "cli_missing", "failed"}:
            return Outcome(kind, detail="review fixture")
        if kind == "changes":
            script.reviews = ["changes"]
        if kind == "invalid":
            return Outcome("ran", {}, usage=SCRIPTED_USAGE)
    return script(node, model, state, instruction)
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=PstackPin.model_validate_json(pin))):
    result = run_live_delivery(repo="Example/Engine", issue=8, check="exit 0",
        checkpoint=root / "run.sqlite3", run=LiveRun(forge, workspace), dispatcher=FailoverDispatcher(worker))
print(json.dumps({"result": result, "calls": calls, "posts": len(forge.reviews), "merges": len(forge.merge_calls)}))
sys.exit(run_exit_code(result))
'''


class ReviewerAvailabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.pin = fixture_pin(cls.root / "skills")

    def invoke(self, root, author, outcomes, code):
        bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
        process = subprocess.run([sys.executable, "-c", bootstrap + SCRIPT, str(root),
                                  self.pin.model_dump_json(), author, json.dumps(outcomes)],
                                 text=True, capture_output=True)
        self.assertEqual(process.returncode, code, process.stderr)
        return json.loads(process.stdout)

    def test_selection_matrix_and_full_history(self):
        config = load_config()
        for authors, expected in ((["claude"], ["gpt"]), (["gpt"], ["claude"]),
                                  (["grok"], ["claude", "gpt"]), (["gpt", "grok"], ["claude"])):
            with self.subTest(authors=authors):
                selected = order_for("review", config, {}, [f"{family}-model" for family in authors])
                self.assertEqual([m.family for m in selected], expected)

    def test_fresh_process_availability_and_recovery(self):
        for author, outcomes in (("claude", ["quota"]), ("claude", ["cli_missing"]),
                                 ("grok", ["cli_missing", "cli_missing"]),
                                 ("grok", ["quota", "cli_missing"]),
                                 ("grok", ["cli_missing", "quota"])):
            with self.subTest(author=author, outcomes=outcomes), TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                first = self.invoke(root, author, outcomes, 75)
                self.assertEqual(first["result"]["halt"], "reviewer_unavailable")
                self.assertEqual([a["outcome"] for a in first["result"]["attempts"] if a["stage"] == "review"], outcomes)
                expected = ["gpt-6.1-sol-high"] if author == "claude" else ["claude-opus-5-5-high", "gpt-6.1-sol-high"]
                self.assertEqual(first["calls"], expected)
                again = self.invoke(root, author, outcomes, 75)
                self.assertEqual(again["calls"], expected)
                self.assertEqual(again["result"]["review_rounds"], 0)
                self.assertEqual((again["posts"], again["merges"]), (0, 0))
                ready = self.invoke(root, author, ["ran"], 0)
                self.assertEqual(ready["calls"], expected[:1])
                self.assertEqual(ready["result"]["review_rounds"], 1)
                self.assertEqual(ready["result"]["attempts"][:len(again["result"]["attempts"])], again["result"]["attempts"])
                self.assertEqual((ready["posts"], ready["merges"]), (1, 1))
                self.assertEqual(ready["result"]["path"].count("implement"), 1)

    def test_failover_recovers_but_invalid_or_failed_reviews_are_terminal(self):
        for outcomes in (["cli_missing", "ran"], ["quota", "ran"], ["failed"], ["invalid"],
                         ["quota", "failed"], ["cli_missing", "invalid"],
                         ["changes", "failed"], ["changes", "quota", "invalid"]):
            code = 0 if outcomes[-1] == "ran" else 1
            author = "claude" if len(outcomes) == 1 or outcomes == ["changes", "failed"] else "grok"
            with self.subTest(outcomes=outcomes), TemporaryDirectory(dir=self.root) as directory:
                result = self.invoke(Path(directory), author, outcomes, code)
                self.assertEqual(len(result["calls"]), len(outcomes))
                if code:
                    self.assertEqual(result["result"]["halt"], "invalid_output")
                    self.assertEqual(result["result"]["review_rounds"], int(outcomes[0] == "changes"))
                    self.assertEqual((result["posts"], result["merges"]), (int(outcomes[0] == "changes"), 0))
                    self.assertEqual(self.invoke(Path(directory), author, [], 1)["calls"], [])

    def test_empty_eligibility_is_terminal_and_names_all_excluded_families(self):
        with TemporaryDirectory(dir=self.root) as directory:
            result = self.invoke(Path(directory), "mixed", ["changes"], 1)
            self.assertEqual(result["result"]["halt"], "no_reviewer")
            self.assertIn("claude, gpt families", result["result"]["halt_detail"])
            self.assertEqual(result["calls"], ["claude-opus-5-5-high"])
            self.assertEqual((result["posts"], result["merges"]), (1, 0))
            self.assertEqual(self.invoke(Path(directory), "mixed", [], 1)["calls"], [])

    def test_resume_preserves_accepted_rounds_and_failure_counters(self):
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            first = self.invoke(root, "grok", ["changes", "quota", "cli_missing"], 75)["result"]
            again = self.invoke(root, "grok", ["quota", "cli_missing"], 75)["result"]
            for field in ("review_findings", "review_rounds", "verify_failures", "proof_failures", "spec_failures"):
                self.assertEqual(first[field], again[field])
            ready = self.invoke(root, "grok", ["ran"], 0)["result"]
            self.assertEqual(ready["review_rounds"], 2)
            self.assertEqual(len(ready["review_findings"]), 2)
            self.assertEqual(ready["review_findings"][0], first["review_findings"][0])

    def test_old_checkpoint_reselects_and_indeterminate_call_never_replays(self):
        for pending in (False, True):
            with self.subTest(pending=pending), TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                self.invoke(root, "claude", ["quota"], 75)
                with SqliteSaver.from_conn_string(str(root / "run.sqlite3")) as saver:
                    config = {"configurable": {"thread_id": "example/engine#8"}}
                    saved = saver.get_tuple(config)
                    checkpoint = saved.checkpoint
                    values = checkpoint["channel_values"]
                    values["halt"] = "quota_exhausted"
                    values["dispatch_plan"]["models"] = ["claude-opus-5-5-high", "gpt-6.1-sol-high", "grok-4.7-high"]
                    if pending:
                        values["pending_dispatch"] = {"stage": "review", "index": 2, "quota_model": None}
                    saver.put(saved.config, checkpoint, saved.metadata, {})
                result = self.invoke(root, "claude", ["ran"], 1 if pending else 0)
                self.assertEqual(result["calls"], [] if pending else ["gpt-6.1-sol-high"])
                if pending:
                    self.assertEqual(result["result"]["halt"], "usage_unknown")
                    self.assertEqual(result["result"]["attempts"][-1]["model"], "grok-4.7-high")
