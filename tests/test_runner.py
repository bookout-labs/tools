import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.cli import _print_run, main
from foreman.dispatch import CliMissing, FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.dry_run import DEFAULT_RISK
from foreman.runner import (
    _read_branch_diff,
    comment_on_pull_request,
    commit_implementation,
    post_inline_review_comments,
    publish_reviewed_branch,
    run_live_delivery,
)
from foreman.telemetry import TelemetryStore
from foreman.worker import CliWorker, stage_schema


HEAD = "a" * 40
OPEN_PR = {
    "number": 12, "state": "OPEN", "headRefOid": HEAD.upper(),
    "headRefName": "8-list-items", "baseRefName": "main",
}
ARM_ARGS = ["gh", "pr", "merge", "--auto", "--match-head-commit", HEAD]
SLICE = {
    "title": "List items once",
    "acceptance": "The view lists every item once.",
    "depends_on": [],
    "screen": "docs/screens/items.md",
    "properties": [{"name": "Unique", "constraint": "unique_item_id"}],
}
EMPTY_SLICE = {**SLICE, "screen": "", "properties": []}
BROWSER = {"screen": SLICE["screen"], "trace": "traces/items.zip", "passed": True}
FORMAL = {"ran": True, "constraints": [{"constraint": "unique_item_id", "rejected": True}]}
APPROVE = {"review": "approve", "risk": DEFAULT_RISK}


def slice_body(item):
    return "Issue body\n\n```json\n" + json.dumps(item) + "\n```"


def clean_worktree(cwd):
    return HEAD, True


def worker_reply(args, reply, model=None):
    tool = args[0]
    flag = "--model" if tool == "claude" else "-m"
    model = model or args[args.index(flag) + 1]
    body = json.dumps(reply) if isinstance(reply, dict) else reply
    if tool == "codex":
        return subprocess.CompletedProcess(args, 0, body, f"model: {model}\n")
    field = "result" if tool == "claude" else "text"
    return subprocess.CompletedProcess(args, 0, json.dumps({field: body, "modelUsage": {model: {}}}), "")


class FakeGitHub:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else ["slice"]
        self.body = slice_body(EMPTY_SLICE) if "slice" in self.labels else "Issue body"
        self.created = []
        self.tools_issues = []
        self.merged = set()
        self.label_calls = []
        self.issue_labels = {}

    def load_issue(self, repo, number):
        if number in self.issue_labels:
            title, body, _ = self.created[number - 101]
            return {"title": title, "body": body, "labels": self.issue_labels[number]}
        return {"title": "Issue title", "body": self.body, "labels": self.labels}

    def create_issue(self, repo, title, body, labels):
        if repo == "bookout-labs/tools":
            self.tools_issues.append((title, body, labels))
            return 200 + len(self.tools_issues)
        self.created.append((title, body, labels))
        number = 100 + len(self.created)
        self.issue_labels[number] = list(labels)
        return number

    def open_issue_titles(self, repo):
        if repo == "bookout-labs/tools":
            return [title for title, _, _ in self.tools_issues]
        return [title for title, _, _ in self.created]

    def is_merged(self, repo, number):
        return number in self.merged

    def add_label(self, repo, number, label):
        self.label_calls.append((repo, number, label))
        self.issue_labels[number].append(label)


