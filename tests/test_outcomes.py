import tests

from foreman.state import RunState, StartSettings, SCHEMA_VERSION

import unittest

from foreman.delivery import halted
from foreman.runner import checkpoint_state
from foreman.outcomes import HALTS, Halt, RESUMABLE_HALTS, exit_code


class HaltPolicyTest(unittest.TestCase):
    def test_every_halt_has_an_exit_and_a_reason(self):
        self.assertEqual(set(HALTS), set(Halt))
        for code, row in HALTS.items():
            with self.subTest(code=code):
                self.assertTrue(row.reason.strip())
                self.assertEqual(exit_code(code), 75 if code in RESUMABLE_HALTS else 1)
                self.assertIs(type(halted(code, "failed", "merge")["halt"]), str)
        self.assertEqual(exit_code(None), 1)

    def test_checkpoint_refuses_unknown_and_retired_codes(self):
        for code in ("no_reviewer", "no_reviewr"):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                checkpoint_state(dict(schema_version=SCHEMA_VERSION, halt=code, run=RunState(repo="Example/Engine"), start=StartSettings(human_login="fixture-owner", max_tokens=100, max_budget_usd="3"), check="true", path=[]))

    def test_merge_halts_retry_and_github_failures_are_routes_not_halts(self):
        self.assertEqual(RESUMABLE_HALTS, {"merge_held", "merge_not_armed", "readiness_unavailable", "env_unavailable", "publish_failed",
                                           "publication_unreconciled", "git_failed", "implementation_rejected", "review_not_posted",
                                           "budget_exceeded", "dirty_worktree", "base_check_failed", "base_protected_changed"})
        self.assertFalse({"pr_conflict", "ci_pending", "ci_red", "queue_rejected"} & {code.value for code in Halt})

    def test_a_budget_halt_resumes_after_the_cap_is_raised(self):
        # Item 19: the operator raises the cap and adds Ready again, so the halt exits as resumable.
        self.assertEqual(exit_code(Halt.budget_exceeded), 75)
        self.assertIn("FOREMAN_MAX_BUDGET_USD", HALTS[Halt.budget_exceeded].reason)
