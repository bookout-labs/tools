import tests

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import patch

from pydantic import ValidationError
import os
from unittest.mock import Mock

from foreman.github import (ForgeError, HandoffChanged, IssueBlocker, LiveForge, MergeNotArmed, QueueRejected, CommandTimedOut,
                            CommandFailed, queue_receipt, render_slice_body)
from foreman.readiness import Observation, PullSnapshot, QueuePolicy, QueueSnapshot
from foreman.stage_output import PlanState, load_stored_slice
from foreman.steps import merge
from test_readiness import MERGE, READINESS, REVIEWED, ProductionWorkspace, published, stage_run

FIXTURES = Path(__file__).with_name("fixtures") / "github"
QUEUED = json.loads((FIXTURES / "queue-queued.json").read_text())
SNAPSHOT = json.loads((FIXTURES / "queue-dequeued.json").read_text())
HEAD, BASE, NUMBER = SNAPSHOT["headRefOid"], SNAPSHOT["baseRefOid"], SNAPSHOT["number"]
REPO, BRANCH = "bookout-labs/tools", "82-receipt-fixture"
RULES = json.loads((FIXTURES / "queue-rules.json").read_text())
POLICY = QueuePolicy.model_validate(next(r["parameters"] for r in RULES if r["type"] == "merge_queue"))
# No live auto-enabled capture was available. This transition uses the captured schema contract.
AUTO = {**SNAPSHOT, "autoMergeRequest": dict(enabledAt="2026-11-21T00:00:00Z", mergeMethod="MERGE")}
OBSERVATION = Observation(repo=REPO, pr=PullSnapshot(number=NUMBER, state="open", draft=False,
    head=dict(sha=HEAD, ref=BRANCH), base=dict(sha=BASE, ref="main"), mergeable=True,
    merge_commit_sha=None, mergeable_state="clean"), checks=[], outcome="ready", recovery="Eligible", queue_policy=POLICY)


def interpret(snapshot):
    return queue_receipt(QueueSnapshot.model_validate(snapshot), REPO, NUMBER, HEAD, BASE, "main", POLICY)


