import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.delivery import LiveRun
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.dry_run import DEFAULT_RISK
from foreman.github import MergeHoldUnreadable, MergeNotArmed
from foreman.runner import run_live_delivery
from foreman.stage_output import STAGE_OUTPUTS, ImplementOutput, ReviewOutput, parse_output
from foreman.worker import CliWorker
from foreman.workspace import LiveWorkspace, WorktreeInspectionFailed, WorktreeState


HEAD = "a" * 40
BRANCH = "8-list-items"
CLEAN_WORKTREE = WorktreeState(HEAD, True, BRANCH)
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


class FakeWorkspace(LiveWorkspace):
    def __init__(self, cwd, snapshots=(CLEAN_WORKTREE,)):
        super().__init__(cwd)
        self.snapshots = iter(snapshots)
        self.last = CLEAN_WORKTREE

    def inspect(self):
        self.last = next(self.snapshots, self.last)
        if isinstance(self.last, WorktreeInspectionFailed):
            raise self.last
        return self.last


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
        self.merged = set()
        self.label_calls = []
        self.issue_labels = {}
        self.merge_calls = []
        self.hold = None
        self.hold_reads = []

    def merge_hold(self, repo):
        self.hold_reads.append(repo)
        return self.hold

    def arm_auto_merge(self, repo, issue, branch, sha):
        self.merge_calls.append((repo, issue, branch, sha))

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

    def test_non_object_replies_are_invalid(self):
        replies = ('Here is the result: {"triage": "implement"}', '["implement"]')
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
        result, model = dispatcher.run("triage", ["gpt-first", "claude-second", "grok-third"], {}, "")
        self.assertEqual(calls, ["codex", "claude", "grok"])
        self.assertEqual((result, model), ({"triage": "implement"}, "grok-third"))

    def test_codex_reroute_is_rejected_even_with_the_requested_banner(self):
        def run(args, cwd):
            return subprocess.CompletedProcess(
                args, 0, '{"triage":"implement"}', "model: gpt-first\nmodel rerouted: gpt-first -> gpt-other\n"
            )

        with self.assertRaises(QuotaExhausted):
            CliWorker(Path("."), run)("triage", "gpt-first", {}, "")


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
            schema = model.model_json_schema()
            self.assertEqual(schema["type"], "object")
            self.assertNotIn("anyOf", schema)
            self.assertNotIn("oneOf", schema)
            json.loads(json.dumps(schema), object_hook=check)

    def test_extra_field_reports_its_nested_path(self):
        raw = {"implementation": "Done", "browser_proof": {"extra": True}}
        with self.assertRaisesRegex(InvalidOutput, r"^browser_proof\.extra:"):
            parse_output(ImplementOutput, raw)

    def test_formal_rejection_requires_a_boolean(self):
        for fields in ({}, {"rejected": "true"}, {"rejected": 1}):
            with self.subTest(fields=fields):
                raw = {
                    "implementation": "Done",
                    "formal_proof": {"constraints": [{"constraint": "unique_item_id", **fields}]},
                }
                with self.assertRaisesRegex(InvalidOutput, r"^formal_proof\.constraints\.0\.rejected:"):
                    parse_output(ImplementOutput, raw)

    def test_sectioned_implementation_becomes_one_document(self):
        result = parse_output(ImplementOutput, {"implementation": {"Changes": "Built.", "Tests": "Passed."}})
        self.assertEqual(result.implementation, "Changes\nBuilt.\n\nTests\nPassed.")

    def test_punctuated_changes_without_a_note_keeps_null(self):
        result = parse_output(ReviewOutput, {**APPROVE, "review": "changes."})
        self.assertEqual((result.review, result.review_note), ("changes", None))

    def test_hedged_approval_is_invalid(self):
        with self.assertRaisesRegex(InvalidOutput, "^review:"):
            parse_output(ReviewOutput, {**APPROVE, "review": "approve, but please fix X first"})

    def test_review_verdicts_require_lowercase(self):
        for verdict in ("Approve.", "Changes. Add a check."):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(InvalidOutput, "^review:"):
                parse_output(ReviewOutput, {**APPROVE, "review": verdict})

    def test_review_rejects_two_change_notes(self):
        with self.assertRaisesRegex(InvalidOutput, "inline change note conflicts with review_note"):
            parse_output(ReviewOutput, {**APPROVE, "review": "changes. Add a check.", "review_note": "Update the docs."})


class LiveRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.cwd = Path(directory)
        self.checkpoint = self.cwd / "runs.sqlite3"
        self.calls = []
        self.prompts = []
        self.github = FakeGitHub()

    def walk(self, replies, *, issue=8, check="exit 0", snapshots=(CLEAN_WORKTREE,)):
        outputs = iter(replies)

        def run(args, cwd):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            self.prompts.append(args[-1])
            return worker_reply(args, next(outputs))

        return run_live_delivery(
            repo="Example/Engine", issue=issue, check=check,
            checkpoint=self.checkpoint, dispatcher=FailoverDispatcher(CliWorker(self.cwd, run)),
            run=LiveRun(self.github, FakeWorkspace(self.cwd, snapshots)),
        )

    def test_review_note_reaches_the_next_implement_prompt(self):
        built = {"implementation": "Built."}
        changes = {**APPROVE, "review": "changes. Add a check."}
        self.walk([{"triage": "implement"}, built, changes, built, APPROVE])
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
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built", "head_sha": "b" * 40}, APPROVE],
            check="test -f expected",
        )
        self.assertEqual(self.calls, ["claude", "codex", "claude"])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["check_exit_code"], 0)
        self.assertEqual(result["verified_head"], HEAD)
        self.assertEqual(result["head_sha"], "b" * 40)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(result["merge"], "armed")

    def test_cli_uses_one_workspace_root_for_the_worker_check_and_checkpoint(self):
        root = self.cwd / "checkout"
        root.mkdir()
        live_run = LiveRun(self.github, FakeWorkspace(root))
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])

        def worker(args, cwd):
            self.assertEqual(cwd, root)
            return worker_reply(args, next(replies))

        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "touch checked"],
                worker_runner=worker, checkpoint=self.checkpoint, live_run=live_run,
            )
        self.assertEqual(code, 0)
        self.assertTrue((root / "checked").exists())
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        rerun = {"repo": "Example/Engine", "issue": 8, "check": "exit 0", "checkpoint": self.checkpoint}
        self.assertEqual(run_live_delivery(**rerun, run=live_run)["workspace"], str(root.resolve()))
        with self.assertRaisesRegex(ValueError, "Run this issue from"):
            run_live_delivery(**rerun, run=LiveRun(self.github, FakeWorkspace(self.cwd)))

    def test_nonzero_check_exit_consumes_the_cap_without_dispatched_verify_or_review(self):
        result = self.walk([{"triage": "implement"}] + [{"implementation": "built"}] * 3, check="exit 17")
        self.assertEqual(result["check_exit_code"], 17)
        self.assertEqual(result["stop"], "verify_cap")
        self.assertEqual(self.calls, ["claude", "codex", "codex", "codex"])
        self.assertNotIn("merge", result["path"])

    def test_both_proofs_pass_before_review_and_merge(self):
        self.github.body = slice_body(SLICE)
        result = self.walk([
            {"triage": "spec"},
            {"spec_gate": "ready", "spec": "Make IDs unique"},
            {"implementation": "built", "head_sha": HEAD, "browser_proof": BROWSER, "formal_proof": FORMAL},
            APPROVE,
        ])
        self.assertEqual(result["path"][-5:], ["verify", "proof", "review", "merge", "done"])
        self.assertEqual(result["slice"], SLICE)
        self.assertEqual(result["proof_labels"], {})
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

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

    def test_parent_stops_before_review_when_its_proof_is_missing(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "implement", "slice": SLICE}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "proof", "done"])
        self.assertEqual(result["stop"], "insufficient_evidence")
        self.assertEqual(result["proof_labels"], {"browser": "insufficient_evidence", "formal": "insufficient_evidence"})

    def test_parent_cannot_implement_without_a_slice(self):
        self.github = FakeGitHub(["factory"])
        for triage in ({"triage": "implement"}, {"triage": "implement", "slice": None}):
            with self.subTest(triage=triage), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "run.sqlite3"
                self.calls.clear()
                result = self.walk([triage, {"implementation": "built"}, APPROVE])
                self.assertEqual(result["path"], ["triage", "done"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"].startswith("slice:"))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.github.merge_calls, [])
                self.assertEqual(self.github.created, [])

    def test_browser_block_rebuilds_and_rechecks_before_review(self):
        self.github.body = slice_body(SLICE)
        blocked = {"implementation": "built", "formal_proof": FORMAL, "browser_proof": {**BROWSER, "passed": False}}
        fixed = {**blocked, "browser_proof": BROWSER}
        result = self.walk([{"triage": "implement"}, blocked, fixed, APPROVE])
        self.assertEqual(result["path"], [
            "triage", "implement", "verify", "proof", "implement", "verify", "proof", "review", "merge", "done",
        ])
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

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
                worker_runner=None, checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 0)
        self.assertIn("done no_dispatcher", stdout.getvalue())
        self.assertFalse((self.cwd / "ran").exists())

    def test_dirty_or_invalid_worktree_skips_the_check(self):
        for snapshot in (WorktreeState(HEAD, False, BRANCH), WorktreeInspectionFailed("Invalid HEAD")):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}],
                    check="touch ran", snapshots=(snapshot,),
                )
                self.assertEqual(result["stop"], "dirty_worktree")
                self.assertEqual(result["path"], ["triage", "implement", "verify", "done"])
                self.assertFalse((self.cwd / "ran").exists())
                self.assertNotIn("check_exit_code", result)
                self.assertEqual(self.github.merge_calls, [])

    def test_worktree_changes_after_check_leave_the_command_unarmed(self):
        for snapshot, detail in (
            (WorktreeState(HEAD, False, BRANCH), "worktree has changes"),
            (
                WorktreeState("b" * 40, True, BRANCH),
                f"git reset --keep {HEAD}\nThen rerun the same command. New commits need a new run.",
            ),
            (WorktreeState(HEAD, True, None), "HEAD is detached"),
        ):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
                    snapshots=(CLEAN_WORKTREE, snapshot),
                )
                self.assertEqual(result["check_exit_code"], 0)
                self.assertEqual(result["merge"], "not_armed")
                self.assertEqual(result["stop"], "merge_not_armed")
                self.assertIn(detail, result["halt_detail"])
                self.assertEqual(self.github.merge_calls, [])

    def test_retry_after_failed_hold_read_arms_once(self):
        reason = "HTTP 503: Service Unavailable"
        with patch.object(self.github, "merge_hold", side_effect=MergeHoldUnreadable(reason)):
            held = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(held["stop"], "merge_held")
        self.assertEqual(held["merge"], "not_armed")
        self.assertEqual(held["halt_detail"], f"could not read FOREMAN_MERGE_HOLD: {reason}")
        self.assertEqual(held["merge_command"], [
            "gh", "pr", "merge", "<number>", "--repo", "Example/Engine", "--squash", "--auto", "--match-head-commit", HEAD,
        ])
        self.assertEqual(self.github.merge_calls, [])
        released = self.walk([])
        self.assertEqual(released["stop"], "verified")
        self.assertEqual(released["merge"], "armed")
        self.assertEqual(released["path"], held["path"])
        self.assertNotIn("halt_detail", released)
        self.assertEqual(self.walk([]), released)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_retry_after_worktree_is_cleaned_arms_once(self):
        halted = self.walk(
            [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
            snapshots=(CLEAN_WORKTREE, WorktreeState(HEAD, False, BRANCH)),
        )
        self.assertEqual(halted["stop"], "merge_not_armed")
        self.assertEqual(halted["merge"], "not_armed")
        self.assertEqual(self.github.hold_reads, [])
        self.assertEqual(self.github.merge_calls, [])
        released = self.walk([])
        self.assertEqual(released["stop"], "verified")
        self.assertEqual(released["merge"], "armed")
        self.assertEqual(released["path"], halted["path"])
        self.assertEqual(self.walk([]), released)
        self.assertEqual(self.github.hold_reads, ["Example/Engine"])
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_retry_after_reset_to_verified_head_arms_merge(self):
        repo = self.cwd / "engine"
        repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-c", "user.name=Foreman Test", "-c", "user.email=foreman@example.com", *args],
                cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()

        git("init", "-b", BRANCH)
        git("commit", "--allow-empty", "-m", "Verified commit")
        verified_head = git("rev-parse", "HEAD")
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])

        def run_worker(args, cwd):
            reply = next(replies)
            if reply is APPROVE:
                git("commit", "--allow-empty", "-m", "Commit after verification")
            return worker_reply(args, reply)

        def run_delivery():
            return run_live_delivery(
                repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
                dispatcher=FailoverDispatcher(CliWorker(repo, run_worker)),
                run=LiveRun(self.github, LiveWorkspace(repo)),
            )

        halted = run_delivery()
        self.assertEqual(halted["stop"], "merge_not_armed")
        self.assertEqual(self.github.merge_calls, [])
        reset = re.search(rf"^  git reset --keep {verified_head}$", halted["halt_detail"], re.MULTILINE)
        self.assertIsNotNone(reset, halted["halt_detail"])
        git(*reset.group().split()[1:])

        resumed = run_delivery()
        self.assertEqual(resumed["stop"], "verified")
        self.assertEqual(resumed["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, verified_head)])

    def test_live_workspace_reports_head_status_and_branch(self):
        for status, branch in (("", BRANCH + "\n"), ("?? untracked\n", ""), ("", "")):
            with self.subTest(status=status, branch=branch), patch("foreman.workspace.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, HEAD + "\n"),
                subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, status),
                subprocess.CompletedProcess(["git", "branch", "--show-current"], 0, branch),
            ]) as run:
                self.assertEqual(LiveWorkspace(self.cwd).inspect(), WorktreeState(HEAD, not status, branch.strip() or None))
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"], ["git", "branch", "--show-current"],
                ])
                self.assertTrue(all(call.kwargs["cwd"] == self.cwd for call in run.call_args_list))

    def test_live_workspace_rejects_invalid_head_before_the_remaining_inspection(self):
        for head in ("", "abc123", "z" * 40):
            with self.subTest(head=head), patch(
                "foreman.workspace.subprocess.run",
                return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, head + "\n", ""),
            ) as run:
                with self.assertRaisesRegex(WorktreeInspectionFailed, "full 40-hex HEAD SHA"):
                    LiveWorkspace(self.cwd).inspect()
                run.assert_called_once()

    def test_main_displays_the_forge_failure(self):
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        detail = "GraphQL: Auto-merge is disabled"
        with (
            contextlib.chdir(self.cwd),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            patch.object(self.github, "arm_auto_merge", side_effect=MergeNotArmed(detail)) as arm,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8", "--check", "exit 0"],
                worker_runner=lambda args, cwd: worker_reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 0)
        arm.assert_called_once_with("Example/Target", 8, BRANCH, HEAD)
        self.assertIn(f"merge not armed\ncheck exit 0\ndone merge_not_armed\n{detail}", stdout.getvalue())

    def test_parent_arms_its_pull_request_without_filing_slices(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "implement", "slice": EMPTY_SLICE}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(self.github.created, [])

    def test_main_dry_run_reports_a_skipped_merge(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, patch("foreman.github.subprocess.run") as run:
            code = main(["run", "delivery", "--repo", "Example/Target", "--request", "List items", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(
            "merge skipped (dry run): gh pr merge '<number>' --repo Example/Target --squash --auto --match-head-commit '<sha>'\n"
            "done verified",
            stdout.getvalue(),
        )
        run.assert_not_called()

    def test_main_rerun_arms_once_after_the_repository_hold_is_cleared(self):
        self.github.hold = "  Maintenance window "
        repo = "Example/Target"
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        live_run = LiveRun(self.github, FakeWorkspace(self.cwd))

        def invoke():
            with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    ["run", "delivery", "--repo", repo, "--issue", "8", "--check", "exit 0"],
                    worker_runner=lambda args, cwd: worker_reply(args, next(replies)),
                    checkpoint=self.checkpoint, live_run=live_run,
                )
            self.assertEqual(code, 0)
            return stdout.getvalue()

        held = invoke()
        self.assertIn(f"done merge_held\n{self.github.hold}\n", held)
        self.assertEqual(invoke(), held)
        self.assertEqual(self.github.merge_calls, [])
        self.github.hold = ""
        armed = invoke()
        self.assertIn("merge armed\ncheck exit 0\ndone verified", armed)
        self.assertEqual(invoke(), armed)
        self.assertEqual(self.github.hold_reads, [repo, repo, repo])
        self.assertEqual(self.github.merge_calls, [(repo, 8, BRANCH, HEAD)])

    def test_completed_legacy_checkpoint_keeps_its_merge_outcome(self):
        result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        config = {"configurable": {"thread_id": "example/engine#8"}}
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saved = saver.get_tuple(config)
            channels = saved.checkpoint["channel_values"]
            channels["merge_armed"] = channels.pop("merge") == "armed"
            saver.put(saved.config, saved.checkpoint, saved.metadata, {})
        self.assertEqual(self.walk([]), result)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import main
from foreman.delivery import LiveRun
from test_runner import APPROVE, HEAD, FakeGitHub, FakeWorkspace, worker_reply

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
    sys.argv[2:], worker_runner=run, checkpoint=Path(sys.argv[1]),
    live_run=LiveRun(FakeGitHub(), FakeWorkspace(Path.cwd())),
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
        self.assertIn("done verified", completed)
        self.assertEqual(invoke(), completed)
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")
