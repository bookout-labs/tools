import unittest
from dataclasses import dataclass, replace

from foreman.dispatch import InvalidOutput
from foreman.resolution import ResolutionLedger
from foreman.stage_output import FindingResolution, SettledRuling


@dataclass(frozen=True)
class Finding:
    id: str
    kind: str = "contract"
    severity: str = "blocking"
    summary: str = "Preserve the contract."
    ruling_id: str | None = None


class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self.finding = Finding("first")
        self.past = ResolutionLedger[Finding]([([self.finding], {})], [])
        self.resolved = FindingResolution(id="first", status="resolved")

    def test_resolution_requires_every_unresolved_id_exactly_once(self):
        for entries in ([], [self.resolved, self.resolved],
                        [FindingResolution(id="unknown", status="resolved")]):
            with self.subTest(entries=entries), self.assertRaisesRegex(InvalidOutput, "exactly once"):
                self.past.account([], entries)
        entries = [FindingResolution(id="first", status="unresolved")]
        self.assertEqual(self.past.account([], entries), {"first": "unresolved"})
        self.assertEqual(self.past.unresolved(), [self.finding])
        self.assertIsNone(self.past.recurrence([]))

    def test_duplicate_new_and_reused_ids_are_rejected(self):
        duplicate = Finding("new")
        for findings in ([duplicate, duplicate], [self.finding]):
            with self.subTest(findings=findings), self.assertRaisesRegex(InvalidOutput, "IDs must be unique"):
                self.past.account(findings, [self.resolved])
        settled = ResolutionLedger[Finding]([([self.finding], {}), ([], {"first": "resolved"})], [])
        with self.assertRaisesRegex(InvalidOutput, "IDs must be unique"):
            settled.account([self.finding], [])
        self.assertEqual(settled.account([duplicate], []), {})

    def test_rulings_preserve_order_and_only_settle_resolved_findings(self):
        other = Finding("other")
        explicit = SettledRuling(id="design", text="Keep this decision.")
        past = ResolutionLedger[Finding](
            [([self.finding, other], {}), ([], {"first": "resolved", "other": "unresolved"})], [explicit],
        )
        self.assertEqual(past.unresolved(), [other])
        self.assertEqual(past.rulings(), [explicit, SettledRuling(id="finding:first", text=self.finding.summary)])
        for ruling in ("design", "finding:first", None):
            past.check_ruling(Finding("new", ruling_id=ruling))
        for ruling in ("missing", "finding:other", ""):
            with self.subTest(ruling=ruling), self.assertRaisesRegex(InvalidOutput, "unknown settled ruling"):
                past.check_ruling(Finding("new", ruling_id=ruling))

    def test_recurrence_uses_all_prior_findings_and_only_new_blockers(self):
        other = Finding("other", kind="scope")
        past = ResolutionLedger[Finding](
            [([self.finding], {}), ([other], {"first": "resolved"})], [],
        )
        new = Finding("new")
        self.assertEqual(past.recurrence([new]), (self.finding, new))
        self.assertIsNone(past.recurrence([replace(new, severity="nit")]))
        self.assertIsNone(past.recurrence([replace(new, kind="proof")]))
        nit_past = ResolutionLedger[Finding]([([replace(self.finding, severity="nit")], {})], [])
        self.assertIsNone(nit_past.recurrence([new]))
