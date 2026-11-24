import unittest

from foreman.github import DOING_LABEL, DONE_LABEL, NEEDS_ANSWER_LABEL
from foreman.outcomes import HALTS, PAUSES, RELEASED_STOPS, Halt
from foreman.readiness import MergeState
from foreman.stage_output import HumanPrompt
from foreman.state import ChecksState, DeliveryResult, PlanState, ReviewState, RunState
from foreman.status import status_of

HEAD, MERGE = "4f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d", "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d"


def result(stop, *, base="b" * 40, **fields) -> DeliveryResult:
    return {"run": RunState(repo="Example/Engine", issue=8, base_sha=base), "plan": PlanState(), "checks": ChecksState(verified_head=HEAD),
            "review": ReviewState(), "path": ["merge"], "stop": stop, **fields}


class StatusTest(unittest.TestCase):
    def test_every_checkpoint_maps_to_one_label(self):
        self.assertIsNone(status_of(result("blocked"), "triage", None, "bookout-labs/tools"))
        self.assertIsNone(status_of(result(""), "spec", None, "bookout-labs/tools"))
        for stop in RELEASED_STOPS:
            with self.subTest(stop=stop):
                self.assertEqual(status_of(result(stop), "merge", None, "bookout-labs/tools").label, DONE_LABEL)
        for stop in PAUSES:
            with self.subTest(stop=stop):
                self.assertEqual(status_of(result(stop), "merge", None, "bookout-labs/tools").label, DOING_LABEL)
        prompt = HumanPrompt(stage="spec", question="Which items?", revision="a" * 64)
        asked = status_of(result("needs_human"), "spec", prompt, "bookout-labs/tools")
        self.assertEqual((asked.label, asked.facts), (NEEDS_ANSWER_LABEL, [prompt.reply]))
        for code in Halt:
            with self.subTest(code=code):
                halted = result(code.value, halt=code.value, halt_detail="Synthetic detail.", watch={"issue": 26, "action": "filed"})
                admitted = status_of(halted, "merge", None, "bookout-labs/tools")
                self.assertEqual(admitted.label, DONE_LABEL)
                self.assertIn(f"Foreman halted with {code}. {HALTS[code].reason}", admitted.text)
                self.assertIn("Synthetic detail.", admitted.text)
                self.assertEqual("Rerun Foreman on this issue to resume." in admitted.text, HALTS[code].recovery == "retry")
                self.assertEqual(admitted.facts, ["Watch: bookout-labs/tools#26"])
                # Before admission Foreman owns no label.
                self.assertIsNone(status_of(result(code.value, base="", halt=code.value, halt_detail="Synthetic detail."), "triage", None, "x/y").label)

    def test_outcomes_carry_their_links(self):
        merged = status_of(result("merged", merge=MergeState(merge_commit_sha=MERGE, observation={"pr": {"number": 12}}),
                                  released=["Example/Engine#9"]), "merge", None, "bookout-labs/tools")
        self.assertEqual(merged.facts, ["Pull request: #12", f"Verified head: {HEAD}", f"Merge commit: {MERGE}", "Released: Example/Engine#9"])
        closed = status_of(result("closed", dispatch_plan={"target": {"number": 14}}), "merge", None, "bookout-labs/tools")
        self.assertEqual(closed.facts, ["Pull request: #14"])
        filed = status_of(result("children_filed", released=["Example/Engine#101"]), "spec", None, "bookout-labs/tools")
        self.assertEqual(filed.facts, ["Released: Example/Engine#101"])
