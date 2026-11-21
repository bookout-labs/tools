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
from foreman.adapters import Outcome, Usage
from foreman.cli import run_exit_code
from foreman.config import ModelSpec, PstackPin, load_config, order_for
from foreman.outcomes import Halt
from foreman.delivery import LiveRun, compile_delivery, halted
from foreman.github import ForgeError
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.runner import run_live_delivery
from langgraph.checkpoint.sqlite import SqliteSaver
from foreman.workspace import WorktreeState
from test_runner import FakeGitHub, FakeWorkspace, BRANCH

root, pin, author, outcomes, legacy = sys.argv[1:]
root = Path(root)
workspace = FakeWorkspace(root)
workspace.current = WorktreeState(workspace.current.head, True, BRANCH)
forge = FakeGitHub()
posted = []
original_post = forge.post_review
def post(*args, marker):
    posted.append({"body": args[2], "marker": marker})
    if legacy == "post_failure":
        raise ForgeError("post unavailable")
    return original_post(*args, marker=marker)
forge.post_review = post
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
        if kind == "interrupt":
            raise RuntimeError("review interrupted")
        if kind in {"quota", "cli_missing", "failed"}:
            return Outcome(kind, detail="review fixture")
        if kind == "quota_budget":
            return Outcome("quota", usage=Usage(input_tokens=20_000_000, output_tokens=0))
        if kind == "unknown":
            return Outcome("ran", {})
        if kind in {"changes", "ran"}:
            script.reviews = ["changes" if kind == "changes" else "approve"]
        if kind == "invalid":
            return Outcome("ran", {}, usage=SCRIPTED_USAGE)
    return script(node, model, state, instruction)
config = replace(load_config(), pstack=PstackPin.model_validate_json(pin))
if legacy == "single":
    for name, node in config.nodes.items():
        if node.models:
            config.nodes[name] = replace(node, models=(ModelSpec.parse("gpt-6.1-sol-high"),))
if legacy in {"pause", "pending"}:
    names = [m.name for m in config.nodes["review"].models]
    if legacy == "pending":
        # An older custom table puts Grok at the third eligible position.
        names.append("gpt-6.1-terra-high")
    names.append("grok-4.7-high")
    config.nodes["review"] = replace(config.nodes["review"], models=tuple(ModelSpec.parse(m) for m in names))
def legacy_order(node, config, used, implement_models=None):
    models = order_for(node, config, used, implement_models)
    return [m for m in models if m.family != author] if node == "review" and legacy in {"pause", "pending"} else models
def legacy_halt(reason, detail, stage):
    return halted(Halt.quota_exhausted if reason == "reviewer_unavailable" else Halt(reason), detail, stage)
run = LiveRun(forge, workspace)
with patch("foreman.runner.load_config", return_value=config), patch("foreman.delivery.order_for", legacy_order), patch("foreman.delivery.halted", legacy_halt if legacy == "pause" else halted):
    try:
        result = run_live_delivery(repo="Example/Engine", issue=8, check="exit 0",
            checkpoint=root / "run.sqlite3", run=run, dispatcher=worker)
    except RuntimeError:
        assert legacy == "pending"
        with SqliteSaver.from_conn_string(str(root / "run.sqlite3")) as saver:
            graph = compile_delivery(worker, config, run=run, checkpointer=saver)
            snapshot = graph.get_state({"configurable": {"thread_id": "example/engine#8"}})
            assert snapshot.next == ("review_call",), snapshot.next
            assert not snapshot.values.get("halt")
            assert snapshot.values["pending_dispatch"]["index"] == 2
        print(json.dumps({"calls": calls}))
        sys.exit(99)

