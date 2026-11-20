import json
import subprocess
import unittest

from foreman.feedback import collect_review_feedback, collect_triage_feedback


class FeedbackCollectorTest(unittest.TestCase):
    def test_collect_review_feedback_extracts_human_signals_and_redacts_pii(self):
        payload = [
            {
                "number": 21,
                "title": "Fix query for owner@example.invalid",
                "url": "https://github.com/Example/Engine/pull/21",
                "state": "OPEN",
                "createdAt": "2026-11-20T10:00:00Z",
                "comments": [
                    {"author": {"login": "bot"}, "body": "Reviewed by gpt-6.2-sol-high.\nVerdict: approve."},
                    {"author": {"login": "reviewer"}, "body": "Call (555) 234-5678 or check ghp_0123456789abcdefghij0123456789"},
                ],
                "reviews": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "body": "Handle null case."}],
                "commits": [
                    {"oid": "a", "authors": [{"login": "foreman-bot", "email": "foreman@example.invalid"}]},
                    {"oid": "b", "authors": [{"login": "reviewer", "email": "reviewer@example.invalid"}]},
                ],
            },
            {
                "number": 22,
                "title": "Abandoned attempt",
                "url": "https://github.com/Example/Engine/pull/22",
                "state": "CLOSED",
                "closedAt": "2026-11-20T11:00:00Z",
                "mergedAt": None,
                "comments": [],
                "reviews": [],
                "commits": [{"oid": "c", "authors": [{"login": "foreman-bot"}]}],
            },
            {
                "number": 23,
                "title": "Clean zero-touch PR",
                "url": "https://github.com/Example/Engine/pull/23",
                "state": "MERGED",
                "mergedAt": "2026-11-20T12:00:00Z",
                "comments": [{"author": {"login": "foreman-bot"}, "body": "Reviewed by gpt-6.2-sol-high."}],
                "reviews": [],
                "commits": [{"oid": "d", "authors": [{"login": "dependabot[bot]"}]}],
            },
        ]
        items = collect_review_feedback(
            "Example/Engine",
            since_days=3650,
            runner=lambda args: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
        )
        self.assertEqual([i["number"] for i in items], [21, 22])
        self.assertEqual(
            items[0]["signals"],
            ["changes_requested", "followup_commits:1", "human_comment"],
        )
        self.assertIn("closed_without_merge", items[1]["signals"])
        joined = json.dumps(items)
        self.assertNotIn("owner@example.invalid", joined)
        self.assertNotIn("(555) 234-5678", joined)
        self.assertNotIn("ghp_0123456789abcdefghij0123456789", joined)

    def test_bodyless_human_review_known_pr_filter_and_commits_after_head(self):
        payload = [
            {
                "number": 41,
                "title": "Foreman PR",
                "url": "https://github.com/Example/Engine/pull/41",
                "state": "OPEN",
                "createdAt": "2026-11-20T10:00:00Z",
                "comments": [],
                "reviews": [
                    {"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "body": ""},
                    {"author": {"login": "foreman-reviewer"}, "state": "CHANGES_REQUESTED", "body": ""},
                ],
                # Foreman commits under the owner's account, so authorship alone cannot tell them apart.
                "commits": [
                    {"oid": "h1", "authors": [{"login": "owner"}]},
                    {"oid": "h2", "authors": [{"login": "owner"}]},
                    {"oid": "h3", "authors": [{"login": "owner"}]},
                ],
            },
            {
                "number": 42,
                "title": "Someone else's PR",
                "url": "https://github.com/Example/Engine/pull/42",
                "state": "CLOSED",
                "closedAt": "2026-11-20T11:00:00Z",
                "mergedAt": None,
                "comments": [],
                "reviews": [],
                "commits": [],
            },
        ]
        runner = lambda args: subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        items = collect_review_feedback(
            "Example/Engine",
            since_days=3650,
            runner=runner,
            known_pr_urls={"https://github.com/Example/Engine/pull/41"},
            head_shas={"https://github.com/Example/Engine/pull/41": "h2"},
        )
        self.assertEqual([i["number"] for i in items], [41])
        self.assertEqual(items[0]["signals"], ["changes_requested", "followup_commits:1"])
        self.assertEqual(items[0]["notes"], [])

        unfiltered = collect_review_feedback("Example/Engine", since_days=3650, runner=runner)
        self.assertEqual([i["number"] for i in unfiltered], [41, 42])
        self.assertIn("followup_commits:3", unfiltered[0]["signals"])

    def test_collect_triage_feedback_finds_reopened_not_planned_and_overrides(self):
        payload = [
            {
                "number": 31,
                "title": "Parent issue split wrong for user@example.invalid",
                "url": "https://github.com/Example/Engine/issues/31",
                "state": "CLOSED",
                "stateReason": "NOT_PLANNED",
                "createdAt": "2026-11-20T10:00:00Z",
                "labels": [{"name": "factory"}],
                "comments": [{"author": {"login": "owner"}, "body": "Wrong slice, should go to spec"}],
            },
            {
                "number": 32,
                "title": "Normal closed issue",
                "url": "https://github.com/Example/Engine/issues/32",
                "state": "CLOSED",
                "stateReason": "COMPLETED",
                "createdAt": "2026-11-20T10:00:00Z",
                "labels": [{"name": "slice"}],
                "comments": [],
            },
        ]
        items = collect_triage_feedback(
            "Example/Engine",
            since_days=3650,
            runner=lambda args: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
        )
        self.assertEqual([i["number"] for i in items], [31])
        self.assertEqual(items[0]["signals"], ["closed_not_planned", "human_triage_note"])
        self.assertNotIn("user@example.invalid", json.dumps(items))

        runner = lambda args: subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        self.assertEqual(collect_triage_feedback("Example/Engine", since_days=3650, runner=runner, known_issues={32}), [])


if __name__ == "__main__":
    unittest.main()
