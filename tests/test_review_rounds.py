import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from foreman.delivery import LiveRun, compile_delivery
from foreman.config import load_config
from foreman.dispatch import FailoverDispatcher
from foreman.adapters import Outcome
from foreman.stage_output import ReviewOutput, parse_output
from test_runner import APPROVE, FINDING, BASE, HEAD, EMPTY_SLICE, FakeGitHub, FakeWorkspace


class ReviewRoundsTest(unittest.TestCase):
    def run_rounds(self, reviews, *, initial=None, crash=False, answer=False, bad_diff=False, gate_heads=None):
        self.prompts = []
        self.diffs = []
        self.gate_reads = []
        self.forge = FakeGitHub()
        with TemporaryDirectory() as directory:
            outer = self

            class Workspace(FakeWorkspace):
                current_head = HEAD

                def inspect(self):
                    return type(super().inspect())(self.current_head, True, "feature")

                def files_at(self, head, prefix):
                    outer.gate_reads.append(head)
                    return {"tests/fixtures/manifest.toml", "tests/fixtures/result.txt"} if head in (gate_heads or ()) else set()

                def read_file_at(self, head, path):
                    return '[fixtures."result.txt"]\nkind = "captured_run"\ntool = "example"\nversion = "1"\nargv = ["example"]'

                def review_diff(self, base, head, *, interdiff=False):
                    outer.diffs.append((base, head, interdiff))
                    diff = super().review_diff(base, head, interdiff=interdiff)
                    if bad_diff:
                        return type(diff)(diff.command, "--- a/x\n+++ b/x\n@@ -1 +1 @@\ninvalid\n")
                    return diff

            replies = iter(reviews)
            crashed = False
            workspace = Workspace(Path(directory))
            implementations = 0

            def dispatch(node, model, state, instruction):
                nonlocal crashed, implementations
                self.prompts.append((node, state, instruction))
                if node == "review":
                    raw = next(replies)
                    return Outcome("ran", parse_output(ReviewOutput, raw).model_dump())
                if crash and state.get("review_findings") and not crashed:
                    crashed = True
                    raise RuntimeError("process lost")
                if node == "triage":
                    return Outcome("ran", {"triage": "implement", "slice": None, "human_question": None})
                implementations += 1
                if gate_heads is not None and HEAD not in gate_heads:
                    workspace.current_head = str(implementations) * 40
                return Outcome("ran", {"implementation": "Fixed.", "head_sha": HEAD,
                                       "browser_proof": None, "formal_proof": None})

            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint")) as saver:
                graph = compile_delivery(FailoverDispatcher(dispatch), load_config(),
                                         run=LiveRun(self.forge, workspace), checkpointer=saver)
                config = {"configurable": {"thread_id": "rounds"}}
                state = {"repo": "Example/Repo", "issue": 8, "issue_kind": "slice", "slice": EMPTY_SLICE,
                         "check": "exit 0", **(initial or {})}
                if crash:
                    with self.assertRaisesRegex(RuntimeError, "process lost"):
                        graph.invoke(state, config)
                    graph = compile_delivery(FailoverDispatcher(dispatch), load_config(),
                                             run=LiveRun(self.forge, workspace), checkpointer=saver)
                    result = graph.invoke(None, config)
                else:
                    result = graph.invoke(state, config)
                if answer:
                    self.assertEqual(result["halt"], "design_rethink")
                    self.assertEqual(result["review_rounds"], 1)
                    result = graph.invoke(Command(resume="Redesign the ownership."), config)
                return result

    def test_nits_converge_and_outside_interdiff_becomes_note(self):
        first = {**FINDING, "severity": "nit"}
        result = self.run_rounds([
            {**APPROVE, "findings": [first]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
             "findings": [{**first, "id": "new", "line": 1}]},
        ])
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["reviewed_head"], HEAD)
        self.assertEqual(len(result["review_findings"]), 2)
        self.assertEqual(self.diffs, [(BASE, HEAD, False), (HEAD, HEAD, True)])
        self.assertIn("Coordinator note new", self.forge.reviews[-1][3])
        implement = [s for n, s, _ in self.prompts if n == "implement"][-1]
        self.assertEqual(implement["findings"], [first])
        later = [p for p in self.prompts if p[0] == "review"][-1]
        self.assertNotIn("review_findings", later[1])
        self.assertIn("Settled, don't reopen", later[2])
        self.assertIn('"id": "f1"', later[2])

    def test_recurrence_pauses_without_consuming_round_or_patching(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                                  "findings": [{**FINDING, "id": "again"}]}])
        self.assertEqual(result["halt"], "design_rethink")
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(result["path"].count("implement"), 2)
        self.assertIn('"id":"f1"', result["halt_detail"])
        self.assertIn('"id":"again"', result["halt_detail"])
        self.assertFalse(self.forge.merge_calls)

    def test_design_answer_resumes_implementation(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                                  "findings": [{**FINDING, "id": "again"}]},
                                 {**APPROVE, "resolutions": [{"id": "again", "status": "resolved"}]}], answer=True)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["settled_rulings"][0]["text"], "Redesign the ownership.")

    def test_resume_saved_round_preserves_findings_and_reviewed_head(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}], crash=True)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(len(result["review_findings"]), 2)
        self.assertEqual(self.diffs[-1], (HEAD, HEAD, True))

    def test_missing_duplicate_unknown_resolutions_and_reused_ids_fail_closed(self):
        resolved = {"id": "f1", "status": "resolved"}
        for resolutions, findings in (([], []), ([resolved, resolved], []),
                                       ([{"id": "missing", "status": "resolved"}], []), ([resolved], [FINDING])):
            with self.subTest(resolutions=resolutions, findings=findings):
                result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                         {**APPROVE, "resolutions": resolutions, "findings": findings}])
                self.assertEqual(result["halt"], "invalid_output")
                self.assertFalse(self.forge.merge_calls)

    def test_settled_ruling_disagreement_is_a_note(self):
        result = self.run_rounds([{**APPROVE, "findings": [{**FINDING, "ruling_id": "size"}]}],
                                 initial={"settled_rulings": [{"id": "size", "text": "Size rule lives in the contract."}]})
        self.assertEqual(result["merge"], "armed")
        self.assertIn("Coordinator note f1", result["detail"])
        self.assertIn("Size rule lives in the contract.", self.prompts[-1][2])

    def test_resolved_findings_travel_as_settled_rulings(self):
        nit = {**FINDING, "severity": "nit"}
        result = self.run_rounds([
            {**APPROVE, "findings": [nit]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}], "findings": [{**nit, "id": "f2"}]},
            {**APPROVE, "resolutions": [{"id": "f2", "status": "resolved"}],
             "findings": [{**nit, "id": "f3", "ruling_id": "finding:f1"}]},
        ])
        self.assertEqual(result["merge"], "armed")
        self.assertIn('"id": "finding:f1"', self.prompts[-1][2])
        self.assertEqual(result["review_findings"][-1]["coordinator_notes"][0]["id"], "f3")

    def test_unresolved_blocking_finding_stays_blocking_until_cap(self):
        unresolved = {**APPROVE, "resolutions": [{"id": "f1", "status": "unresolved"}]}
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]}, unresolved, unresolved])
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["review_rounds"], 3)
        self.assertEqual(result["review_change_kind"], "blocking")
        self.assertEqual(result["findings"], [FINDING])

    def test_malformed_diff_and_unknown_ruling_fail_closed(self):
        for bad_diff, finding in ((True, FINDING), (False, {**FINDING, "ruling_id": "unknown"})):
            with self.subTest(bad_diff=bad_diff):
                result = self.run_rounds([{**APPROVE, "findings": [finding]}], bad_diff=bad_diff)
                self.assertEqual(result["halt"], "invalid_output")
                self.assertFalse(self.forge.merge_calls)

    def test_tool_fact_gate_runs_outside_interdiff_and_resolves_at_later_head(self):
        fact = {"claim": "The CLI accepts example.", "fixture": "result.txt"}
        result = self.run_rounds([
            {**APPROVE, "findings": [{**FINDING, "severity": "nit"}]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}], "tool_facts": [fact]},
            APPROVE,
        ], gate_heads={"3" * 40})
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.gate_reads, ["2" * 40, "3" * 40])
        second, third = result["review_findings"][1:]
        gate_id = second["tool_fact_gate"]
        self.assertEqual(second["findings"][0]["file"], "tests/fixtures/manifest.toml")
        self.assertEqual(second["coordinator_notes"], [])
        self.assertEqual(third["resolutions"], {gate_id: "resolved"})
        self.assertIn(gate_id, self.prompts[-1][2])
        self.assertEqual(result["review_rounds"], 2)

    def test_tool_fact_gate_carries_without_recurrence_until_unchanged_cap(self):
        fact = {"claim": "The CLI accepts example.", "fixture": "result.txt"}
        result = self.run_rounds([{**APPROVE, "tool_facts": [fact]}, APPROVE, APPROVE], gate_heads=set(), crash=True)
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["review_rounds"], 3)
        self.assertEqual(result["review_change_kind"], "blocking")
        self.assertEqual(result["findings"][0]["id"], "tool-fact-gate:1")
        self.assertEqual(result["review_findings"][-1]["resolutions"], {"tool-fact-gate:1": "unresolved"})
        self.assertFalse(self.forge.merge_calls)

    def test_reviewer_cannot_resolve_or_impersonate_tool_fact_gate(self):
        fact = {"claim": "The CLI accepts example.", "fixture": None}
        for second in (
            {**APPROVE, "resolutions": [{"id": "tool-fact-gate:1", "status": "resolved"}]},
            {**APPROVE, "findings": [{**FINDING, "id": "tool-fact-gate:2"}]},
        ):
            with self.subTest(second=second):
                result = self.run_rounds([{**APPROVE, "tool_facts": [fact]}, second])
                self.assertEqual(result["halt"], "invalid_output")
                self.assertFalse(self.forge.merge_calls)

    def test_tool_fact_gate_does_not_resolve_at_same_head(self):
        fact = {"claim": "The CLI accepts example.", "fixture": None}
        corrected = {**fact, "fixture": "result.txt"}
        result = self.run_rounds([{**APPROVE, "tool_facts": [fact]},
                                  {**APPROVE, "tool_facts": [corrected]}, APPROVE], gate_heads={HEAD})
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["findings"][0]["id"], "tool-fact-gate:1")