class CliWorkerTest(unittest.TestCase):
    def test_error_details_redact_the_full_response_before_clipping(self):
        secret = "secret-start  remaining-secret"

        def run(args, cwd):
            return subprocess.CompletedProcess(args, 1, "", "x" * 185 + " " + secret)

        with patch.dict("os.environ", {"FOREMAN_TEST_TOKEN": secret}):
            with self.assertRaises(InvalidOutput) as ctx:
                CliWorker(Path("."), run)("triage", "gpt-6.2-sol-high", {}, "Triage")
        self.assertIn("[REDACTED]", str(ctx.exception))
        self.assertNotIn("secret-start", str(ctx.exception))

    def test_quota_errors_keep_reported_usage_for_budget_checks(self):
        usage = {"input_tokens": 25, "output_tokens": 5}
        cases = (
            ("claude-opus-5-5-high", "claude-opus-5-5", 1, {
                "is_error": True, "result": "quota exhausted", "usage": usage,
            }),
            ("grok-4.7-high", "grok-4.7", 0, {
                "type": "error", "message": "quota exhausted", "usage": usage,
            }),
            ("gpt-6.2-sol-high", "gpt-6.2-sol", 1, {"type": "turn.failed", "usage": usage}),
        )
        for model, slug, code, body in cases:
            with self.subTest(model=model):
                def run(args, cwd):
                    stderr = "quota exhausted" if code else ""
                    return subprocess.CompletedProcess(args, code, json.dumps(body), stderr)

                worker = CliWorker(Path("."), run)
                with self.assertRaises(QuotaExhausted):
                    worker("triage", model, {}, "Triage")
                self.assertEqual(worker.last_meta["model_usage"][slug], usage)

    def test_all_three_commands_pin_the_configured_slug_and_parse_the_final_result(self):
        models = {
            "gpt-6.2-sol-high": ["codex", "exec", "-m", "gpt-6.2-sol", "-c", "model_reasoning_effort=high"],
            "gpt-6.1-sol-high": ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high"],
            "claude-opus-5-5-high": ["claude", "--model", "claude-opus-5-5", "--effort", "high"],
            "grok-4.7-high": ["grok", "-m", "grok-4.7", "--reasoning-effort", "high"],
        }
        for model, prefix in models.items():
            with self.subTest(model=model):
                def run(args, cwd):
                    self.assertEqual(args[:len(prefix)], prefix)
                    self.assertNotIn("-high", " ".join(args[:-1]))
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

    def test_quota_triggers_failover_and_records_attempts(self):
        calls = []
        attempts = []

        def run(args, cwd):
            calls.append(args[0])
            if args[0] == "claude":
                return subprocess.CompletedProcess(args, 1, "", "API error 429: quota exceeded")
            return worker_reply(args, {"triage": "implement"})

        dispatcher = FailoverDispatcher(CliWorker(Path("."), run))
        result = dispatcher.run("triage", ["claude-first", "grok-second"], {}, "", attempts=attempts)
        self.assertEqual(calls, ["claude", "grok"])
        self.assertEqual(result, {"triage": "implement", "model": "grok-second"})
        self.assertEqual([a["status"] for a in attempts], ["quota", "ran"])

    def test_codex_model_mismatch_stops_as_invalid_output_without_failover(self):
        calls = []
        attempts = []

        def run(args, cwd):
            calls.append(args[0])
            return worker_reply(args, {"triage": "implement"}, "gpt-other")

        dispatcher = FailoverDispatcher(CliWorker(Path("."), run))
        with self.assertRaises(InvalidOutput) as ctx:
            dispatcher.run("triage", ["gpt-first", "claude-second"], {}, "", attempts=attempts)
        self.assertEqual(calls, ["codex"])
        self.assertIn("gpt-other", str(ctx.exception))
        self.assertIn("gpt-first", str(ctx.exception))
        self.assertEqual(
            attempts,
            [{"node": "triage", "model": "gpt-first", "status": "stopped", "detail": str(ctx.exception)}],
        )

    def test_codex_reroute_stops_as_invalid_output_even_with_the_requested_banner(self):
        def run(args, cwd):
            return subprocess.CompletedProcess(
                args, 0, '{"triage":"implement"}', "model: gpt-first\nmodel rerouted: gpt-first -> gpt-other\n"
            )

        with self.assertRaises(InvalidOutput) as ctx:
            CliWorker(Path("."), run)("triage", "gpt-first", {}, "")
        self.assertIn("gpt-first -> gpt-other", str(ctx.exception))

    def test_same_provider_tool_router_still_counts_as_the_requested_model(self):
        def run(args, cwd):
            self.assertEqual(args[:5], ["claude", "--model", "claude-opus-5-5", "--effort", "high"])
            self.assertNotIn("-high", " ".join(args[:-1]))
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "result": json.dumps({"triage": "implement"}),
                "modelUsage": {"claude-opus-5-5": {}, "claude-haiku-5-5": {}},
            }), "")

        result = CliWorker(Path("."), run)("triage", "claude-opus-5-5-high", {}, "")
        self.assertEqual(result, {"triage": "implement"})

    def test_a_tool_router_without_the_requested_model_stops_as_invalid_output(self):
        def run(args, cwd):
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "result": json.dumps({"triage": "implement"}),
                "modelUsage": {"claude-haiku-5-5": {}},
            }), "")

        with self.assertRaises(InvalidOutput) as ctx:
            CliWorker(Path("."), run)("triage", "claude-opus-5-5-high", {}, "")
        self.assertIn("claude-haiku-5-5", str(ctx.exception))
        self.assertIn("claude-opus-5-5", str(ctx.exception))

    def test_grok_build_id_is_the_requested_model(self):
        def run(args, cwd):
            self.assertEqual(args[:5], ["grok", "-m", "grok-4.7", "--reasoning-effort", "high"])
            self.assertNotIn("-high", " ".join(args[:-1]))
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "text": json.dumps({"triage": "implement"}),
                "modelUsage": {"grok-4.7-build": {}},
            }), "")

        result = CliWorker(Path("."), run)("triage", "grok-4.7-high", {}, "")
        self.assertEqual(result, {"triage": "implement"})

    def test_another_provider_or_a_different_id_stops_as_invalid_output(self):
        cases = (
            ("grok-4.7-high", {"grok-4.70": {}}, "grok-4.70"),
            ("claude-opus-5-5-high", {"claude-opus-5-5": {}, "gpt-6.1-sol": {}}, "gpt-6.1-sol"),
            ("claude-opus-5-5-high", {"gpt-6.1-sol": {}}, "gpt-6.1-sol"),
        )
        for model, usage, expected_name in cases:
            with self.subTest(model=model, usage=sorted(usage)):
                def run(args, cwd, usage=usage):
                    field = "result" if args[0] == "claude" else "text"
                    return subprocess.CompletedProcess(args, 0, json.dumps({
                        field: json.dumps({"triage": "implement"}),
                        "modelUsage": usage,
                    }), "")

                with self.assertRaises(InvalidOutput) as ctx:
                    CliWorker(Path("."), run)("triage", model, {}, "")
                self.assertIn(expected_name, str(ctx.exception))

    def test_unrecognized_error_exit_stops_as_invalid_output_and_names_response(self):
        for message in ("unknown model: claude-bad", "model claude-bad not available"):
            with self.subTest(message=message):
                def run(args, cwd, message=message):
                    return subprocess.CompletedProcess(args, 1, "", message)

                with self.assertRaises(InvalidOutput) as ctx:
                    CliWorker(Path("."), run)("triage", "claude-opus-5-5-high", {}, "")
                self.assertIn(message, str(ctx.exception))

    def test_missing_cli_raises_cli_missing_and_tries_next_installed_cli(self):
        attempts = []

        def run(args, cwd):
            if args[0] == "codex":
                raise FileNotFoundError("codex")
            return worker_reply(args, {"triage": "implement"})

        with self.assertRaises(CliMissing):
            CliWorker(Path("."), run)("triage", "gpt-6.1-sol-high", {}, "")
        dispatcher = FailoverDispatcher(CliWorker(Path("."), run))
        result = dispatcher.run("triage", ["gpt-6.1-sol-high", "claude-opus-5-5-high"], {}, "", attempts=attempts)
        self.assertEqual(result, {"triage": "implement", "model": "claude-opus-5-5-high"})
        self.assertEqual([a["status"] for a in attempts], ["cli_missing", "ran"])

    def test_a_successful_body_without_a_usage_report_is_not_quota(self):
        def run(args, cwd):
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"result": json.dumps({"triage": "implement"})}), ""
            )

        result = CliWorker(Path("."), run)("triage", "claude-opus-5-5-high", {}, "")
        self.assertEqual(result, {"triage": "implement"})

    def test_spec_prompt_states_the_document_shape(self):
        seen = []

        def run(args, cwd):
            seen.append(args)
            return worker_reply(args, {"spec": "Plan", "spec_gate": "needs_human"})

        result = CliWorker(Path("."), run)("spec", "gpt-6.1-sol-high", {}, "Write the spec.")
        self.assertEqual(
            seen[0][:6],
            ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high"],
        )
        self.assertNotIn("-high", " ".join(seen[0][:-1]))
        self.assertIn("The spec value is one document", seen[0][-1])
        self.assertEqual(result["spec"], "Plan")

    def test_implement_prompt_states_the_document_shape(self):
        seen = []

        def run(args, cwd):
            seen.append(args)
            return worker_reply(args, {"implementation": "built", "head_sha": "a" * 40})

        result = CliWorker(Path("."), run)("implement", "gpt-6.2-sol-high", {}, "Implement the change.")
        self.assertEqual(
            seen[0][:6],
            ["codex", "exec", "-m", "gpt-6.2-sol", "-c", "model_reasoning_effort=high"],
        )
        self.assertIn("The implementation value is one document", seen[0][-1])
        self.assertEqual(result["implementation"], "built")


class LiveRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.cwd = Path(directory)
        self.checkpoint = self.cwd / "runs.sqlite3"
        self.calls = []
        self.github = FakeGitHub()

    def walk(self, replies, *, issue=8, check="exit 0", run_gh=None, inspect_worktree=clean_worktree):
        outputs = iter(replies)

        def run(args, cwd):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            return worker_reply(args, next(outputs))

        return run_live_delivery(
            repo="Example/Engine", issue=issue, check=check, cwd=self.cwd,
            checkpoint=self.checkpoint, dispatcher=FailoverDispatcher(CliWorker(self.cwd, run)),
            run_gh=run_gh, github=self.github, inspect_worktree=inspect_worktree,
        )

    def test_checkpoint_run_identity_survives_resume_and_telemetry_is_written_once(self):
        telemetry_path = self.cwd / "telemetry.sqlite3"
        calls = []

        def handler(node, model, state, instruction):
            calls.append(node)
            handler.last_meta = {"input_tokens": 10}
            if node == "triage" and not state.get("human_answers"):
                return {"triage": "needs_human", "human_question": "What should be listed?"}
            return {"triage": "implement"} if node == "triage" else (
                {"implementation": "Built."} if node == "implement" else APPROVE
            )

        args = dict(
            repo="Example/Engine", issue=8, check="exit 0", cwd=self.cwd, checkpoint=self.checkpoint,
            dispatcher=FailoverDispatcher(handler), github=self.github, inspect_worktree=clean_worktree,
            telemetry_path=telemetry_path,
        )
        start = "2026-01-01T00:00:00+00:00"
        with patch("foreman.runner.utcnow_iso", return_value=start):
            paused = run_live_delivery(**args)
        self.assertEqual(paused["stop"], "needs_human")
        self.assertTrue(paused["run_id"])

        with patch("foreman.runner.utcnow_iso", return_value="2026-01-01T00:01:00+00:00"):
            completed = run_live_delivery(**args, answer="Open items")
        store = TelemetryStore(telemetry_path)
        first = store.get_run(completed["run_id"])
        stage_calls = store.list_stage_calls(completed["run_id"])
        self.assertEqual(completed["run_id"], paused["run_id"])
        self.assertEqual(first["started_at"], start)
        self.assertEqual(first["total_tokens"], 40)
        self.assertEqual(len(stage_calls), 4)

        with patch("foreman.runner.utcnow_iso", return_value="2026-01-01T00:02:00+00:00"):
            replayed = run_live_delivery(**args)
        self.assertEqual(replayed["run_id"], completed["run_id"])
        self.assertEqual(replayed["started_at"], start)
        self.assertEqual(store.list_runs(), [first])
        self.assertEqual(store.list_stage_calls(completed["run_id"]), stage_calls)
        self.assertEqual(calls, ["triage", "triage", "implement", "review"])

    def test_load_slice_issue(self):
        result = self.walk([{"triage": "question"}, {"research": "answer"}])
        self.assertEqual(result["request"], "Issue title\n\n" + slice_body(EMPTY_SLICE))
        self.assertEqual(result["issue_kind"], "slice")
        self.assertEqual(result["slice"], EMPTY_SLICE)

    def test_missing_or_invalid_slice_stops_before_triage(self):
        for body in ("Issue body", "Issue body\n\n```json\n{\n```", slice_body({"screen": ""})):
            with self.subTest(body=body), TemporaryDirectory(dir=self.cwd) as directory:
                self.github.body = body
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([])
                self.assertEqual(result["stop"], "missing_slice")
                self.assertEqual(self.calls, [])
                self.assertEqual(self.walk([])["stop"], "missing_slice")

    def test_load_parent_and_file_slices_only_once(self):
        self.github = FakeGitHub(["factory"])
        second = {**SLICE, "title": "Finish items", "depends_on": [SLICE["title"]]}
        result = self.walk([{"triage": "spec"}, {
            "spec": "Plan", "spec_gate": "ready", "slices": [SLICE, second],
        }])
        self.assertEqual(result["issue_kind"], "parent")
        self.assertEqual(result["request"], "Issue title\n\nIssue body")
        self.assertEqual(result["child_issue_numbers"], [101, 102])
        self.assertEqual([x[0] for x in self.github.created], [SLICE["title"], second["title"]])
        self.assertEqual(self.github.created[0][2], ["slice", "factory"])
        self.assertEqual(self.github.created[1][2], ["slice"])
        for text in (SLICE["acceptance"], SLICE["screen"], "unique_item_id"):
            self.assertIn(text, self.github.created[0][1])
        self.assertIn(SLICE["title"], self.github.created[1][1])
        for item, (_, body, _) in zip([SLICE, second], self.github.created):
            prose, fence, encoded = body.partition("\n\n```json\n")
            self.assertEqual(fence, "\n\n```json\n")
            self.assertEqual(json.loads(encoded.removesuffix("\n```")), item)
            self.assertTrue(prose.startswith("Acceptance:\n" + item["acceptance"]))
        resumed = self.walk([])
        self.assertEqual(resumed["child_issue_numbers"], [101, 102])
        self.assertEqual(len(self.github.created), 2)
        self.assertEqual(self.github.label_calls, [])
        self.github.merged.add(101)
        self.walk([])
        self.walk([])
        self.assertEqual(self.github.label_calls, [("Example/Engine", 102, "factory")])
        child = self.walk([{"triage": "question"}, {"research": "answer"}], issue=102)
        self.assertEqual(child["slice"], second)

    def test_unknown_dependency_does_not_label(self):
        self.github = FakeGitHub(["factory"])
        second = {**SLICE, "title": "Finish items", "depends_on": ["Missing"]}
        with patch("foreman.delivery.clean_slices", return_value=[SLICE, second]):
            self.walk([{"triage": "spec"}, {"spec": "Plan", "spec_gate": "ready", "slices": [SLICE, second]}])
        self.walk([])
        self.assertEqual(self.github.label_calls, [])

    def test_injected_workers_reach_merge_after_a_real_check_in_the_repo(self):
        (self.cwd / "expected").touch()
        calls = []

        def gh(args):
            calls.append(args)
            return 0

        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built", "head_sha": "b" * 40}, APPROVE],
            check="test -f expected", run_gh=gh,
        )
        self.assertEqual(self.calls, ["claude", "codex", "codex"])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["check_exit_code"], 0)
        self.assertEqual(result["head_sha"], HEAD)
        self.assertEqual(result["merge_args"], ARM_ARGS)
        self.assertEqual(calls, [ARM_ARGS])
        self.assertTrue(result["merge_armed"])

    def test_nonzero_check_exit_consumes_the_cap_without_dispatched_verify_or_review(self):
        result = self.walk([{"triage": "implement"}] + [{"implementation": "built"}] * 3, check="exit 17")
        self.assertEqual(result["check_exit_code"], 17)
        self.assertEqual(result["check_exits"], [17, 17, 17])
        self.assertEqual(result["stop"], "verify_cap")
        self.assertEqual(self.calls, ["claude", "codex", "codex", "codex"])
        self.assertNotIn("merge", result["path"])

    def test_missing_check_program_stops_once_without_using_a_review_round(self):
        for code, reason in ((127, "was not found"), (126, "could not run")):
            with self.subTest(code=code), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                self.calls = []
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}],
                    check=f"exit {code}",
                )
                self.assertEqual(result["stop"], "check_unavailable")
                self.assertEqual(result["path"], ["triage", "implement", "verify", "done"])
                self.assertEqual(result.get("review_rounds", 0), 0)
                self.assertEqual(result["check_exit_code"], code)
                self.assertEqual(result["check_exits"], [code])
                self.assertNotIn("review", result["path"])
                self.assertEqual(
                    result["halt_detail"],
                    f"the check exited {code} because a program it runs {reason}",
                )
                self.assertEqual(result["proposal"]["kind"], "setup")
                self.assertFalse(result["proposal"]["file_issue"])
                self.assertIn("No graph change.", result["proposal"]["summary"])
                self.assertEqual(self.calls, ["claude", "codex"])

    def test_log_names_each_verify_exit_and_the_review_verdict(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            _print_run({
                "path": [
                    "triage", "implement", "verify", "implement", "verify",
                    "implement", "verify", "review", "done",
                ],
                "check": "npm run check",
                "check_exit_code": 0,
                "check_exits": [127, 127, 0],
                "models_used": {
                    "triage": "claude-opus-5-5-high",
                    "implement": "gpt-6.2-sol-high",
                    "review": "gpt-6.2-sol-high",
                },
                "review": "changes",
                "review_note": "Add a hermetic RPC-first regression test.",
                "risk": {"level": "low"},
                "stop": "review_cap",
                "merge_armed": False,
            })
        text = stdout.getvalue()
        self.assertEqual(text.count("verify npm run check exit 127"), 2)
        self.assertIn("verify npm run check exit 0", text)
        self.assertIn("review gpt-6.2-sol-high changes risk low", text)
        self.assertIn("check exit 0", text)
        self.assertIn("done review_cap", text)
        self.assertIn("review note: Add a hermetic RPC-first regression test.", text)

    def test_log_keeps_each_review_verdict(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            _print_run({
                "path": [
                    "triage", "implement", "verify", "review",
                    "implement", "verify", "review", "merge", "done",
                ],
                "check": "npm run check",
                "check_exit_code": 0,
                "check_exits": [0, 0],
                "models_used": {"review": "gpt-6.2-sol-high"},
                "review": "approve",
                "review_note": "The RPC test is in.",
                "risk": {"level": "low"},
                "review_log": [
                    {
                        "model": "gpt-6.2-sol-high",
                        "verdict": "changes",
                        "level": "medium",
                        "note": "Add the RPC-first test.",
                    },
                    {
                        "model": "gpt-6.2-sol-high",
                        "verdict": "approve",
                        "level": "low",
                        "note": "The RPC test is in.",
                    },
                ],
                "stop": "verified",
                "merge_armed": True,
                "pull_request": "https://github.com/DoctorCRE/doctorcre-engine/pull/895",
            })
        lines = stdout.getvalue().splitlines()
        self.assertEqual(
            [line for line in lines if line.startswith("review gpt-6.2-sol-high")],
            [
                "review gpt-6.2-sol-high changes risk medium",
                "review gpt-6.2-sol-high approve risk low",
            ],
        )
        self.assertEqual(
            [line for line in lines if line.startswith("review note:")],
            ["review note: Add the RPC-first test.", "review note: The RPC test is in."],
        )
        self.assertLess(
            lines.index("review note: Add the RPC-first test."),
            lines.index("review gpt-6.2-sol-high approve risk low"),
        )

    def test_both_proofs_pass_before_review_and_record_the_merge_command(self):
        self.github.body = slice_body(SLICE)
        result = self.walk([
            {"triage": "spec"},
            {"spec_gate": "ready", "spec": "Make IDs unique", "slice": EMPTY_SLICE},
            {"implementation": "built", "head_sha": HEAD, "browser_proof": BROWSER, "formal_proof": FORMAL},
            APPROVE,
        ])
        self.assertEqual(result["path"][-5:], ["verify", "proof", "review", "merge", "done"])
        self.assertEqual(result["slice"], SLICE)
        self.assertEqual(result["proof_labels"], {})
        self.assertEqual(result["merge_args"], ARM_ARGS)
        self.assertFalse(result["merge_armed"])

    def test_missing_proof_stops_before_review(self):
        self.github.body = slice_body(SLICE)
        for field, artifact, missing in (("formal_proof", FORMAL, "browser"), ("browser_proof", BROWSER, "formal")):
            with self.subTest(missing=missing), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([
                    {"triage": "implement", "slice": EMPTY_SLICE}, {"implementation": "built", field: artifact},
                ])
                self.assertEqual(result["stop"], "insufficient_evidence")
                self.assertEqual(result["proof_labels"], {missing: "insufficient_evidence"})
                self.assertNotIn("review", result["path"])

    def test_browser_block_rebuilds_and_rechecks_before_review(self):
        self.github.body = slice_body(SLICE)
        blocked = {"implementation": "built", "formal_proof": FORMAL, "browser_proof": {**BROWSER, "passed": False}}
        fixed = {**blocked, "browser_proof": BROWSER}
        result = self.walk([{"triage": "implement"}, blocked, fixed, APPROVE])
        self.assertEqual(result["path"], [
            "triage", "implement", "verify", "proof", "implement", "verify", "proof", "review", "merge", "done",
        ])
        self.assertEqual(result["review_rounds"], 1)
        self.assertFalse(result["merge_armed"])
        self.assertEqual(result["merge_args"], ARM_ARGS)

    def test_formal_block_consumes_the_review_cap(self):
        self.github.body = slice_body(SLICE)
        blocked = {
            "implementation": "built", "browser_proof": BROWSER,
            "formal_proof": {"constraints": [{"constraint": "unique_item_id", "rejected": False}]},
        }
        result = self.walk([{"triage": "implement"}] + [blocked] * 3)
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["proof_labels"], {"formal": "block"})
        self.assertEqual(result["review_rounds"], 3)
        self.assertNotIn("review", result["path"])

    def test_unconfigured_worker_stops_without_running_the_check(self):
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "touch ran"],
                worker_runner=None, checkpoint=self.checkpoint, github=self.github, inspect_worktree=clean_worktree,
            )
        self.assertEqual(code, 0)
        self.assertIn("done no_dispatcher", stdout.getvalue())
        self.assertFalse((self.cwd / "ran").exists())

    def test_dirty_or_invalid_worktree_skips_the_check(self):
        for snapshot in ((HEAD, False), ("", True), ("abc123", True), ("z" * 40, True)):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}],
                    check="touch ran", inspect_worktree=lambda cwd: snapshot,
                    run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
                )
                self.assertEqual(result["stop"], "dirty_worktree")
                self.assertEqual(result["path"], ["triage", "implement", "verify", "done"])
                self.assertFalse((self.cwd / "ran").exists())
                self.assertNotIn("check_exit_code", result)

    def test_worktree_changes_after_check_leave_the_command_unarmed(self):
        for snapshot in ((HEAD, False), ("b" * 40, True)):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                snapshots = iter([(HEAD, True), snapshot])
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
                    inspect_worktree=lambda cwd: next(snapshots),
                    run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
                )
                self.assertEqual(result["check_exit_code"], 0)
                self.assertEqual(result["merge_args"], ARM_ARGS)
                self.assertFalse(result["merge_armed"])

    def test_failed_merge_stops_unpublished(self):
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}, APPROVE],
            run_gh=lambda args: 1,
        )
        self.assertFalse(result["merge_armed"])
        self.assertEqual(result["merge_args"], ARM_ARGS)
        self.assertEqual(result["stop"], "unpublished")
        self.assertEqual(result["halt_detail"], "the reviewed commit has no open pull request")

    def test_default_merge_runner_discards_stdin_and_returns_exit_code(self):
        from foreman.cli import _run_gh

        with patch("foreman.cli.subprocess.run", return_value=subprocess.CompletedProcess(ARM_ARGS, 1)) as run:
            self.assertEqual(_run_gh(ARM_ARGS), 1)
        run.assert_called_once_with(ARM_ARGS, shell=False, stdin=subprocess.DEVNULL)

    def test_default_pr_lookup_handles_gh_responses(self):
        from foreman.cli import _find_pr

        args = [
            "gh", "pr", "view", "--repo", "Example/Engine", "--json",
            "number,state,headRefOid,headRefName,baseRefName", "",
        ]
        for exit_code, output, expected in (
            (0, json.dumps(OPEN_PR), OPEN_PR),
            (1, json.dumps(OPEN_PR), None),
            (0, "invalid JSON", None),
            (0, "[]", None),
        ):
            with self.subTest(exit_code=exit_code, output=output), patch(
                "foreman.cli.subprocess.run", return_value=subprocess.CompletedProcess(args, exit_code, output),
            ) as run:
                self.assertEqual(_find_pr("Example/Engine"), expected)
                run.assert_called_once_with(
                    args, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True,
                )

    def test_default_inspector_reads_head_and_porcelain_status(self):
        from foreman.runner import _inspect_worktree

        for status in ("", "?? untracked\n"):
            with self.subTest(status=status), patch("foreman.runner.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, HEAD + "\n"),
                subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, status),
            ]) as run:
                self.assertEqual(_inspect_worktree(self.cwd), (HEAD, not status))
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"],
                ])
                self.assertTrue(all(call.kwargs["shell"] is False for call in run.call_args_list))
                self.assertTrue(all(call.kwargs["cwd"] == self.cwd for call in run.call_args_list))

    def run_main_merge(self, checkpoint, find_pr, run_gh, *, read_merge_hold=lambda repo: None):
        replies = iter([{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}, APPROVE])

        def worker(args, cwd):
            return worker_reply(args, next(replies))

        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                worker_runner=worker, checkpoint=checkpoint, github=self.github, run_gh=run_gh, find_pr=find_pr,
                inspect_worktree=clean_worktree, read_merge_hold=read_merge_hold,
            )
        self.assertEqual(code, 0)
        self.assertIn(" ".join(ARM_ARGS), stdout.getvalue())
        return stdout.getvalue()

    def test_main_arms_or_records_merge_using_injected_runner(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), TemporaryDirectory(dir=self.cwd) as directory:
                calls = []

                def find_pr(repo):
                    self.assertEqual(repo, "Example/Engine")
                    return OPEN_PR

                def gh(args):
                    calls.append(args)
                    return exit_code

                result = self.run_main_merge(Path(directory) / "run.sqlite3", find_pr, gh)
                self.assertEqual(calls, [ARM_ARGS])
                if exit_code == 0:
                    self.assertIn("merge armed", result)
                    self.assertIn("done verified", result)
                    self.assertEqual(self.github.tools_issues, [])
                else:
                    self.assertIn("merge not armed", result)
                    self.assertIn("done unpublished", result)
                    self.assertNotIn("done verified", result)
                    self.assertNotIn("merge arm recorded", result)
                    self.assertEqual(
                        self.github.tools_issues[-1][0], "Publish the reviewed branch before merge",
                    )

    def test_main_does_not_arm_when_there_is_no_pull_request(self):
        self.assert_merge_stays_unarmed(None)

    def test_main_does_not_arm_when_the_pull_request_head_differs(self):
        self.assert_merge_stays_unarmed({**OPEN_PR, "headRefOid": "b" * 40})

    def test_main_does_not_arm_when_the_pull_request_is_closed(self):
        self.assert_merge_stays_unarmed({**OPEN_PR, "state": "CLOSED"})

    def test_main_does_not_arm_when_the_pull_request_is_for_another_issue(self):
        self.assert_merge_stays_unarmed({**OPEN_PR, "headRefName": "80-list-items"})

    def test_main_does_not_arm_when_the_pull_request_targets_another_branch(self):
        self.assert_merge_stays_unarmed({**OPEN_PR, "baseRefName": "pr-match"})

    def assert_merge_stays_unarmed(self, pr):
        def unexpected_merge(args):
            self.fail(f"unexpected merge command {args}")

        result = self.run_main_merge(self.checkpoint, lambda repo: pr, unexpected_merge)
        self.assertIn("merge not armed", result)
        self.assertIn("done unpublished", result)
        self.assertIn("the reviewed commit has no open pull request", result)
        self.assertNotIn("done verified", result)
        self.assertNotIn("merge arm recorded", result)
        self.assertEqual(self.github.tools_issues[-1][0], "Publish the reviewed branch before merge")

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import main
from test_runner import APPROVE, HEAD, OPEN_PR, FakeGitHub, clean_worktree, worker_reply

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

