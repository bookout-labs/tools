"""Adapter snapshots refreshed for retry diagnostics (#218)."""
import tests
import ast

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import get_type_hints
import unittest
from unittest.mock import patch

from foreman.delivery import MODEL_STAGES, run_delivery, stage_text
from foreman.stages import STAGES, Acceptance
from foreman.state import DeliveryState, PlanState
from foreman.stage_output import ResearchOutput
from foreman.skill_rules import SkillMethod, SkillRule, SkillMissing, export_skills, stage_methods
from skill_fixture import skill_fixture
from foreman.dry_run import DryRun, ScriptHandler
from foreman.worker import CliWorker
from test_delivery import EMPTY_SLICE


def capture():
    captures = {}
    for kind in ("feature", "bug_fix", "refactoring", "perf_issue"):
        for route in ("spec", "question", "implement"):
            records = []
            item = {**EMPTY_SLICE, "stateful": True}
            script = ScriptHandler(triage=route,
                                   work_kind=kind, reviews=["changes", "approve"])

            def dispatch(node, model, state, instruction):
                response = script(node, model, state, instruction)

                class Adapter:
                    env = {}

                    def command(self, call):
                        records.append({"stage": node, "model": model.name,
                            "instruction": sha256(instruction.encode()).hexdigest(),
                            "prompt": sha256(call.prompt.encode()).hexdigest(),
                            "prompt_bytes": len(call.prompt.encode()),
                            "schema": sha256(call.schema.encode()).hexdigest(),
                            "edit": call.edit, "checks": list(call.checks),
                            "roots": [str(root) for root in call.read_roots],
                            "methods": state["dispatch_plan"]["methods"]})
                        return ["fixture"]

                    def read(self, process, call):
                        return response

                with patch.dict("foreman.worker.ADAPTERS", {model.family: lambda _: Adapter()}):
                    return CliWorker(Path.cwd(), lambda *args: CompletedProcess([], 0))(node, model, state, instruction)

            with patch("foreman.delivery.uuid4", return_value=SimpleNamespace(hex="stable")), patch(
                "foreman.skill_rules.PACKAGE_SKILLS", Path("/LOCAL")), patch(
                "foreman.delivery.PACKAGE", Path(__file__).resolve().parents[1] / "src/foreman"):
                with patch("foreman.delivery.DryRun", side_effect=lambda **kwargs: DryRun(root=Path("/TARGET"), **kwargs)):
                    # Keep audit hashes independent of the checkout path.
                    result = run_delivery(dispatch, repo="Example/Engine", request="Change items", check="check",
                    slice=None if route == "question" else item, scratch=Path("/FIXTURE"), skills=lambda *_: Path("/METHODS"))
            captures[f"{kind}/{route}"] = {"calls": records, "stop": result["stop"],
                "attempts": [(a["stage"], a["model"], a["methods"]) for a in result["attempts"]]}
    return captures


