import json
import unittest
from pathlib import Path

from foreman.dispatch import InvalidOutput
from foreman.spec_check import AcceptedSpecCheck, SpecHistory, SpecRound, accept
from foreman.stage_output import PlanState, SpecAuthor, SpecCandidate, SpecCheckOutput, parse_output
from test_spec_candidate import SLICE


FINDING = dict(id="f1", kind="contract", slice="Save", section="slice", summary="Keep IDs unique.", ruling_id=None)


def output(findings=(), notes=()):
    return parse_output(SpecCheckOutput, dict(findings=list(findings), notes=list(notes), human_question=None))


def candidate(**changes):
    raw = dict(slices=[SLICE], spec="Save with a unique ID.", work_kind="feature",
               author=SpecAuthor(stage="spec", model="gpt-test", family="gpt"))
    return SpecCandidate.model_validate({**raw, **changes})


class SpecCheckTests(unittest.TestCase):
    def first(self):
        return accept(SpecHistory(), output([FINDING]), candidate())

    def test_native_fixture_and_contradictions(self):
        raw = json.loads(Path("tests/fixtures/spec-check.json").read_text())
        checked = parse_output(SpecCheckOutput, raw)
        self.assertEqual(accept(SpecHistory(), checked, candidate()).verdict, "gaps")
        self.assertEqual(accept(SpecHistory(), output(), candidate()).verdict, "clear")
        for verdict in ("clear", "gaps"):
            with self.assertRaises(InvalidOutput):
                parse_output(SpecCheckOutput, {**raw, "verdict": verdict})
        for summary in ("", " \t"):
            with self.assertRaises(InvalidOutput):
                output([{**FINDING, "summary": summary}])
        self.assertEqual(output([{**FINDING, "summary": "  Valid.  "}]).findings[0].summary, "Valid.")

    def test_clear_approval_is_bound_and_round_history_roundtrips(self):
        accepted = accept(SpecHistory(), output(notes=[FINDING]), candidate())
        self.assertEqual(accepted.verdict, "clear")
        self.assertTrue(accepted.approval.matches(candidate()))
        self.assertFalse(accepted.approval.matches(candidate(spec="Changed")))
        self.assertEqual(SpecHistory.model_validate_json(accepted.history.model_dump_json()), accepted.history)
        self.assertEqual(accepted.history.rounds[0].notes[0].id, "f1")
        self.assertEqual(AcceptedSpecCheck.model_validate_json(accepted.model_dump_json()), accepted)
        for history, approval in ((self.first().history, accepted.approval),
                                  (accepted.history, None),
                                  (accepted.history, {"revision": "0" * 64, "round": 1}),
                                  (accepted.history, {"revision": candidate().revision, "round": 2})):
            with self.subTest(approval=approval), self.assertRaisesRegex(ValueError, "approval must match"):
                AcceptedSpecCheck(history=history, approval=approval)
            raw = dict(history=json.loads(history.model_dump_json()), approval=approval.model_dump() if hasattr(approval, "model_dump") else approval)
            with self.assertRaisesRegex(ValueError, "approval must match"):
                AcceptedSpecCheck.model_validate_json(json.dumps(raw))

    def test_rounds_store_only_the_checked_candidate_and_its_report(self):
        raw = self.first().history.model_dump()
        for extra in ("resolutions", "interdiff"):
            entry = {**raw["rounds"][0], extra: {}}
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
                SpecRound(**entry)

    def test_targets_missing_renamed_and_duplicate_titles(self):
        for payload in (output([{**FINDING, "slice": "Missing"}]), output(notes=[{**FINDING, "slice": "Missing"}])):
            with self.assertRaisesRegex(InvalidOutput, "unknown candidate slice"):
                accept(SpecHistory(), payload, candidate())
        renamed = candidate(slices=[{**SLICE, "title": "Renamed"}])
        with self.assertRaisesRegex(InvalidOutput, "unknown candidate slice"):
            accept(self.first().history, output([FINDING]), renamed)
        accepted = accept(self.first().history, output([{**FINDING, "slice": "Renamed"}]), renamed)
        self.assertEqual(accepted.verdict, "gaps")
        self.assertEqual(accept(accepted.history, output(), renamed).verdict, "clear")
        with self.assertRaisesRegex(ValueError, "titles must be unique"):
            candidate(slices=[SLICE, SLICE])

    def test_ids_are_unique_within_a_round_only(self):
        for payload in (dict(findings=[FINDING, FINDING]), dict(findings=[FINDING], notes=[FINDING])):
            with self.subTest(payload=payload), self.assertRaisesRegex(InvalidOutput, "unique within a round"):
                accept(SpecHistory(), output(**payload), candidate())
        # Engine #1225: round 3 repeated an earlier note's ID. Each round reports on its own candidate.
        noted = accept(SpecHistory(), output(notes=[FINDING]), candidate())
        changed = candidate(spec="Changed")
        second = accept(noted.history, output([FINDING]), changed)
        self.assertEqual(second.verdict, "gaps")
        for payload in (output(notes=[FINDING]), output([{**FINDING, "summary": "Different gap."}])):
            with self.subTest(payload=payload):
                third = accept(second.history, payload, candidate(spec="Again"))
                self.assertEqual(len(third.history.rounds), 3)
        self.assertEqual(accept(second.history, output(notes=[FINDING]), changed).verdict, "clear")

    def test_verdict_and_clearance_follow_the_latest_check_of_a_revision(self):
        gaps = self.first()
        self.assertEqual([f.id for f in gaps.findings], ["f1"])
        self.assertFalse(gaps.history.clears(candidate().revision))
        cleared = accept(gaps.history, output(), candidate())
        self.assertTrue(cleared.history.clears(candidate().revision))
        self.assertTrue(cleared.approval.matches(candidate()))
        reopened = accept(cleared.history, output([FINDING]), candidate())
        self.assertFalse(reopened.history.clears(candidate().revision))
        self.assertFalse(cleared.history.clears(candidate(spec="Unchecked").revision))
        plan = PlanState(spec_candidate=candidate().record(), spec_history={"rounds": cleared.history.rounds})
        self.assertTrue(SpecHistory.cleared(plan))
        self.assertFalse(SpecHistory.cleared(plan.model_copy(update={"spec_candidate": None})))

    def test_settled_rulings_turn_findings_into_notes_and_unknown_rulings_fail(self):
        past = SpecHistory(settled=[dict(id="design:1", text="Use a unique ID.")])
        current = candidate()
        accepted = accept(past, output([{**FINDING, "ruling_id": "design:1"}]), current)
        self.assertEqual(accepted.verdict, "clear")
        self.assertEqual(accepted.history.rounds[0].notes[0].id, "f1")
        self.assertEqual(accepted.history.settled, past.settled)
        for payload in (output([{**FINDING, "ruling_id": "unknown"}]), output(notes=[{**FINDING, "ruling_id": "unknown"}]),
                        output([{**FINDING, "ruling_id": "finding:f1"}])):
            with self.subTest(payload=payload), self.assertRaisesRegex(InvalidOutput, "unknown settled ruling"):
                accept(self.first().history, payload, current)

    def test_candidate_snapshot_is_preserved(self):
        current = candidate()
        accepted = accept(SpecHistory(), output(), current)
        checked_revision = accepted.history.rounds[0].candidate["revision"]
        current.slices[0].acceptance = "Changed after acceptance"
        self.assertFalse(accepted.approval.matches(current))
        self.assertEqual(accepted.history.rounds[0].candidate["revision"], checked_revision)
        raw = json.loads(accepted.history.model_dump_json())
        raw["rounds"][0]["candidate"]["spec"] = "Tampered"
        with self.assertRaises(ValueError):
            SpecHistory.model_validate(raw)

