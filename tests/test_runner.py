import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from foreman.cli import main
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.dry_run import DEFAULT_RISK
from foreman.runner import run_live_delivery
from foreman.worker import CliWorker


HEAD = "a" * 40
ARM_ARGS = ["gh", "pr", "merge", "--auto", "--match-head-commit", HEAD]
SLICE = {
    "title": "List items once",
    "acceptance": "The view lists every item once.",
    "depends_on": [],
    "screen": "docs/screens/items.md",
    "properties": [{"name": "Unique", "constraint": "unique_item_id"}],
}
BROWSER = {"screen": SLICE["screen"], "trace": "traces/items.zip", "passed": True}
FORMAL = {"ran": True, "constraints": [{"constraint": "unique_item_id", "rejected": True}]}
APPROVE = {"review": "approve", "risk": DEFAULT_RISK}


def worker_reply(args, reply, model=None):
    tool = args[0]
    flag = "--model" if tool == "claude" else "-m"
    model = model or args[args.index(flag) + 1]
    body = json.dumps(reply) if isinstance(reply, dict) else reply
    if tool == "codex":
        return subprocess.CompletedProcess(args, 0, body, f"model: {model}\n")
    field = "result" if tool == "claude" else "text"
    return subprocess.CompletedProcess(args, 0, json.dumps({field: body, "modelUsage": {model: {}}}), "")


class CliWorkerTest(unittest.TestCase):
    def test_all_three_commands_pin_the_configured_slug_and_parse_the_final_result(self):
        models = {
            "gpt-6.1-sol-high": ["codex", "exec", "-m", "gpt-6.1-sol-high"],
            "claude-opus-5-5-high": ["claude", "--model", "claude-opus-5-5-high"],
            "grok-4.7-high": ["grok", "-m", "grok-4.7-high"],
        }
        for model, prefix in models.items():
            with self.subTest(model=model):
                def run(args, cwd):
                    self.assertEqual(args[:len(prefix)], prefix)
                    self.assertEqual(cwd, Path("engine"))
                    self.assertIn('{"issue": 12}', args[-1])
                    return worker_reply(args, {"triage": "implement"})

                result = CliWorker(Path("engine"), run)("triage", model, {"issue": 12}, "Read the issue.")
                self.assertEqual(result, {"triage": "implement"})

    def test_prose_and_extra_keys_are_invalid(self):
        replies = ('Here is the result: {"triage": "implement"}', {"triage": "implement", "next": "merge"})
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(InvalidOutput):
                worker = CliWorker(Path("."), lambda args, cwd: worker_reply(args, reply))
                worker("triage", "gpt-6.1-sol-high", {}, "")

    def test_model_mismatch_and_quota_trigger_failover(self):
        calls = []

        def run(args, cwd):
            calls.append(args[0])
            if args[0] == "codex":
                return worker_reply(args, {"triage": "implement"}, "gpt-other")
            if args[0] == "claude":
                return subprocess.CompletedProcess(args, 1, "", "API error 429: quota exceeded")
            return worker_reply(args, {"triage": "implement"})

        dispatcher = FailoverDispatcher(CliWorker(Path("."), run))
        result = dispatcher.run("triage", ["gpt-first", "claude-second", "grok-third"], {}, "")
        self.assertEqual(calls, ["codex", "claude", "grok"])
        self.assertEqual(result, {"triage": "implement", "model": "grok-third"})

    def test_codex_reroute_is_rejected_even_with_the_requested_banner(self):
        def run(args, cwd):
            return subprocess.CompletedProcess(
                args, 0, '{"triage":"implement"}', "model: gpt-first\nmodel rerouted: gpt-first -> gpt-other\n"
            )

        with self.assertRaises(QuotaExhausted):
            CliWorker(Path("."), run)("triage", "gpt-first", {}, "")


class LiveRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.cwd = Path(directory)
        self.checkpoint = self.cwd / "runs.sqlite3"
        self.calls = []

    def walk(self, replies, *, check="exit 0", run_gh=None):
        outputs = iter(replies)

        def run(args, cwd):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            return worker_reply(args, next(outputs))

        return run_live_delivery(
            repo="Example/Engine", issue=8, check=check, cwd=self.cwd,
            checkpoint=self.checkpoint, dispatcher=FailoverDispatcher(CliWorker(self.cwd, run)),
            run_gh=run_gh,
        )

    def test_injected_workers_reach_merge_after_a_real_check_in_the_repo(self):
        (self.cwd / "expected").touch()
        calls = []

        def gh(args):
            calls.append(args)
            return 0

        empty = {**SLICE, "screen": "", "properties": []}
        result = self.walk(
            [{"triage": "implement", "slice": empty}, {"implementation": "built", "head_sha": HEAD}, APPROVE],
            check="test -f expected", run_gh=gh,
        )
        self.assertEqual(self.calls, ["claude", "codex", "claude"])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["check_exit_code"], 0)
        self.assertEqual(result["merge_args"], ARM_ARGS)
        self.assertEqual(calls, [ARM_ARGS])
        self.assertTrue(result["merge_armed"])

    def test_nonzero_check_exit_consumes_the_cap_without_dispatched_verify_or_review(self):
        result = self.walk([{"triage": "implement"}] + [{"implementation": "built"}] * 3, check="exit 17")
        self.assertEqual(result["check_exit_code"], 17)
        self.assertEqual(result["stop"], "verify_cap")
        self.assertEqual(self.calls, ["claude", "codex", "codex", "codex"])
        self.assertNotIn("merge", result["path"])

    def test_both_proofs_pass_before_review_and_record_the_merge_command(self):
        result = self.walk([
            {"triage": "spec"},
            {"spec_gate": "ready", "spec": "Make IDs unique", "slice": SLICE},
            {"implementation": "built", "head_sha": HEAD, "browser_proof": BROWSER, "formal_proof": FORMAL},
            APPROVE,
        ])
        self.assertEqual(result["path"][-5:], ["verify", "proof", "review", "merge", "done"])
        self.assertEqual(result["proof_labels"], {})
        self.assertEqual(result["merge_args"], ARM_ARGS)
        self.assertFalse(result["merge_armed"])

    def test_missing_proof_stops_before_review(self):
        for field, artifact, missing in (("formal_proof", FORMAL, "browser"), ("browser_proof", BROWSER, "formal")):
            with self.subTest(missing=missing), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([
                    {"triage": "implement", "slice": SLICE}, {"implementation": "built", field: artifact},
                ])
                self.assertEqual(result["stop"], "insufficient_evidence")
                self.assertEqual(result["proof_labels"], {missing: "insufficient_evidence"})
                self.assertNotIn("review", result["path"])

    def test_browser_block_rebuilds_and_rechecks_before_review(self):
        blocked = {"implementation": "built", "formal_proof": FORMAL, "browser_proof": {**BROWSER, "passed": False}}
        fixed = {**blocked, "browser_proof": BROWSER}
        result = self.walk([{"triage": "implement", "slice": SLICE}, blocked, fixed, APPROVE])
        self.assertEqual(result["path"], [
            "triage", "implement", "verify", "proof", "implement", "verify", "proof", "review", "merge", "done",
        ])
        self.assertEqual(result["review_rounds"], 1)
        self.assertFalse(result["merge_armed"])
        self.assertNotIn("merge_args", result)

    def test_formal_block_consumes_the_review_cap(self):
        blocked = {
            "implementation": "built", "browser_proof": BROWSER,
            "formal_proof": {"constraints": [{"constraint": "unique_item_id", "rejected": False}]},
        }
        result = self.walk([{"triage": "implement", "slice": SLICE}] + [blocked] * 3)
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["proof_labels"], {"formal": "block"})
        self.assertEqual(result["review_rounds"], 3)
        self.assertNotIn("review", result["path"])

    def test_unconfigured_worker_stops_without_running_the_check(self):
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "touch ran"],
                worker_runner=None, checkpoint=self.checkpoint,
            )
        self.assertEqual(code, 0)
        self.assertIn("done no_dispatcher", stdout.getvalue())
        self.assertFalse((self.cwd / "ran").exists())

    def test_missing_or_abbreviated_head_does_not_call_gh(self):
        for head in (None, "abc123"):
            with self.subTest(head=head), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built", "head_sha": head}, APPROVE],
                    run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
                )
                self.assertFalse(result["merge_armed"])
                self.assertNotIn("merge_args", result)

    def test_failed_gh_leaves_the_exact_command_recorded_but_unarmed(self):
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}, APPROVE],
            run_gh=lambda args: 1,
        )
        self.assertFalse(result["merge_armed"])
        self.assertEqual(result["merge_args"], ARM_ARGS)

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import main
from test_runner import APPROVE, HEAD, worker_reply

def run(args, cwd):
    prompt = args[-1]
    state = json.loads(prompt.split("Delivery state:\\n")[1])
    if "Required keys: triage." in prompt:
        assert not state.get("human_answers")
        assert state["request"] == "List items"
        return worker_reply(args, {"triage": "spec"})
    if "Required keys: spec, spec_gate." in prompt:
        if not state.get("human_answers"):
            return worker_reply(args, {
                "spec": "A decision is open", "spec_gate": "needs_human", "human_question": "Which items?"
            })
        assert state["human_answers"] == ["Only open items"]
        assert state["request"] == "List items"
        return worker_reply(args, {"spec": "List open items", "spec_gate": "ready"})
    if "Required keys: implementation." in prompt:
        return worker_reply(args, {"implementation": "built", "head_sha": HEAD})
    return worker_reply(args, APPROVE)

sys.exit(main(sys.argv[2:], worker_runner=run, checkpoint=Path(sys.argv[1])))
"""
        def invoke(*flags):
            bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
            return subprocess.run(
                [sys.executable, "-c", bootstrap + script, str(self.checkpoint), "run", "delivery",
                 "--repo", "Example/Engine", "--issue", "8", "--check", "echo checked >> check.log", *flags],
                cwd=self.cwd, text=True, capture_output=True, check=True,
            ).stdout

        first = invoke("--request", "List items")
        self.assertIn("paused needs_human\nWhich items?", first)
        self.assertEqual(invoke(), first)
        self.assertFalse((self.cwd / "check.log").exists())
        completed = invoke("--answer", "Only open items")
        self.assertEqual(completed.count("triage "), 1)
        self.assertEqual(completed.count("spec "), 2)
        self.assertIn(" ".join(ARM_ARGS), completed)
        self.assertIn("done verified", completed)
        self.assertEqual(invoke(), completed)
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")
