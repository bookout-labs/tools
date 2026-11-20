import unittest
from unittest.mock import patch

from foreman.watcher import TOOLS_REPO, file_proposal_issue, watch_run


class StubGitHub:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.created = []

    def load_issue(self, repo, number):
        raise AssertionError("unexpected load_issue")

    def create_issue(self, repo, title, body, labels):
        self.created.append((repo, title, body, labels))
        return 10 + len(self.created)

    def open_issue_titles(self, repo):
        self.checked_repo = repo
        return list(self.existing)

    def is_merged(self, repo, number):
        raise AssertionError("unexpected is_merged")

    def add_label(self, repo, number, label):
        raise AssertionError("unexpected add_label")


class WatcherTest(unittest.TestCase):
    def test_evidence_redacts_secrets_before_whitespace_normalization_and_clipping(self):
        secret = "secret-start  remaining-secret"
        state = {
            "halt": "budget_exceeded",
            "halt_detail": "x" * 185 + " " + secret,
            "attempts": [{"node": "review", "status": "ran", "detail": "x" * 85 + " " + secret}],
        }
        with patch.dict("os.environ", {"FOREMAN_TEST_TOKEN": secret}):
            proposal = watch_run(state, "budget_exceeded")
        self.assertEqual(proposal["evidence"].count("[REDACTED]"), 2)
        self.assertNotIn("secret-start", proposal["evidence"])

    def test_clean_run_and_intentional_merge_hold_return_none(self):
        base = {
            "repo": "DoctorCRE/doctorcre-engine",
            "issue": 875,
            "path": ["triage", "implement", "verify", "review", "merge", "done"],
            "attempts": [{"node": "triage", "model": "claude-opus-5-5-high", "status": "ran", "detail": ""}],
        }
        self.assertIsNone(watch_run(base, "verified"))
        self.assertIsNone(watch_run({**base, "halt": "merge_held", "halt_detail": "production switch"}, "merge_held"))

    def test_quota_failover_and_quota_exhausted_report_quota_without_graph_change(self):
        failover_state = {
            "repo": "DoctorCRE/doctorcre-engine",
            "issue": 875,
            "path": ["triage", "implement", "verify", "review", "merge", "done"],
            "attempts": [
                {"node": "triage", "model": "claude-opus-5-5-high", "status": "quota", "detail": "429"},
                {"node": "triage", "model": "grok-4.7-high", "status": "ran", "detail": ""},
            ],
        }
        proposal = watch_run(failover_state, "verified")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["kind"], "quota")
        self.assertFalse(proposal["file_issue"])
        self.assertEqual(proposal["target"], "")
        self.assertIn("No graph change.", proposal["summary"])

        exhausted = watch_run(
            {
                "repo": "DoctorCRE/doctorcre-engine",
                "issue": 875,
                "halt": "quota_exhausted",
                "halt_detail": "triage quota exhausted: 429",
                "path": ["triage", "done"],
                "attempts": [{"node": "triage", "model": "claude-opus-5-5-high", "status": "quota", "detail": "429"}],
            },
            "quota_exhausted",
        )
        self.assertEqual(exhausted["kind"], "quota")
        self.assertFalse(exhausted["file_issue"])

    def test_failures_identify_evidence_file_to_change_and_human_pr_rule(self):
        cases = (
            ("invalid_output", "claude worker exited 1: unknown option --effort-x", ["triage", "done"], "src/foreman/worker.py"),
            ("invalid_output", "claude usage reported claude-haiku-5-5, expected claude-opus-5-5", ["triage", "done"], "src/foreman/worker.py"),
            ("invalid_output", "codex rerouted gpt-6.1-sol: gpt-6.1-sol -> gpt-other", ["implement", "done"], "src/foreman/worker.py"),
            ("invalid_output", "spec did not return text", ["triage", "spec", "done"], "src/foreman/stages/spec.md"),
            ("invalid_output", "implement could not commit: hook declined", ["triage", "implement", "done"], "src/foreman/runner.py"),
            ("invalid_output", "slice property needs exactly name and constraint", ["triage", "spec", "done"], "src/foreman/stages/spec.md"),
            ("cli_missing", "codex CLI is not installed", ["implement", "done"], "src/foreman/worker.py"),
            ("merge_held", "could not read FOREMAN_MERGE_HOLD: auth failed", ["triage", "implement", "verify", "review", "merge", "done"], "src/foreman/cli.py"),
            ("unpublished", "could not push 875-health-release: rejected", ["triage", "implement", "verify", "review", "merge", "done"], "src/foreman/runner.py"),
            ("review_unrecorded", "could not record the review: denied", ["triage", "implement", "verify", "review", "merge", "done"], "src/foreman/runner.py"),
        )
        for stop, detail, path, expected_target in cases:
            with self.subTest(stop=stop, detail=detail):
                state = {
                    "repo": "DoctorCRE/doctorcre-engine",
                    "issue": 874,
                    "request": "PRIVATE_REQUEST_TEXT_NEVER_LEAK",
                    "workspace": "/private/workspace/path",
                    "halt": stop,
                    "halt_detail": detail,
                    "path": path,
                    "attempts": [{"node": path[-2], "model": "claude-opus-5-5-high", "status": "stopped", "detail": detail}],
                }
                proposal = watch_run(state, stop)
                self.assertIsNotNone(proposal)
                self.assertEqual(proposal["kind"], "graph_fix")
                self.assertEqual(proposal["target"], expected_target)
                self.assertTrue(proposal["file_issue"])
                self.assertIn(expected_target, proposal["summary"])
                self.assertIn("A person opens the pull request.", proposal["summary"])
                self.assertIn("does not edit the graph, merge, or launch a coding agent", proposal["summary"])
                self.assertNotIn("PRIVATE_REQUEST_TEXT_NEVER_LEAK", proposal["summary"])
                self.assertNotIn("/private/workspace/path", proposal["summary"])
                self.assertIn("DoctorCRE/doctorcre-engine#874", proposal["evidence"])

    def test_missing_check_program_reports_setup_without_a_graph_issue(self):
        proposal = watch_run(
            {
                "repo": "DoctorCRE/doctorcre-engine",
                "issue": 876,
                "halt": "check_unavailable",
                "halt_detail": "the check exited 127 because a program it runs was not found",
                "path": ["triage", "implement", "verify", "done"],
                "attempts": [{"node": "implement", "model": "gpt-6.2-sol-high", "status": "ran", "detail": ""}],
                "request": "PRIVATE_REQUEST_TEXT_NEVER_LEAK",
                "workspace": "/private/workspace/path",
            },
            "check_unavailable",
        )
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["kind"], "setup")
        self.assertEqual(proposal["target"], "")
        self.assertFalse(proposal["file_issue"])
        self.assertIn("No graph change.", proposal["summary"])
        self.assertIn("Install the programs the check runs, then rerun this issue.", proposal["summary"])
        self.assertIn("the check exited 127", proposal["evidence"])
        self.assertNotIn("PRIVATE_REQUEST_TEXT_NEVER_LEAK", proposal["summary"])
        self.assertNotIn("/private/workspace/path", proposal["summary"])

    def test_file_proposal_issue_skips_duplicate_titles(self):
        proposal = watch_run(
            {
                "repo": "DoctorCRE/doctorcre-engine",
                "issue": 874,
                "halt": "invalid_output",
                "halt_detail": "claude usage reported claude-haiku-5-5, expected claude-opus-5-5",
                "path": ["triage", "done"],
                "attempts": [{"node": "triage", "model": "claude-opus-5-5-high", "status": "stopped", "detail": "mismatch"}],
            },
            "invalid_output",
        )
        fresh = StubGitHub()
        self.assertEqual(file_proposal_issue(proposal, fresh), 11)
        self.assertEqual(fresh.checked_repo, TOOLS_REPO)
        self.assertEqual(len(fresh.created), 1)
        self.assertEqual(fresh.created[0][0], TOOLS_REPO)

        duplicate = StubGitHub(existing=[proposal["title"]])
        self.assertIsNone(file_proposal_issue(proposal, duplicate))
        self.assertEqual(duplicate.created, [])

    def test_env_unavailable_and_budget_exceeded_report_setup_with_scrubbed_pii(self):
        for halt_name in ("env_unavailable", "budget_exceeded"):
            with self.subTest(halt=halt_name):
                proposal = watch_run(
                    {
                        "repo": "DoctorCRE/doctorcre-engine",
                        "issue": 876,
                        "halt": halt_name,
                        "halt_detail": "No PostgreSQL server answered for owner@example.invalid (555) 234-5678",
                        "path": ["triage", "implement", "verify", "done"],
                        "attempts": [{"node": "implement", "model": "gpt-6.2-sol-high", "status": "ran", "detail": ""}],
                    },
                    halt_name,
                )
                self.assertIsNotNone(proposal)
                self.assertEqual(proposal["kind"], "setup")
                self.assertFalse(proposal["file_issue"])
                self.assertNotIn("owner@example.invalid", proposal["evidence"])
                self.assertNotIn("(555) 234-5678", proposal["evidence"])


if __name__ == "__main__":
    unittest.main()
