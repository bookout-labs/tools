import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.review_round import GraphCheck, ReviewHistory, accept
from foreman.delivery import LiveRun, compile_delivery
from foreman.config import load_config
from foreman.dispatch import FailoverDispatcher
from foreman.adapters import Outcome
from foreman.stage_output import ReviewOutput, parse_output
from test_runner import APPROVE, FINDING, BASE, HEAD, EMPTY_SLICE, FakeGitHub, FakeWorkspace


class ReviewRoundsTest(unittest.TestCase):
    def run_rounds(self, reviews, *, initial=None, crash=False, bad_diff=False, gate_heads=None):
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
                    diff = super().review_diff(base, head)
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
                return result

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
        self.assertEqual(json.loads(self.prompts[-1][2].split("Settled, don't reopen:\n")[1])[0]["id"], "finding:f1")
        self.assertEqual(result["review_findings"][-1]["coordinator_notes"][0]["id"], "f3")

    def test_full_diff_rounds_resume_and_keep_worker_output(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}], crash=True)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.diffs, [(BASE, HEAD, False)] * 2)
        raw = parse_output(ReviewOutput, {**APPROVE, "findings": [FINDING]})
        before = raw.model_dump()
        past = ReviewHistory()
        accepted = accept(past, raw, HEAD, None)
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(past.rounds, [])
        self.assertEqual(accepted.change_kind, "blocking")

    def test_gate_rejects_every_previously_failing_head(self):
        past = ReviewHistory()
        output = parse_output(ReviewOutput, APPROVE)
        for head, fails, expected in [("a", True, "changes"), ("b", True, "changes"),
                                      ("b", False, "changes"), ("c", False, "approve")]:
            accepted = accept(past, output, head, GraphCheck(failing_heads=[head], detail="Missing fixture") if fails else None)
            self.assertEqual(accepted.review, expected)
            past = accepted.history
        self.assertEqual(past.rulings(), [])

    def test_malformed_history_halts_only_in_review(self):
        result = self.run_rounds([APPROVE], initial={"review_findings": [{"broken": True}]})
        self.assertEqual(result["halt"], "invalid_output")
        self.assertIn("implement", result["path"])
