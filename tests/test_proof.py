from copy import deepcopy
import unittest

from foreman.proof import (
    ProofError,
    accept_browser_proof,
    accept_formal_proof,
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

    def test_screen_with_trace_is_accepted(self):
        self.assertTrue(accept_browser_proof(
            self.slice,
            {"screen": "docs/screens/deals.md", "trace": "artifacts/deals.zip"},
        ))

    def test_screen_without_trace_is_rejected(self):
        for trace in (None, "", "  ", 1):
            with self.subTest(trace=trace), self.assertRaises(ProofError):
                accept_browser_proof(self.slice, {"screen": self.slice["screen"], "trace": trace})
        with self.assertRaises(ProofError):
            accept_browser_proof(self.slice, {"screen": self.slice["screen"]})

    def test_trace_for_another_screen_is_rejected(self):
        with self.assertRaises(ProofError):
            accept_browser_proof(
                self.slice,
                {"screen": "docs/screens/contacts.md", "trace": "artifacts/contacts.zip"},
            )

    def test_missing_browser_artifact_is_rejected(self):
        with self.assertRaises(ProofError):
            accept_browser_proof(self.slice, None)

    def test_property_discharged_by_rejection_is_accepted(self):
        self.assertTrue(accept_formal_proof(self.slice, {
            "constraints": [{"constraint": "deals_amount_positive", "rejected": True}],
        }))

    def test_typecheck_alone_is_rejected(self):
        with self.assertRaises(ProofError):
            accept_formal_proof(self.slice, {"typecheck": "passed"})

    def test_every_property_needs_its_named_constraint(self):
        self.slice["properties"].append(
            {"name": "Deal has an owner", "constraint": "deals_owner_required"}
        )
        artifact = {"constraints": [
            {"constraint": "deals_amount_positive", "rejected": True},
        ]}
        with self.assertRaises(ProofError):
            accept_formal_proof(self.slice, artifact)
        artifact["constraints"].append({"constraint": "deals_owner_required", "rejected": True})
        self.assertTrue(accept_formal_proof(self.slice, artifact))

    def test_rejection_must_be_boolean_true(self):
        for rejected in (False, None, 1, "true"):
            with self.subTest(rejected=rejected), self.assertRaises(ProofError):
                accept_formal_proof(self.slice, {"constraints": [
                    {"constraint": "deals_amount_positive", "rejected": rejected},
                ]})

    def test_missing_or_wrong_constraint_is_rejected(self):
        for evidence in ([], [{"constraint": "other_constraint", "rejected": True}]):
            with self.subTest(evidence=evidence), self.assertRaises(ProofError):
                accept_formal_proof(self.slice, {"constraints": evidence})

    def test_malformed_formal_evidence_is_rejected(self):
        for artifact in (None, {}, {"constraints": {}}, {"constraints": [None]},
                         {"constraints": [{"rejected": True}]}):
            with self.subTest(artifact=artifact), self.assertRaises(ProofError):
                accept_formal_proof(self.slice, artifact)

    def test_empty_requirements_need_no_artifact(self):
        slice = {"screen": "", "properties": []}
        self.assertTrue(accept_browser_proof(slice, None))
        self.assertTrue(accept_formal_proof(slice, None))

    def test_malformed_requirements_are_rejected(self):
        with self.assertRaises(ProofError):
            browser_required({"screen": None})
        for properties in (None, {}, [None], [{"name": "Amount is positive"}],
                           [{"name": "", "constraint": "deals_amount_positive"}]):
            with self.subTest(properties=properties), self.assertRaises(ProofError):
                formal_required({"properties": properties})

    def test_score_verdicts(self):
        self.assertEqual(score_verdict("approve"), "pass")
        self.assertEqual(score_verdict("block"), "fail")
        self.assertEqual(score_verdict("insufficient_evidence"), "insufficient_evidence")

    def test_unknown_score_labels_are_rejected(self):
        for label in ("pass", "", None, []):
            with self.subTest(label=label), self.assertRaises(ProofError):
                score_verdict(label)

    def test_acceptance_does_not_mutate_inputs(self):
        browser = {"screen": self.slice["screen"], "trace": "artifacts/deals.zip"}
        formal = {"constraints": [{"constraint": "deals_amount_positive", "rejected": True}]}
        before = deepcopy((self.slice, browser, formal))
        accept_browser_proof(self.slice, browser)
        accept_formal_proof(self.slice, formal)
        self.assertEqual((self.slice, browser, formal), before)


if __name__ == "__main__":
    unittest.main()


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
