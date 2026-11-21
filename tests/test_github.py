import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from pydantic import ValidationError

from foreman.github import DryRunForge, LiveForge, MergeNotArmed


HEAD = "a" * 40
REPO = "Example/Target"
BRANCH = ["git", "branch", "--show-current"]
BRANCH_RESULT = subprocess.CompletedProcess(BRANCH, 0, "8-list-items\n", "")
PR = {
    "number": 12, "state": "OPEN", "headRefOid": HEAD.upper(), "headRefName": "8-list-items",
    "baseRefName": "main", "autoMergeRequest": None,
}
VIEW_FIELDS = [
    "gh", "pr", "view", "--repo", REPO, "--json",
    "number,state,headRefOid,headRefName,baseRefName,autoMergeRequest", "--",
]
VIEW = [*VIEW_FIELDS, "8-list-items"]
CONFIRM = [*VIEW_FIELDS, "12"]
MERGE = ["gh", "pr", "merge", "12", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]


def pr_result(pr=PR, command=VIEW):
    return subprocess.CompletedProcess(command, 0, json.dumps(pr), "")


class LiveForgeTest(unittest.TestCase):
    @patch("foreman.github.subprocess.check_output")
    def test_load_issue_validates_labels_and_normalizes_an_empty_body(self, run):
        run.side_effect = [
            '{"title":"Track items","body":null,"labels":[{"name":"slice","color":"eeeeee"}]}',
            '{"title":"Track items","body":"","labels":[{"name":12}]}',
        ]
        forge = LiveForge()
        self.assertEqual(forge.load_issue(REPO, 8), {"title": "Track items", "body": "", "labels": ["slice"]})
        with self.assertRaises(ValidationError):
            forge.load_issue(REPO, 8)

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
    def test_is_merged_rejects_invalid_issue_and_pr_responses(self, run):
        run.side_effect = [
            '{"closed":"true","closedByPullRequestsReferences":[]}',
            '{"closed":true,"closedByPullRequestsReferences":[{"number":3}]}',
            '{"state":"COMPLETED"}',
        ]
        for _ in range(2):
            with self.assertRaises(ValidationError):
                LiveForge().is_merged(REPO, 8)

    @patch("foreman.github.subprocess.check_output")
    def test_add_label_preserves_existing_labels(self, run):
        LiveForge().add_label("Example/Engine", 2, "factory")
        run.assert_called_once_with(["gh", "issue", "edit", "2", "--repo", "Example/Engine", "--add-label", "factory"], text=True)

    @patch("foreman.github.subprocess.run")
    def test_arms_the_checked_pull_request_in_the_requested_repository(self, run):
        run.side_effect = [
            BRANCH_RESULT,
            pr_result(),
            subprocess.CompletedProcess(MERGE, 0, "", ""),
        ]
        cwd = Path("checkout")
        self.assertEqual(LiveForge(cwd).arm_auto_merge(REPO, 8, HEAD), "armed")
        options = {"cwd": cwd, "stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "timeout": 30}
        self.assertEqual(run.call_args_list, [call(command, **options) for command in (BRANCH, VIEW, MERGE)])

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
                run.side_effect = [BRANCH_RESULT, pr_result({**PR, field: value})]
                with self.assertRaisesRegex(MergeNotArmed, reason):
                    LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [BRANCH, VIEW])

    @patch("foreman.github.subprocess.run")
    def test_invalid_pull_request_output_fails_before_the_merge(self, run):
        for stdout in (
            "{", "[]", json.dumps({key: value for key, value in PR.items() if key != "baseRefName"}),
            json.dumps({**PR, "number": "12"}), json.dumps({**PR, "state": "UNKNOWN"}),
        ):
            with self.subTest(stdout=stdout):
                run.reset_mock()
                run.side_effect = [BRANCH_RESULT, subprocess.CompletedProcess(VIEW, 0, stdout, "")]
                with self.assertRaisesRegex(MergeNotArmed, "Invalid response from gh pr view"):
                    LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [BRANCH, VIEW])

    @patch("foreman.github.subprocess.run")
    def test_pr_lookup_errors_stop_before_any_merge(self, run):
        for branch, reason in (
            ("37-local-only", 'no pull requests found for branch "37-local-only"'),
            ("8-list-items", "could not determine current branch: failed to run git: not on any branch"),
        ):
            with self.subTest(reason=reason):
                view = [*VIEW_FIELDS, branch]
                run.reset_mock()
                run.side_effect = [
                    subprocess.CompletedProcess(BRANCH, 0, branch + "\n", ""),
                    subprocess.CompletedProcess(view, 1, "", reason + "\n"),
                ]
                with self.assertRaises(MergeNotArmed) as exc:
                    LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertEqual(str(exc.exception), reason)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [BRANCH, view])

    def test_detached_head_stops_before_consulting_github(self):
        with TemporaryDirectory() as directory:
            cwd = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Fixture", "--allow-empty"],
                cwd=cwd, check=True,
            )
            subprocess.run(["git", "switch", "--detach", "-q"], cwd=cwd, check=True)
            branch_result = subprocess.run(BRANCH, cwd=cwd, check=True, capture_output=True, text=True)
            with patch("foreman.github.subprocess.run", return_value=branch_result) as run:
                with self.assertRaisesRegex(MergeNotArmed, "HEAD is detached"):
                    LiveForge(cwd).arm_auto_merge(REPO, 8, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [BRANCH])

    @patch("foreman.github.subprocess.run")
    def test_gh_failures_carry_stderr(self, run):
        reason = "GraphQL: Auto-merge is disabled for this repository (enablePullRequestAutoMerge)"
        run.side_effect = [
            BRANCH_RESULT, pr_result(),
            subprocess.CompletedProcess(MERGE, 1, "", reason + "\n"),
        ]
        with self.assertRaises(MergeNotArmed) as exc:
            LiveForge().arm_auto_merge(REPO, 8, HEAD)
        self.assertEqual(str(exc.exception), reason)

    @patch("foreman.github.subprocess.run")
    def test_wraps_launch_errors_and_view_timeouts(self, run):
        for completed, error, command in (
            ([BRANCH_RESULT, pr_result()], FileNotFoundError("gh"), "gh pr merge"),
            ([BRANCH_RESULT], subprocess.TimeoutExpired(VIEW, 30), "gh pr view"),
        ):
            with self.subTest(command=command):
                run.side_effect = [*completed, error]
                with self.assertRaisesRegex(MergeNotArmed, f"{command} failed") as exc:
                    LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertIs(exc.exception.__cause__, error)

    @patch("foreman.github.subprocess.run")
    def test_merge_timeout_checks_the_same_pr_for_an_auto_merge_request(self, run):
        request = {"enabledAt": "2026-11-20T15:00:00Z", "mergeMethod": "SQUASH"}
        for reply, armed in (
            (pr_result({**PR, "autoMergeRequest": request}, CONFIRM), True),
            (pr_result(PR, CONFIRM), False),
            (pr_result({**PR, "headRefOid": "b" * 40, "autoMergeRequest": request}, CONFIRM), False),
            (subprocess.CompletedProcess(CONFIRM, 1, "", "HTTP 503: Service Unavailable"), False),
        ):
            with self.subTest(armed=armed, reply=reply):
                run.reset_mock()
                run.side_effect = [BRANCH_RESULT, pr_result(), subprocess.TimeoutExpired(MERGE, 30), reply]
                if armed:
                    self.assertEqual(LiveForge().arm_auto_merge(REPO, 8, HEAD), "armed")
                else:
                    with self.assertRaisesRegex(MergeNotArmed, "timed out and was not confirmed"):
                        LiveForge().arm_auto_merge(REPO, 8, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [BRANCH, VIEW, MERGE, CONFIRM])


