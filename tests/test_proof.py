import tests

import unittest

from foreman.proof import (
    browser_proof_label,
    browser_required,
    formal_proof_label,
    formal_required,
)
from foreman.stage_output import BrowserProof, FormalProof


class ProofTest(unittest.TestCase):
    def setUp(self):
        self.slice = {
            "title": "Show active deals",
            "acceptance": "Active deals appear and edits save.",
            "builds": [],
            "uses": [],
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


class ProofLabelTest(unittest.TestCase):
    def test_browser_evidence_boundary(self):
        screen = "docs/screens/deals.md"
        slice = {"screen": screen}
        cases = (
            (None, "insufficient_evidence"),
            ({}, "insufficient_evidence"),
            ({"screen": screen}, "insufficient_evidence"),
            ({"screen": screen, "trace": "  "}, "insufficient_evidence"),
            ({"screen": "other", "trace": "trace.zip"}, "block"),
            ({"screen": screen, "trace": "trace.zip", "passed": False}, "block"),
            ({"screen": screen, "trace": "trace.zip"}, None),
        )
        for raw, label in cases:
            with self.subTest(raw=raw):
                artifact = None if raw is None else BrowserProof.model_validate({
                    "screen": None, "trace": None, "passed": None, **raw,
                }).record()
                self.assertEqual(browser_proof_label(slice, artifact), label)

    def test_formal_evidence_boundary(self):
        slice = {"properties": [{"name": "Positive amount", "constraint": "positive_amount"}]}
        cases = (
            (None, "insufficient_evidence"),
            ({}, "insufficient_evidence"),
            ({"ran": False, "typecheck": "passed"}, "insufficient_evidence"),
            ({"constraints": []}, "insufficient_evidence"),
            ({"typecheck": "passed"}, "block"),
            ({"constraints": [{"constraint": "positive_amount", "rejected": False}]}, "block"),
            ({"constraints": [{"constraint": "owner_required", "rejected": True}]}, "insufficient_evidence"),
        )
        for raw, label in cases:
            with self.subTest(raw=raw):
                artifact = None if raw is None else FormalProof.model_validate({
                    "ran": None, "constraints": None, "typecheck": None, **raw,
                }).record()
                self.assertEqual(formal_proof_label(slice, artifact), label)

        rejected_amount = FormalProof.model_validate({
            "ran": None, "typecheck": None,
            "constraints": [{"constraint": "positive_amount", "rejected": True}],
        }).record()
        self.assertIsNone(formal_proof_label(slice, rejected_amount))

        two_properties = {"properties": [*slice["properties"], {"name": "Owner is required", "constraint": "owner_required"}]}
        self.assertEqual(formal_proof_label(two_properties, rejected_amount), "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
