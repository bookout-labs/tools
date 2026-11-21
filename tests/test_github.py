import json
import subprocess
import unittest
from unittest.mock import call, patch

from pydantic import ValidationError

from foreman.github import (
    CommandFailed, CommandTimedOut, ForgeError, LiveForge, MergeHoldUnreadable, MergeNotArmed, ReviewComment,
)


HEAD = "a" * 40
BASE = "b" * 40
REPO = "Example/Target"
BRANCH = "8-list-items"
CLOSING_PR = {
    "id": "PR_synthetic", "number": 3, "repository": {"name": "Closing", "owner": {"login": "Other"}},
    "url": "https://github.com/Other/Closing/pull/3",
}
PR = {
    "number": 12, "state": "OPEN", "headRefOid": HEAD.upper(), "headRefName": "8-list-items",
    "baseRefName": "main", "baseRefOid": BASE, "autoMergeRequest": None,
}
VIEW_FIELDS = [
    "gh", "pr", "view", "--repo", REPO, "--json",
    "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isCrossRepository,autoMergeRequest", "--",
]
VIEW = [*VIEW_FIELDS, BRANCH]
CONFIRM = [*VIEW_FIELDS, "12"]
MERGE = ["gh", "pr", "merge", "12", "--repo", REPO, "--squash", "--auto", "--match-head-commit", HEAD]


def gh_result(stdout):
    return subprocess.CompletedProcess([], 0, stdout, "")


def pr_result(pr=PR, command=VIEW):
    return subprocess.CompletedProcess(command, 0, json.dumps(pr), "")


class LiveForgeTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(LiveForge, "default_branch", return_value="main"))

    @patch("foreman.github.subprocess.run")
    def test_pull_request_base_uses_the_forge_base_ref(self, run):
        run.return_value = pr_result({**PR, "baseRefName": "release/current"})
        self.assertEqual(LiveForge().pull_request_base(REPO, BRANCH), "release/current")
        self.assertEqual(run.call_args.args[0], VIEW)

    @patch("foreman.github.subprocess.run")
    def test_merge_hold_reads_the_requested_repository_variable(self, run):
        for stdout, hold in (
            ('[{"name":"REGION","value":"test"},{"name":"FOREMAN_MERGE_HOLD","value":"  Maintenance\\n"}]', "  Maintenance\n"),
            ('[{"name":"FOREMAN_MERGE_HOLD","value":""}]', ""),
            ('[{"name":"REGION","value":"test"}]', None),
            ("[]", None),
        ):
            with self.subTest(hold=hold):
                run.return_value = gh_result(stdout)
                self.assertEqual(LiveForge().merge_hold(REPO), hold)
                self.assertEqual(run.call_args.args[0], [
                    "gh", "variable", "list", "--repo", REPO, "--json", "name,value",
                ])

    @patch("foreman.github.subprocess.run")
    def test_merge_hold_fails_closed_on_command_and_validation_errors(self, run):
        reason = "failed to get variables: HTTP 404: Not Found (https://api.github.com/repos/Example/Missing/actions/variables?per_page=100)"
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", reason),
            gh_result('[{"name":"FOREMAN_MERGE_HOLD","value":123}]'),
            subprocess.TimeoutExpired(["gh", "variable", "list"], 30),
        ]
        with self.assertRaises(MergeHoldUnreadable) as error:
            LiveForge().merge_hold("Example/Missing")
        self.assertEqual(str(error.exception), reason)
        self.assertIsInstance(error.exception.__cause__, CommandFailed)
        with self.assertRaisesRegex(MergeHoldUnreadable, "Invalid ActionsVariables response"):
            LiveForge().merge_hold(REPO)
        with self.assertRaisesRegex(MergeHoldUnreadable, "gh variable list timed out"):
            LiveForge().merge_hold(REPO)

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
        for closed, references, state, base, default, expected in (
            (False, [CLOSING_PR], None, None, None, False),
            (True, [], None, None, None, False),
            (True, [CLOSING_PR], "CLOSED", "main", None, False),
            (True, [CLOSING_PR], "MERGED", "main", "main", True),
            (True, [CLOSING_PR], "MERGED", "release", "main", False),
            (True, [CLOSING_PR], "MERGED", "trunk", "trunk", True),
        ):
            with self.subTest(closed=closed, state=state, base=base, default=default):
                run.reset_mock()
                issue = {"closed": closed, "closedByPullRequestsReferences": references}
                replies = [gh_result(json.dumps(issue))]
                commands = [
                    ["gh", "issue", "view", "1", "--repo", REPO, "--json", "closed,closedByPullRequestsReferences"],
                ]
                if state is not None:
                    replies.append(gh_result(json.dumps({"state": state, "baseRefName": base})))
                    commands.append(["gh", "pr", "view", "3", "--repo", "Other/Closing", "--json", "state,baseRefName"])
                if default is not None:
                    replies.append(gh_result(json.dumps({"defaultBranchRef": {"name": default}})))
                    commands.append(["gh", "repo", "view", "Other/Closing", "--json", "defaultBranchRef"])
                run.side_effect = replies
                self.assertEqual(LiveForge().is_merged(REPO, 1), expected)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], commands)

    @patch("foreman.github.subprocess.run")
    def test_is_merged_rejects_invalid_issue_and_pr_responses(self, run):
        run.side_effect = [
            gh_result('{"closed":"true","closedByPullRequestsReferences":[]}'),
            gh_result('{"closed":true,"closedByPullRequestsReferences":[{"number":3}]}'),
            gh_result(json.dumps({"closed": True, "closedByPullRequestsReferences": [CLOSING_PR]})),
            gh_result('{"state":"COMPLETED"}'),
        ]
        for _ in range(3):
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
        self.assertIsNone(run.call_args.kwargs["timeout"])
        with self.assertRaises(ForgeError):
            forge.create_issue(REPO, "Plan", "", [])
        self.assertIsNone(run.call_args.kwargs["timeout"])

    @patch("foreman.github.subprocess.run")
    def test_issue_transport_errors_leave_the_boundary_as_forge_errors(self, run):
        stderr = "gh: Not Found (HTTP 404)"
        run.return_value = subprocess.CompletedProcess([], 1, "", stderr)
        with self.assertRaisesRegex(CommandFailed, "Not Found") as error:
            LiveForge().load_issue(REPO, 8)
        self.assertNotIsInstance(error.exception, MergeNotArmed)

    @patch("foreman.github.subprocess.run")
    def test_native_blockers_use_github_issue_ids_and_read_every_page(self, run):
        blocker = {"id": 643, "number": 9, "title": "Build items",
                   "repository_url": f"https://api.github.com/repos/{REPO}",
                   "html_url": f"https://github.com/{REPO}/issues/9", "state": "open"}
        run.side_effect = [gh_result(json.dumps(blocker)), gh_result(json.dumps(blocker)),
                           gh_result(json.dumps([[blocker], [{**blocker, "id": 644, "number": 10, "state": "closed"}]])),
                           gh_result(json.dumps([[{**blocker, "state": "unknown"}]]))]
        forge = LiveForge()
        forge.add_blocker(REPO, 8, 9)
        self.assertEqual([(item.number, item.state) for item in forge.blockers(REPO, 8)], [(9, "open"), (10, "closed")])
        endpoint = f"repos/{REPO}/issues/8/dependencies/blocked_by"
        self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [
            ["gh", "api", f"repos/{REPO}/issues/9"],
            ["gh", "api", endpoint, "--method", "POST", "-F", "issue_id=643"],
            ["gh", "api", endpoint, "--paginate", "--slurp"],
        ])
        with self.assertRaises(ForgeError):
            forge.blockers(REPO, 8)

    @patch("foreman.github.subprocess.run")
    def test_review_write_redacts_synthetic_secrets_before_json_encoding(self, run):
        synthetic_token = "ghp_" + "a" * 36
        run.side_effect = [pr_result(), pr_result(), gh_result("")]
        forge = LiveForge()
        target = forge.review_target(REPO, 8, BRANCH, HEAD)
        forge.post_review(REPO, target, f"Synthetic {synthetic_token}", [{
            "path": f"docs/{synthetic_token}.txt", "line": 2, "side": "RIGHT",
            "body": f'Synthetic password="abc" and abc {synthetic_token}',
        }])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["commit_id"], HEAD.upper())
        self.assertEqual(payload["body"], "Synthetic [redacted:GitHub Token]")
        self.assertEqual(payload["comments"][0]["path"], "docs/[redacted:GitHub Token].txt")
        self.assertEqual(payload["comments"][0]["body"],
                         'Synthetic password="[redacted:Secret Keyword]" and abc [redacted:GitHub Token]')

    @patch("foreman.github.subprocess.run")
    def test_posts_the_review_and_inline_comments_for_the_verified_head(self, run):
        moved = {**PR, "baseRefOid": "c" * 40, "headRefOid": "d" * 40}
        run.side_effect = [pr_result(), pr_result(moved, CONFIRM), gh_result("")]
        forge = LiveForge()
        target = forge.review_target(REPO, 8, BRANCH, HEAD)
        comments: list[ReviewComment] = [{"path": "items.txt", "line": 2, "side": "RIGHT", "body": "[nit] Use @literal."}]
        forge.post_review(REPO, target, "Longer review detail.", comments)
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", "repos/Example/Target/pulls/12/reviews", "--method", "POST",
            "-H", "X-GitHub-Api-Version: 2022-11-28", "--input", "-",
        ])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {
            "commit_id": HEAD.upper(), "event": "COMMENT", "body": "Longer review detail.", "comments": comments,
        })

    @patch("foreman.github.subprocess.run")
    def test_review_does_not_post_if_the_pull_request_closes_or_is_retargeted(self, run):
        for field, value in (("state", "CLOSED"), ("baseRefName", "release")):
            with self.subTest(field=field, value=value):
                run.reset_mock()
                run.side_effect = [pr_result(), pr_result({**PR, field: value}, CONFIRM)]
                forge = LiveForge()
                target = forge.review_target(REPO, 8, BRANCH, HEAD)
                with self.assertRaisesRegex(ForgeError, "changed during review"):
                    forge.post_review(REPO, target, "Longer review detail.", [])
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW, CONFIRM])

    @patch("foreman.github.subprocess.run")
    def test_arms_the_checked_pull_request_in_the_requested_repository(self, run):
        run.side_effect = [
            pr_result(),
            subprocess.CompletedProcess(MERGE, 0, "", ""),
        ]
        self.assertIsNone(LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD))
        options = {"stdin": subprocess.DEVNULL, "input": None, "capture_output": True, "text": True, "timeout": 30}
        self.assertEqual(run.call_args_list, [call(command, **options) for command in (VIEW, MERGE)])

    @patch("foreman.github.subprocess.run")
    def test_rejects_pull_requests_that_fail_a_merge_check(self, run):
        for field, value, reason in (
            ("state", "CLOSED", "pull request is not open"),
            ("state", "MERGED", "pull request is not open"),
            ("headRefOid", "b" * 40, "head differs"),
            ("headRefName", "80-list-items", "must start with 8-"),
            ("baseRefName", "release", "must target the repository default branch"),
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
                with self.assertRaisesRegex(MergeNotArmed, "Invalid PullRequest response") as exc:
                    LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
                self.assertIs(type(exc.exception.__cause__), ForgeError)
                self.assertIsInstance(exc.exception.__cause__.__cause__, ValidationError)
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
        self.assertIsInstance(error.exception.__cause__, CommandFailed)
        self.assertIs(error.exception.__cause__.__cause__, launch_error)
        run.reset_mock()
        run.side_effect = subprocess.TimeoutExpired(VIEW, 30)
        with self.assertRaisesRegex(MergeNotArmed, "gh pr view timed out") as error:
            LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
        self.assertIsInstance(error.exception.__cause__, CommandTimedOut)
        self.assertEqual(error.exception.__cause__.operation, "gh pr view")
        self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW])

    @patch("foreman.github.subprocess.run")
    def test_merge_timeout_confirms_an_open_squash_request_or_merge_at_the_verified_head(self, run):
        squash_request = {"enabledAt": "2026-11-20T15:00:00Z", "mergeMethod": "SQUASH"}
        wrong_method = {**squash_request, "mergeMethod": "MERGE"}

        for previous, reply, armed in (
            (None, pr_result({**PR, "autoMergeRequest": squash_request}, CONFIRM), True),
            (None, pr_result(PR, CONFIRM), False),
            (wrong_method, pr_result({**PR, "autoMergeRequest": squash_request}, CONFIRM), True),
            (None, pr_result({**PR, "autoMergeRequest": wrong_method}, CONFIRM), False),
            (squash_request, pr_result({**PR, "autoMergeRequest": squash_request}, CONFIRM), True),
            (None, pr_result({**PR, "state": "CLOSED", "autoMergeRequest": squash_request}, CONFIRM), False),
            (None, pr_result({**PR, "state": "MERGED"}, CONFIRM), True),
            (None, pr_result({**PR, "state": "MERGED", "headRefOid": "b" * 40}, CONFIRM), False),
            (None, pr_result({**PR, "headRefOid": "b" * 40, "autoMergeRequest": squash_request}, CONFIRM), False),
        ):
            with self.subTest(armed=armed, previous=previous, reply=reply):
                run.reset_mock()
                run.side_effect = [
                    pr_result({**PR, "autoMergeRequest": previous}), subprocess.TimeoutExpired(MERGE, 30), reply,
                ]
                if armed:
                    self.assertIsNone(LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD))
                else:
                    with self.assertRaisesRegex(MergeNotArmed, "timed out and was not confirmed"):
                        LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW, MERGE, CONFIRM])

    @patch("foreman.github.subprocess.run")
    def test_merge_timeout_preserves_failed_confirmation_read_message(self, run):
        reason = "HTTP 503: Service Unavailable"
        run.side_effect = [
            pr_result(), subprocess.TimeoutExpired(MERGE, 30), subprocess.CompletedProcess(CONFIRM, 1, "", reason),
        ]
        with self.assertRaises(MergeNotArmed) as exc:
            LiveForge().arm_auto_merge(REPO, 8, BRANCH, HEAD)
        self.assertEqual(str(exc.exception), reason)
        self.assertIsInstance(exc.exception.__cause__, CommandFailed)