print(json.dumps({"result": result, "calls": calls, "posted": posted, "posts": len(forge.reviews), "merges": len(forge.merge_calls)}))
sys.exit(run_exit_code(result))
'''


class ReviewerAvailabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.pin = fixture_pin(cls.root / "skills")

    def invoke(self, root, author, outcomes, code, legacy="current"):
        bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
        process = subprocess.run([sys.executable, "-c", bootstrap + SCRIPT, str(root),
                                  self.pin.model_dump_json(), author, json.dumps(outcomes), legacy],
                                 text=True, capture_output=True)
        self.assertEqual(process.returncode, code, process.stderr)
        return json.loads(process.stdout)

    def test_selection_matrix_and_full_history(self):
        config = load_config()
        for authors, expected in ((["claude"], ["gpt", "claude"]), (["gpt"], ["claude", "gpt"]),
                                  (["grok"], ["claude", "gpt"]), (["gpt", "grok"], ["claude", "gpt"]),
                                  (["gpt", "claude"], ["claude", "gpt"])):
            with self.subTest(authors=authors):
                selected = order_for("review", config, {}, [f"{family}-model" for family in authors])
                self.assertEqual([m.family for m in selected], expected)

    def test_fresh_process_availability_and_recovery(self):
        for author, outcomes in (("claude", ["quota", "quota"]), ("claude", ["cli_missing", "cli_missing"]),
                                 ("claude", ["quota", "cli_missing"]), ("claude", ["cli_missing", "quota"]),
                                 ("grok", ["cli_missing", "cli_missing"]),
                                 ("grok", ["quota", "cli_missing"]),
                                 ("grok", ["cli_missing", "quota"])):
            with self.subTest(author=author, outcomes=outcomes), TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                first = self.invoke(root, author, outcomes, 75)
                self.assertEqual(first["result"]["halt"], "reviewer_unavailable")
                self.assertEqual([a["outcome"] for a in first["result"]["attempts"] if a["stage"] == "review"], outcomes)
                expected = ["gpt-6.1-sol-high", "claude-opus-5-5-high"] if author == "claude" else ["claude-opus-5-5-high", "gpt-6.1-sol-high"]
                self.assertEqual(first["calls"], expected)
                for model, kind in zip(expected, outcomes):
                    self.assertIn(f"review {model}: {kind}. review fixture", first["result"]["halt_detail"])
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
        for author, outcomes, code, rounds, posts, models in (
            ("gpt", ["cli_missing", "ran"], 0, 1, 1, ["claude-opus-5-5-high", "gpt-6.1-sol-high"]),
            ("gpt", ["quota", "ran"], 0, 1, 1, ["claude-opus-5-5-high", "gpt-6.1-sol-high"]),
            ("claude", ["failed"], 1, 0, 0, ["gpt-6.1-sol-high"]),
            ("claude", ["invalid"], 1, 0, 0, ["gpt-6.1-sol-high"]),
            ("grok", ["quota", "failed"], 1, 0, 0, ["claude-opus-5-5-high", "gpt-6.1-sol-high"]),
            ("grok", ["cli_missing", "invalid"], 1, 0, 0, ["claude-opus-5-5-high", "gpt-6.1-sol-high"]),
            ("claude", ["changes", "failed"], 1, 1, 1, ["gpt-6.1-sol-high"] * 2),
            ("grok", ["changes", "quota", "invalid"], 1, 1, 1, ["claude-opus-5-5-high"] * 2 + ["gpt-6.1-sol-high"]),
        ):
            with self.subTest(outcomes=outcomes), TemporaryDirectory(dir=self.root) as directory:
                result = self.invoke(Path(directory), author, outcomes, code)
                self.assertEqual(result["calls"], models)
                attempts = [a for a in result["result"]["attempts"] if a["stage"] == "review"]
                self.assertEqual([a["model"] for a in attempts], models)
                self.assertEqual([a["outcome"] for a in attempts], ["ran" if k in {"changes", "invalid"} else k for k in outcomes])
                self.assertEqual([a["usage"] is None for a in attempts], [k in {"quota", "cli_missing", "failed"} for k in outcomes])
                self.assertEqual(result["result"]["review_rounds"], rounds)
                self.assertEqual(result["posts"], posts)
                if not code:
                    self.assertEqual(result["result"]["halt_detail"], "")
                    provenance = result["result"]["review_findings"][-1]["provenance"]
                    self.assertEqual(provenance, attempts[-1]["provenance"])
                    self.assertEqual(provenance["reviewer"], models[-1])
                    self.assertTrue(provenance["same_family"])
                    self.assertEqual(provenance["reason"], "preceding_candidates_unavailable")
                    self.assertIn(json.dumps(provenance, separators=(",", ":")), result["posted"][-1]["body"])
                if code:
                    self.assertEqual(result["result"]["halt"], "invalid_output")
                    self.assertIn(f"review {models[-1]}:", result["result"]["halt_detail"])
                    self.assertEqual(result["result"]["review_rounds"], rounds)
                    self.assertEqual((result["posts"], result["merges"]), (posts, 0))
                    self.assertEqual(self.invoke(Path(directory), author, [], 1)["calls"], [])

    def test_mixed_history_completes_and_single_family_can_run(self):
        for author, outcomes, mode, calls, rounds in (
            ("mixed", ["changes", "ran"], "current", ["claude-opus-5-5-high"] * 2, 2),
            ("gpt", ["ran"], "single", ["gpt-6.1-sol-high"], 1),
        ):
            with self.subTest(author=author), TemporaryDirectory(dir=self.root) as directory:
                result = self.invoke(Path(directory), author, outcomes, 0, mode)
                self.assertEqual(result["calls"], calls)
                self.assertEqual(result["result"]["review_rounds"], rounds)
                self.assertEqual((result["posts"], result["merges"]), (rounds, 1))
                provenance = result["result"]["review_findings"][-1]["provenance"]
                self.assertTrue(provenance["same_family"])
                self.assertEqual(provenance["reason"], "no_other_family_configured")
                self.assertIn(json.dumps(provenance, separators=(",", ":")), result["posted"][-1]["body"])

    def test_budget_and_indeterminate_usage_stop_before_same_family_call(self):
        for outcome, halt in (("quota_budget", "budget_exceeded"), ("unknown", "usage_unknown")):
            with self.subTest(outcome=outcome), TemporaryDirectory(dir=self.root) as directory:
                result = self.invoke(Path(directory), "gpt", [outcome], 1)
                self.assertEqual(result["calls"], ["claude-opus-5-5-high"])
                self.assertEqual(result["result"]["halt"], halt)
                self.assertEqual(result["result"]["review_rounds"], 0)
                self.assertEqual((result["posts"], result["merges"]), (0, 0))
                attempts = [a for a in result["result"]["attempts"] if a["stage"] == "review"]
                self.assertEqual(len(attempts), 1)

    def test_later_cross_family_round_does_not_inherit_fallback(self):
        with TemporaryDirectory(dir=self.root) as directory:
            result = self.invoke(Path(directory), "gpt", ["quota", "changes", "ran"], 0)
            first, second = result["result"]["review_findings"]
            self.assertTrue(first["provenance"]["same_family"])
            self.assertFalse(second["provenance"]["same_family"])
            self.assertIsNone(second["provenance"]["reason"])
            self.assertEqual(second["provenance"]["contributors"], ["gpt-6.1-sol-high"])
            self.assertIn(json.dumps(second["provenance"], separators=(",", ":")), result["posted"][-1]["body"])

    def test_post_retry_keeps_provenance_and_marker_without_another_call(self):
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            first = self.invoke(root, "gpt", ["quota", "ran"], 75, "post_failure")
            self.assertEqual(first["result"]["halt"], "review_not_posted")
            ready = self.invoke(root, "gpt", [], 0)
            self.assertEqual(ready["calls"], [])
            self.assertEqual(first["posted"], ready["posted"])
            self.assertEqual(first["result"]["review_findings"], ready["result"]["review_findings"])
            self.assertEqual(first["result"]["attempts"], ready["result"]["attempts"])

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
                if pending:
                    old = self.invoke(root, "claude", ["quota", "cli_missing", "interrupt"], 99, "pending")
                    self.assertEqual(old["calls"], ["gpt-6.1-sol-high", "gpt-6.1-terra-high", "grok-4.7-high"])
                else:
                    old = self.invoke(root, "claude", ["quota", "quota"], 75, "pause")
                    self.assertEqual(old["calls"], ["gpt-6.1-sol-high", "grok-4.7-high"])
                    self.assertEqual(old["result"]["stop"], "quota_exhausted")
                    with SqliteSaver.from_conn_string(str(root / "run.sqlite3")) as saver:
                        saved = saver.get_tuple({"configurable": {"thread_id": "example/engine#8"}})
                        self.assertTrue(any(channel == "__interrupt__" for _, channel, _ in saved.pending_writes))
                result = self.invoke(root, "claude", ["ran"], 1 if pending else 0)
                self.assertEqual(result["calls"], [] if pending else ["gpt-6.1-sol-high"])
                if pending:
                    self.assertEqual(result["result"]["halt"], "usage_unknown")
                    self.assertEqual(result["result"]["attempts"][-1]["model"], "grok-4.7-high")
