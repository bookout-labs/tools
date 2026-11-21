import tests

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
                checkpoint_state(dict(halt=code, token_cap=100, repo="Example/Engine", check="true", path=[]))
