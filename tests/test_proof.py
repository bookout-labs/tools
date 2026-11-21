import unittest

from foreman.proof import (
    ProofError,
    browser_required,
    formal_required,
    score_verdict,
)


class ProofTest(unittest.TestCase):
    def setUp(self):
        self.slice = {
            "title": "Show active deals",
            "acceptance": "Active deals appear and edits save.",
            "depends_on": [],
            "screen": "docs/screens/deals.md",
            "properties": [
                {"name": "Amount is positive", "constraint": "deals_amount_positive"},
            ],
        }

    def test_requirements_follow_screen_and_properties(self):
        self.assertTrue(browser_required(self.slice))
        self.assertTrue(formal_required(self.slice))
        self.assertFalse(browser_required({"screen": ""}))
        self.assertFalse(formal_required({"properties": []}))

    def test_score_verdicts(self):
        self.assertEqual(score_verdict("approve"), "pass")
        self.assertEqual(score_verdict("block"), "fail")
        self.assertEqual(score_verdict("insufficient_evidence"), "insufficient_evidence")

    def test_unknown_score_labels_are_rejected(self):
        for label in ("pass", "", None, []):
            with self.subTest(label=label), self.assertRaises(ProofError):
                score_verdict(label)


class ProofLabelTest(unittest.TestCase):
    def test_browser_evidence_boundary(self):
        from foreman.proof import browser_proof_label
        slice = {"screen": "docs/screens/deals.md"}
        for artifact in (None, {}, {"screen": slice["screen"]}):
            self.assertEqual(browser_proof_label(slice, artifact), "insufficient_evidence")
        self.assertEqual(browser_proof_label(slice, {"screen": "other", "trace": "trace.zip"}), "block")
        self.assertEqual(browser_proof_label(slice, {"screen": slice["screen"], "trace": "trace.zip", "passed": False}), "block")
        self.assertIsNone(browser_proof_label(slice, {"screen": slice["screen"], "trace": "trace.zip"}))

    def test_formal_evidence_boundary(self):
        from foreman.proof import formal_proof_label
        slice = {"properties": [{"name": "Positive amount", "constraint": "positive_amount"}]}
        for artifact in (None, {}, {"constraints": []}, {"constraints": [{"constraint": "positive_amount"}]}):
            self.assertEqual(formal_proof_label(slice, artifact), "insufficient_evidence")
        self.assertEqual(formal_proof_label(slice, {"typecheck": "passed"}), "block")
        self.assertEqual(formal_proof_label(slice, {"constraints": [{"constraint": "positive_amount", "rejected": False}]}), "block")
        self.assertIsNone(formal_proof_label(slice, {"constraints": [{"constraint": "positive_amount", "rejected": True}]}))


if __name__ == "__main__":
    unittest.main()
