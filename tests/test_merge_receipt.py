import tests

import json
import unittest
from unittest.mock import patch

from foreman.github import LiveForge, MergeNotArmed, CommandTimedOut, CommandFailed
from test_github import PR, HEAD, BASE, BRANCH, REPO

SNAPSHOT = dict(number=12, state="OPEN", headRefOid=HEAD, baseRefOid=BASE,
                baseRefName="main", isDraft=False, autoMergeRequest=None, mergeQueueEntry=None)
QUEUED = {**SNAPSHOT, "mergeQueueEntry": dict(id="queue-12", state="QUEUED", headCommit=dict(oid=HEAD))}
AUTO = {**SNAPSHOT, "autoMergeRequest": dict(enabledAt="2026-11-21T00:00:00Z", mergeMethod="REBASE")}
POLICY = [dict(type="merge_queue", parameters=dict(merge_method="REBASE", grouping_strategy="ALLGREEN"))]


class MergeReceiptTest(unittest.TestCase):
    def arm(self, before=SNAPSHOT, after=QUEUED, policy=POLICY, error=None):
        self.commands = []
        snapshots = iter([before, after])

        def command(argv, **kwargs):
            self.commands.append(argv)
            if argv[1:3] == ["pr", "view"]:
                return json.dumps(PR)
            if argv[1:3] == ["api", "graphql"]:
                return json.dumps(next(snapshots))
            if argv[1:3] == ["pr", "merge"]:
                if error:
                    raise error
                return ""
            return json.dumps(policy)

        with patch.object(LiveForge, "default_branch", return_value="main"), patch.object(LiveForge, "_run_command", side_effect=command):
            return LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)

    def writes(self):
        return [argv for argv in self.commands if argv[1:3] == ["pr", "merge"]]

    def test_normal_success_requires_observed_receipt_and_policy_method(self):
        receipt = self.arm()
        self.assertEqual((receipt.pr, receipt.source_head, receipt.method, receipt.identity), (12, HEAD, "REBASE", "queue-12"))
        self.assertEqual(receipt.queue_policy.model_dump()["grouping_strategy"], "ALLGREEN")
        self.assertEqual(self.writes(), [["gh", "pr", "merge", "12", "--repo", REPO, "--auto", "--match-head-commit", HEAD]])
        self.assertEqual(self.arm(after=AUTO).status, "AUTO_MERGE_ENABLED")
        with self.assertRaises(MergeNotArmed):
            self.arm(after=SNAPSHOT)

    def test_replay_already_queued_or_auto_enabled_does_not_write(self):
        for snapshot in (QUEUED, AUTO):
            self.assertTrue(self.arm(before=snapshot).identity)
            self.assertEqual(self.writes(), [])

    def test_timeout_confirmation_and_rejected_write(self):
        self.assertEqual(self.arm(error=CommandTimedOut("gh pr merge")).status, "QUEUED")
        for error in (CommandTimedOut("gh pr merge"), CommandFailed("rejected")):
            with self.assertRaises(MergeNotArmed):
                self.arm(after=SNAPSHOT, error=error)

    def test_policy_missing_or_ambiguous_never_writes(self):
        for policy in ([], [dict(type="merge_queue")], POLICY + [dict(type="merge_queue", parameters=dict(merge_method="SQUASH"))]):
            with self.assertRaisesRegex(MergeNotArmed, "policy"):
                self.arm(policy=policy)
            self.assertEqual(self.writes(), [])

    def test_head_base_closure_retarget_draft_and_queue_rejection(self):
        for change in (dict(headRefOid="d"*40), dict(baseRefOid="d"*40), dict(state="CLOSED"),
                       dict(baseRefName="release"), dict(isDraft=True),
                       dict(mergeQueueEntry=dict(id="old-entry", state="QUEUED", headCommit=dict(oid="d"*40))),
                       dict(mergeQueueEntry=dict(id="queue-12", state="UNMERGEABLE", headCommit=dict(oid=HEAD)))):
            with self.subTest(change=change), self.assertRaises(MergeNotArmed):
                self.arm(after={**QUEUED, **change})
            with self.assertRaises(MergeNotArmed):
                self.arm(before={**QUEUED, **change})
            self.assertEqual(self.writes(), [])