class MergeReceiptTest(unittest.TestCase):
    def arm(self, before=SNAPSHOT, after=QUEUED, error=None):
        self.commands = []
        snapshots = iter([before, after])
        def command(repo, argv, **kwargs):
            self.assertEqual(repo, REPO)
            self.commands.append(argv)
            if argv[1:3] == ["api", "graphql"]:
                return json.dumps(next(snapshots))
            if argv[1:3] == ["pr", "merge"]:
                if error:
                    raise error
                return ""
            self.fail(f"Unexpected second policy read: {argv}")
        with patch.object(LiveForge, "_run_command", side_effect=command):
            return LiveForge().arm_auto_merge(REPO, 82, BRANCH, HEAD, OBSERVATION)

    def writes(self):
        return [argv for argv in self.commands if argv[1:3] == ["pr", "merge"]]

    def test_captures_establish_schema_and_queue_contract(self):
        schema = json.loads((FIXTURES / "queue-schema.json").read_text())["data"]
        self.assertEqual({entry["name"] for entry in schema["states"]["enumValues"]},
                         {"QUEUED", "AWAITING_CHECKS", "MERGEABLE", "UNMERGEABLE", "LOCKED"})
        fields = json.loads((FIXTURES / "queue-pr-schema.json").read_text())["data"]
        self.assertTrue({"mergeMethod", "enabledAt"} <= {field["name"] for field in fields["auto"]["fields"]})
        self.assertIsNone(QUEUED["mergeQueueEntry"]["headCommit"])
        awaiting = json.loads((FIXTURES / "queue-awaiting.json").read_text())
        self.assertNotEqual(awaiting["mergeQueueEntry"]["headCommit"]["oid"], HEAD)
        self.assertEqual(interpret(awaiting).status, "AWAITING_CHECKS")
        self.assertIn("no merge strategy is required", (FIXTURES / "merge-queue-help.txt").read_text())

    def test_normal_success_requires_observed_receipt_and_policy_method(self):
        receipt = self.arm()
        self.assertEqual((receipt.pr, receipt.source_head, receipt.method, receipt.identity), (NUMBER, HEAD, POLICY.merge_method, QUEUED["mergeQueueEntry"]["id"]))
        self.assertEqual(self.writes(), [["gh", "pr", "merge", str(NUMBER), "--repo", REPO, "--auto", "--squash", "--match-head-commit", HEAD]])
        self.assertEqual(interpret(AUTO).status, "AUTO_MERGE_ENABLED")
        with self.assertRaisesRegex(MergeNotArmed, "not confirmed"):
            self.arm(after=SNAPSHOT)

    def test_replay_existing_admission_or_merged_never_writes(self):
        for snapshot in (QUEUED, AUTO, {**SNAPSHOT, "state": "MERGED"}):
            self.assertTrue(self.arm(before=snapshot).identity)
            self.assertEqual(self.writes(), [])

    def test_timeout_confirmation_and_rejected_write_stderr(self):
        self.assertEqual(self.arm(error=CommandTimedOut("gh pr merge")).status, "QUEUED")
        with self.assertRaisesRegex(MergeNotArmed, "not confirmed"):
            self.arm(after=SNAPSHOT, error=CommandTimedOut("gh pr merge"))
        with self.assertRaisesRegex(MergeNotArmed, "GraphQL: admission rejected") as error:
            self.arm(error=CommandFailed("GraphQL: admission rejected"))
        self.assertIsInstance(error.exception.__cause__, CommandFailed)

    def test_locked_at_reviewed_base_is_eligible(self):
        locked = {**QUEUED, "mergeQueueEntry": {**QUEUED["mergeQueueEntry"], "state": "LOCKED"}}
        receipt = interpret(locked)
        self.assertEqual((receipt.status, receipt.base), ("LOCKED", BASE))

    def test_changed_base_at_queue_readback_cannot_clear_review(self):
        moved = {**SNAPSHOT, "baseRefOid": "d" * 40}
        for state in (moved, {**moved, "mergeQueueEntry": QUEUED["mergeQueueEntry"]},
                      {**moved, "state": "MERGED"}):
            with self.subTest(state=state), self.assertRaises(HandoffChanged):
                self.arm(before=state)
            self.assertEqual(self.writes(), [])
        with self.assertRaises(HandoffChanged):
            self.arm(after={**moved, "state": "MERGED"})
        self.assertEqual(len(self.writes()), 1)

        saved = interpret(QUEUED)
        with patch.object(LiveForge, "_queue_snapshot", return_value=QueueSnapshot.model_validate(moved)):
            with self.assertRaises(HandoffChanged):
                LiveForge().reconcile_merge(saved)

    def test_saved_receipt_missing_entry_is_rejection_not_reenqueue(self):
        receipt = interpret(QUEUED)
        with patch.object(LiveForge, "_queue_snapshot", return_value=QueueSnapshot.model_validate(SNAPSHOT)), patch.object(LiveForge, "_run_command") as command:
            with self.assertRaisesRegex(QueueRejected, receipt.identity):
                LiveForge().reconcile_merge(receipt)
            command.assert_not_called()

    def test_identity_closure_draft_and_queue_rejection(self):
        for change in (dict(headRefOid="d"*40), dict(state="CLOSED"), dict(baseRefName="release"), dict(isDraft=True),
                       dict(mergeQueueEntry={**QUEUED["mergeQueueEntry"], "state": "UNMERGEABLE"})):
            with self.subTest(change=change), self.assertRaises(MergeNotArmed):
                interpret({**QUEUED, **change})
        for change in (dict(state="UNKNOWN"), dict(mergeQueueEntry={**QUEUED["mergeQueueEntry"], "state": "UNKNOWN"})):
            with self.assertRaises(ValidationError):
                interpret({**QUEUED, **change})

    @patch("foreman.github.subprocess.run")
    def test_launch_errors_and_view_timeouts_stay_wrapped(self, run):
        launch = FileNotFoundError("gh")
        run.side_effect = launch
        with self.assertRaises(MergeNotArmed) as error:
            LiveForge().arm_auto_merge(REPO, 82, BRANCH, HEAD, OBSERVATION)
        self.assertIsInstance(error.exception.__cause__, CommandFailed)
        self.assertIsNone(error.exception.__cause__.__cause__)
        run.side_effect = subprocess.TimeoutExpired(["gh", "pr", "view"], 30)
        with self.assertRaisesRegex(CommandTimedOut, "gh pr view timed out") as error:
            LiveForge().readiness(REPO, 82, BRANCH, HEAD)
        self.assertIsInstance(error.exception.__cause__, subprocess.TimeoutExpired)


MERGED = Observation(repo="Example/Engine", pr=PullSnapshot.model_validate({**READINESS, "state": "closed", "merged": True}), checks=[],
                     outcome="ready", recovery="Merged.", queue_policy={"merge_method": "SQUASH"})
DEPENDENT = IssueBlocker.model_validate({"number": 9, "title": "Dependent", "state": "open", "html_url": "https://github.com/Example/Engine/issues/9",
                                         "repository_url": "https://api.github.com/repos/Example/Engine"})


