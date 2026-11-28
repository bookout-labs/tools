"""Reviewer eligibility and checkpoint recovery across independent processes."""
import tests

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.state import ReviewState, checkpoint_serializer
from foreman.config import order_for, parse_config
from skill_fixture import fixture_pin


SCRIPT = r'''
import json
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch
from foreman.adapters import Outcome, Usage
from foreman.cli import run_exit_code
from foreman.config import DEFAULT_PATH, ModelSpec, SkillPin, load_config, order_for
from foreman.outcomes import Halt
from foreman.delivery import LiveRun, compile_delivery, halted
from foreman.github import ForgeError
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.runner import run_live_delivery
from foreman.review_round import QUESTIONS
from foreman.state import checkpoint_serializer
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
passes = []
before_rejection = None
posts_before_rejection = None
def forbidden(*args, **kwargs):
    raise AssertionError("terminal resume attempted an external effect")
if legacy == "reject_resume":
    forge.post_review = forbidden
    forge.arm_auto_merge = forbidden
def worker(node, model, state, instruction):
    global before_rejection, posts_before_rejection
    if legacy == "reject_resume":
        forbidden()
    if node == "implement":
        builder = ("claude" if state["review"].review == "changes" else "gpt") if author == "mixed" else author
        if model.family != builder:
            return Outcome("quota", usage=SCRIPTED_USAGE)
        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
    if node == "review":
        passes.append(next(name for name, question in QUESTIONS.items() if question in instruction))
    if node == "review" and passes[-1] != "a":
        # Each queued outcome is pass (a)'s. Passes (b) and (c) find nothing.
        return script(node, model, state, instruction)
    if node == "review":
        calls.append(model.name)
        kind = next(queue)
        if kind in {"placeholder", "unresolved", "rejected_failed"}:
            with SqliteSaver.from_conn_string(str(root / "run.sqlite3")) as saver:
                saver.serde = checkpoint_serializer()
                before_rejection = saver.get({"configurable": {"thread_id": "example/engine#8"}})["channel_values"]
            posts_before_rejection = list(posted)
            usage = Usage(input_tokens=11, output_tokens=7, cache_read_input_tokens=5,
                          cache_creation_input_tokens=3, total_cost_usd=0.125)
            if kind == "rejected_failed":
                return Outcome("failed", detail="review JSON was truncated", usage=usage)
            if kind == "placeholder":
                capture = json.loads((Path(sys.path[0]) / "fixtures/review/placeholder-99.json").read_text())
                payload = {"findings": [], "human_question": None, "risk": capture["risk"]}
            else:
                payload = script(node, model, state, instruction).output
                payload["findings"] = [{"location": {"side": "head", "path": "missing.txt", "line": 1}}]
            return Outcome("ran", payload, usage=usage)
        if kind == "interrupt":
            raise RuntimeError("review interrupted")
        if kind in {"quota", "cli_missing", "failed"}:
            return Outcome(kind, detail="review fixture", usage=None if kind == "cli_missing" else SCRIPTED_USAGE)
        if kind == "quota_budget":
            return Outcome("quota", usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=20_000_000, output_tokens=0))
        if kind == "unknown":
            return Outcome("ran", {})
        if kind in {"changes", "ran"}:
            script.reviews = ["changes" if kind == "changes" else "approve"]
        if kind == "invalid":
            return Outcome("ran", {}, usage=SCRIPTED_USAGE)
    return script(node, model, state, instruction)
config = replace(load_config(DEFAULT_PATH), pstack=SkillPin.model_validate_json(pin), matt_skills=SkillPin.model_validate_json(pin).model_copy(update={"directory": "skills"}))
if legacy == "single":
    for name, node in config.nodes.items():
        if node.models:
            config.nodes[name] = replace(node, models=(ModelSpec.parse("gpt-6.1-sol-high"),))
if legacy == "pending":
    names = [m.name for m in config.nodes["review"].models]
    if legacy == "pending":
        # An older custom table puts Grok at the third eligible position.
        names.append("gpt-6.1-terra-high")
    names.append("grok-4.7-high")
    config.nodes["review"] = replace(config.nodes["review"], models=tuple(ModelSpec.parse(m) for m in names))
def legacy_order(node, config, used, implement_models=None):
    models = order_for(node, config, used, implement_models)
    return [m for m in models if m.family != author] if node == "review" and legacy == "pending" else models
run = LiveRun(forge, workspace)
with patch("foreman.runner.load_config", return_value=config), patch("foreman.stages.order_for", legacy_order):
    try:
        result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=root / "run.sqlite3", run=run, dispatcher=worker)
    except RuntimeError:
        assert legacy == "pending"
        with SqliteSaver.from_conn_string(str(root / "run.sqlite3")) as saver:
            saver.serde = checkpoint_serializer()
            graph = compile_delivery(worker, config, run=run, checkpointer=saver)
            snapshot = graph.get_state({"configurable": {"thread_id": "example/engine#8"}})
            assert snapshot.next == ("review",), snapshot.next
            assert not snapshot.values.get("halt")
        print(json.dumps({"calls": calls}))
        sys.exit(99)

print(json.dumps({"result": result, "calls": calls, "passes": passes, "posted": posted, "before_rejection": before_rejection, "posts_before_rejection": posts_before_rejection, "posts": len(forge.reviews), "merges": len(forge.merge_calls)}, default=lambda value: value.model_dump(mode="json")))
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
        return json.loads(process.stdout, object_hook=lambda value: {**value, "review": ReviewState.model_validate(value["review"])} if isinstance(value.get("review"), dict) else value)

    @staticmethod
    def pass_a(invoked):
        """The review attempts of pass (a), whose outcomes the fixture queues. Each review call is one attempt."""
        attempts = [a for a in invoked["result"]["attempts"] if a["stage"] == "review"]
        return [attempt for attempt, name in zip(attempts, invoked["passes"], strict=True) if name == "a"]

    def test_selection_matrix_and_full_history(self):
        config = parse_config({"max_review_rounds": 3, "nodes": {
            "review": {"role": "reviewer", "models": ["claude-reviewer", "gpt-reviewer"]},
        }})
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
                self.assertEqual((first["result"]["halt"], first["result"]["stop"]), ("worker_unavailable", "worker_unavailable"))
                self.assertEqual([a["outcome"] for a in first["result"]["attempts"] if a["stage"] == "review"], outcomes)
                expected = ["gpt-6.1-sol-high", "claude-opus-5-5-high"] if author == "claude" else ["claude-opus-5-5-high", "gpt-6.1-sol-high"]
                self.assertEqual(first["calls"], expected)
                for model, kind in zip(expected, outcomes):
                    self.assertIn(f"{model}: {kind}. review fixture", first["result"]["halt_detail"])
                again = self.invoke(root, author, outcomes, 75)
                self.assertEqual(again["calls"], expected)
                self.assertEqual(len(again["result"]["review"].review_findings), 0)
                self.assertEqual((again["posts"], again["merges"]), (0, 0))
                ready = self.invoke(root, author, ["ran"], 0)
                self.assertEqual(ready["calls"], expected[:1])
                self.assertEqual(len(ready["result"]["review"].review_findings), 1)
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
                attempts = self.pass_a(result)
                self.assertEqual([a["model"] for a in attempts], models)
                self.assertEqual([a["outcome"] for a in attempts], ["ran" if k in {"changes", "invalid"} else k for k in outcomes])
                self.assertEqual([a["usage"] is None for a in attempts], [k == "cli_missing" for k in outcomes])
                self.assertEqual(len(result["result"]["review"].review_findings), rounds)
                self.assertEqual(result["posts"], posts)
                if not code:
                    # RV5: each pass starts its own selection, so passes (b) and (c) prefer the other family again.
                    every = [a for a in result["result"]["attempts"] if a["stage"] == "review"]
                    self.assertEqual([(name, a["model"]) for a, name in zip(every, result["passes"]) if name != "a"],
                                     [("b", "claude-opus-5-5-high"), ("c", "claude-opus-5-5-high")])
                    self.assertEqual(result["result"].get("halt_detail", ""), "")
                    provenance = result["result"]["review"].review_findings[-1]["provenance"]["a"]
                    self.assertEqual(provenance, attempts[-1]["provenance"])
                    self.assertEqual(provenance["reviewer"], models[-1])
                    self.assertTrue(provenance["same_family"])
                    self.assertEqual(provenance["reason"], "preceding_candidates_unavailable")
                    self.assertIn(f"Pass (a) reviewer {provenance['reviewer']}; implemented by {', '.join(provenance['contributors'])}; same-family fallback: preceding candidates unavailable.", result["posted"][-1]["body"])
                if code:
                    self.assertEqual(result["result"]["halt"], "invalid_output")
                    self.assertIn(f"review output from {models[-1]}:", result["result"]["halt_detail"])
                    self.assertEqual(len(result["result"]["review"].review_findings), rounds)
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
                self.assertEqual(len(result["result"]["review"].review_findings), rounds)
                self.assertEqual((result["posts"], result["merges"]), (rounds, 1))
                provenance = result["result"]["review"].review_findings[-1]["provenance"]["a"]
                self.assertTrue(provenance["same_family"])
                self.assertEqual(provenance["reason"], "no_other_family_configured")
                self.assertIn(f"Pass (a) reviewer {provenance['reviewer']}; implemented by {', '.join(provenance['contributors'])}; same-family fallback: no other family configured.", result["posted"][-1]["body"])

    def test_budget_and_indeterminate_usage_stop_before_same_family_call(self):
        # D10: unknown usage is charged at the remaining allowance, which stops the run.
        for outcome, halt in (("quota_budget", "budget_exceeded"), ("unknown", "budget_exceeded")):
            with self.subTest(outcome=outcome), TemporaryDirectory(dir=self.root) as directory:
                result = self.invoke(Path(directory), "gpt", [outcome], 75)
                self.assertEqual(result["calls"], ["claude-opus-5-5-high"])
                self.assertEqual(result["result"]["halt"], halt)
                self.assertEqual(len(result["result"]["review"].review_findings), 0)
                self.assertEqual((result["posts"], result["merges"]), (0, 0))
                attempts = [a for a in result["result"]["attempts"] if a["stage"] == "review"]
                self.assertEqual(len(attempts), 1)

    def test_later_cross_family_round_does_not_inherit_fallback(self):
        with TemporaryDirectory(dir=self.root) as directory:
            result = self.invoke(Path(directory), "gpt", ["quota", "changes", "ran"], 0)
            first, second = (report["provenance"]["a"] for report in result["result"]["review"].review_findings)
            self.assertTrue(first["same_family"])
            self.assertFalse(second["same_family"])
            self.assertIsNone(second["reason"])
            self.assertEqual(second["contributors"], ["gpt-6.1-sol-high"])
            self.assertIn("Pass (a) reviewer claude-opus-5-5-high; implemented by gpt-6.1-sol-high; same-family fallback: not needed.", result["posted"][-1]["body"])

    def test_post_retry_keeps_provenance_and_marker_without_another_call(self):
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            first = self.invoke(root, "gpt", ["quota", "ran"], 75, "post_failure")
            self.assertEqual(first["result"]["halt"], "review_not_posted")
            ready = self.invoke(root, "gpt", [], 0)
            self.assertEqual(ready["calls"], [])
            self.assertEqual(first["posted"], ready["posted"])
            self.assertEqual(first["result"]["review"].review_findings, ready["result"]["review"].review_findings)
            self.assertEqual(first["result"]["attempts"], ready["result"]["attempts"])

    def test_resume_preserves_accepted_rounds_and_failure_counters(self):
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            first = self.invoke(root, "grok", ["changes", "quota", "cli_missing"], 75)["result"]
            again = self.invoke(root, "grok", ["quota", "cli_missing"], 75)["result"]
            for field in ("review", "checks", "plan"):
                self.assertEqual(first[field], again[field])
            ready = self.invoke(root, "grok", ["ran"], 0)["result"]
            self.assertEqual(len(ready["review"].review_findings), 2)
            self.assertEqual(ready["review"].review_findings[0], first["review"].review_findings[0])

    def test_interrupted_call_is_charged_once_and_never_replays(self):
        with TemporaryDirectory(dir=self.root) as directory:
            root = Path(directory)
            old = self.invoke(root, "claude", ["quota", "cli_missing", "interrupt"], 99, "pending")
            self.assertEqual(old["calls"], ["gpt-6.1-sol-high", "gpt-6.1-terra-high", "grok-4.7-high"])
            result = self.invoke(root, "claude", ["ran"], 75)
            self.assertEqual(result["calls"], [])
            self.assertEqual(result["result"]["halt"], "budget_exceeded")
            self.assertIn("usage_unknown", result["result"]["halt_detail"])
            last = result["result"]["attempts"][-1]
            self.assertEqual((last["model"], last["outcome"]), ("grok-4.7-high", "interrupted"))
            self.assertIn("work_charge", last)
            again = self.invoke(root, "claude", ["ran"], 75)
            self.assertEqual((again["calls"], again["result"]["attempts"]), ([], result["result"]["attempts"]))

    def test_rejected_reviews_preserve_accepted_state_and_resume_terminally(self):
        fields = ("review",)
        cases = ((["placeholder"], 0), (["changes", "placeholder"], 1),
                 (["changes", "unresolved"], 1), (["changes", "rejected_failed"], 1),
                 (["changes", "quota", "placeholder"], 1),
                 (["changes", "cli_missing", "rejected_failed"], 1))
        for outcomes, rounds in cases:
            with self.subTest(outcomes=outcomes), TemporaryDirectory(dir=self.root) as directory:
                root = Path(directory)
                first = self.invoke(root, "grok", outcomes, 1)
                result, before = first["result"], first["before_rejection"]
                self.assertIsNotNone(before)
                for field in fields:
                    self.assertEqual(field in result, field in before, field)
                    self.assertEqual(result.get(field), before.get(field), field)
                self.assertEqual(result["models_used"], before["models_used"])
                # The final dispatch's fallback attempts reach the run state with its result.
                self.assertEqual(result["attempts"][:len(before["attempts"])], before["attempts"])
                self.assertEqual(len(result["attempts"]) - len(before["attempts"]), len(outcomes) - rounds)
                self.assertEqual(result["path"][-3:], ["publish", "verify", "review"])
                self.assertEqual(result["path"].count("implement"), rounds + 1)
                self.assertEqual(result["path"].count("check"), rounds + 1)
                self.assertEqual(len(result["review"].review_findings), rounds)
                self.assertEqual(first["posted"], first["posts_before_rejection"])
                self.assertEqual((first["posts"], first["merges"]), (rounds, 0))
                self.assertEqual(result["halt"], "invalid_output")
                attempt = result["attempts"][-1]
                self.assertEqual(attempt["stage"], "review")
                self.assertEqual(attempt["outcome"], "failed" if outcomes[-1] == "rejected_failed" else "ran")
                self.assertEqual(attempt["usage"], {"input": 11, "output": 7, "cache_read": 5,
                                                   "cache_creation": 3, "cost_usd": 0.125})
                self.assertNotIn("provenance", attempt)
                prefix = f"review output from {attempt['model']}: "
                self.assertTrue(result["halt_detail"].startswith(prefix))
                if outcomes[-1] == "placeholder":
                    self.assertIn("risk: Extra inputs", result["halt_detail"][len(prefix):])
                elif outcomes[-1] == "rejected_failed":
                    self.assertEqual(result["halt_detail"], prefix + "review JSON was truncated")
                else:
                    self.assertIn("findings.0", result["halt_detail"][len(prefix):])
                review_attempts = self.pass_a(first)
                self.assertEqual(len(review_attempts), len(outcomes))
                if rounds:
                    accepted = result["review"].review_findings[0]
                    self.assertEqual(result["review"].review, "changes")
                    self.assertEqual(accepted["findings"][0]["severity"], "P1")
                    self.assertEqual(accepted["provenance"]["a"], review_attempts[0]["provenance"])
                    self.assertEqual(result["models_used"]["review"], review_attempts[0]["model"])
                    self.assertIn("The first item is listed twice.", first["posted"][0]["body"])
                    self.assertEqual(result["review"].review_receipt["id"], 1)
                    if len(outcomes) == 3:
                        self.assertNotEqual(attempt["model"], result["models_used"]["review"])
                else:
                    self.assertNotIn("review", result["models_used"])
                    self.assertEqual(result["review"], ReviewState(review_run_id=result["review"].review_run_id))
                resumed = self.invoke(root, "grok", [], 1, "reject_resume")
                self.assertEqual(resumed["result"], result)
                self.assertEqual((resumed["calls"], resumed["posted"], resumed["posts"], resumed["merges"]),
                                 ([], [], 0, 0))
