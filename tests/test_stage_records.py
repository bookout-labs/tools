"""Stage records and ownership contracts."""
import tests
from foreman.stages import STAGES
from foreman.state import ReviewState, PlanState, ChecksState, RunState, SCHEMA_VERSION
from foreman.dry_run import DEFAULT_RISK
from foreman.review_context import SCRIPTED_REVIEW_CONTEXT
import unittest

from pydantic import TypeAdapter, ValidationError

from foreman.stage_output import (
    CandidateSlice, CandidateSliceRecord, SpecAuthor, SpecAuthorRecord, SpecCandidate, SpecCandidateRecord, SliceProvenance, SliceProvenanceRecord,
    PendingDispatch, PendingDispatchRecord, DispatchPlan, DispatchPlanRecord, Attempt, AttemptRecord, AttemptUsage, AttemptUsageRecord, ConstraintResult, ConstraintResultRecord,
    BrowserProof, BrowserProofRecord, Finding, FindingRecord, FormalProof, FormalProofRecord,
    MergeRisk, MergeRiskRecord, Seam, SeamRecord, Slice, SliceRecord, SliceProperty,
    SlicePropertyRecord, StoredSlice, ToolFact, ToolFactRecord,
)


class StageRecordTests(unittest.TestCase):
    def test_validated_records_round_trip(self):
        author = {"stage": "spec", "model": "gpt-test", "family": "gpt"}
        slice = {"title": "Ship", "stateful": False, "acceptance": "Pass", "builds": [], "uses": [], "screen": "", "properties": [], "depends_on": [], "blocker_numbers": []}
        candidate = SpecCandidate(slices=[CandidateSlice.model_validate(slice)], spec="Ship", work_kind="feature", author=SpecAuthor.model_validate(author))
        usage = {"input": 3, "output": 4, "cache_read": 0, "cache_creation": 0, "cost_usd": None}
        cases = (
            (SpecAuthor, SpecAuthorRecord, author),
            (SliceProvenance, SliceProvenanceRecord, {"author": author, "revision": candidate.revision}),
            (CandidateSlice, CandidateSliceRecord, slice),
            (SpecCandidate, SpecCandidateRecord, candidate.record()),
            (PendingDispatch, PendingDispatchRecord, {"stage": "triage", "index": 0, "quota_model": None}),
            (DispatchPlan, DispatchPlanRecord, {"budget_usd": None, "models": [], "instruction": "test", "methods": [], "read_roots": [], "target": {"state": "OPEN", "number": 1, "headRefOid": "h", "headRefName": "branch", "baseRefName": "main", "baseRefOid": "b", "isCrossRepository": False, "autoMergeRequest": None}, "scope": {"command": "diff", "inline": [("a.py", 2)]}}),
            (AttemptUsage, AttemptUsageRecord, usage),
            (Attempt, AttemptRecord, {"stage": "triage", "model": None, "commit": "pin", "methods": [], "outcome": "ran", "usage": usage}),
            (ConstraintResult, ConstraintResultRecord, {"constraint": "positive", "rejected": True}),
            (BrowserProof, BrowserProofRecord, {"screen": None, "trace": None, "passed": None}),
            (FormalProof, FormalProofRecord, {"ran": True, "constraints": [{"constraint": "positive", "rejected": True}], "typecheck": False}),
            (Finding, FindingRecord, {"id": "f1", "kind": "correctness", "fix": "Rename.", "ruling_id": None, "file": "a.py", "line": 1, "severity": "nit", "summary": "Rename."}),
            (MergeRisk, MergeRiskRecord, DEFAULT_RISK),
            (Seam, SeamRecord, {"name": "db", "exists": True, "path": "db.py"}),
            (SliceProperty, SlicePropertyRecord, {"name": "Positive", "constraint": "positive"}),
            (ToolFact, ToolFactRecord, {"claim": "Tool accepts JSON.", "fixture": None, "graph_check": None}),
            (ToolFact, ToolFactRecord, {"claim": "Checked.", "fixture": None, "graph_check": {"head": "a" * 40, "command": "exit 0", "exit_code": 0}}),
            (Slice, SliceRecord, {"title": "Ship", "stateful": False, "acceptance": "Pass", "builds": [], "uses": [], "screen": "", "properties": []}),
        )
        for model, record, raw in cases:
            with self.subTest(model=model.__name__):
                optional = {"depends_on", "blocker_numbers", "provenance"} if model is Slice else set()
                self.assertEqual(set(model.model_fields) | set(model.model_computed_fields), set(record.__annotations__) - optional)
                value = (SpecCandidate.from_record(raw) if model is SpecCandidate else model.model_validate(raw, context=SCRIPTED_REVIEW_CONTEXT)).record()
                self.assertEqual(value, raw)
                self.assertEqual((SpecCandidate.from_record(value) if model is SpecCandidate else model.model_validate(value, context=SCRIPTED_REVIEW_CONTEXT)).record(), raw)
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

    def test_legacy_review_scope_remains_readable(self):
        from foreman.workspace import ReviewScope
        scope = {"command": "diff", "inline": [("a.py", 2)], "eligible": []}
        plan = DispatchPlan.model_validate({"models": [], "instruction": "review", "methods": [],
                                           "target": None, "scope": scope})
        self.assertEqual(TypeAdapter(ReviewScope).validate_python(plan.scope).record(),
                         {"command": "diff", "inline": [("a.py", 2)]})

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

    def test_candidate_nested_boundaries_reject_coercion(self):
        from test_spec_candidate import CandidateTests, SLICE
        candidate = CandidateTests().candidate().record()
        for model, record, raw in (
            (SpecAuthor, SpecAuthorRecord, {"stage": "spec", "model": 1, "family": "gpt"}),
            (SliceProvenance, SliceProvenanceRecord, {"author": {"stage": "spec", "model": 1, "family": "gpt"}, "revision": "0" * 64}),
            (CandidateSlice, CandidateSliceRecord, {**SLICE, "stateful": "true"}),
            (SpecCandidate, SpecCandidateRecord, {**candidate, "slices": [{**SLICE, "stateful": "true"}]}),
        ):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError):
                    (SpecCandidate.from_record(raw) if model is SpecCandidate else model.model_validate(raw))
                with self.assertRaises(ValidationError):
                    TypeAdapter(record).validate_python(raw, strict=True)

    def test_stored_slice_keeps_checkpoint_metadata(self):
        raw = {"title": "Ship", "acceptance": "Pass", "screen": "", "properties": [],
               "depends_on": ["Database"], "blocker_numbers": [17], "work_kind": "feature"}
        record = StoredSlice.model_validate(raw).record()
        self.assertEqual(record["depends_on"], ["Database"])
        self.assertEqual(record["blocker_numbers"], [17])
        self.assertNotIn("work_kind", record)

    def test_current_checkpoint_restores_slice_defaults(self):
        from foreman.runner import checkpoint_state
        state = checkpoint_state({"review": ReviewState(), "schema_version": SCHEMA_VERSION, "run": RunState(token_cap=20_000_000, repo="example/repo"), "checks": ChecksState(check="true"), "path": [], "plan": PlanState(slice={"title": "Ship", "acceptance": "Pass", "screen": "", "properties": []})})
        self.assertFalse(state["plan"].slice["stateful"])
        self.assertEqual(state["plan"].slice["builds"], [])
        self.assertEqual(state["plan"].slice["uses"], [])

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
        from foreman.review_context import SCRIPTED_REVIEW_CONTEXT
        from foreman.stage_output import parse_output

        state = {"review": ReviewState(), "run": RunState(repo="org/repo"), "checks": ChecksState(check=""), "path": [], "plan": PlanState(issue_kind="slice")}
        handler = ScriptHandler(reviews=["changes"])
        for stage, row in STAGES.items():
            if (contract := row.output) is None:
                continue
            with self.subTest(stage=stage):
                if stage == "spec_check":
                    with self.assertRaisesRegex(NotImplementedError, "candidate-bound"):
                        stage_update(stage, parse_output(contract, {"findings": [], "notes": [], "resolutions": []}), state, "model")
                    continue
                payload = {"verify": "fail"} if stage == "verify" else handler(stage, None, state, "").output
                output = parse_output(contract, payload, context=SCRIPTED_REVIEW_CONTEXT)
                update = stage_update(stage, output, state, "model")
                key = {"implement": "implementation", "review": "review", "verify": "checks", "triage": "plan", "spec": "plan"}.get(stage, stage)
                self.assertIn(key, update)
                self.assertEqual(update["path"], [stage])
                self.assertEqual(update["models_used"][stage], "model")
                if stage == "verify":
                    self.assertEqual(update["checks"].verify, "fail")
                    self.assertEqual(update["checks"].verify_failures, 1)

    def test_checkpoint_rejects_incomplete_nested_record(self):
        from foreman.delivery import read_delivery_state

        with self.assertRaises(ValidationError):
            read_delivery_state({"review": ReviewState(), "run": RunState(repo="org/repo"), "checks": ChecksState(check=""), "path": [], "stop": "sliced", "plan": {"slice": {"title": "Incomplete"}}})
