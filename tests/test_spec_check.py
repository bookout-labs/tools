import json
import unittest
from pathlib import Path

from foreman.dispatch import InvalidOutput
from foreman.spec_check import SpecHistory, accept
from foreman.stage_output import SpecAuthor, SpecCandidate, SpecCheckOutput, parse_output
from test_spec_candidate import SLICE


FINDING = dict(id="f1", kind="contract", slice="Save", section="slice", summary="Keep IDs unique.", ruling_id=None)


def output(findings=(), resolutions=(), notes=()):
    return parse_output(SpecCheckOutput, dict(findings=list(findings), resolutions=list(resolutions), notes=list(notes)))


def candidate(**changes):
    raw = dict(slices=[SLICE], spec="Save with a unique ID.", work_kind="feature",
               author=SpecAuthor(stage="spec", model="gpt-test", family="gpt"))
    return SpecCandidate.model_validate({**raw, **changes})


class SpecCheckTests(unittest.TestCase):
    def first(self):
        return accept(SpecHistory(), output([FINDING]), candidate(), max_rounds=3)

    def test_native_fixture_and_contradictions(self):
        raw = json.loads(Path("tests/fixtures/spec-check.json").read_text())
        checked = parse_output(SpecCheckOutput, raw)
        self.assertEqual(checked.verdict, "gaps")
        self.assertEqual(output().verdict, "clear")
        for verdict in ("clear", "gaps"):
            with self.assertRaises(InvalidOutput):
                parse_output(SpecCheckOutput, {**raw, "verdict": verdict})
        for summary in ("", " \t"):
            with self.assertRaises(InvalidOutput):
                output([{**FINDING, "summary": summary}])
        self.assertEqual(output([{**FINDING, "summary": "  Valid.  "}]).findings[0].summary, "Valid.")

    def test_clear_approval_is_bound_and_round_history_roundtrips(self):
        accepted = accept(SpecHistory(), output(notes=[FINDING]), candidate(), max_rounds=3)
        self.assertEqual(accepted.verdict, "clear")
        self.assertTrue(accepted.approval.matches(candidate()))
        self.assertFalse(accepted.approval.matches(candidate(spec="Changed")))
        self.assertEqual(SpecHistory.model_validate_json(accepted.history.model_dump_json()), accepted.history)
        self.assertEqual(accepted.history.rounds[0].notes[0].id, "f1")

    def test_targets_missing_renamed_and_duplicate_titles(self):
        for payload in (output([{**FINDING, "slice": "Missing"}]), output(notes=[{**FINDING, "slice": "Missing"}])):
            with self.assertRaisesRegex(InvalidOutput, "unknown candidate slice"):
                accept(SpecHistory(), payload, candidate(), max_rounds=3)
        renamed = candidate(slices=[{**SLICE, "title": "Renamed"}])
        with self.assertRaisesRegex(InvalidOutput, "unknown candidate slice"):
            accept(self.first().history, output([{**FINDING, "id": "f2"}], [{"id": "f1", "status": "resolved"}]), renamed, max_rounds=3)
        accepted = accept(self.first().history, output([{**FINDING, "id": "f2", "slice": "Renamed", "kind": "scope"}],
                          [{"id": "f1", "status": "resolved"}]), renamed, max_rounds=3)
        self.assertEqual(accepted.verdict, "gaps")
        with self.assertRaisesRegex(InvalidOutput, "titles must be unique"):
            accept(SpecHistory(), output(), candidate(slices=[SLICE, SLICE]), max_rounds=3)

    def test_accounting_missing_duplicate_unknown_and_reused_ids(self):
        resolved = {"id": "f1", "status": "resolved"}
        for entries, findings in (([], []), ([resolved, resolved], []),
                                  ([{"id": "unknown", "status": "resolved"}], []), ([resolved], [FINDING])):
            with self.subTest(entries=entries), self.assertRaises(InvalidOutput):
                accept(self.first().history, output(findings, entries), candidate(), max_rounds=3)
        with self.assertRaises(InvalidOutput):
            accept(SpecHistory(), output([FINDING, FINDING]), candidate(), max_rounds=3)

    def test_scope_uses_structural_slice_and_global_section_changes(self):
        past = self.first().history
        resolved = [{"id": "f1", "status": "resolved"}]
        new = {**FINDING, "id": "f2", "kind": "seam"}
        for changed in (candidate(), candidate(spec="Changed prose")):
            with self.assertRaisesRegex(InvalidOutput, "outside changed"):
                accept(past, output([new], resolved), changed, max_rounds=3)
        for changed, section in ((candidate(slices=[{**SLICE, "acceptance": "Changed"}]), "slice"),
                                 (candidate(spec="Changed prose"), "spec"),
                                 (candidate(work_kind="bug_fix"), "work_kind")):
            accepted = accept(past, output([{**new, "section": section}], resolved), changed, max_rounds=3)
            self.assertEqual(accepted.verdict, "gaps")
            self.assertIsNone(accepted.approval)
        with self.assertRaisesRegex(InvalidOutput, "outside changed"):
            accept(past, output([new], resolved), candidate(slices=[SLICE, {**SLICE, "title": "Other"}]), max_rounds=3)

    def test_settled_findings_become_notes_and_unknown_rulings_fail(self):
        resolved = [{"id": "f1", "status": "resolved"}]
        second = accept(self.first().history, output(resolutions=resolved), candidate(), max_rounds=3)
        note = {**FINDING, "id": "f2", "ruling_id": "finding:f1"}
        accepted = accept(second.history, output([note]), candidate(), max_rounds=3)
        self.assertEqual(accepted.verdict, "clear")
        self.assertEqual(accepted.history.rounds[-1].notes[0].id, "f2")
        self.assertEqual(accepted.unsettled, "")
        for payload in (output([{**note, "ruling_id": "unknown"}]), output(notes=[{**note, "ruling_id": "unknown"}])):
            with self.assertRaisesRegex(InvalidOutput, "unknown settled ruling"):
                accept(second.history, payload, candidate(), max_rounds=3)

    def test_recurrence_across_all_rounds_and_unresolved_at_cap(self):
        first = self.first()
        changed = candidate(spec="Changed")
        new = {**FINDING, "id": "f2", "section": "spec"}
        second = accept(first.history, output(resolutions=[{"id": "f1", "status": "resolved"}]), candidate(), max_rounds=3)
        repeated = accept(second.history, output([new]), changed, max_rounds=3)
        self.assertIn("Recurring spec kind contract", repeated.unsettled)
        self.assertIsNone(repeated.approval)
        unresolved = output(resolutions=[{"id": "f1", "status": "unresolved"}])
        second = accept(first.history, unresolved, candidate(), max_rounds=3)
        self.assertEqual(second.unsettled, "")
        capped = accept(second.history, unresolved, candidate(), max_rounds=3)
        self.assertEqual(capped.verdict, "gaps")
        self.assertIn("Unresolved spec findings at round cap: f1", capped.unsettled)
        self.assertIsNone(capped.approval)
        with self.assertRaisesRegex(InvalidOutput, "round cap"):
            accept(capped.history, unresolved, candidate(), max_rounds=3)
        self.assertEqual(len(first.history.rounds), 1)

    def test_human_ruling_and_candidate_snapshot_are_preserved(self):
        past = SpecHistory(settled=[dict(id="design:1", text="Use a unique ID.")])
        current = candidate()
        accepted = accept(past, output([{**FINDING, "ruling_id": "design:1"}]), current, max_rounds=3)
        self.assertEqual(accepted.verdict, "clear")
        self.assertEqual(accepted.history.accounting().rulings()[0].id, "design:1")
        checked_revision = accepted.history.rounds[0].candidate["revision"]
        current.slices[0].acceptance = "Changed after acceptance"
        self.assertFalse(accepted.approval.matches(current))
        self.assertEqual(accepted.history.rounds[0].candidate["revision"], checked_revision)
        raw = json.loads(accepted.history.model_dump_json())
        raw["rounds"][0]["candidate"]["spec"] = "Tampered"
        with self.assertRaises(ValueError):
            SpecHistory.model_validate(raw)
        self.assertEqual(past.rounds, [])

    def test_slice_order_is_part_of_the_structural_interdiff(self):
        other = {**SLICE, "title": "Other"}
        first = accept(SpecHistory(), output(), candidate(slices=[SLICE, other]), max_rounds=3)
        accepted = accept(first.history, output([FINDING]), candidate(slices=[other, SLICE]), max_rounds=3)
        self.assertEqual(accepted.verdict, "gaps")
