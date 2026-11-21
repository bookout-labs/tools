import unittest

from pydantic import TypeAdapter, ValidationError

from foreman.stage_output import (
    BrowserProof, BrowserProofRecord, Finding, FindingRecord, FormalProof, FormalProofRecord,
    MergeRisk, MergeRiskRecord, Seam, SeamRecord, Slice, SliceRecord, SliceProperty,
    SlicePropertyRecord, StoredSlice, ToolFact, ToolFactRecord,
)


class StageRecordTests(unittest.TestCase):
    def test_validated_records_round_trip(self):
        cases = (
            (BrowserProof, BrowserProofRecord, {"screen": None, "trace": None, "passed": None}),
            (FormalProof, FormalProofRecord, {"ran": True, "constraints": [{"constraint": "positive", "rejected": True}], "typecheck": False}),
            (Finding, FindingRecord, {"file": "a.py", "line": 1, "severity": "nit", "summary": "Rename."}),
            (MergeRisk, MergeRiskRecord, dict.fromkeys(("alignment", "impact", "verification", "rollback", "error_detection"), "Evidence") | {"level": "low"}),
            (Seam, SeamRecord, {"name": "db", "exists": True, "path": "db.py"}),
            (SliceProperty, SlicePropertyRecord, {"name": "Positive", "constraint": "positive"}),
            (ToolFact, ToolFactRecord, {"claim": "Tool accepts JSON.", "fixture": None}),
            (Slice, SliceRecord, {"title": "Ship", "stateful": False, "acceptance": "Pass", "builds": [], "uses": [], "screen": "", "properties": []}),
        )
        for model, record, raw in cases:
            with self.subTest(model=model.__name__):
                value = model.model_validate(raw).record()
                self.assertEqual(value, raw)
                self.assertEqual(TypeAdapter(record).validate_python(value, strict=True), raw)
                for key in record.__required_keys__:
                    with self.assertRaises(ValidationError):
                        TypeAdapter(record).validate_python({k: v for k, v in value.items() if k != key}, strict=True)
                with self.assertRaises(ValidationError):
                    model.model_validate({**raw, "extra": True})

    def test_nested_boundaries_reject_coercion(self):
        for raw in ({"ran": "true", "constraints": None, "typecheck": None},
                    {"ran": True, "constraints": [{"constraint": "positive", "rejected": 1}], "typecheck": None}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                FormalProof.model_validate(raw)

    def test_stored_slice_keeps_checkpoint_metadata(self):
        raw = {"title": "Ship", "acceptance": "Pass", "screen": "", "properties": [],
               "depends_on": ["Database"], "blocker_numbers": [17], "work_kind": "feature"}
        record = StoredSlice.model_validate(raw).record()
        self.assertEqual(record["depends_on"], ["Database"])
        self.assertEqual(record["blocker_numbers"], [17])
        self.assertNotIn("work_kind", record)