class ProductionConfirmationTest(unittest.TestCase):
    """merge.confirm runs the contract's production check against the merge commit, then releases dependents or files the revert."""

    def merged(self, command, *, state=REVIEWED, marked=None, created=None, **check):
        forge = Mock(**{"issue_comment_records.return_value": [], "readiness.return_value": MERGED, "blocking.return_value": [DEPENDENT],
                        "pull_request_for.return_value": published("MERGED"), "variables.return_value": {}})
        numbers = iter([41, 42])
        forge.marked_issues.side_effect = lambda repo, marker: (marked or {}).get(marker.split()[2], [])
        forge.create_issue.side_effect = created or (lambda *args, **kwargs: next(numbers))
        with patch("foreman.steps.merge.interrupt", side_effect=AssertionError("Confirm never pauses the stage.")):
            return merge.compile_stage(stage_run(forge, ProductionWorkspace(command, **check))).invoke(state), forge

    def test_the_check_gets_the_merge_commit_and_no_credentials(self):
        """The command's one argument is merge_commit_sha, and the host's credentials never reach its environment."""
        with patch.dict(os.environ, {"GH_TOKEN": "ghp_" + "a" * 36, "FOREMAN_TEST_TOKEN": "test-value"}):
            ended, _ = self.merged(f'test "$1" = {MERGE} && test -z "$GH_TOKEN$FOREMAN_TEST_TOKEN" && test "$#" = 1')
        self.assertEqual((ended["stop"], ended.get("halt"), ended["released"]), ("merged", None, ["Example/Engine#9"]))

    def test_ME6_not_deployed_then_passing_ends_merged_and_past_the_limit_files_no_revert(self):
        # The check itself waits: its first read finds the merge commit not deployed, its next read finds it passing.
        with TemporaryDirectory() as directory:
            ended, forge = self.merged(f"until test -f {directory}/deployed; do touch {directory}/deployed; sleep 0.1; done")
        self.assertEqual((ended["stop"], ended.get("halt"), ended["released"]), ("merged", None, ["Example/Engine#9"]))
        for name, command, check, detail in (
                ("not deployed", "exit 3", {}, "reported it not deployed (exit 3)"),
                ("past the time limit", "sleep 30", {"timeout_seconds": 1}, "did not end within 1 seconds")):
            with self.subTest(name):
                ended, forge = self.merged(command, **check)
                self.assertEqual((ended["stop"], ended["halt"]), ("production_unconfirmed", "production_unconfirmed"))
                self.assertIn(detail, ended["halt_detail"])
                self.assertIn("Foreman filed no revert", ended["halt_detail"])
                forge.create_issue.assert_not_called()
                forge.blocking.assert_not_called()

    def test_ME7_a_failed_check_files_one_ready_revert_and_one_regression_and_releases_nothing(self):
        ended, forge = self.merged("echo checkout broken; exit 1")
        self.assertEqual((ended["stop"], ended.get("halt"), ended["released"]), ("reverted", None, ["Example/Target#42"]))
        self.assertEqual((ended["merge"].regression_issue, ended["merge"].revert_issue), (41, 42))
        self.assertIn("checkout broken", ended["merge"].production_failure)
        forge.blocking.assert_not_called()
        (regression, regression_kwargs), (revert, revert_kwargs) = forge.create_issue.call_args_list
        self.assertEqual((regression[3], revert[3]), ([], ["slice", "Ready"]))
        self.assertIn("checkout broken", regression[2])
        self.assertIn("Regression: #41", revert[2])
        # Triage loads the filed revert issue's slice: kind revert, carrying the merge commit it undoes.
        item = load_stored_slice(render_slice_body(revert[2], revert_kwargs["child"]))
        self.assertEqual((item.kind, item.merge_commit_sha), ("revert", MERGE))
        self.assertEqual(item.record()["kind"], "revert")

    def test_ME7_a_revert_runs_own_failed_check_stops_unconfirmed_and_files_nothing(self):
        reverting = {**REVIEWED, "plan": PlanState.model_validate({"issue_kind": "slice", "slice": {
            "title": "Revert", "stateful": False, "acceptance": "Reverted.", "files": [], "screen": "", "properties": [],
            "kind": "revert", "merge_commit_sha": "d" * 40}})}
        ended, forge = self.merged("exit 1", state=reverting)
        self.assertEqual((ended["stop"], ended["halt"]), ("production_unconfirmed", "production_unconfirmed"))
        self.assertIn("no further revert", ended["halt_detail"])
        forge.create_issue.assert_not_called()

    def test_repeated_issue_writes_adopt_the_issue_carrying_the_saved_marker(self):
        ended, forge = self.merged("exit 1", marked={"regression": [51], "revert": [52]})
        self.assertEqual((ended["stop"], ended["released"]), ("reverted", ["Example/Target#52"]))
        forge.create_issue.assert_not_called()
        # A write that failed may have landed: GitHub's marked issue decides.
        landed: dict[str, list[int]] = {}

        def lands(repo, title, body, labels, *, marker, child=None):
            landed[marker.split()[2]] = [61 if child is None else 62]
            raise ForgeError("timed out")
        ended, forge = self.merged("exit 1", marked=landed, created=lands)
        self.assertEqual((ended["stop"], ended["released"], forge.create_issue.call_count), ("reverted", ["Example/Target#62"], 2))
