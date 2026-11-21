import json
from dataclasses import replace
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from foreman.review_round import GraphCheck, ReviewHistory, accept
from foreman.delivery import LiveRun, compile_delivery, read_delivery_state
from foreman.config import load_config
from foreman.adapters import Outcome
from foreman.dry_run import SCRIPTED_USAGE
from foreman.stage_output import ReviewOutput, parse_output
from foreman.review_context import ReviewContext, SCRIPTED_REVIEW_CONTEXT
from skill_fixture import skill_fixture
from test_runner import APPROVE, FINDING, BASE, HEAD, EMPTY_SLICE, FakeGitHub, FakeWorkspace


class ReviewRoundsTest(unittest.TestCase):
    def run_rounds(self, reviews, *, initial=None, crash=False, answer=False, bad_diff=False, divergent=False, gate_heads=None, advance_head=False, post_crash=False, review_crash=False, review_quota=False, bad_interdiff=False):
        self.prompts = []
        self.diffs = []
        self.gate_reads = []
        self.forge = FakeGitHub()
        original_post = self.forge.post_review
        receipts = {}
        def post(*args, marker):
            if marker in receipts:
                return receipts[marker]
            receipts[marker] = original_post(*args, marker=marker)
            if post_crash:
                raise RuntimeError("process lost")
            return receipts[marker]
        self.forge.post_review = post
        with TemporaryDirectory() as directory, skill_fixture() as (settings, scratch):
            if review_quota:
                builder = settings.nodes["implement"]
                settings.nodes["implement"] = replace(builder, models=tuple(m for m in builder.models if m.family == "grok"))
            outer = self

            class Workspace(FakeWorkspace):
                def commit(self, message):
                    super().commit(message)
                    if answer or advance_head:
                        self.current = type(self.current)(str(implementations) * 40, True, self.current.branch)

                def review_context(self, base, head):
                    context = super().review_context(base, head)
                    return ReviewContext(context.base, replace(context.head, files=context.head.files | {"main.txt"}, lines={**context.head.lines, "main.txt": 1})) if divergent else context

                def files_at(self, head, prefix):
                    outer.gate_reads.append(head)
                    return {"tests/fixtures/manifest.toml", "tests/fixtures/result.txt"} if head in (gate_heads or ()) else set()

                def read_file_at(self, head, path):
                    return '[fixtures."result.txt"]\nkind = "captured_run"\ntool = "example"\nversion = "1"\nargv = ["example"]'

                def review_diff(self, base, head, *, interdiff=False):
                    outer.diffs.append((base, head, interdiff))
                    diff = super().review_diff(base, head, interdiff=interdiff)
                    if divergent and interdiff:
                        return type(diff)(diff.command, "diff --git a/main.txt b/main.txt\n--- a/main.txt\n+++ b/main.txt\n@@ -0,0 +1 @@\n+mainline\n")
                    if bad_diff or (bad_interdiff and interdiff):
                        return type(diff)(diff.command, "--- a/x\n+++ b/x\n@@ -1 +1 @@\ninvalid\n")
                    return diff

            replies = iter(reviews)
            crashed = False
            quota_returned = False
            workspace = Workspace(Path(directory))
            implementations = 0

            def dispatch(node, model, state, instruction):
                nonlocal crashed, implementations, quota_returned
                self.prompts.append((node, state, instruction))
                if node == "review":
                    if review_crash and not crashed:
                        crashed = True
                        raise RuntimeError("process lost")
                    if review_quota and not quota_returned:
                        quota_returned = True
                        return Outcome("quota")
                    raw = next(replies)
                    return Outcome("ran", raw, usage=SCRIPTED_USAGE)
                if crash and state.get("review_findings") and not crashed:
                    crashed = True
                    raise RuntimeError("process lost")
                if node == "triage":
                    return Outcome("ran", {"triage": "implement", "work_kind": "feature", "slice": None, "human_question": None}, usage=SCRIPTED_USAGE)
                implementations += 1
                workspace.current = type(workspace.current)(workspace.current.head, False, workspace.current.branch)
                return Outcome("ran", {"implementation": "Fixed.",
                                       "browser_proof": None, "formal_proof": None}, usage=SCRIPTED_USAGE)

            with SqliteSaver.from_conn_string(str(Path(directory) / "checkpoint")) as saver:
                graph = compile_delivery(dispatch, settings, scratch=scratch,
                                         run=LiveRun(self.forge, workspace), checkpointer=saver)
                config = {"configurable": {"thread_id": "rounds"}}
                state = {"repo": "Example/Repo", "issue": 8, "issue_kind": "slice", "slice": EMPTY_SLICE,
                         "check": "exit 0", "token_cap": 20_000_000, **(initial or {})}
                if crash or post_crash or review_crash:
                    with self.assertRaisesRegex(RuntimeError, "process lost"):
                        graph.invoke(state, config)
                    self.interrupted = read_delivery_state({"stop": "interrupted", **graph.get_state(config).values})
                    (scratch / "resume").mkdir()
                    graph = compile_delivery(dispatch, settings, scratch=scratch / "resume",
                                             run=LiveRun(self.forge, workspace), checkpointer=saver)
                    result = graph.invoke(None, config)
                else:
                    result = graph.invoke(state, config)
                if answer:
                    self.assertEqual(result["halt"], "design_rethink")
                    self.assertEqual(read_delivery_state({"stop": "needs_human", **result})["review_rounds"], 2)
                    result = graph.invoke(Command(resume="Redesign the ownership."), config)
                return read_delivery_state({"stop": "needs_human", **result})

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
        self.assertEqual(self.diffs, [(BASE, HEAD, False), (BASE, HEAD, False), (HEAD, HEAD, True)])
        raw = parse_output(ReviewOutput, {**APPROVE, "findings": [FINDING]}, context=SCRIPTED_REVIEW_CONTEXT)
        before = raw.model_dump()
        past = ReviewHistory()
        accepted = accept(past, raw, HEAD, None)
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(past.rounds, [])
        self.assertEqual(accepted.change_kind, "blocking")

    def test_gate_rejects_every_previously_failing_head(self):
        past = ReviewHistory()
        output = parse_output(ReviewOutput, APPROVE, context=SCRIPTED_REVIEW_CONTEXT)
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
    def test_recurrence_pauses_and_counts_accepted_round(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                                  "findings": [{**FINDING, "id": "again"}]}])
        self.assertEqual(result["halt"], "design_rethink")
        self.assertEqual(result["review_rounds"], 2)
        self.assertEqual(len(self.forge.reviews), 2)
        self.assertEqual(result["review_receipt"]["id"], 2)
        self.assertEqual(result["path"].count("implement"), 2)
        self.assertEqual([json.loads(line)["id"] for line in result["halt_detail"].splitlines()[1:]], ["f1", "again"])
        self.assertFalse(self.forge.merge_calls)

    def test_design_answer_resumes_implementation(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                                  "findings": [{**FINDING, "id": "again"}]},
                                 {**APPROVE, "resolutions": [{"id": "again", "status": "resolved"}]}], answer=True)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["settled_rulings"][0]["text"], "Redesign the ownership.")

        instruction = [p[2] for p in self.prompts if p[0] == "review"][2]
        self.assertEqual(json.loads(instruction.split("Settled, don't reopen:\n")[1])[0]["id"], "design:2")

    def test_outside_interdiff_nit_needs_explicit_resolution(self):
        nit = {**FINDING, "severity": "nit"}
        for status in ("resolved", "unresolved"):
            result = self.run_rounds([{**APPROVE, "findings": [nit]},
                {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}],
                 "findings": [{**nit, "id": "main", "file": "main.txt", "line": 1}, {**nit, "id": "old"}]},
                {**APPROVE, "resolutions": [{"id": "main", "status": status}, {"id": "old", "status": status}]}], divergent=True)
            self.assertEqual(result["review_rounds"], 3)
            self.assertEqual(result["stop"], "verified" if status == "resolved" else "review_cap")
            self.assertIn("main.txt:1 [nit]", self.forge.reviews[1][3])
            self.assertNotIn("Coordinator note main", self.forge.reviews[1][3])
            self.assertEqual(len(self.forge.reviews[1][4]), 1)
            self.assertEqual(result["path"].count("implement"), 3)

    def test_unknown_settled_ruling_rejected(self):
        result = self.run_rounds([{**APPROVE, "findings": [{**FINDING, "ruling_id": "unknown"}]}])
        self.assertEqual(result["halt"], "invalid_output")

    def test_graph_then_nit_then_approval_counts_at_cap(self):
        fact = {"claim": "CLI behavior", "fixture": "result.txt"}
        result = self.run_rounds([{**APPROVE, "tool_facts": [fact]},
            {**APPROVE, "findings": [{**FINDING, "severity": "nit"}]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}],
            advance_head=True, gate_heads=["2" * 40, "3" * 40])
        self.assertEqual(result["review_rounds"], 3)
        self.assertEqual(result["merge"], "armed")
        self.assertIn("Graph verdict: changes", self.forge.reviews[0][3])

    def test_review_dispatch_crash_resumes_with_one_accepted_round(self):
        result = self.run_rounds([APPROVE], review_crash=True)
        self.assertEqual(self.interrupted["pending_dispatch"]["stage"], "review")
        self.assertEqual(self.interrupted["review_rounds"], 0)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(len(result["review_findings"]), 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual([p[0] for p in self.prompts].count("review"), 2)

    def test_review_quota_failover_accepts_one_round(self):
        result = self.run_rounds([APPROVE], review_quota=True)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(len(result["review_findings"]), 1)
        attempts = [a for a in result["attempts"] if a["stage"] == "review"]
        self.assertEqual([a["outcome"] for a in attempts], ["quota", "ran"])
        self.assertNotEqual(attempts[0]["model"], attempts[1]["model"])
        self.assertEqual(len(self.forge.reviews), 1)

    def test_post_crash_reuses_accepted_round(self):
        result = self.run_rounds([APPROVE], post_crash=True)
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(len(self.forge.reviews), 1)
        self.assertEqual(result["review_receipt"]["id"], 1)
        self.assertEqual([p[0] for p in self.prompts].count("review"), 1)

    def test_interdiff_need_not_parse_for_focused_instruction(self):
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]},
            {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}], bad_interdiff=True)
        self.assertEqual(result["merge"], "armed")
        self.assertIn((HEAD, HEAD, True), self.diffs)

    def test_diff_parse_failure_is_tolerated_first_and_unavailable_later(self):
        result = self.run_rounds([APPROVE], bad_diff=True)
        self.assertEqual(result["merge"], "armed")
        result = self.run_rounds([{**APPROVE, "findings": [FINDING]}, APPROVE], bad_diff=True)
        self.assertEqual(result["halt"], "review_unavailable")

    def test_gate_never_counts_as_reviewer_recurrence(self):
        fact = {"claim": "CLI behavior", "fixture": None}
        result = self.run_rounds([{**APPROVE, "tool_facts": [fact]},
                                 {**APPROVE, "findings": [FINDING]},
                                 {**APPROVE, "resolutions": [{"id": "f1", "status": "resolved"}]}])
        self.assertEqual(result["stop"], "review_cap")