class DryRunForgeTest(unittest.TestCase):
    @patch("foreman.github.subprocess.run")
    def test_records_a_merge_command_without_executing_it(self, run):
        forge = DryRunForge()
        self.assertEqual(forge.arm_auto_merge(REPO, None, None), "skipped")
        self.assertEqual(forge.merge_commands, [
            ["gh", "pr", "merge", "<number>", "--repo", REPO, "--squash", "--auto", "--match-head-commit", "<sha>"],
        ])
        run.assert_not_called()

    @patch("foreman.github.subprocess.run")
    def test_dry_run_can_create_read_and_label_slices(self, run):
        forge = DryRunForge(
            issues={(REPO, 1): {"title": "List items", "body": "Show the whole list.", "labels": ["factory"]}},
            merged={(REPO, 1)},
        )
        child = forge.create_issue(REPO, "Build list", "One row per item.", ["slice"])
        forge.add_label(REPO, child, "factory")
        forge.add_label(REPO, child, "factory")
        self.assertEqual(forge.load_issue(REPO, child), {
            "title": "Build list", "body": "One row per item.", "labels": ["slice", "factory"],
        })
        self.assertTrue(forge.is_merged(REPO, 1))
        self.assertFalse(forge.is_merged(REPO, child))
        run.assert_not_called()