class StageDeclarationsTest(unittest.TestCase):
    def test_adapter_payload_and_method_order_match_snapshot(self):
        baseline = Path(__file__).parent / "fixtures/stage-calls.json"
        actual = {key: sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                  for key, value in capture().items()}
        self.assertEqual(actual, json.loads(baseline.read_text()))

    def test_row_callbacks_reach_the_worker_and_state(self):
        from test_delivery import walk

        for name in ("triage", "research", "spec", "implement", "review"):
            with self.subTest(stage=name):
                row = STAGES[name]
                seen = []
                script = ScriptHandler(triage="question" if name == "research" else "spec", slices=[EMPTY_SLICE])

                def dispatch(stage, model, state, instruction):
                    if stage == name:
                        self.assertTrue(instruction.endswith("\nRow instruction"))
                        seen.append(stage)
                    return script(stage, model, state, instruction)

                def accept(output, context):
                    return {**row.accept(output, context), "research": "Row accepted"}

                def prepare(state, workspace, forge):
                    _, target, scope = row.prepare(state, workspace, forge)
                    return "\nRow instruction", target, scope

                with patch.dict(STAGES, {name: replace(row, prepare=prepare, accept=accept)}):
                    result = walk(dispatch, slice=None) if name in {"research", "spec"} else walk(dispatch)
                self.assertEqual(seen, [name])
                self.assertEqual(result["research"], "Row accepted")
                self.assertEqual(result["stop"], {"research": "answered", "spec": "sliced"}.get(name, "verified"))
                self.assertEqual(result["attempts"][-1]["stage"], {"research": "research", "spec": "spec_check"}.get(name, "review"))

    def assert_no_stage_policy(self, source, module):
        expected = {"call", "stage_update"} if module == "delivery.py" else {"clean", "reserve", "launch", "charge", "reconcile"}
        visited = set()
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if module == "delivery.py" and function.name not in expected:
                continue
            visited.add(function.name)
            for branch in ast.walk(function):
                conditions = []
                if isinstance(branch, (ast.If, ast.IfExp)):
                    conditions = [branch.test]
                elif isinstance(branch, ast.Match):
                    conditions = [branch.subject, *(case.pattern for case in branch.cases),
                                  *(case.guard for case in branch.cases if case.guard is not None)]
                for condition in conditions:
                    literals = {n.value for n in ast.walk(condition) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                    self.assertFalse(literals & set(STAGES), ast.unparse(condition))
        self.assertTrue(expected <= visited, expected - visited)

    def test_dispatch_has_no_stage_policy_branches(self):
        root = Path(__file__).resolve().parents[1] / "src/foreman"
        for name in ("delivery.py", "dispatch.py"):
            self.assert_no_stage_policy((root / name).read_text(), name)

    def test_dispatch_policy_check_rejects_cases_guards_helpers_and_missing_functions(self):
        for body in ('match name:\n        case "review": pass',
                     'match name:\n        case _ if name == "review": pass',
                     'if name == "review": pass', 'if name == "implement": pass'):
            with self.subTest(body=body), self.assertRaises(AssertionError):
                self.assert_no_stage_policy("def clean(): pass\ndef reserve(): pass\ndef launch(): pass\ndef charge(): pass\n"
                                            "def reconcile(): pass\ndef helper(name):\n    " + body, "dispatch.py")
        with self.assertRaises(AssertionError):
            self.assert_no_stage_policy('def call(name):\n    if name == "implement": pass\ndef stage_update(): pass', "delivery.py")
        for module in ("delivery.py", "dispatch.py"):
            with self.subTest(module=module), self.assertRaises(AssertionError):
                self.assert_no_stage_policy("def renamed(): pass", module)

    def test_review_projection_and_citation_context_come_from_row(self):
        from foreman.adapters import Outcome
        from foreman.dry_run import SCRIPTED_USAGE
        from foreman.review_context import SCRIPTED_REVIEW_CONTEXT
        from test_delivery import walk

        row, script, seen = STAGES["review"], ScriptHandler(), []
        def project(state, workspace):
            projected, _ = row.project(state, workspace)
            projected["research"] = "Row projection"
            seen.append(True)
            head = SCRIPTED_REVIEW_CONTEXT.head
            return projected, replace(SCRIPTED_REVIEW_CONTEXT, head=replace(head,
                files=head.files | {"row.py"}, lines={**head.lines, "row.py": 7}, changed=head.changed | {"row.py"}))
        def dispatch(stage, model, state, instruction):
            outcome = script(stage, model, state, instruction)
            if stage == "review":
                self.assertEqual(state["research"], "Row projection")
                outcome.output["findings"] = [{"id": "row", "kind": "correctness", "severity": "P2",
                    "location": {"side": "head", "path": "row.py", "line": 7}, "ruling_id": None,
                    "evidence": "Current row accepts a blank label.", "impact": "The display has no label.",
                    "summary": "Row citation"}]
                return Outcome("ran", outcome.output, usage=SCRIPTED_USAGE)
            return outcome
        with patch.dict(STAGES, review=replace(row, project=project)):
            result = walk(dispatch)
        # Dispatch projects the worker's state; acceptance projects the citation context.
        self.assertEqual(seen, [True, True])
        self.assertEqual(result["stop"], "verified", result.get("halt_detail"))
        self.assertEqual(result["review"].review_findings[0]["findings"][0]["location"]["path"], "row.py")

    def test_implement_acceptance_keeps_model_history(self):
        from foreman.delivery import stage_update
        from foreman.stage_output import ImplementOutput
        from foreman.state import ChecksState, ReviewState, RunState

        state = {"plan": PlanState(), "review": ReviewState(), "run": RunState(repo="org/repo"),
                 "checks": ChecksState(check="test", verify_failures=2), "path": []}
        for used, prior, model, expected in (
            ({}, None, "first", ["first"]),
            ({"implement": "first"}, None, "second", ["first", "second"]),
            ({"implement": "second"}, ["first", "second"], "third", ["first", "second", "third"]),
            ({"implement": "second"}, [], "third", ["third"]),
            ({}, None, None, None),
        ):
            with self.subTest(model=model, prior=prior):
                current = {**state, "models_used": used}
                if prior is not None:
                    current["implement_models"] = prior
                update = stage_update("implement", ImplementOutput(implementation="Done"), current, model)
                self.assertEqual(update["implementation"], "Done")
                self.assertNotIn("checks", update)
                self.assertEqual(update.get("implement_models"), expected)

    def test_spec_split_instructions_reach_the_worker(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from foreman.delivery import DEFAULT_TOKEN_CAP, LiveRun, compile_delivery
        from foreman.dry_run import DryRun
        from foreman.stage_output import PULL_REQUEST_LINES
        from foreman.state import ChecksState, ReviewState, RunState, checkpoint_serializer

        for lines, override in ((PULL_REQUEST_LINES + 1, False), (PULL_REQUEST_LINES - 1, False),
                                (PULL_REQUEST_LINES, False),
                                (PULL_REQUEST_LINES + 1, True)):
            with self.subTest(lines=lines, override=override), skill_fixture() as (config, scratch):
                script, instructions = ScriptHandler(), []
                def dispatch(stage, model, state, instruction):
                    if stage == "spec":
                        instructions.append(instruction)
                    return script(stage, model, state, instruction)
                dry = DryRun()
                graph = compile_delivery(dispatch, config, run=LiveRun(dry, dry),
                                         scratch=scratch, checkpointer=InMemorySaver(serde=checkpoint_serializer()))
                size = {"head": "head", "base": "base", "lines": lines}
                state = {"plan": PlanState(triage="spec", issue_kind="parent"), "review": ReviewState(),
                         "checks": ChecksState(), "run": RunState(repo="org/repo", token_cap=DEFAULT_TOKEN_CAP),
                         "pr_size": size, "path": ["triage"]}
                if override:
                    state["pr_size_override"] = size
                thread = {"configurable": {"thread_id": "spec-instructions"}}
                graph.update_state(thread, state, as_node="triage")
                graph.invoke(None, thread)
                self.assertEqual(len(instructions), 1)
                suffix = (f"\n\nThe pull request has {lines} changed lines. The limit is {PULL_REQUEST_LINES}."
                          " Split the work into independently shippable pull requests."
                          " For a slice issue, describe the split in spec.")
                if lines > PULL_REQUEST_LINES and not override:
                    self.assertTrue(instructions[0].endswith(suffix))
                else:
                    self.assertNotIn("Split the work into independently shippable pull requests.", instructions[0])

    def test_implement_acceptance_records_accepted_model_without_model_name(self):
        from foreman.config import ModelSpec
        from foreman.delivery import stage_update
        from foreman.stage_output import ImplementOutput
        from foreman.state import ChecksState

        model = ModelSpec.parse("gpt-6.1-sol-high")
        output = ImplementOutput(implementation="Done")
        update = stage_update("implement", output, {"checks": ChecksState(), "implement_models": ["first"]},
                              accepted_model=model)
        self.assertEqual(update["implement_models"], ["first", model.name])

    def test_verify_row_preserves_checks_and_counts_failures(self):
        from foreman.stage_output import VerifyOutput
        from foreman.state import ChecksState

        checks = ChecksState(check="test", verify_failures=2)
        for verdict, failures in (("pass", 2), ("fail", 3)):
            with self.subTest(verdict=verdict):
                update = STAGES["verify"].accept(VerifyOutput(verify=verdict), Acceptance({"checks": checks}))
                self.assertEqual(update["checks"], checks.model_copy(update={"verify": verdict, "verify_failures": failures}))
        self.assertEqual(checks.verify_failures, 2)

    def test_contract_change_reaches_the_adapter(self):
        with patch.dict(STAGES, triage=replace(STAGES["triage"], output=ResearchOutput)):
            calls = capture()["feature/question"]["calls"]
        self.assertEqual(calls[0]["schema"], sha256(json.dumps(ResearchOutput.model_json_schema()).encode()).hexdigest())

    def test_export_checks_methods_from_the_canonical_row(self):
        with skill_fixture() as (config, scratch):
            missing = SkillRule(skill=SkillMethod(path="absent/SKILL.md", kind="skill"))
            with patch.dict(STAGES, spec_check=replace(STAGES["spec_check"], skills=(missing,))):
                with self.assertRaises(SkillMissing):
                    export_skills(config.pstack, scratch)

    def test_declared_inputs_are_delivery_channels(self):
        fields = get_type_hints(DeliveryState)
        for name, row in STAGES.items():
            with self.subTest(stage=name):
                self.assertFalse(set(row.input_fields) - fields.keys())

    def test_spec_checker_uses_the_independent_evidence_probe(self):
        with skill_fixture() as (config, scratch):
            root = export_skills(config.pstack, scratch)
            _, methods, _ = stage_methods("spec_check", {"plan": PlanState()}, root)
        self.assertEqual(methods, ["interrogate/SKILL.md#L19-L19"])

    def test_active_and_non_model_resources_stay_distinct(self):
        self.assertEqual(MODEL_STAGES, ("triage", "research", "spec", "implement", "review"))
        self.assertEqual(STAGES["spec_check"].kind, "model")
        self.assertEqual(STAGES["spec_check"].model_selector, "spec")
        self.assertEqual(STAGES["spec_check"].prompt, "spec_check.md")
        for name, row in STAGES.items():
            if row.prompt is not None:
                with self.subTest(stage=name):
                    self.assertTrue(stage_text(name))
        self.assertEqual(STAGES["verify"].kind, "deterministic")
        self.assertIsNone(STAGES["verify"].prompt)

    def test_condition_change_reaches_runtime_methods(self):
        row = STAGES["research"]
        skills = (*row.skills[:-1], row.skills[-1].model_copy(update={"when": None}))
        with patch.dict(STAGES, research=replace(row, skills=skills)):
            calls = capture()["feature/question"]["calls"]
        self.assertIn("why/SKILL.md", calls[-1]["methods"])
