import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch

from foreman.github import DryRunForge, LiveForge, MergeNotArmed


HEAD = "a" * 40
REPO = "Example/Target"
PR = {"number": 12, "state": "OPEN", "headRefOid": HEAD.upper(), "headRefName": "8-list-items", "baseRefName": "main"}
VIEW = [
    "gh", "pr", "view", "--repo", REPO, "--json", "number,state,headRefOid,headRefName,baseRefName", "",
]
MERGE = ["gh", "pr", "merge", "12", "--repo", REPO, "--auto", "--match-head-commit", HEAD]


class LiveForgeTest(unittest.TestCase):
    @patch("foreman.github.subprocess.check_output")
    def test_only_closed_issue_with_merged_closing_pull_request_is_merged(self, run):
        client = LiveForge()
        for closed, refs, expected in (
            (False, [{"number": 3, "state": "MERGED"}], False),
            (True, [], False),
            (True, [{"number": 3, "state": "CLOSED"}], False),
            (True, [{"number": 3, "state": "MERGED"}], True),
        ):
            with self.subTest(closed=closed, refs=refs):
                run.return_value = json.dumps({"closed": closed, "stateReason": "COMPLETED", "closedByPullRequestsReferences": refs})
                self.assertEqual(client.is_merged("Example/Engine", 1), expected)
        run.side_effect = [json.dumps({"closed": True, "stateReason": "COMPLETED", "closedByPullRequestsReferences": [{"number": 3}]}), json.dumps({"state": "MERGED"})]
        self.assertTrue(client.is_merged("Example/Engine", 1))
        self.assertEqual(run.call_args.args[0], ["gh", "pr", "view", "3", "--repo", "Example/Engine", "--json", "state"])

    @patch("foreman.github.subprocess.check_output")
    def test_add_label_preserves_existing_labels(self, run):
        LiveForge().add_label("Example/Engine", 2, "factory")
        run.assert_called_once_with(["gh", "issue", "edit", "2", "--repo", "Example/Engine", "--add-label", "factory"], text=True)

    @patch("foreman.github.subprocess.run")
    def test_arms_the_checked_pull_request_in_the_requested_repository(self, run):
        run.side_effect = [
            subprocess.CompletedProcess(VIEW, 0, json.dumps(PR), ""),
            subprocess.CompletedProcess(MERGE, 0, "", ""),
        ]
        cwd = Path("checkout")
        LiveForge(cwd).arm_auto_merge(REPO, 8, HEAD)
        options = {"cwd": cwd, "stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "timeout": 30}
        self.assertEqual(run.call_args_list, [call(VIEW, **options), call(MERGE, **options)])

    @patch("foreman.github.subprocess.run")
    def test_rejects_pull_requests_that_fail_a_merge_check(self, run):
        for field, value, reason in (
            ("state", "CLOSED", "pull request is not open"),
            ("state", "MERGED", "pull request is not open"),
            ("headRefOid", "b" * 40, "head differs"),
            ("headRefName", "80-list-items", "must start with 8-"),
            ("baseRefName", "release", "must target main"),
        ):
            with self.subTest(field=field, value=value):
                run.reset_mock()
                run.return_value = subprocess.CompletedProcess(VIEW, 0, json.dumps({**PR, field: value}), "")
                with self.assertRaisesRegex(MergeNotArmed, reason):
                    LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW])

    @patch("foreman.github.subprocess.run")
    def test_invalid_pull_request_output_fails_before_the_merge(self, run):
        run.return_value = subprocess.CompletedProcess(VIEW, 0, "{", "")
        with self.assertRaisesRegex(MergeNotArmed, "Invalid response from gh pr view"):
            LiveForge().arm_auto_merge(REPO, 8, HEAD)
        run.assert_called_once()

    @patch("foreman.github.subprocess.run")
    def test_gh_failures_carry_stderr(self, run):
        reason = "GraphQL: Auto-merge is disabled for this repository (enablePullRequestAutoMerge)"
        run.side_effect = [
            subprocess.CompletedProcess(VIEW, 0, json.dumps(PR), ""),
            subprocess.CompletedProcess(MERGE, 1, "", reason + "\n"),
        ]
        with self.assertRaises(MergeNotArmed) as exc:
            LiveForge().arm_auto_merge(REPO, 8, HEAD)
        self.assertEqual(str(exc.exception), reason)

    @patch("foreman.github.subprocess.run")
    def test_wraps_launch_errors_and_timeouts(self, run):
        for error in (FileNotFoundError("gh"), subprocess.TimeoutExpired(MERGE, 30)):
            with self.subTest(error=error):
                run.side_effect = [subprocess.CompletedProcess(VIEW, 0, json.dumps(PR), ""), error]
                with self.assertRaisesRegex(MergeNotArmed, "gh pr merge failed") as exc:
                    LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertIs(exc.exception.__cause__, error)


class DryRunForgeTest(unittest.TestCase):
    @patch("foreman.github.subprocess.run")
    def test_records_a_merge_command_without_executing_it(self, run):
        forge = DryRunForge()
        forge.arm_auto_merge(REPO, None, None)
        self.assertEqual(forge.merge_commands, [
            ["gh", "pr", "merge", "<pull-request>", "--repo", REPO, "--auto", "--match-head-commit", "<verified-sha>"],
        ])
        run.assert_not_called()
