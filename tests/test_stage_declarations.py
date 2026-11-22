"""Adapter snapshots captured on main a68e815985cb6222cdfc807d53105f8644f437cf before registry replacement."""
import tests

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
from foreman.stages import STAGES
from foreman.state import DeliveryState, PlanState
from foreman.stage_output import ResearchOutput
from foreman.skill_rules import PstackMethod, SkillRule, SkillMissing, export_skills, stage_methods
from skill_fixture import skill_fixture
from foreman.dry_run import ScriptHandler
from foreman.worker import CliWorker
from test_delivery import EMPTY_SLICE


def capture():
    captures = {}
    for kind in ("feature", "bug_fix", "refactoring", "perf_issue"):
        for route in ("spec", "question", "proof"):
            records = []
            item = {**EMPTY_SLICE, "stateful": True}
            if route == "proof":
                item.update(screen="docs/screens/items.md", properties=[{"name": "Unique", "constraint": "unique"}])
            script = ScriptHandler(triage="implement" if route == "proof" else route,
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
                result = run_delivery(dispatch, repo="Example/Engine", request="Change items", check="check",
                    slice=item, scratch=Path("/FIXTURE"), skills=lambda *_: Path("/METHODS"))
            captures[f"{kind}/{route}"] = {"calls": records, "stop": result["stop"],
                "attempts": [(a["stage"], a["model"], a["methods"]) for a in result["attempts"]]}
    return captures


class StageDeclarationsTest(unittest.TestCase):
    def test_adapter_payload_and_method_order_match_before_refactor(self):
        baseline = Path(__file__).parent / "fixtures/stage-calls.json"
        actual = {key: sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                  for key, value in capture().items()}
        self.assertEqual(actual, json.loads(baseline.read_text()))

    def test_contract_change_reaches_the_adapter(self):
        with patch.dict(STAGES, triage=replace(STAGES["triage"], output=ResearchOutput)):
            calls = capture()["feature/question"]["calls"]
        self.assertEqual(calls[0]["schema"], sha256(json.dumps(ResearchOutput.model_json_schema()).encode()).hexdigest())

    def test_export_checks_methods_from_the_canonical_row(self):
        with skill_fixture() as (config, scratch):
            missing = SkillRule(skill=PstackMethod(path="absent/SKILL.md", kind="skill"))
            with patch.dict(STAGES, spec_check=replace(STAGES["spec_check"], skills=(missing,))):
                with self.assertRaises(SkillMissing):
                    export_skills(config.pstack, scratch)

    def test_declared_inputs_are_delivery_channels(self):
        fields = get_type_hints(DeliveryState)
        for name, row in STAGES.items():
            with self.subTest(stage=name):
                self.assertFalse(set(row.input_fields) - fields.keys())

    def test_spec_check_methods_include_proof(self):
        with skill_fixture() as (config, scratch):
            root = export_skills(config.pstack, scratch)
            _, methods, _ = stage_methods("spec_check", {"plan": PlanState()}, root)
        self.assertIn("principle-prove-it-works/SKILL.md", methods)

    def test_active_and_non_model_resources_stay_distinct(self):
        self.assertEqual(MODEL_STAGES, ("triage", "research", "spec", "implement", "review"))
        self.assertEqual(STAGES["spec_check"].kind, "dormant")
        self.assertEqual(STAGES["spec_check"].prompt, "spec_check.md")
        for name, row in STAGES.items():
            if row.prompt is not None:
                with self.subTest(stage=name):
                    self.assertTrue(stage_text(name))
        for name in ("verify", "proof"):
            self.assertEqual(STAGES[name].kind, "deterministic")
            self.assertIsNone(STAGES[name].prompt)
        self.assertIsNone(STAGES["proof"].output)

    def test_condition_change_reaches_runtime_methods(self):
        row = STAGES["research"]
        skills = (*row.skills[:-1], row.skills[-1].model_copy(update={"when": None}))
        with patch.dict(STAGES, research=replace(row, skills=skills)):
            calls = capture()["feature/question"]["calls"]
        self.assertIn("why/SKILL.md", calls[-1]["methods"])
