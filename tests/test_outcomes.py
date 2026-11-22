import tests

from foreman.state import ChecksState, PlanState, ReviewState, RunState, SCHEMA_VERSION, checkpoint_serializer

import unittest
from typing import get_args

from foreman.delivery import halted
from foreman.runner import checkpoint_state
from foreman.readiness import Outcome
from foreman.outcomes import HALTS, Halt, READINESS_HALTS, RESUMABLE_HALTS, exit_code


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
                checkpoint_state(dict(schema_version=SCHEMA_VERSION, halt=code, run=RunState(token_cap=100, repo="Example/Engine"), check="true", path=[]))

    def test_schema_12_keeps_the_retired_size_halt_readable(self):
        values = dict(schema_version=12, halt="pr_size_unavailable", stop="pr_size_unavailable",
                      halt_detail="The pull request size could not be measured.", path=["review", "done"],
                      run=RunState(repo="Example/Engine", token_cap=100), checks=ChecksState(),
                      plan=PlanState(), review=ReviewState())
        serializer = checkpoint_serializer()
        state = checkpoint_state(serializer.loads_typed(serializer.dumps_typed(values)))
        self.assertIsNotNone(state)
        self.assertEqual(state["halt"], "pr_size_unavailable")
        self.assertEqual(exit_code(state["halt"]), 1)
        self.assertNotIn(state["halt"], RESUMABLE_HALTS)

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
