import json
from datetime import UTC, datetime
import subprocess
import unittest
from unittest.mock import call, patch

from pydantic import ValidationError

from foreman.github import CommandTimedOut, ForgeError, LiveForge, MergeNotArmed


HEAD = "a" * 40
REPO = "Example/Target"
BRANCH = "8-list-items"
PR = {
    "number": 12, "state": "OPEN", "headRefOid": HEAD.upper(), "headRefName": "8-list-items",
    "baseRefName": "main", "autoMergeRequest": None,
}
VIEW_FIELDS = [
    "gh", "pr", "view", "--repo", REPO, "--json",
    "number,state,headRefOid,headRefName,baseRefName,autoMergeRequest", "--",
]
VIEW = [*VIEW_FIELDS, BRANCH]
CONFIRM = [*VIEW_FIELDS, "12"]
MERGE = ["gh", "pr", "merge", "12", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]


def gh_result(stdout):
    return subprocess.CompletedProcess([], 0, stdout, "")


def pr_result(pr=PR, command=VIEW):
    return subprocess.CompletedProcess(command, 0, json.dumps(pr), "")


class LiveForgeTest(unittest.TestCase):
    @patch("foreman.github.subprocess.run")
    def test_load_issue_validates_labels_and_normalizes_an_empty_body(self, run):
        run.side_effect = [
            gh_result('{"title":"Track items","body":null,"labels":[{"name":"slice","color":"eeeeee"}]}'),
            gh_result('{"title":"Track items","body":"","labels":[{"name":12}]}'),
        ]
        forge = LiveForge()
        self.assertEqual(forge.load_issue(REPO, 8), {"title": "Track items", "body": "", "labels": ["slice"]})
        with self.assertRaises(ForgeError) as exc:
            forge.load_issue(REPO, 8)
        self.assertIsInstance(exc.exception.__cause__, ValidationError)

    @patch("foreman.github.subprocess.run")
    def test_only_closed_issue_with_merged_closing_pull_request_is_merged(self, run):
        client = LiveForge()
        for closed, refs, expected in (
            (False, [{"number": 3, "state": "MERGED"}], False),
            (True, [], False),
            (True, [{"number": 3, "state": "CLOSED"}], False),
            (True, [{"number": 3, "state": "MERGED"}], True),
        ):
            with self.subTest(closed=closed, refs=refs):
                run.return_value = gh_result(json.dumps({"closed": closed, "closedByPullRequestsReferences": refs}))
                self.assertEqual(client.is_merged("Example/Engine", 1), expected)
        run.side_effect = [
            gh_result('{"closed":true,"closedByPullRequestsReferences":[{"number":3}]}'),
            gh_result('{"state":"MERGED"}'),
        ]
        self.assertTrue(client.is_merged("Example/Engine", 1))
        self.assertEqual(run.call_args.args[0], ["gh", "pr", "view", "3", "--repo", "Example/Engine", "--json", "state"])

    @patch("foreman.github.subprocess.run")
    def test_is_merged_rejects_invalid_issue_and_pr_responses(self, run):
        run.side_effect = [
            gh_result('{"closed":"true","closedByPullRequestsReferences":[]}'),
            gh_result('{"closed":true,"closedByPullRequestsReferences":[{"number":3}]}'),
            gh_result('{"state":"COMPLETED"}'),
        ]
        for _ in range(2):
            with self.assertRaises(ForgeError) as exc:
                LiveForge().is_merged(REPO, 8)
            self.assertIsInstance(exc.exception.__cause__, ValidationError)

    @patch("foreman.github.subprocess.run")
    def test_add_label_preserves_existing_labels(self, run):
        run.return_value = gh_result("")
        LiveForge().add_label("Example/Engine", 2, "factory")
        self.assertEqual(run.call_args.args[0], ["gh", "issue", "edit", "2", "--repo", "Example/Engine", "--add-label", "factory"])

    @patch("foreman.github.subprocess.run")
    def test_create_issue_parses_the_number_and_passes_literal_fields(self, run):
        run.side_effect = [
            gh_result('{"number":42,"html_url":"https://github.com/Example/Target/issues/42"}'),
            gh_result('{"number":"42"}'),
        ]
        forge = LiveForge()
        self.assertEqual(forge.create_issue(REPO, "Plan {repo}", "@draft", ["slice", "factory"]), 42)
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", "repos/Example/Target/issues", "--method", "POST", "-f", "title=Plan {repo}",
            "-f", "body=@draft", "-f", "labels[]=slice", "-f", "labels[]=factory",
        ])
        with self.assertRaises(ForgeError):
            forge.create_issue(REPO, "Plan", "", [])

    @patch("foreman.github.subprocess.run")
    def test_issue_transport_errors_leave_the_boundary_as_forge_errors(self, run):
        stderr = "gh: Not Found (HTTP 404)"
        run.return_value = subprocess.CompletedProcess([], 1, "", stderr)
        with self.assertRaisesRegex(ForgeError, "Not Found") as error:
            LiveForge().load_issue(REPO, 8)
        self.assertNotIsInstance(error.exception, MergeNotArmed)

    @patch("foreman.github.subprocess.run")
    def test_arms_the_checked_pull_request_in_the_requested_repository(self, run):
        run.side_effect = [
            pr_result(),
            subprocess.CompletedProcess(MERGE, 0, "", ""),
        ]
        self.assertIsNone(LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD))
        options = {"stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "timeout": 30}
        self.assertEqual(run.call_args_list, [call(command, **options) for command in (VIEW, MERGE)])

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
                run.side_effect = [pr_result({**PR, field: value})]
                with self.assertRaisesRegex(MergeNotArmed, reason):
                    LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW])

    @patch("foreman.github.subprocess.run")
    def test_invalid_pull_request_output_fails_before_the_merge(self, run):
        for stdout in (
            "{", "[]", json.dumps({key: value for key, value in PR.items() if key != "baseRefName"}),
            json.dumps({**PR, "number": "12"}), json.dumps({**PR, "state": "UNKNOWN"}),
        ):
            with self.subTest(stdout=stdout):
                run.reset_mock()
                run.side_effect = [subprocess.CompletedProcess(VIEW, 0, stdout, "")]
                with self.assertRaisesRegex(MergeNotArmed, "Invalid response from gh pr view"):
                    LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW])

    @patch("foreman.github.subprocess.run")
    def test_pr_lookup_errors_stop_before_any_merge(self, run):
        for branch, reason in (
            ("37-local-only", 'no pull requests found for branch "37-local-only"'),
            (BRANCH, "HTTP 401: Bad credentials (https://api.github.com/graphql)"),
        ):
            with self.subTest(reason=reason):
                view = [*VIEW_FIELDS, branch]
                run.reset_mock()
                run.side_effect = [
                    subprocess.CompletedProcess(view, 1, "", reason + "\n"),
                ]
                with self.assertRaises(MergeNotArmed) as exc:
                    LiveForge().arm_auto_merge(REPO, 8, branch, HEAD)
                self.assertEqual(str(exc.exception), reason)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [view])

    @patch("foreman.github.subprocess.run")
    def test_gh_failures_carry_stderr(self, run):
        reason = "GraphQL: Auto-merge is disabled for this repository (enablePullRequestAutoMerge)"
        run.side_effect = [
            pr_result(),
            subprocess.CompletedProcess(MERGE, 1, "", reason + "\n"),
        ]
        with self.assertRaises(MergeNotArmed) as exc:
            LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
        self.assertEqual(str(exc.exception), reason)

    @patch("foreman.github.subprocess.run")
    def test_wraps_launch_errors_and_view_timeouts(self, run):
        launch_error = FileNotFoundError("gh")
        run.side_effect = [pr_result(), launch_error]
        with self.assertRaisesRegex(MergeNotArmed, "gh pr merge failed") as error:
            LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
        self.assertIs(error.exception.__cause__, launch_error)
        run.reset_mock()
        run.side_effect = subprocess.TimeoutExpired(VIEW, 30)
        with self.assertRaises(CommandTimedOut) as error:
            LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
        self.assertEqual(error.exception.operation, "gh pr view")
        self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW])

    @patch("foreman.github.subprocess.run")
    def test_merge_timeout_requires_a_new_squash_request_at_the_verified_head(self, run):
        started_at = datetime(2026, 11, 20, 15, tzinfo=UTC)
        new_request = {"enabledAt": "2026-11-20T15:00:00Z", "mergeMethod": "SQUASH"}
        older_request = {**new_request, "enabledAt": "2026-11-20T14:59:59Z"}
        wrong_method = {**new_request, "mergeMethod": "MERGE"}

        for previous, reply, armed in (
            (None, pr_result({**PR, "autoMergeRequest": new_request}, CONFIRM), True),
            (None, pr_result(PR, CONFIRM), False),
            (wrong_method, pr_result({**PR, "autoMergeRequest": new_request}, CONFIRM), False),
            (None, pr_result({**PR, "autoMergeRequest": wrong_method}, CONFIRM), False),
            (None, pr_result({**PR, "autoMergeRequest": older_request}, CONFIRM), False),
            (None, pr_result({**PR, "headRefOid": "b" * 40, "autoMergeRequest": new_request}, CONFIRM), False),
            (None, subprocess.CompletedProcess(CONFIRM, 1, "", "HTTP 503: Service Unavailable"), False),
        ):
            with self.subTest(armed=armed, previous=previous, reply=reply):
                run.reset_mock()
                run.side_effect = [
                    pr_result({**PR, "autoMergeRequest": previous}), subprocess.TimeoutExpired(MERGE, 30), reply,
                ]
                with patch("foreman.github.datetime", wraps=datetime) as clock:
                    clock.now.return_value = started_at
                    if armed:
                        self.assertIsNone(LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD))
                    else:
                        with self.assertRaisesRegex(MergeNotArmed, "timed out and was not confirmed"):
                            LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW, MERGE, CONFIRM])
