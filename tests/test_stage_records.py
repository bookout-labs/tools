import unittest

from pydantic import TypeAdapter, ValidationError

from foreman.stage_output import (
    PendingDispatch, PendingDispatchRecord, DispatchPlan, DispatchPlanRecord, Attempt, AttemptRecord, AttemptUsage, AttemptUsageRecord, ConstraintResult, ConstraintResultRecord,
    BrowserProof, BrowserProofRecord, Finding, FindingRecord, FormalProof, FormalProofRecord,
    MergeRisk, MergeRiskRecord, Seam, SeamRecord, Slice, SliceRecord, SliceProperty,
    SlicePropertyRecord, StoredSlice, ToolFact, ToolFactRecord,
)


class StageRecordTests(unittest.TestCase):
    def test_validated_records_round_trip(self):
        usage = {"input": 3, "output": 4, "cache_read": 0, "cache_creation": 0, "cost_usd": None}
        cases = (
            (PendingDispatch, PendingDispatchRecord, {"stage": "triage", "index": 0, "quota_model": None}),
            (DispatchPlan, DispatchPlanRecord, {"models": [], "instruction": "test", "methods": [], "target": {"state": "OPEN", "number": 1, "headRefOid": "h", "headRefName": "branch", "baseRefName": "main", "baseRefOid": "b", "isCrossRepository": False, "autoMergeRequest": None}, "scope": {"command": "diff", "inline": [("a.py", 2)], "eligible": []}}),
            (AttemptUsage, AttemptUsageRecord, usage),
            (Attempt, AttemptRecord, {"stage": "triage", "model": None, "commit": "pin", "methods": [], "outcome": "ran", "usage": usage}),
            (ConstraintResult, ConstraintResultRecord, {"constraint": "positive", "rejected": True}),
            (BrowserProof, BrowserProofRecord, {"screen": None, "trace": None, "passed": None}),
            (FormalProof, FormalProofRecord, {"ran": True, "constraints": [{"constraint": "positive", "rejected": True}], "typecheck": False}),
            (Finding, FindingRecord, {"id": "f1", "kind": "correctness", "fix": "Rename.", "ruling_id": None, "file": "a.py", "line": 1, "severity": "nit", "summary": "Rename."}),
            (MergeRisk, MergeRiskRecord, dict.fromkeys(("alignment", "impact", "verification", "rollback", "error_detection"), "Evidence") | {"level": "low"}),
            (Seam, SeamRecord, {"name": "db", "exists": True, "path": "db.py"}),
            (SliceProperty, SlicePropertyRecord, {"name": "Positive", "constraint": "positive"}),
            (ToolFact, ToolFactRecord, {"claim": "Tool accepts JSON.", "fixture": None, "graph_check": None}),
            (ToolFact, ToolFactRecord, {"claim": "Checked.", "fixture": None, "graph_check": {"head": "a" * 40, "command": "exit 0", "exit_code": 0}}),
            (Slice, SliceRecord, {"title": "Ship", "stateful": False, "acceptance": "Pass", "builds": [], "uses": [], "screen": "", "properties": []}),
        )
        for model, record, raw in cases:
            with self.subTest(model=model.__name__):
                self.assertEqual(set(model.model_fields), set(record.__annotations__) - {"depends_on", "blocker_numbers"})
                value = model.model_validate(raw).record()
                self.assertEqual(value, raw)
                self.assertEqual(model.model_validate(value).record(), raw)
                self.assertEqual(TypeAdapter(record).validate_python(value, strict=True), raw)
                for key in record.__required_keys__:
                    with self.assertRaises(ValidationError):
                        TypeAdapter(record).validate_python({k: v for k, v in value.items() if k != key}, strict=True)
                with self.assertRaises(ValidationError):
                    model.model_validate({**raw, "extra": True})
                with self.assertRaises(ValidationError):
                    TypeAdapter(record).validate_python({**raw, "extra": True}, strict=True)
        raw = {"models": [], "instruction": "review", "methods": [], "target": None, "scope": None}
        for field, invalid in (("target", {}), ("scope", "{}"), ("scope", {"command": "diff", "inline": ["bad"], "eligible": None})):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                DispatchPlan.model_validate({**raw, field: invalid})
        for extra in ({"index": -1}, {"model": "worker"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                PendingDispatch.model_validate({"stage": "review", "index": 0, "quota_model": None, **extra})

    def test_attempt_usage_rejects_negative_counts(self):
        usage = {"input": 3, "output": 4, "cache_read": 0, "cache_creation": 0, "cost_usd": None}
        for key in ("input", "output", "cache_read", "cache_creation"):
            for shape in (AttemptUsage, TypeAdapter(AttemptUsageRecord)):
                with self.subTest(key=key, shape=shape), self.assertRaises(ValidationError):
                    if shape is AttemptUsage:
                        shape.model_validate({**usage, key: -1})
                    else:
                        shape.validate_python({**usage, key: -1}, strict=True)

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

    def test_legacy_checkpoint_restores_slice_defaults(self):
        from foreman.runner import checkpoint_state
        state = checkpoint_state({"token_cap": 20_000_000, "repo": "example/repo", "check": "true", "path": [], "slice": {"title": "Ship", "acceptance": "Pass", "screen": "", "properties": []}})
        self.assertFalse(state["slice"]["stateful"])
        self.assertEqual(state["slice"]["builds"], [])
        self.assertEqual(state["slice"]["uses"], [])

    def test_attempt_record_keeps_usage_and_rejects_extra_fields(self):
        usage = AttemptUsage(input=3, output=4, cache_read=0, cache_creation=0, cost_usd=None)
        record = Attempt(stage="triage", model=None, commit="pin", methods=[], outcome="ran", usage=usage).record()
        self.assertEqual(record["usage"], {"input": 3, "output": 4, "cache_read": 0, "cache_creation": 0, "cost_usd": None})
        for shape, value in ((AttemptRecord, record), (AttemptUsageRecord, record["usage"])):
            with self.subTest(shape=shape), self.assertRaises(ValidationError):
                TypeAdapter(shape).validate_python({**value, "extra": True}, strict=True)
        with self.assertRaises(ValidationError):
            TypeAdapter(AttemptRecord).validate_python({**record, "usage": {"input": 3}}, strict=True)

class StageOwnershipTests(unittest.TestCase):
    def test_new_stage_requires_update(self):
        from foreman.stage_output import StageOutput

        class NewStage(StageOutput):
            answer: str

        with self.assertRaises(TypeError):
            NewStage(answer="Done")

    def test_every_registered_stage_owns_its_update(self):
        from foreman.delivery import stage_update
        from foreman.dry_run import ScriptHandler
        from foreman.stage_output import STAGE_OUTPUTS, parse_output

        state = {"repo": "org/repo", "check": "", "path": [], "issue_kind": "slice", "review_rounds": 2}
        handler = ScriptHandler(reviews=["changes"], verify="fail")
        for stage, contract in STAGE_OUTPUTS.items():
            with self.subTest(stage=stage):
                output = parse_output(contract, handler(stage, None, state, "").output)
                update = stage_update(stage, output, state, "model")
                key = {"implement": "implementation", "review": "findings"}.get(stage, stage)
                self.assertIn(key, update)
                self.assertEqual(update["path"], [stage])
                self.assertEqual(update["models_used"][stage], "model")
                if stage == "verify":
                    self.assertEqual(update["review_rounds"], 3)

    def test_checkpoint_rejects_incomplete_nested_record(self):
        from foreman.delivery import read_delivery_state

        with self.assertRaises(ValidationError):
            read_delivery_state({"repo": "org/repo", "check": "", "path": [], "stop": "sliced", "slice": {"title": "Incomplete"}})
