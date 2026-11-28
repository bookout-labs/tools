import tests

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import call, patch

from pydantic import ValidationError

from foreman.redaction import redact
from foreman.stage_output import spec_digest
from foreman.github import (
    CommandFailed, CommandTimedOut, ForgeError, LinkedIssue, LiveForge, VariablesUnreadable, MergeNotArmed, ReviewComment,
)
from test_runner import FakeGitHub


HEAD = "a" * 40
BASE = "b" * 40
REPO = "Example/Target"
GITHUB_FIXTURES = Path(__file__).with_name("fixtures") / "github"
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
MERGE = ["gh", "pr", "merge", "12", "--repo", REPO, "--auto", "--squash", "--match-head-commit", HEAD]


def gh_result(stdout):
    return subprocess.CompletedProcess([], 0, stdout, "")


def pr_result(pr=PR, command=VIEW):
    return subprocess.CompletedProcess(command, 0, json.dumps(pr), "")


class WriteInterrupted(BaseException):
    pass


class FakeGitHubTest(unittest.TestCase):
    def test_bodies_survive_updates_and_markers_include_closed_and_merged_issues(self):
        forge = FakeGitHub()
        first = forge.create_issue(REPO, "First", "marker old", [], marker="")
        second = forge.create_issue(REPO, "Second", "marker done", [], marker="")
        third = forge.create_issue(REPO, "Third", "marker merged", [], marker="")
        forge.create_issue("Other/Repo", "Elsewhere", "marker", [], marker="")
        forge.closed.add(second)
        forge.merged.add(third)
        forge.update_issue_body(REPO, first, "replaced")
        forge.update_issue_body(REPO, 8, "marker root")
        self.assertEqual(forge.load_issue(REPO, first)["body"], "replaced")
        self.assertEqual(forge.load_issue(REPO, 8)["body"], "marker root")
        self.assertEqual(forge.marked_issues(REPO, "marker"), [102, 103, 8])
        self.assertEqual(forge.marked_issues(REPO, "absent"), [])

    def test_relationships_preserve_identity_state_and_one_parent_per_child(self):
        forge = FakeGitHub()
        child = forge.create_issue(REPO, "Child", "", [], marker="")
        self.assertIsNone(forge.parent_issue(REPO, child))
        forge.add_sub_issue(REPO, 8, child)
        forge.closed.add(child)
        self.assertEqual([(issue.id, issue.number, issue.state) for issue in forge.sub_issues(REPO, 8)],
                         [(10101, 101, "closed")])
        self.assertEqual(forge.parent_issue(REPO, child).number, 8)
        for parent in (8, 9):
            with self.subTest(parent=parent), self.assertRaises(ForgeError):
                forge.add_sub_issue(REPO, parent, child)
        self.assertEqual(forge.parent_issue(REPO, child).number, 8)
        self.assertEqual(forge.sub_issues(REPO, 9), [])
        self.assertIsNone(forge.parent_issue("Other/Repo", child))

    def test_preset_conflicts_and_child_counts_are_visible_and_enforced(self):
        forge = FakeGitHub()
        forge.parents[REPO, 9] = 7
        forge.preset_sub_issues(REPO, 8, 99)
        self.assertEqual(forge.parent_issue(REPO, 9).number, 7)
        with self.assertRaises(ForgeError):
            forge.add_sub_issue(REPO, 8, 9)
        forge.add_sub_issue(REPO, 8, 10)
        self.assertEqual(len(forge.sub_issues(REPO, 8)), 100)
        with self.assertRaises(ForgeError):
            forge.add_sub_issue(REPO, 8, 11)
        self.assertIsNone(forge.parent_issue(REPO, 11))
        forge.preset_sub_issues(REPO, 7, 2)
        self.assertEqual(len(forge.sub_issues(REPO, 7)), 3)
        self.assertEqual(len(forge.sub_issues(REPO, 8)), 100)

    def test_injected_failures_expose_whether_each_write_committed(self):
        for operation in ("create_issue", "add_sub_issue", "update_issue_body"):
            for after_write in (False, True):
                for error_type in (ForgeError, WriteInterrupted):
                    with self.subTest(operation=operation, after_write=after_write, error=error_type):
                        forge = FakeGitHub()
                        error = error_type("Interrupted")
                        forge.fail_next(operation, after_write=after_write, error=error)
                        with self.assertRaises(error_type) as caught:
                            if operation == "create_issue":
                                forge.create_issue(REPO, "Child", "marker", [], marker="")
                            elif operation == "add_sub_issue":
                                forge.add_sub_issue(REPO, 8, 9)
                            else:
                                forge.update_issue_body(REPO, 8, "marker")
                        self.assertIs(caught.exception, error)
                        if operation == "create_issue":
                            self.assertEqual(forge.marked_issues(REPO, "marker"), [101] if after_write else [])
                            if after_write:
                                self.assertEqual(forge.load_issue(REPO, 101)["body"], "marker")
                        elif operation == "add_sub_issue":
                            self.assertEqual([child.number for child in forge.sub_issues(REPO, 8)],
                                             [9] if after_write else [])
                            parent = forge.parent_issue(REPO, 9)
                            self.assertEqual(parent.number if parent else None, 8 if after_write else None)
                        else:
                            self.assertEqual(forge.load_issue(REPO, 8)["body"], "marker" if after_write else forge.body)
                        # A consumed failure must not poison the next write.
                        if operation == "create_issue":
                            self.assertEqual(forge.create_issue(REPO, "Next", "marker", [], marker=""), 102 if after_write else 101)
                        elif operation == "add_sub_issue":
                            forge.add_sub_issue(REPO, 8, 10)
                            self.assertEqual(forge.parent_issue(REPO, 10).number, 8)
                        else:
                            forge.update_issue_body(REPO, 8, "next")
                            self.assertEqual(forge.load_issue(REPO, 8)["body"], "next")


class LiveForgeTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(LiveForge, "default_branch", return_value="main"))

    @patch("foreman.github.subprocess.run")
    def test_variables_read_the_requested_repository(self, run):
        for stdout, variables in (
            ('[{"name":"REGION","value":"test"},{"name":"FOREMAN_MERGE_HOLD","value":"  Maintenance\\n"}]',
             {"REGION": "test", "FOREMAN_MERGE_HOLD": "  Maintenance\n"}),
            ('[{"name":"FOREMAN_MERGE_HOLD","value":""}]', {"FOREMAN_MERGE_HOLD": ""}),
            ("[]", {}),
        ):
            with self.subTest(variables=variables):
                run.return_value = gh_result(stdout)
                self.assertEqual(LiveForge().variables(REPO), variables)
                self.assertEqual(run.call_args.args[0], [
                    "gh", "variable", "list", "--repo", REPO, "--json", "name,value",
                ])

    @patch("foreman.github.subprocess.run")
    def test_variables_fail_closed_on_command_and_validation_errors(self, run):
        reason = "failed to get variables: HTTP 404: Not Found (https://api.github.com/repos/Example/Missing/actions/variables?per_page=100)"
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", reason),
            gh_result('[{"name":"FOREMAN_MERGE_HOLD","value":123}]'),
            subprocess.TimeoutExpired(["gh", "variable", "list"], 30),
        ]
        with self.assertRaises(VariablesUnreadable) as error:
            LiveForge().variables("Example/Missing")
        self.assertEqual(str(error.exception), reason)
        self.assertIsInstance(error.exception.__cause__, CommandFailed)
        with self.assertRaisesRegex(VariablesUnreadable, "Invalid ActionsVariables response"):
            LiveForge().variables(REPO)
        with self.assertRaisesRegex(VariablesUnreadable, "gh variable list timed out"):
            LiveForge().variables(REPO)

    @patch("foreman.github.subprocess.run")
    def test_app_variables_permission_denial_is_fail_closed(self, run):
        with patch("foreman.github.credentials.environment", return_value={"GH_TOKEN": "synthetic"}):
            run.return_value = subprocess.CompletedProcess([], 1, "", "HTTP 403")
            with self.assertRaisesRegex(VariablesUnreadable, "permission denied"):
                LiveForge().variables(REPO)

    @patch("foreman.github.subprocess.run")
    def test_load_issue_validates_labels_and_normalizes_an_empty_body(self, run):
        run.side_effect = [
            gh_result(json.dumps({"number": 8, "repository_url": f"https://api.github.com/repos/{REPO}",
                                  "title": "Track items", "body": None, "labels": [{"name": "slice", "color": "eeeeee"}],
                                  "user": {"login": "octocat"}})),
            gh_result(json.dumps({"number": 8, "repository_url": f"https://api.github.com/repos/{REPO}",
                                  "title": "Track items", "body": "", "labels": [{"name": 12}]})),
        ]
        forge = LiveForge()
        self.assertEqual(forge.load_issue(REPO, 8), {"repo": REPO, "number": 8, "title": "Track items", "body": "", "labels": ["slice"],
                                                    "author": "octocat"})
        with self.assertRaises(ForgeError) as exc:
            forge.load_issue(REPO, 8)
        self.assertIsInstance(exc.exception.__cause__, ValidationError)

    @patch("foreman.github.subprocess.run")
    def test_an_issue_counts_merged_once_its_closing_pull_request_merged(self, run):
        for reason, references, state, base, default, expected in (
            # GitHub closes the issue after the merge, so an open issue with a merged closing pull request counts.
            ("", [CLOSING_PR], "MERGED", "main", "main", True),
            ("", [CLOSING_PR], "OPEN", "main", None, False),
            # A reopened issue is unfinished, so its earlier merged pull request no longer counts.
            ("REOPENED", [CLOSING_PR], None, None, None, False),
            ("COMPLETED", [], None, None, None, False),
            ("COMPLETED", [CLOSING_PR], "CLOSED", "main", None, False),
            ("COMPLETED", [CLOSING_PR], "MERGED", "main", "main", True),
            ("COMPLETED", [CLOSING_PR], "MERGED", "release", "main", False),
            ("COMPLETED", [CLOSING_PR], "MERGED", "trunk", "trunk", True),
        ):
            with self.subTest(reason=reason, state=state, base=base, default=default):
                run.reset_mock()
                issue = {"stateReason": reason, "closedByPullRequestsReferences": references}
                replies = [gh_result(json.dumps(issue))]
                commands = [
                    ["gh", "issue", "view", "1", "--repo", REPO, "--json", "stateReason,closedByPullRequestsReferences"],
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
    def test_pinned_merge_evidence_comes_from_the_target_repository(self, run):
        local_pr = {**CLOSING_PR, "repository": {"name": REPO.split("/")[1], "owner": {"login": REPO.split("/")[0]}}}
        merge = "a" * 40
        run.side_effect = [
            gh_result(json.dumps({"stateReason": "COMPLETED", "closedByPullRequestsReferences": [CLOSING_PR, local_pr]})),
            gh_result(json.dumps({"state": "MERGED", "baseRefName": "main", "mergeCommit": {"oid": merge}})),
            gh_result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        ]
        seen = []
        self.assertFalse(LiveForge().is_merged(REPO, 8, contains=lambda commit: seen.append(commit) or False))
        self.assertEqual(seen, [merge])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["gh", "pr", "view", "3", "--repo", REPO, "--json", "state,baseRefName,mergeCommit"])

    @patch("foreman.github.subprocess.run")
    def test_is_merged_rejects_invalid_issue_and_pr_responses(self, run):
        run.side_effect = [
            gh_result('{"stateReason":"reopened","closedByPullRequestsReferences":[]}'),
            gh_result('{"stateReason":"COMPLETED","closedByPullRequestsReferences":[{"number":3}]}'),
            gh_result(json.dumps({"stateReason": "COMPLETED", "closedByPullRequestsReferences": [CLOSING_PR]})),
            gh_result('{"state":"COMPLETED"}'),
        ]
        for _ in range(3):
            with self.assertRaises(ForgeError) as exc:
                LiveForge().is_merged(REPO, 8)
            self.assertIsInstance(exc.exception.__cause__, ValidationError)

    @patch("foreman.github.subprocess.run")
    def test_edit_labels_adds_and_removes_in_one_edit_and_keeps_other_labels(self, run):
        run.return_value = gh_result("")
        forge = LiveForge()
        forge.edit_labels("Example/Engine", 2, ["Ready"], [])
        self.assertEqual(run.call_args.args[0], ["gh", "issue", "edit", "2", "--repo", "Example/Engine", "--add-label", "Ready"])
        forge.edit_labels("Example/Engine", 2, ["Needs answer"], ["Ready", "Doing"])
        self.assertEqual(run.call_args.args[0], ["gh", "issue", "edit", "2", "--repo", "Example/Engine",
                                                 "--add-label", "Needs answer", "--remove-label", "Ready,Doing"])
        forge.edit_labels("Example/Engine", 2, [], ["Doing"])
        self.assertEqual(run.call_args.args[0], ["gh", "issue", "edit", "2", "--repo", "Example/Engine", "--remove-label", "Doing"])

    @patch("foreman.github.subprocess.run")
    def test_ensure_label_creates_only_a_missing_label(self, run):
        read = ["gh", "api", "repos/Example/Engine/labels/Needs%20answer"]
        create = ["gh", "api", "repos/Example/Engine/labels", "--method", "POST", "-f", "name=Needs answer", "-f", "color=d93f0b"]
        missing = subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
        for responses, commands in (
            ([gh_result("{}")], [read]),  # An existing label keeps its color.
            ([missing, gh_result("{}")], [read, create]),
            ([missing, subprocess.CompletedProcess([], 1, "", "gh: Validation Failed (HTTP 422)")], [read, create]),
        ):
            with self.subTest(commands=len(commands)):
                run.reset_mock()
                run.side_effect = responses
                LiveForge().ensure_label("Example/Engine", "Needs answer", "d93f0b")
                self.assertEqual([entry.args[0] for entry in run.call_args_list], commands)
        run.side_effect = [subprocess.CompletedProcess([], 1, "", "gh: Forbidden (HTTP 403)")]
        with self.assertRaises(CommandFailed):
            LiveForge().ensure_label("Example/Engine", "Doing", "1d76db")

    @patch("foreman.github.subprocess.run")
    def test_upsert_comment_converges_foreman_own_status_comment(self, run):
        marker, head, token = "<!-- foreman status -->", "4f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d", "ghp_" + "A" * 36
        # Full redaction would wipe a real SHA, so a fact keeps it while credentials are still removed.
        body = f"Done with [redacted:GitHub Token].\n\nVerified head: {head}\n\nLeak: [redacted:GitHub Token]\n\n{marker}"
        read = ["gh", "api", f"repos/{REPO}/issues/8/comments?per_page=100", "--paginate", "--slurp"]
        post = ["gh", "api", f"repos/{REPO}/issues/8/comments", "--method", "POST", "-f", f"body={body}"]
        patch_six = ["gh", "api", f"repos/{REPO}/issues/comments/6", "--method", "PATCH", "-f", f"body={body}"]
        human = {"id": 5, "user": {"login": "octocat"}, "body": f"Quoting {marker}"}

        def own(text):
            return {"id": 6, "user": {"login": "bookout-labs-foreman[bot]"}, "body": text}

        for existing, writes in (([human], [post]), ([human, own(f"Older.\n\n{marker}")], [patch_six]), ([own(body)], [])):
            with self.subTest(writes=writes):
                run.reset_mock()
                run.side_effect = [gh_result(json.dumps([existing])), gh_result('{"id": 7}')]
                # The write returns the comment's id: the one GitHub created, or the one Foreman already owned.
                self.assertEqual(LiveForge().upsert_comment(REPO, 8, f"Done with {token}.", [f"Verified head: {head}", f"Leak: {token}"],
                                                            marker=marker), 7 if writes == [post] else 6)
                self.assertEqual([entry.args[0] for entry in run.call_args_list], [read, *writes])

    @patch("foreman.github.subprocess.run")
    def test_create_issue_parses_the_number_and_passes_literal_fields(self, run):
        run.side_effect = [
            gh_result('{"number":42,"html_url":"https://github.com/Example/Target/issues/42"}'),
            gh_result('{"number":"42"}'),
        ]
        forge = LiveForge()
        self.assertEqual(forge.create_issue(REPO, "Plan {repo}", "@draft", ["slice", "Ready"], marker=""), 42)
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", "repos/Example/Target/issues", "--method", "POST", "-f", "title=Plan {repo}",
            "-f", "body=@draft", "-f", "labels[]=slice", "-f", "labels[]=Ready",
        ])
        self.assertIsNone(run.call_args.kwargs["timeout"])
        with self.assertRaises(ForgeError):
            forge.create_issue(REPO, "Plan", "", [], marker="")
        self.assertIsNone(run.call_args.kwargs["timeout"])

    @patch("foreman.github.subprocess.run")
    def test_sub_issues_flattens_pages_preserving_global_ids(self, run):
        run.return_value = gh_result((GITHUB_FIXTURES / "sub-issues.json").read_text())
        children = LiveForge().sub_issues(REPO, 8)
        self.assertTrue(all(isinstance(child, LinkedIssue) for child in children))
        self.assertEqual([(child.id, child.number, child.state) for child in children],
                         [(1, 1347, "open"), (2, 1349, "closed")])
        self.assertTrue(children[0].in_repo("OCTOCAT/hello-world"))
        self.assertFalse(children[0].in_repo(REPO))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", f"repos/{REPO}/issues/8/sub_issues?per_page=100", "--paginate", "--slurp",
        ])

    @patch("foreman.github.subprocess.run")
    def test_parent_issue_only_normalizes_the_captured_no_parent_error(self, run):
        linked = (GITHUB_FIXTURES / "linked-issue.json").read_text()
        no_parent = json.loads((GITHUB_FIXTURES / "no-parent.json").read_text())
        run.side_effect = [gh_result(linked), subprocess.CompletedProcess(
            [], 1, json.dumps(no_parent), f"gh: {no_parent['message']} (HTTP {no_parent['status']})\n",
        )]
        forge = LiveForge()
        parent = forge.parent_issue(REPO, 1349)
        self.assertEqual(parent, LinkedIssue(id=1, number=1347, title="Found a bug", state="open",
                                            repository_url="https://api.github.com/repos/octocat/Hello-World",
                                            html_url="https://github.com/octocat/Hello-World/issues/1347"))
        self.assertIsNone(forge.parent_issue(REPO, 1349))
        self.assertEqual([invocation.args[0] for invocation in run.call_args_list],
                         [["gh", "api", f"repos/{REPO}/issues/1349/parent"]] * 2)
        run.side_effect = None
        for error in json.loads((GITHUB_FIXTURES / "parent-errors.json").read_text()):
            with self.subTest(error=error):
                run.return_value = subprocess.CompletedProcess([], **error)
                with self.assertRaises(ForgeError):
                    forge.parent_issue(REPO, 1349)

    @patch("foreman.github.subprocess.run")
    def test_add_sub_issue_resolves_the_child_id_before_posting_without_reparenting(self, run):
        response = (GITHUB_FIXTURES / "linked-issue.json").read_text()
        run.side_effect = [gh_result(response), gh_result(response)]
        self.assertIsNone(LiveForge().add_sub_issue(REPO, 8, 1347))
        self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [
            ["gh", "api", f"repos/{REPO}/issues/1347"],
            ["gh", "api", f"repos/{REPO}/issues/8/sub_issues", "--method", "POST", "-F", "sub_issue_id=1"],
        ])

    @patch("foreman.github.subprocess.run")
    def test_add_sub_issue_does_not_write_after_a_failed_identity_read(self, run):
        for result in (subprocess.CompletedProcess([], 1, "", "gh: Forbidden (HTTP 403)"),
                       gh_result('{"id":"1347"}')):
            with self.subTest(result=result):
                run.reset_mock()
                run.return_value = result
                with self.assertRaises(ForgeError):
                    LiveForge().add_sub_issue(REPO, 8, 1347)
                run.assert_called_once()

    @patch("foreman.github.subprocess.run")
    def test_update_issue_body_patches_literal_redacted_text(self, run):
        run.return_value = gh_result((GITHUB_FIXTURES / "linked-issue.json").read_text())
        secret = "ghp_" + "a" * 36
        self.assertIsNone(LiveForge().update_issue_body(f"Example/{secret}", 8, f"@draft {{repo}}\n{secret}"))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", "repos/Example/[redacted:GitHub Token]/issues/8", "--method", "PATCH", "-f",
            "body=@draft {repo}\n[redacted:GitHub Token]",
        ])
        run.return_value = subprocess.CompletedProcess([], 1, "", "gh: Forbidden (HTTP 403)")
        with self.assertRaises(ForgeError):
            LiveForge().update_issue_body(REPO, 8, "Changed")

    @patch("foreman.github.subprocess.run")
    def test_marked_issues_reads_all_states_and_pages_without_pull_requests(self, run):
        run.return_value = gh_result((GITHUB_FIXTURES / "marked-issues.json").read_text())
        forge = LiveForge()
        self.assertEqual(forge.marked_issues(REPO, "<!-- foreman:issue-103 -->"), [1347, 1349])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", f"repos/{REPO}/issues?state=all&per_page=100", "--paginate", "--slurp",
        ])
        self.assertEqual(forge.marked_issues(REPO, "<!-- absent -->"), [])

    @patch("foreman.github.subprocess.run")
    def test_relationship_and_marker_reads_reject_malformed_or_failed_responses(self, run):
        forge = LiveForge()
        for read, payload in ((lambda: forge.parent_issue(REPO, 8), "null"),
                              (lambda: forge.sub_issues(REPO, 8), '[[{"id":1,"number":8}]]'),
                              (lambda: forge.marked_issues(REPO, "marker"), '[[{"number":8,"body":123}]]')):
            for result in (gh_result(payload), subprocess.CompletedProcess([], 1, "", "HTTP 422")):
                with self.subTest(payload=payload, result=result):
                    run.return_value = result
                    with self.assertRaises(ForgeError):
                        read()
            run.side_effect = subprocess.TimeoutExpired(["gh", "api"], 30)
            with self.assertRaises(ForgeError):
                read()
            run.side_effect = None

    @patch("foreman.github.subprocess.run")
    def test_issue_comment_records_preserve_the_second_page_marker_and_a_null_body(self, run):
        run.return_value = gh_result((GITHUB_FIXTURES / "issue-comments.json").read_text())
        records = LiveForge().issue_comment_records(REPO, 1347)
        self.assertEqual([(record.id, record.body) for record in records], [(1, "Me too"), (2, "<!-- foreman:issue-93 -->"), (3, None)])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", f"repos/{REPO}/issues/1347/comments?per_page=100", "--paginate", "--slurp",
        ])

    @patch("foreman.github.subprocess.run")
    def test_issue_comment_records_are_empty_after_successful_empty_reads(self, run):
        forge = LiveForge()
        for payload in ('[]', '[[]]', '[[], []]'):
            with self.subTest(payload=payload):
                run.return_value = gh_result(payload)
                self.assertEqual(forge.issue_comment_records(REPO, 8), [])

    @patch("foreman.github.subprocess.run")
    def test_issue_comment_records_reject_failed_or_malformed_responses(self, run):
        forge = LiveForge()
        for result in (
            subprocess.CompletedProcess([], 1, '[[{"id":1,"body":"valid"}]]', "HTTP 502"),
            gh_result('[[{"id":1,"body":"valid"}],{"message":"Bad Gateway"}]'),
            gh_result('[[{"id":1,"body":"valid"}],[{"id":2,"body":12}]]'),
            gh_result('[[{"id":1,"body":"valid"}],[{}]]'),
            gh_result('[[{"id":1,"body":"valid"}],'),
        ):
            with self.subTest(result=result):
                run.return_value = result
                with self.assertRaises(ForgeError):
                    forge.issue_comment_records(REPO, 8)
        for number in (0, -1, "2", True):
            with self.subTest(number=number):
                run.return_value = gh_result(json.dumps([[{"id": number, "body": "invalid"}]]))
                with self.assertRaises(ForgeError):
                    forge.issue_comment_records(REPO, 8)

    @patch("foreman.github.subprocess.run")
    def test_comment_issue_posts_literal_body_and_redacts_secrets(self, run):
        run.return_value = gh_result((GITHUB_FIXTURES / "created-comment.json").read_text())
        forge = LiveForge()
        self.assertIsNone(forge.comment_issue(REPO, 1347, "@draft {repo}\nMe too", marker=""))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", f"repos/{REPO}/issues/1347/comments", "--method", "POST", "-f", "body=@draft {repo}\nMe too",
        ])
        token = "ghp_" + "a" * 36
        forge.comment_issue(f"Example/{token}", 48, f"#48 secret: {token}", marker="")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", "repos/Example/[redacted:GitHub Token]/issues/48/comments", "--method", "POST",
            "-f", "body=#48 secret: [redacted:GitHub Token]",
        ])
        run.return_value = subprocess.CompletedProcess([], 1, "", "HTTP 403")
        with self.assertRaises(ForgeError):
            forge.comment_issue(REPO, 48, "Me too", marker="")

    @patch("foreman.github.subprocess.run")
    def test_foreman_markers_are_written_verbatim_while_the_content_is_redacted(self, run):
        # A marker's digest looks like a secret; redacting it would leave the write unfindable (engine #1048).
        marker = f"<!-- foreman slice {spec_digest([REPO.lower(), 8, 'Child'])} -->"
        self.assertNotEqual(redact(marker), marker)
        token = "ghp_" + "a" * 36
        run.return_value = gh_result('{"number":42}')
        forge = LiveForge()
        forge.create_issue(REPO, "Child", f"secret: {token}", [], marker=marker)
        self.assertIn(f"body=secret: [redacted:GitHub Token]\n\n{marker}", run.call_args.args[0])
        forge.comment_issue(REPO, 8, f"answer {token}", marker=marker)
        self.assertIn(f"body=answer [redacted:GitHub Token]\n\n{marker}", run.call_args.args[0])

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
        run.side_effect = [pr_result(), pr_result(), gh_result("[[]]"), gh_result(json.dumps({"id": 1, "state": "COMMENTED", "commit_id": PR["headRefOid"], "body": "posted"}))]
        forge = LiveForge()
        target = forge.review_target(REPO, 8, BRANCH, HEAD)
        forge.post_review(REPO, target, f"Synthetic {synthetic_token}", [{
            "path": f"docs/{synthetic_token}.txt", "line": 2, "side": "RIGHT",
            "body": f'Synthetic password="abc" and abc {synthetic_token}',
        }], marker="run:1:head")
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["commit_id"], HEAD.upper())
        self.assertEqual(payload["body"], "Synthetic [redacted:GitHub Token]\n\n<!-- foreman-review:run:1:head -->")
        self.assertEqual(payload["comments"][0]["path"], "docs/[redacted:GitHub Token].txt")
        self.assertEqual(payload["comments"][0]["body"],
                         'Synthetic password="[redacted:Secret Keyword]" and abc [redacted:GitHub Token]')

    @patch("foreman.github.subprocess.run")
    def test_posts_the_review_and_inline_comments_for_the_verified_head(self, run):
        unrelated = {"id": 9, "state": "COMMENTED", "commit_id": None, "body": "Another author"}
        run.side_effect = [pr_result(), pr_result(PR, CONFIRM), gh_result(json.dumps([[unrelated]])),
                           gh_result(json.dumps({"id": 1, "state": "COMMENTED", "commit_id": PR["headRefOid"], "body": "posted"}))]
        forge = LiveForge()
        target = forge.review_target(REPO, 8, BRANCH, HEAD)
        comments: list[ReviewComment] = [{"path": "items.txt", "line": 2, "side": "RIGHT", "body": "[nit] Use @literal."}]
        forge.post_review(REPO, target, "Longer review detail.", comments, marker="run:1:head")
        self.assertEqual(run.call_args.args[0], [
            "gh", "api", "repos/Example/Target/pulls/12/reviews", "--method", "POST",
            "-H", "X-GitHub-Api-Version: 2022-11-28", "--input", "-",
        ])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {
            "commit_id": HEAD.upper(), "event": "COMMENT", "body": "Longer review detail.\n\n<!-- foreman-review:run:1:head -->", "comments": comments,
        })

    @patch("foreman.github.subprocess.run")
    def test_review_does_not_post_if_the_pull_request_closes_or_is_retargeted(self, run):
        for field, value in (("state", "CLOSED"), ("baseRefName", "release"), ("headRefOid", "d" * 40), ("baseRefOid", "c" * 40)):
            with self.subTest(field=field, value=value):
                run.reset_mock()
                run.side_effect = [pr_result(), pr_result({**PR, field: value}, CONFIRM)]
                forge = LiveForge()
                target = forge.review_target(REPO, 8, BRANCH, HEAD)
                with self.assertRaisesRegex(ForgeError, "changed during review"):
                    forge.post_review(REPO, target, "Longer review detail.", [], marker="run:1:head")
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [VIEW, CONFIRM])

    @patch("foreman.github.subprocess.run")
    def test_review_timeout_reconciles_receipt_without_duplicate_post(self, run):
        posted = {"id": 42, "state": "COMMENTED", "commit_id": PR["headRefOid"],
                  "body": "Graph verdict: changes.\n\n<!-- foreman-review:run:2:head -->"}
        run.side_effect = [pr_result(), pr_result(), gh_result("[[]]"),
            subprocess.TimeoutExpired(["gh", "api"], 30), pr_result(), gh_result(json.dumps([[posted]]))]
        forge = LiveForge()
        target = forge.review_target(REPO, 8, BRANCH, HEAD)
        with self.assertRaises(CommandTimedOut):
            forge.post_review(REPO, target, "Graph verdict: changes.", [], marker="run:2:head")
        receipt = forge.post_review(REPO, target, "Graph verdict: changes.", [], marker="run:2:head")
        self.assertEqual(receipt, {k: posted[k] for k in ("id", "state", "commit_id")})
        self.assertEqual(sum("POST" in c.args[0] for c in run.call_args_list), 1)

    @patch("foreman.github.subprocess.run")
    def test_marked_review_with_null_commit_is_rejected(self, run):
        marked = {"id": 42, "state": "COMMENTED", "commit_id": None,
                  "body": "<!-- foreman-review:run:2:head -->"}
        run.side_effect = [pr_result(), pr_result(), gh_result(json.dumps([[marked]]))]
        forge = LiveForge()
        target = forge.review_target(REPO, 8, BRANCH, HEAD)
        with self.assertRaises(ForgeError):
            forge.post_review(REPO, target, "Graph verdict: changes.", [], marker="run:2:head")

    @patch("foreman.github.subprocess.run")
    def test_review_permission_rejection_and_invalid_receipt_fail_closed(self, run):
        for response in (subprocess.CompletedProcess([], 403, "", "Forbidden"),
                         gh_result(json.dumps({"id": 1, "state": "APPROVED", "commit_id": HEAD, "body": "bad"}))):
            run.side_effect = [pr_result(), pr_result(), gh_result("[[]]"), response]
            forge = LiveForge()
            target = forge.review_target(REPO, 8, BRANCH, HEAD)
            with self.assertRaises(ForgeError):
                forge.post_review(REPO, target, "Graph verdict: approve.", [], marker="run:1:head")

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
                with self.assertRaisesRegex(ForgeError, reason):
                    LiveForge().review_target(REPO, 8, BRANCH, HEAD)
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
                with self.assertRaisesRegex(ForgeError, "Invalid PullRequest response") as exc:
                    LiveForge().review_target(REPO, 8, BRANCH, HEAD)
                self.assertIsInstance(exc.exception.__cause__, ValidationError)
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
                with self.assertRaises(ForgeError) as exc:
                    LiveForge().review_target(REPO, 8, branch, HEAD)
                self.assertEqual(str(exc.exception), reason)
                self.assertEqual([invocation.args[0] for invocation in run.call_args_list], [view])
