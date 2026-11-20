import unittest

from foreman.measure import MeasureError, grade, load_measure, propose, selected


class MeasureTest(unittest.TestCase):
    def test_packaged_scores(self):
        config = load_measure()
        self.assertEqual(config.sampling_rate, 10)
        self.assertEqual(config.passing_score, 0.5)
        self.assertTrue(grade("approve").passed)
        self.assertFalse(grade("block").passed)
        self.assertTrue(grade("insufficient_evidence").passed)
        self.assertEqual(grade("block").score, 0.2)

    def test_one_run_in_ten_is_graded(self):
        self.assertTrue(selected(0))
        self.assertFalse(selected(1))
        self.assertTrue(selected(10))

    def test_proposal_leaves_the_model_list_alone(self):
        text = propose("redundant-tests")
        self.assertIn("skills/code-quality.md", text)
        self.assertIn("does not change models.toml or max_review_rounds", text)
        self.assertIn("A person opens the pull request.", text)

    def test_unknown_failure_is_rejected(self):
        with self.assertRaises(MeasureError):
            propose("rewrite-models")


if __name__ == "__main__":
    unittest.main()
