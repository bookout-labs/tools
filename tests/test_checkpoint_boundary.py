import tests
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

import test_spec_candidate
from foreman.delivery import LiveRun
from foreman.dry_run import DryRun, ScriptHandler
from foreman.outcomes import Halt
from foreman.readiness import MergeState
from foreman.workspace import WorkState
from foreman.runner import run_live_delivery
from foreman.state import PlanState, ChecksState, RunState, SCHEMA_VERSION, CheckpointVersionError, checkpoint_serializer, require_schema_version


class CheckpointBoundaryTests(unittest.TestCase):
    def test_old_checkpoint_is_refused_before_validation_or_execution(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "old.sqlite"
            shutil.copyfile(Path(__file__).parent / "fixtures/state/before-move.sqlite", checkpoint)
            for version in (None, 0, 1, 2, 3, 4, 5, SCHEMA_VERSION + 1, True, "1"):
                with self.subTest(version=version):
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saver.serde = checkpoint_serializer()
                        saved = saver.get_tuple({"configurable": {"thread_id": "before-move"}})
                        values = saved.checkpoint["channel_values"]
                        values.clear()
                        values.update(path=["triage"], attempts=[], review_rounds=2, triage="implement", spec="Plan", spec_gate="ready", issue_kind="slice", work_kind="feature", child_issue_numbers=[9])
                        checks = ChecksState(check="true", preflight="env", check_exit_code=0, verify="pass",
                            verified_head="b" * 40, browser_proof={"screen": "page", "trace": "trace", "passed": True},
                            formal_proof={"ran": True, "typecheck": True, "constraints": [{"constraint": "unique", "rejected": True}]},
                            proof_labels={"browser": "block"})
                        run = RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout",
                                       token_cap=100, max_budget_usd="1.5")
                        work = WorkState(branch="8-change", base="main", attempt_head="a" * 40,
                            candidate={"disposition": "edits", "fingerprint": "edits"}, partial_edits="partial",
                            head_sha="b" * 40, repair_head="c" * 40)
                        merge = MergeState(status="not_armed", receipt={"status": "QUEUED"},
                            observation={"outcome": "ready"}, observed_head="c" * 40, command=["gh", "pr", "merge"])
                        revision = version if type(version) is int else 1
                        if version is not None:
                            values["schema_version"] = version
                        if revision >= 5:
                            values["checks"] = checks
                        else:
                            values.update(checks.model_dump())
                        if revision >= 4:
                            values["run"] = run
                        else:
                            values.update(run.model_dump())
                        if revision >= 3:
                            values["work"] = work
                        else:
                            values.update(delivery_branch=work.branch, delivery_base=work.base, attempt_head=work.attempt_head,
                                implementation_candidate=work.candidate, partial_edits=work.partial_edits,
                                head_sha=work.head_sha, repair_head=work.repair_head)
                        if revision >= 2:
                            values["merge"] = merge
                        else:
                            values.update(merge=merge.status, merge_receipt=merge.receipt, readiness_observation=merge.observation,
                                merge_observed_head=merge.observed_head, merge_command=merge.command)
                        saver.put({"configurable": {"thread_id": "a/b#8", "checkpoint_ns": ""}}, saved.checkpoint, saved.metadata, {})
                    before = checkpoint.read_bytes()
                    with patch("foreman.runner.compile_delivery") as compile_graph, \
                         patch("foreman.runner.checkpoint_state") as validate, \
                         patch("foreman.runner.load_config") as config, \
                         patch("foreman.runner.require_schema_version", wraps=require_schema_version) as refuse:
                        with self.assertRaisesRegex(CheckpointVersionError, "schema_version.*Start a new run") as raised:
                            run_live_delivery(repo="a/b", issue=8, check="", checkpoint=checkpoint,
                                              dispatcher=ScriptHandler(), run=LiveRun(DryRun(), DryRun()))
                        self.assertIn(str(checkpoint), str(raised.exception))
                        if version is None:
                            self.assertIn("schema_version missing", str(raised.exception))
                        refuse.assert_called_once()
                        self.assertEqual(refuse.call_args.args[0], values)
                        compile_graph.assert_not_called()
                        validate.assert_not_called()
                        config.assert_not_called()
                    self.assertEqual(checkpoint.read_bytes(), before)

    def test_serializer_only_reconstructs_allowed_domain_types(self):
        from foreman.delivery import HumanPrompt
        serde = checkpoint_serializer()
        with self.assertNoLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            self.assertIs(serde.loads_typed(serde.dumps_typed(Halt.quota_exhausted)), Halt.quota_exhausted)
            merge = MergeState(status="not_armed", receipt={"status": "QUEUED"}, observed_head="c" * 40,
                               observation={"outcome": "ready"}, command=["gh", "pr", "merge"])
            self.assertEqual(serde.loads_typed(serde.dumps_typed(merge)), merge)
            work = WorkState(branch="8-change", base="main", attempt_head="a" * 40,
                             candidate={"disposition": "adopted", "head": "b" * 40, "tree": "c" * 40, "fingerprint": "tree"},
                             partial_edits="partial", head_sha="b" * 40, repair_head="d" * 40)
            self.assertEqual(serde.loads_typed(serde.dumps_typed(work)), work)
            run = RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout", token_cap=100, max_budget_usd="1.5")
            self.assertEqual(serde.loads_typed(serde.dumps_typed(run)), run)
            checks = ChecksState(check="true", preflight="env", check_exit_code=0, verify="pass", verified_head="b" * 40,
                browser_proof={"screen": "page", "trace": "trace", "passed": True},
                formal_proof={"ran": True, "typecheck": True, "constraints": [{"constraint": "unique", "rejected": True}]},
                proof_labels={"browser": "block"})
            self.assertEqual(serde.loads_typed(serde.dumps_typed(checks)), checks)
            plan = test_spec_candidate.CandidateTests().candidate().update(PlanState(triage="implement", spec_gate="ready", issue_kind="parent",
                spec_rejection={"kind": "dangling_seam", "detail": "Missing"}, child_issue_numbers=[9],
                pr_split={"plan": "Split", "gate": "ready", "question": "Keep?", "slices": [test_spec_candidate.SLICE]}), single=True)
            self.assertEqual(serde.loads_typed(serde.dumps_typed(plan)), plan)
        blocked = HumanPrompt(stage="triage", question="Which?")
        with self.assertLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            restored = serde.loads_typed(serde.dumps_typed(blocked))
        self.assertNotIsInstance(restored, HumanPrompt)

    def test_every_pause_survives_a_fresh_process(self):
        script = '''
import json
from pathlib import Path
import sys
from langgraph.checkpoint.sqlite import SqliteSaver
from foreman.config import load_config
from foreman.delivery import LiveRun, compile_delivery
from foreman.dry_run import DryRun, ScriptHandler
from foreman.runner import checkpoint_state, snapshot_result
from foreman.state import PlanState, ChecksState, RunState, checkpoint_serializer
from foreman.readiness import MergeState
from foreman.workspace import WorkState

path, phase, state_json, predecessor = sys.argv[1:]
config = {"configurable": {"thread_id": "pause"}}
with SqliteSaver.from_conn_string(path) as saver:
    saver.serde = checkpoint_serializer()
    graph = compile_delivery(ScriptHandler(), load_config(),
                             run=LiveRun(DryRun(), DryRun()), checkpointer=saver)
    if phase == "write":
        graph.update_state(config, {**json.loads(state_json),
            "plan": PlanState.model_validate(json.loads(state_json)["plan"]),
            "checks": ChecksState.model_validate(json.loads(state_json)["checks"]),
            "run": RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout", token_cap=100, max_budget_usd="1.5"),
            "merge": MergeState(status="not_armed", receipt={"status": "QUEUED"}, observed_head="c" * 40,
                observation={"outcome": "ready"}, command=["gh", "pr", "merge"]),
            "work": WorkState(branch="8-change", base="main", attempt_head="a" * 40,
                candidate={"disposition": "adopted", "head": "b" * 40, "tree": "c" * 40, "fingerprint": "tree"},
                partial_edits="partial", head_sha="b" * 40, repair_head="d" * 40)}, as_node=predecessor)
        if predecessor != "done":
            graph.invoke(None, config, durability="sync")
    saved = graph.get_state(config)
    assert checkpoint_state(saved.values) is not None
    assert saved.values["work"] == WorkState(branch="8-change", base="main", attempt_head="a" * 40,
        candidate={"disposition": "adopted", "head": "b" * 40, "tree": "c" * 40, "fingerprint": "tree"},
        partial_edits="partial", head_sha="b" * 40, repair_head="d" * 40)
    assert saved.values["plan"] == PlanState.model_validate(json.loads(state_json)["plan"])
    assert saved.values["checks"] == ChecksState.model_validate(json.loads(state_json)["checks"])
    assert saved.values["run"] == RunState(repo="a/b", issue=8, request="Build it", workspace="/checkout", token_cap=100, max_budget_usd="1.5")
    assert isinstance(saved.values["merge"], MergeState)
    assert saved.values["merge"].receipt == {"status": "QUEUED"}
    assert saved.values["merge"].observed_head == "c" * 40
    assert saved.values["merge"].observation == {"outcome": "ready"}
    assert saved.values["merge"].command == ["gh", "pr", "merge"]
    print(json.dumps({"values": saved.values, "next": saved.next,
                      "interrupts": [item.value for item in saved.interrupts],
                      "result": snapshot_result(saved)}, sort_keys=True, default=lambda value: value.model_dump(mode="json")))
'''
        size = {"head": "a" * 40, "merge_base": "b" * 40, "lines": 401}
        cases = [
            ("triage", "triage_call", {"triage": "needs_human"}),
            ("spec", "spec_call", {"spec_gate": "needs_human"}),
            ("review", "review_post", {"halt": "design_rethink", "review": "changes"}),
            ("pr_size", "spec_call", {"spec_gate": "ready", "pr_size": size}),
            ("quota", "triage_call", {"halt": "quota_exhausted"}),
        ]
        cases += [(code.value, "done", {"halt": code.value, "stop": code.value}) for code in Halt]
        env = {**os.environ, "LANGGRAPH_STRICT_MSGPACK": "true"}
        with TemporaryDirectory() as directory:
            for name, predecessor, extra in cases:
                with self.subTest(pause=name):
                    stage = "spec" if name == "pr_size" else "triage" if name == "quota" else name
                    plan = PlanState(triage="implement", spec="Plan", spec_gate="ready", issue_kind="parent", work_kind="feature",
                        spec_rejection={"kind": "dangling_seam", "detail": "Missing seam"}, child_issue_numbers=[9],
                        pr_split={"plan": "Split", "gate": "ready", "question": "Keep?", "slices": [test_spec_candidate.SLICE]})
                    state = {"schema_version": SCHEMA_VERSION, "checks": ChecksState(check="true", preflight="env", check_exit_code=0,
                        verify="pass", verified_head="b" * 40, browser_proof={"screen": "page", "trace": "trace", "passed": True},
                        formal_proof={"ran": True, "typecheck": True, "constraints": [{"constraint": "unique", "rejected": True}]},
                        proof_labels={"browser": "block"}), "path": [stage], "human_question": "Which?", "halt_detail": "Waiting.", "attempts": [{"stage": "triage", "model": "gpt",
                                 "commit": "a" * 40, "methods": [], "outcome": "ran", "usage": {
                                     "input": 1, "output": 2, "cache_read": 0, "cache_creation": 0,
                                     "cost_usd": None}}], **extra, "plan": PlanState()}
                    plan = test_spec_candidate.CandidateTests().candidate().update(plan, single=True).model_copy(update={"spec_rejection": None})
                    state["plan"] = plan.model_copy(update={key: state.pop(key) for key in ("triage", "spec_gate") if key in state})
                    checkpoint = str(Path(directory) / f"{name}.sqlite")
                    outputs = []
                    for phase in ("write", "read"):
                        process = subprocess.run([sys.executable, "-c", script, checkpoint, phase,
                                                  json.dumps(state, default=lambda value: value.model_dump(mode="json")), predecessor],
                                                 text=True, capture_output=True, env=env, check=False)
                        self.assertEqual(process.returncode, 0, process.stderr)
                        self.assertEqual(process.stderr, "")
                        outputs.append(json.loads(process.stdout))
                    self.assertEqual(*outputs)
                    if predecessor != "done":
                        self.assertEqual(len(outputs[1]["interrupts"]), 1)
                        payload = outputs[1]["interrupts"][0]
                        self.assertEqual(payload.get("stage", payload.get("reason")),
                                         "quota_exhausted" if name == "quota" else name)