sys.exit(main(
    sys.argv[2:], worker_runner=run, checkpoint=Path(sys.argv[1]), github=FakeGitHub(),
    run_gh=lambda args: 0, find_pr=lambda repo: OPEN_PR, inspect_worktree=clean_worktree,
    read_merge_hold=lambda repo: None,
))
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
        self.assertIn("merge armed", completed)
        self.assertIn(" ".join(ARM_ARGS), completed)
        self.assertIn("done verified", completed)
        self.assertEqual(invoke(), completed)
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")

    def test_default_merge_hold_reader_handles_gh_variable_states(self):
        from foreman.cli import _read_merge_hold

        args = ["gh", "variable", "get", "FOREMAN_MERGE_HOLD", "--repo", "Example/Engine"]
        for exit_code, stdout, stderr, expected in (
            (0, "production switch\n", "", "production switch"),
            (0, "   \n", "", None),
            (1, "", "variable FOREMAN_MERGE_HOLD was not found\n", None),
            (1, "", "HTTP 404: Not Found\n", None),
            (1, "", "auth required\n", "could not read FOREMAN_MERGE_HOLD: auth required"),
        ):
            with self.subTest(exit_code=exit_code, stdout=stdout, stderr=stderr), patch(
                "foreman.cli.subprocess.run", return_value=subprocess.CompletedProcess(args, exit_code, stdout, stderr),
            ) as run:
                self.assertEqual(_read_merge_hold("Example/Engine"), expected)
                run.assert_called_once_with(
                    args, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                )
        with patch("foreman.cli.subprocess.run", side_effect=OSError("gh missing")):
            self.assertEqual(_read_merge_hold("Example/Engine"), "could not read FOREMAN_MERGE_HOLD: gh missing")

    def test_main_holds_merge_without_arming_when_variable_is_set(self):
        def unexpected_merge(args):
            self.fail(f"unexpected merge command {args}")

        output = self.run_main_merge(
            self.checkpoint, lambda repo: OPEN_PR, unexpected_merge,
            read_merge_hold=lambda repo: "production switch",
        )
        self.assertIn("merge held", output)
        self.assertIn("done merge_held\nproduction switch", output)
        self.assertEqual(self.github.tools_issues, [])

    def test_live_cli_watcher_clean_run_and_quota_failover_file_nothing(self):
        clean_output = self.run_main_merge(self.checkpoint, lambda repo: OPEN_PR, lambda args: 0)
        self.assertIn("done verified", clean_output)
        self.assertEqual(self.github.tools_issues, [])

        with TemporaryDirectory(dir=self.cwd) as directory:
            replies = iter([
                {"triage": "implement"},
                {"implementation": "built", "head_sha": HEAD},
                APPROVE,
            ])

            def quota_once(args, cwd):
                if args[0] == "claude" and not getattr(quota_once, "tripped", False):
                    quota_once.tripped = True
                    return subprocess.CompletedProcess(args, 1, "", "429 quota exceeded")
                return worker_reply(args, next(replies))

            with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                    worker_runner=quota_once, checkpoint=Path(directory) / "run.sqlite3",
                    github=self.github, run_gh=lambda args: 0, find_pr=lambda repo: OPEN_PR,
                    inspect_worktree=clean_worktree, read_merge_hold=lambda repo: None,
                )
            self.assertEqual(code, 0)
            self.assertIn("No graph change.", stdout.getvalue())
            self.assertEqual(self.github.tools_issues, [])

    def test_live_cli_watcher_prints_and_deduplicates_graph_fix_issues(self):
        def run_failure(checkpoint_path):
            def mismatch_worker(args, cwd):
                return subprocess.CompletedProcess(args, 0, json.dumps({
                    "result": json.dumps({"triage": "implement"}),
                    "modelUsage": {"claude-haiku-5-5": {}},
                }), "")

            with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                    worker_runner=mismatch_worker, checkpoint=checkpoint_path,
                    github=self.github, inspect_worktree=clean_worktree, read_merge_hold=lambda repo: None,
                )
            self.assertEqual(code, 0)
            return stdout.getvalue()

        with TemporaryDirectory(dir=self.cwd) as dir1, TemporaryDirectory(dir=self.cwd) as dir2:
            first = run_failure(Path(dir1) / "run.sqlite3")
            self.assertIn("done invalid_output", first)
            self.assertIn("File to change: src/foreman/worker.py", first)
            self.assertIn("A person opens the pull request.", first)
            self.assertIn("watcher issue bookout-labs/tools#201", first)
            self.assertEqual(len(self.github.tools_issues), 1)
            second = run_failure(Path(dir2) / "run.sqlite3")
            self.assertIn("A person opens the pull request.", second)
            self.assertNotIn("watcher issue", second)
            self.assertEqual(len(self.github.tools_issues), 1)


