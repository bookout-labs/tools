import tests

import unittest
from typing import get_args

from foreman.cli import run_exit_code
from foreman.delivery import halted
from foreman.outcomes import HALTS, Halt, READINESS_HALTS, RESUMABLE_HALTS, exit_code
from foreman.readiness import Outcome


class HaltPolicyTest(unittest.TestCase):
    def test_every_halt_has_an_exit_and_a_reason(self):
        self.assertEqual(set(HALTS), set(Halt))
        for code, row in HALTS.items():
            with self.subTest(code=code):
                self.assertTrue(row.reason.strip())
                self.assertEqual(exit_code(code), 75 if code in RESUMABLE_HALTS else 1)
                self.assertEqual(run_exit_code(dict(halt=code)), row.exit)
        self.assertEqual(exit_code(None), 1)

    def test_halt_codes_keep_the_string_checkpoint_format(self):
        self.assertIs(type(halted(Halt.ci_red, "failed", "merge")["halt"]), str)

    def test_retry_and_merge_recovery_preserve_the_existing_choices(self):
        merge = {"merge_held", "merge_not_armed", "pr_conflict", "ci_pending", "ci_red",
                 "readiness_unavailable", "queue_rejected"}
        self.assertEqual({code for code, row in HALTS.items() if row.recovery == "recheck_merge"}, merge)
        self.assertEqual(RESUMABLE_HALTS, merge | {"env_unavailable", "publish_failed", "git_failed",
                         "implementation_rejected", "quota_exhausted", "review_not_posted", "reviewer_unavailable"})

    def test_readiness_outcomes_appear_in_exactly_one_row(self):
        outcomes = [outcome for row in HALTS.values() for outcome in row.readiness]
        self.assertCountEqual(outcomes, get_args(Outcome))
        self.assertEqual(READINESS_HALTS, dict(queue_rejected="queue_rejected", ready="merge_not_armed",
                         conflict="pr_conflict", pending="ci_pending", red="ci_red", policy="readiness_unavailable",
                         head_changed="merge_not_armed", closed="merge_not_armed", retargeted="merge_not_armed"))
