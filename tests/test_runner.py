import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.cli import main
from foreman.config import ModelSpec, NodeConfig, load_config
from foreman.delivery import run_delivery
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.dry_run import DEFAULT_RISK
from foreman.runner import run_live_delivery
from foreman.stage_output import STAGE_OUTPUTS, ImplementOutput, ReviewOutput, Slice, parse_output
from foreman.worker import CliWorker
from test_adapters import fixture_process


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
APPROVE = {"review": "approve", "risk": DEFAULT_RISK, "review_note": None}


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
    if "triage" in reply or "spec" in reply:
        reply = {"slice": None, "human_question": None, **reply}
    if "spec" in reply:
        reply = {"slices": None, **reply}
    if "review" in reply:
        reply = {"review_note": None, **reply}
    field = "structured_output" if tool == "claude" else "structuredOutput"
    envelope = {field: reply, "modelUsage": {model: {}}}
    if tool == "claude":
        envelope.update(type="result", subtype="success", is_error=False)
    return subprocess.CompletedProcess(args, 0, json.dumps(envelope), "")


class FakeGitHub:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else ["slice"]
        self.body = slice_body(EMPTY_SLICE) if "slice" in self.labels else "Issue body"
        self.created = []
        self.merged = set()
        self.label_calls = []
        self.issue_labels = {}

    def load_issue(self, repo, number):
        if number in self.issue_labels:
            title, body, _ = self.created[number - 101]
            return {"title": title, "body": body, "labels": self.issue_labels[number]}
        return {"title": "Issue title", "body": self.body, "labels": self.labels}

    def create_issue(self, repo, title, body, labels):
        self.created.append((title, body, labels))
        number = 100 + len(self.created)
        self.issue_labels[number] = list(labels)
        return number

    def is_merged(self, repo, number):
        return number in self.merged

    def add_label(self, repo, number, label):
        self.label_calls.append((repo, number, label))
        self.issue_labels[number].append(label)


class CliWorkerTest(unittest.TestCase):
    def test_commands_use_the_configured_models_and_parse_the_final_result(self):
        models = {
            "gpt-6.1-sol-high": ["codex", "exec", "-m", "gpt-6.1-sol-high"],
            "claude-opus-5-5-high": ["claude", "--model", "claude-opus-5-5", "--effort", "high"],
            "grok-4.7-high": ["grok", "-m", "grok-4.7", "--reasoning-effort", "high"],
        }
        for model, prefix in models.items():
            with self.subTest(model=model):
                def run(args, cwd):
                    self.assertEqual(args[:len(prefix)], prefix)
                    self.assertEqual(cwd, Path("engine"))
                    self.assertIn('{"issue": 12}', args[-1])
                    if args[0] != "codex":
                        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]),
                                         STAGE_OUTPUTS["triage"].model_json_schema())
                    return worker_reply(args, {"triage": "implement"})

                result = CliWorker(Path("engine"), run)(
                    "triage", ModelSpec.parse(model), {"issue": 12}, "Read the issue.",
                )
                self.assertEqual(result.kind, "ran")
                self.assertEqual(result.output["triage"], "implement")

    def test_non_object_replies_are_invalid(self):
        replies = ('Here is the result: {"triage": "implement"}', '["implement"]')
        for reply in replies:
            with self.subTest(reply=reply):
                worker = CliWorker(Path("."), lambda args, cwd: worker_reply(args, reply))
                outcome = worker("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "")
                self.assertEqual(outcome.kind, "failed")
                self.assertIn("did not return a JSON object", outcome.detail)

    def walk_triage(self, models, run):
        config = load_config()
        config.nodes["triage"] = NodeConfig("coordinator", tuple(ModelSpec.parse(model) for model in models))
        return run_delivery(FailoverDispatcher(CliWorker(Path("."), run)), repo="Example/Engine",
                            request="List items", check="exit 0", config=config)

    def test_delivery_falls_through_only_for_quota_or_missing_cli(self):
        for cli in ("claude", "grok"):
            for case, stop in (("quota", "needs_human"), ("cli_missing", "needs_human"),
                               ("is_error", "invalid_output"), ("wrong_model", "invalid_output")):
                with self.subTest(cli=cli, case=case):
                    calls = []

                    def run(args, cwd):
                        calls.append(args[0])
                        if args[0] == "codex":
                            return worker_reply(args, {"triage": "needs_human", "human_question": "Which items?"})
                        if case == "cli_missing":
                            raise FileNotFoundError(args[0])
                        exit_code = 0 if case == "wrong_model" else 1
                        return fixture_process(args, case, exit_code)

                    result = self.walk_triage((f"{cli}-test", "gpt-test"), run)
                    self.assertEqual(result["stop"], stop)
                    self.assertEqual(calls, [cli, "codex"] if stop == "needs_human" else [cli])
                    if case == "is_error":
                        message = "Could not start the session." if cli == "claude" else "Unknown model: quota-example"
                        self.assertIn(message, result["halt_detail"])

    def test_delivery_stops_for_an_incomplete_native_stage_reply(self):
        def run(args, cwd):
            self.assertEqual(args[0], "claude")
            stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "structured_output": {"triage": "question"}})
            return subprocess.CompletedProcess(args, 0, stdout, "")

        result = self.walk_triage(("claude-test", "gpt-test"), run)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("Field required", result["halt_detail"])

    def test_delivery_stops_when_every_cli_is_missing(self):
        calls = []

        def run(args, cwd):
            calls.append(args[0])
            raise FileNotFoundError(args[0])

        result = self.walk_triage(("claude-test", "grok-test", "gpt-test"), run)
        self.assertEqual(calls, ["claude", "grok", "codex"])
        self.assertEqual(result["stop"], "cli_missing")
        self.assertIn("codex executable is unavailable", result["halt_detail"])

    def test_codex_reroute_is_rejected_even_with_the_requested_banner(self):
        def run(args, cwd):
            return subprocess.CompletedProcess(
                args, 0, '{"triage":"implement"}', "model: gpt-first\nmodel rerouted: gpt-first -> gpt-other\n"
            )

        outcome = CliWorker(Path("."), run)("triage", ModelSpec.parse("gpt-first"), {}, "")
        self.assertEqual(outcome.kind, "quota")