def git_repo(path: Path) -> str:
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (path / "README").write_text("base\n")
    git("add", "README")
    git("commit", "-m", "base")
    return git("rev-parse", "HEAD").stdout.strip()


class CommitImplementationTest(unittest.TestCase):
    def test_review_diff_uses_the_specified_commit_even_after_head_moves(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            git_repo(repo)

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            git("switch", "-c", "review")
            (repo / "README").write_text("reviewed change\n")
            git("commit", "-am", "reviewable")
            head = git("rev-parse", "HEAD")
            (repo / "README").write_text("later change\n")
            git("commit", "-am", "later")

            self.assertIn("+reviewed change", _read_branch_diff(repo, head))
            self.assertNotIn("later change", _read_branch_diff(repo, head))

    def test_runner_commits_a_dirty_slice_branch_and_leaves_main(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            base = git_repo(repo)
            (repo / "note.txt").write_text("release\n")
            sha = commit_implementation(repo, 875, "The health check names the running release")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            main = subprocess.run(
                ["git", "rev-parse", "main"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout
            message = subprocess.run(
                ["git", "log", "-1", "--format=%s"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(sha, head)
            self.assertNotEqual(sha, base)
            self.assertEqual(main, base)
            self.assertEqual(branch, "875-the-health-check-names-the-running")
            self.assertEqual(status, "")
            self.assertEqual(message, "The health check names the running release (#875)")
            self.assertEqual(commit_implementation(repo, 875, "The health check names the running release"), sha)

    def test_runner_keeps_an_existing_slice_branch_and_a_clean_commit(self):
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            git_repo(repo)
            subprocess.run(["git", "checkout", "-b", "875-health-release"], cwd=repo, check=True, capture_output=True)
            (repo / "note.txt").write_text("release\n")
            sha = commit_implementation(repo, 875, "Issue title")
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(branch, "875-health-release")
            self.assertIn("#875", subprocess.run(
                ["git", "log", "-1", "--format=%s"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout)
            (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
            (repo / ".git" / "hooks" / "pre-commit").chmod(0o755)
            (repo / "note.txt").write_text("again\n")
            with self.assertRaises(InvalidOutput) as caught:
                commit_implementation(repo, 875, "Issue title")
            self.assertIn("implement could not commit", str(caught.exception))
            again = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(again, sha)

    def test_live_run_commits_before_verify(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            base = git_repo(repo)
            checkpoint = root / "runs.sqlite3"

            def handler(node, model, state, instruction):
                if node == "implement":
                    (repo / "note.txt").write_text("release\n")
                    return {"implementation": "Added the release."}
                if node == "review":
                    self.assertIn("[NEW:1] release", instruction)
                    return {**APPROVE, "comments": [
                        {"path": "note.txt", "line": 1, "side": "RIGHT", "body": "Checked."},
                    ]}
                return {"triage": "implement"}

            order = []

            def publish(cwd, repo_name, issue, sha, *, body=None):
                order.append("publish")
                self.assertEqual((repo_name, issue), ("Example/Engine", 875))
                self.assertEqual(sha, subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
                ).stdout.strip())
                self.assertIsNotNone(body)
                self.assertIn("Closes #875", body)
                self.assertIn("## Summary", body)
                self.assertIn("## Verification", body)
                self.assertIn("## Merge Risk", body)
                self.assertNotIn("Issue title", body)
                return "https://github.com/Example/Engine/pull/21"

            recorded = []

            def run_gh(args):
                order.append(list(args))
                return 0

            def record(cwd, repo_name, url, body, run=None):
                order.append("record")
                self.assertEqual((repo_name, url), ("Example/Engine", "https://github.com/Example/Engine/pull/21"))
                recorded.append(body)

            with (
                patch("foreman.runner.publish_reviewed_branch", side_effect=publish),
                patch("foreman.runner.comment_on_pull_request", side_effect=record),
                patch("foreman.runner.post_inline_review_comments") as inline_review,
            ):
                result = run_live_delivery(
                    repo="Example/Engine", issue=875, check="exit 0", cwd=repo,
                    checkpoint=checkpoint, dispatcher=FailoverDispatcher(handler),
                    github=FakeGitHub(), run_gh=run_gh,
                )
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            main = subprocess.run(
                ["git", "rev-parse", "main"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()
            committed = subprocess.run(
                ["git", "show", "HEAD:note.txt"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout
            self.assertEqual(result["stop"], "verified")
            self.assertNotIn("halt", result)
            self.assertIn("verify", result["path"])
            self.assertEqual(result["check_exit_code"], 0)
            self.assertEqual(branch, "875-issue-title")
            self.assertEqual(main, base)
            self.assertEqual(committed, "release\n")
            self.assertEqual(result["head_sha"], subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip())
            self.assertEqual(order, [
                "publish",
                "record",
                ["gh", "pr", "merge", "--auto", "--match-head-commit", result["head_sha"]],
            ])
            self.assertTrue(recorded[0].startswith(f"Reviewed by {result['models_used']['review']}."))
            self.assertIn("Verdict: approve.", recorded[0])
            self.assertIn("Risk: low.", recorded[0])
            self.assertNotIn("Issue title", recorded[0])
            self.assertEqual(result["pull_request"], "https://github.com/Example/Engine/pull/21")
            self.assertTrue(result["merge_armed"])
            inline_review.assert_called_once_with(
                repo, "Example/Engine", result["pull_request"], result["review_comments"],
                commit_id=result["head_sha"],
            )


def _completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class PublishReviewedBranchTest(unittest.TestCase):
    def publish(self, replies, *, sha=HEAD, issue=875):
        calls = []
        pending = list(replies)

        def execute(args, cwd):
            calls.append(list(args))
            self.assertEqual(cwd, Path("engine"))
            if not pending:
                self.fail(f"unexpected command {args}")
            return pending.pop(0)

        try:
            url = publish_reviewed_branch(Path("engine"), "Example/Engine", issue, sha, run=execute)
        except InvalidOutput:
            self.assertEqual(pending, [])
            for args in calls:
                self.assertNotIn("--force", args)
            raise
        self.assertEqual(pending, [])
        for args in calls:
            self.assertNotIn("--force", args)
        return url, calls

    def ready(self, branch="875-health-release", sha=HEAD):
        return [_completed(f"{branch}\n"), _completed(""), _completed(f"{sha}\n")]

    def test_push_then_open_uses_the_commit_subject(self):
        url, calls = self.publish(self.ready() + [
            _completed(),
            _completed(code=1, stderr="no pull request\n"),
            _completed("The health check names the running release (#875)\n"),
            _completed("https://github.com/Example/Engine/pull/21\n"),
        ])
        self.assertEqual(url, "https://github.com/Example/Engine/pull/21")
        self.assertEqual(
            calls[3],
            ["git", "push", "--set-upstream", "origin", "HEAD:refs/heads/875-health-release"],
        )
        self.assertEqual(calls[-1], [
            "gh", "pr", "create", "--repo", "Example/Engine", "--base", "main",
            "--head", "875-health-release",
            "--title", "The health check names the running release (#875)",
            "--body", "Closes #875",
        ])

    def test_existing_pull_request_at_the_sha_is_reused(self):
        payload = json.dumps({
            "url": "https://github.com/Example/Engine/pull/21",
            "state": "OPEN",
            "body": "Closes #875",
            "headRefOid": HEAD.upper(),
            "headRefName": "875-health-release",
            "baseRefName": "main",
        })
        url, calls = self.publish(self.ready() + [_completed(), _completed(payload)])
        self.assertEqual(url, "https://github.com/Example/Engine/pull/21")
        self.assertFalse(any(args[:3] == ["gh", "pr", "create"] for args in calls))
        self.assertFalse(any(args[:3] == ["gh", "pr", "edit"] for args in calls))

    def test_existing_pull_request_body_becomes_closes(self):
        payload = json.dumps({
            "url": "https://github.com/Example/Engine/pull/21",
            "state": "OPEN",
            "body": "Refs #875",
            "headRefOid": HEAD.upper(),
            "headRefName": "875-health-release",
            "baseRefName": "main",
        })
        url, calls = self.publish(self.ready() + [_completed(), _completed(payload), _completed()])
        self.assertEqual(url, "https://github.com/Example/Engine/pull/21")
        self.assertEqual(calls[-1], [
            "gh", "pr", "edit", "https://github.com/Example/Engine/pull/21",
            "--repo", "Example/Engine", "--body", "Closes #875",
        ])
        self.assertFalse(any(args[:3] == ["gh", "pr", "create"] for args in calls))

    def test_comment_posts_the_review_as_one_argument(self):
        calls = []

        def execute(args, cwd):
            calls.append(list(args))
            self.assertEqual(cwd, Path("engine"))
            return _completed()

        comment_on_pull_request(
            Path("engine"), "Example/Engine", "https://github.com/Example/Engine/pull/21",
            "Reviewed by gpt-6.2-sol-high.\nVerdict: approve.", run=execute,
        )
        self.assertEqual(calls, [[
            "gh", "pr", "comment", "https://github.com/Example/Engine/pull/21",
            "--repo", "Example/Engine", "--body", "Reviewed by gpt-6.2-sol-high.\nVerdict: approve.",
        ]])
        with self.assertRaises(InvalidOutput) as caught:
            comment_on_pull_request(
                Path("engine"), "Example/Engine", "https://github.com/Example/Engine/pull/21",
                "Reviewed by gpt-6.2-sol-high.",
                run=lambda args, cwd: _completed(code=1, stderr="denied\n"),
            )
        self.assertEqual(str(caught.exception), "could not record the review: denied")

    def test_open_pull_request_at_another_sha_is_not_replaced(self):
        payload = json.dumps({
            "url": "https://github.com/Example/Engine/pull/21",
            "state": "OPEN",
            "headRefOid": "b" * 40,
            "headRefName": "875-health-release",
            "baseRefName": "main",
        })
        with self.assertRaises(InvalidOutput) as caught:
            self.publish(self.ready() + [_completed(), _completed(payload)])
        self.assertIn("not the reviewed commit", str(caught.exception))

    def test_dirty_tree_wrong_branch_and_wrong_head_do_not_push(self):
        with self.assertRaises(InvalidOutput):
            self.publish([_completed("main\n")])
        with self.assertRaises(InvalidOutput):
            self.publish([_completed("health-release\n")])
        with self.assertRaises(InvalidOutput):
            self.publish([
                _completed("875-health-release\n"),
                _completed(" M src/worker.ts\n"),
                _completed(HEAD + "\n"),
            ])
        with self.assertRaises(InvalidOutput):
            self.publish([
                _completed("875-health-release\n"),
                _completed(""),
                _completed(("b" * 40) + "\n"),
            ])
        with self.assertRaises(InvalidOutput) as caught:
            self.publish([], sha="abc")
        self.assertIn("not a full SHA", str(caught.exception))

    def test_push_failure_names_the_branch(self):
        with self.assertRaises(InvalidOutput) as caught:
            self.publish(self.ready() + [_completed(code=1, stderr="non-fast-forward\n")])
        self.assertEqual(str(caught.exception), "could not push 875-health-release: non-fast-forward")

    def test_create_must_return_an_https_url(self):
        with self.assertRaises(InvalidOutput) as caught:
            self.publish(self.ready() + [
                _completed(),
                _completed(code=1),
                _completed("Subject\n"),
                _completed("created the pull request\n"),
            ])
        self.assertEqual(str(caught.exception), "could not open the pull request")

    def test_default_runner_refuses_main_without_a_shell(self):
        with patch(
            "foreman.runner.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, "main\n", ""),
        ) as run:
            with self.assertRaises(InvalidOutput) as caught:
                publish_reviewed_branch(Path("engine"), "Example/Engine", 875, HEAD)
        self.assertIn("could not publish from main", str(caught.exception))
        run.assert_called_once()
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "--abbrev-ref", "HEAD"])


class Phase1WorkerAndPreflightTest(unittest.TestCase):
    def test_native_schema_flags_codex_jsonl_and_meta_collection(self):
        seen_args = []

        def run(args, cwd):
            seen_args.append(list(args))
            if args[0] == "claude":
                self.assertIn("--json-schema", args)
                return subprocess.CompletedProcess(
                    args,
                    0,
                    json.dumps({
                        "structured_output": {"triage": "implement"},
                        "session_id": "sess-claude-1",
                        "duration_ms": 1200,
                        "num_turns": 2,
                        "total_cost_usd": 0.42,
                        "modelUsage": {"claude-opus-5-5": {"inputTokens": 100, "outputTokens": 25}},
                    }),
                    "",
                )
            self.assertIn("--json", args)
            self.assertIn("--output-schema", args)
            out_path = Path(args[args.index("-o") + 1])
            schema_path = Path(args[args.index("--output-schema") + 1])
            self.assertTrue(schema_path.exists())
            out_path.write_text(json.dumps({"implementation": "Updated file.", "pr_summary": "Ready"}), encoding="utf-8")
            events = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thread-codex-9"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 50}}),
            ])
            return subprocess.CompletedProcess(args, 0, events, "model: gpt-6.2-sol\n")

        dispatcher = FailoverDispatcher(CliWorker(Path("."), run))
        triage_out = dispatcher.run("triage", ["claude-opus-5-5-high"], {"issue": 1}, "Triage")
        self.assertEqual(triage_out, {"triage": "implement", "model": "claude-opus-5-5-high"})
        self.assertEqual(dispatcher.last_meta[0]["session_id"], "sess-claude-1")
        self.assertEqual(dispatcher.last_meta[0]["total_cost_usd"], 0.42)

        impl_out = dispatcher.run("implement", ["gpt-6.2-sol-high"], {"issue": 1}, "Build")
        self.assertEqual(impl_out["implementation"], "Updated file.")
        self.assertEqual(dispatcher.last_meta[0]["session_id"], "thread-codex-9")
        self.assertEqual(
            dispatcher.last_meta[0]["model_usage"]["gpt-6.2-sol"],
            {"input_tokens": 200, "output_tokens": 50},
        )
        self.assertEqual(len(seen_args), 2)

    def test_implement_resume_used_only_for_matching_model(self):
        recorded_args = []

        def run(args, cwd):
            recorded_args.append(list(args))
            return worker_reply(args, {"implementation": "Revised"})

        worker = CliWorker(Path("."), run)
        state = {"models_used": {"implement": "gpt-6.2-sol-high"}, "sessions": {"implement": "thread-99"}}
        worker("implement", "gpt-6.2-sol-high", state, "Fix review")
        self.assertEqual(recorded_args[0][:3], ["codex", "exec", "resume"])
        self.assertEqual(recorded_args[0][-2], "thread-99")
        self.assertLess(recorded_args[0].index("--output-schema"), recorded_args[0].index("thread-99"))

        worker("implement", "claude-opus-5-5-high", state, "Fix review")
        self.assertNotIn("--resume", recorded_args[1])

        worker("implement", "claude-opus-5-5-high", {
            "models_used": {"implement": "claude-opus-5-5-high"},
            "sessions": {"implement": "sess-cl-2"},
        }, "Fix review")
        self.assertIn("--resume", recorded_args[2])
        self.assertEqual(recorded_args[2][recorded_args[2].index("--resume") + 1], "sess-cl-2")

    def test_codex_resume_records_usage_delta_for_only_the_resumed_session(self):
        model = "gpt-6.2-sol-high"
        slug = "gpt-6.2-sol"
        session_id = "test-resume-session"
        cumulative = {
            "input_tokens": 80, "cached_input_tokens": 30, "cache_write_input_tokens": 5,
            "output_tokens": 20, "reasoning_output_tokens": 6, "total_tokens": 105,
        }
        first = {
            "input_tokens": 50, "cached_input_tokens": 20, "cache_write_input_tokens": 7,
            "output_tokens": 10, "reasoning_output_tokens": 3, "total_tokens": 67,
        }
        second = {"input_tokens": 20, "cached_input_tokens": 5, "total_tokens": 25}

        def run(args, cwd):
            self.assertEqual(args[:3], ["codex", "exec", "resume"])
            Path(args[args.index("-o") + 1]).write_text(json.dumps({"implementation": "Updated."}))
            stdout = json.dumps({"type": "turn.completed", "usage": cumulative})
            return subprocess.CompletedProcess(args, 0, stdout, f"model: {slug}\n")

        worker = CliWorker(Path("."), run)
        with TemporaryDirectory() as codex_root, patch.dict("os.environ", {"CODEX_HOME": codex_root}):
            worker("implement", model, {
                "models_used": {"implement": model},
                "sessions": {"implement": session_id},
                "usage_records": [
                    {"session_id": session_id, "model_usage": {slug: first}},
                    {"session_id": session_id, "model_usage": {slug: second}},
                    {"session_id": "other-session", "model_usage": {slug: cumulative}},
                ],
            }, "Update.")
        self.assertEqual(worker.last_meta["session_id"], session_id)
        self.assertEqual(worker.last_meta["model_usage"][slug], {
            "input_tokens": 10, "cached_input_tokens": 5, "cache_write_input_tokens": 0,
            "output_tokens": 10, "reasoning_output_tokens": 3, "total_tokens": 13,
        })

    def test_codex_rollout_requires_consistent_model_metadata_even_with_banner(self):
        slug = "gpt-6.2-sol"
        turn_context = {"type": "turn_context", "payload": {"model": slug}}

        def world_state(model):
            return {"type": "world_state", "payload": {"state": {"collaboration_mode": {"model": model}}}}

        with TemporaryDirectory() as tempdir, patch.dict("os.environ", {"CODEX_HOME": tempdir}):
            session_dir = Path(tempdir) / "sessions"
            session_dir.mkdir()
            rollout = session_dir / "rollout-sid-test.jsonl"

            def run(args, cwd):
                Path(args[args.index("-o") + 1]).write_text(json.dumps({"triage": "implement"}))
                stdout = json.dumps({"type": "thread.started", "thread_id": "sid-test"})
                return subprocess.CompletedProcess(args, 0, stdout, f"model: {slug}\n")

            worker = CliWorker(Path("."), run)
            for events, error in (
                ([world_state(f"{slug}-build")], None),
                ([turn_context, world_state("gpt-other")], "gpt-other"),
                ([{"type": "session_meta"}], "no model metadata"),
            ):
                with self.subTest(events=events):
                    rollout.write_text("\n".join(map(json.dumps, events)))
                    if error is None:
                        self.assertEqual(worker("triage", f"{slug}-high", {}, "Triage"), {"triage": "implement"})
                    else:
                        with self.assertRaisesRegex(InvalidOutput, error):
                            worker("triage", f"{slug}-high", {}, "Triage")

    def test_cli_preflight_halts_with_env_unavailable_before_implement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "runs.sqlite3"
            calls = []

            def run_worker(args, cwd):
                calls.append(args[0])
                return worker_reply(args, {"triage": "implement"})

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(
                    ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                     "--check", "exit 0", "--preflight", "echo 'db down' >&2; exit 2"],
                    worker_runner=run_worker,
                    checkpoint=checkpoint,
                    github=FakeGitHub(),
                    run_gh=lambda cmd: 0,
                    inspect_worktree=clean_worktree,
                    read_merge_hold=lambda repo: None,
                )
            self.assertEqual(code, 0)
            self.assertEqual(calls, ["claude"])
            self.assertIn("done env_unavailable", out.getvalue())

    def test_strict_codex_schema_null_stripping_grok_structured_output_and_rollout_check(self):
        for node in ("triage", "spec", "research", "implement", "review"):
            strict = stage_schema(node, strict=True)
            self.assertEqual(set(strict["required"]), set(strict["properties"].keys()))
            self.assertIs(strict["additionalProperties"], False)

        # Grok camelCase structuredOutput + null stripping
        def grok_run(args, cwd):
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({
                    "structuredOutput": {"triage": "implement", "slice": None, "human_question": None},
                    "sessionId": "gr-1",
                    "modelUsage": {"grok-4.7-build": {}},
                }),
                "",
            )

        res = CliWorker(Path("."), grok_run)("triage", "grok-4.7-high", {"issue": 1}, "Triage")
        self.assertEqual(res, {"triage": "implement"})

        # Codex --json rollout verification without stderr model banner
        with TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sess_dir = codex_home / "sessions" / "2026" / "11" / "20"
            sess_dir.mkdir(parents=True)
            rollout_ok = sess_dir / "rollout-sid-match.jsonl"
            rollout_ok.write_text(
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-6.2-sol"}}) + "\n",
                encoding="utf-8",
            )
            rollout_bad = sess_dir / "rollout-sid-mismatch.jsonl"
            rollout_bad.write_text(
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-other"}}) + "\n",
                encoding="utf-8",
            )

            with unittest.mock.patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                def run_ok(args, cwd):
                    out_file = Path(args[args.index("-o") + 1])
                    out_file.write_text(json.dumps({
                        "implementation": "Fixed.",
                        "head_sha": None,
                        "pr_summary": None,
                        "browser_proof": None,
                        "formal_proof": None,
                    }))
                    ev = "\n".join([
                        json.dumps({"type": "thread.started", "thread_id": "sid-match"}),
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}}),
                    ])
                    return subprocess.CompletedProcess(args, 0, ev, "")

                ok_out = CliWorker(Path("."), run_ok)("implement", "gpt-6.2-sol-high", {"issue": 2}, "Build")
                self.assertEqual(ok_out["implementation"], "Fixed.")
                self.assertNotIn("pr_summary", ok_out)

                def run_bad(args, cwd):
                    out_file = Path(args[args.index("-o") + 1])
                    out_file.write_text(json.dumps({"triage": "implement", "slice": None, "human_question": None}))
                    ev = json.dumps({"type": "thread.started", "thread_id": "sid-mismatch"})
                    return subprocess.CompletedProcess(args, 0, ev, "")

                with self.assertRaises(InvalidOutput) as ctx:
                    CliWorker(Path("."), run_bad)("triage", "gpt-6.2-sol-high", {"issue": 2}, "Triage")
                self.assertIn("gpt-other", str(ctx.exception))

    def test_post_inline_review_comments_uses_gh_api_with_redaction(self):
        calls = []

        def fake_run(args, cwd, payload):
            calls.append((list(args), cwd, json.loads(payload)))
            return subprocess.CompletedProcess(args, 0, "", "")

        post_inline_review_comments(
            Path("engine"),
            "Example/Engine",
            "https://github.com/Example/Engine/pull/42",
            [{
                "path": "src/app.py",
                "line": 12,
                "side": "RIGHT",
                "body": "Avoid logging owner@example.invalid token=ghp_0123456789abcdefghij0123456789",
            }],
            commit_id=HEAD,
            run=fake_run,
        )
        self.assertEqual(len(calls), 1)
        args, cwd, payload = calls[0]
        self.assertEqual(args, ["gh", "api", "repos/Example/Engine/pulls/42/reviews", "--method", "POST", "--input", "-"])
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertNotIn("owner@example.invalid", payload["comments"][0]["body"])
        self.assertNotIn("ghp_0123456789abcdefghij0123456789", payload["comments"][0]["body"])

    def test_post_inline_review_comments_rejects_missing_or_invalid_commit(self):
        comment = {"path": "src/app.py", "line": 12, "side": "RIGHT", "body": "Checked."}
        for commit_id in (None, "abc123", "x" * 40):
            with self.subTest(commit_id=commit_id), self.assertRaisesRegex(InvalidOutput, "full reviewed commit"):
                post_inline_review_comments(
                    Path("engine"), "Example/Engine", "https://github.com/Example/Engine/pull/42", [comment],
                    commit_id=commit_id, run=lambda *_: self.fail("invalid commit reached GitHub"),
                )

