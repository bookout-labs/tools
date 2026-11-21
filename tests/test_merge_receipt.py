import tests

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from foreman.github import LiveForge, MergeNotArmed, QueueRejected, CommandTimedOut, CommandFailed, queue_receipt
from foreman.readiness import Observation, PullSnapshot, QueuePolicy, QueueSnapshot

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
    return queue_receipt(QueueSnapshot.model_validate(snapshot), REPO, NUMBER, HEAD, "main", POLICY)


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

    def test_locked_and_advanced_base_are_eligible(self):
        locked = {**QUEUED, "baseRefOid": "d"*40, "mergeQueueEntry": {**QUEUED["mergeQueueEntry"], "state": "LOCKED"}}
        receipt = interpret(locked)
        self.assertEqual((receipt.status, receipt.base), ("LOCKED", "d"*40))

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