class StageOutputTest(unittest.TestCase):
    def test_schema_snapshot(self):
        snapshot = Path(__file__).with_name("stage-schemas.json").read_text()
        schemas = {name: model.model_json_schema() for name, model in STAGE_OUTPUTS.items()}
        self.assertEqual(schemas, json.loads(snapshot))

    def test_every_object_in_the_schema_is_strict(self):
        def check(schema):
            if schema.get("type") == "object":
                self.assertCountEqual(schema["required"], schema["properties"])
                self.assertIs(schema["additionalProperties"], False)
            return schema

        for model in STAGE_OUTPUTS.values():
            json.loads(json.dumps(model.model_json_schema()), object_hook=check)

    def test_extra_field_reports_its_nested_path(self):
        raw = {
            "implementation": "Done", "head_sha": None, "formal_proof": None,
            "browser_proof": {"screen": None, "trace": None, "passed": None, "extra": True},
        }
        with self.assertRaisesRegex(InvalidOutput, r"^browser_proof\.extra:"):
            parse_output(ImplementOutput, raw)

    def test_screen_rejects_control_characters_and_subdirectories(self):
        for screen in ("docs/screens/na\x01me.md", "docs/screens/a/b.md"):
            with self.subTest(screen=screen), self.assertRaisesRegex(InvalidOutput, r"^screen:"):
                parse_output(Slice, {**SLICE, "screen": screen})

    def test_hedged_approval_is_invalid(self):
        with self.assertRaisesRegex(InvalidOutput, "^review:"):
            parse_output(ReviewOutput, {**APPROVE, "review": "approve, but please fix X first"})

    def test_review_verdicts_require_lowercase(self):
        for verdict in ("Approve.", "Changes. Add a check."):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(InvalidOutput, "^review:"):
                parse_output(ReviewOutput, {**APPROVE, "review": verdict})

class LiveRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.cwd = Path(directory)
        self.checkpoint = self.cwd / "runs.sqlite3"
        self.calls = []
        self.prompts = []
        self.github = FakeGitHub()

    def walk(self, replies, *, issue=8, check="exit 0", run_gh=None, inspect_worktree=clean_worktree):
        outputs = iter(replies)

        def run(args, cwd):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            self.prompts.append(args[-1])
            return worker_reply(args, next(outputs))

        return run_live_delivery(
            repo="Example/Engine", issue=issue, check=check, cwd=self.cwd,
            checkpoint=self.checkpoint, dispatcher=FailoverDispatcher(CliWorker(self.cwd, run)),
            run_gh=run_gh, github=self.github, inspect_worktree=inspect_worktree,
        )

    def test_review_note_reaches_the_next_implement_prompt(self):
        built = {"implementation": "Built."}
        changes = {**APPROVE, "review": "changes", "review_note": "Add a check."}
        self.walk([{"triage": "implement"}, built, changes, built, APPROVE], run_gh=lambda _: 0)
        prompt = self.prompts[3]
        self.assertIn('"title": "ImplementOutput"', prompt)
        self.assertEqual(json.loads(prompt.split("Delivery state:\n")[1])["review_note"], "Add a check.")

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
        self.assertEqual(self.calls, ["claude", "codex", "claude"])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["check_exit_code"], 0)
        self.assertEqual(result["head_sha"], HEAD)
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

    def test_failed_gh_leaves_the_exact_command_recorded_but_unarmed(self):
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}, APPROVE],
            run_gh=lambda args: 1,
        )
        self.assertFalse(result["merge_armed"])
        self.assertEqual(result["merge_args"], ARM_ARGS)

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

    def run_main_merge(self, checkpoint, find_pr, run_gh):
        replies = iter([{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}, APPROVE])

        def worker(args, cwd):
            return worker_reply(args, next(replies))

        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                worker_runner=worker, checkpoint=checkpoint, github=self.github, run_gh=run_gh, find_pr=find_pr,
                inspect_worktree=clean_worktree,
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
                self.assertIn("merge armed" if exit_code == 0 else "merge arm recorded", result)

    def test_main_records_merge_when_there_is_no_pull_request(self):
        self.assert_merge_recorded_without_calling_gh(None)

    def test_main_records_merge_when_the_pull_request_head_differs(self):
        self.assert_merge_recorded_without_calling_gh({**OPEN_PR, "headRefOid": "b" * 40})

    def test_main_records_merge_when_the_pull_request_is_closed(self):
        self.assert_merge_recorded_without_calling_gh({**OPEN_PR, "state": "CLOSED"})

    def test_main_records_merge_when_the_pull_request_is_for_another_issue(self):
        self.assert_merge_recorded_without_calling_gh({**OPEN_PR, "headRefName": "80-list-items"})

    def test_main_records_merge_when_the_pull_request_targets_another_branch(self):
        self.assert_merge_recorded_without_calling_gh({**OPEN_PR, "baseRefName": "pr-match"})

    def assert_merge_recorded_without_calling_gh(self, pr):
        def unexpected_merge(args):
            self.fail(f"unexpected merge command {args}")

        result = self.run_main_merge(self.checkpoint, lambda repo: pr, unexpected_merge)
        self.assertIn("merge arm recorded", result)

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
    schema = json.loads(prompt.split("Output schema:\\n")[1].split("\\n\\nDelivery state:")[0])
    if schema["title"] == "TriageOutput":
        assert not state.get("human_answers")
        assert state["request"] == "List items"
        return worker_reply(args, {"triage": "spec"})
    if schema["title"] == "SpecOutput":
        if not state.get("human_answers"):
            return worker_reply(args, {
                "spec": "A decision is open", "spec_gate": "needs_human", "human_question": "Which items?"
            })
        assert state["human_answers"] == ["Only open items"]
        assert state["request"] == "List items"
        return worker_reply(args, {"spec": "List open items", "spec_gate": "ready"})
    if schema["title"] == "ImplementOutput":
        return worker_reply(args, {"implementation": "built", "head_sha": HEAD})
    return worker_reply(args, APPROVE)

sys.exit(main(
    sys.argv[2:], worker_runner=run, checkpoint=Path(sys.argv[1]), github=FakeGitHub(),
    run_gh=lambda args: 0, find_pr=lambda repo: OPEN_PR, inspect_worktree=clean_worktree,
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
