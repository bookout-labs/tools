from dataclasses import replace
from enum import StrEnum
from pathlib import Path
import unittest
from unittest.mock import patch

from langgraph.graph import StateGraph

from docs.graph.blueprint import project_actual as projection
from foreman.skill_rules import SkillRule


class BlueprintProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.target = projection.load_target()
        cls.waivers = projection.load_waivers()

    def errors(self):
        return projection.check(projection.compare(self.target, projection.project()), self.waivers)

    def assert_unwaived(self, subject):
        self.assertTrue(any(error.startswith("Unwaived:") and subject in error for error in self.errors()),
                        f"Projection did not reject {subject}")

    def test_compiled_graph_matches_blueprint_and_current_waivers(self):
        errors = self.errors()
        self.assertFalse(errors, "\n".join(errors))

    def test_unwaived_node_and_edge_in_compiled_graph(self):
        compile_graph = StateGraph.compile

        def with_new_node(builder, *args, **kwargs):
            builder.add_node("unplanned", lambda _: {})
            builder.add_edge("unplanned", "__end__")
            return compile_graph(builder, *args, **kwargs)

        with patch.object(StateGraph, "compile", with_new_node):
            errors = "\n".join(self.errors())
        self.assertRegex(errors, r"Unwaived: .* node unplanned:")
        self.assertRegex(errors, r"Unwaived: .* edge unplanned -> __end__:")

    def test_unwaived_alias_on_existing_compiled_edge(self):
        compile_graph = StateGraph.compile

        def with_new_route(builder, *args, **kwargs):
            builder.add_conditional_edges("triage", lambda _: "unplanned", {"unplanned": "research"})
            return compile_graph(builder, *args, **kwargs)

        with patch.object(StateGraph, "compile", with_new_route):
            self.assert_unwaived("edge triage -> research")

    def test_unwaived_step_in_nested_dispatch(self):
        dispatch = (StateGraph(dict).add_node("unplanned", lambda _: {})
                    .add_edge("__start__", "unplanned").add_edge("unplanned", "__end__")
                    .compile().with_config({"metadata": {"contract_id": "dispatch"}}))

        def route(state):
            return dispatch.invoke(state)

        triage = (StateGraph(dict).add_node("route", route)
                  .add_edge("__start__", "route").add_edge("route", "__end__").compile())
        delivery = (StateGraph(dict).add_node("triage", triage)
                    .add_edge("__start__", "triage").add_edge("triage", "__end__").compile())

        with patch.object(projection, "compile_delivery", return_value=delivery):
            self.assert_unwaived("[steps/triage.route] node unplanned")

    def test_unwaived_halt(self):
        halts = StrEnum("Halt", [*(halt.value for halt in projection.Halt), "unplanned_halt"])
        with patch.object(projection, "Halt", halts):
            self.assert_unwaived("[shared/halts] unplanned_halt")

    def test_unwaived_owner_symbol(self):
        owner = projection.ROOT / self.target["dispatch"]["implementation"]["module"]
        read_text = Path.read_text

        def with_new_symbol(path, *args, **kwargs):
            text = read_text(path, *args, **kwargs)
            return text + "\n\nif True:\n    def unplanned_owner():\n        pass\n" if path == owner else text

        with patch.object(Path, "read_text", with_new_symbol):
            self.assert_unwaived("owner src/foreman/dispatch.py::unplanned_owner")

    def test_unwaived_skill_on_existing_procedure(self):
        stage = projection.STAGES["triage"]
        extra_skill = SkillRule(skill="review-quality")
        with patch.dict(projection.STAGES, triage=replace(stage, skills=(*stage.skills, extra_skill))):
            self.assert_unwaived("[procedure/triage] skills")

    def test_waiver_becomes_stale_when_injected_difference_disappears(self):
        actual = projection.project()
        baseline = projection.compare(self.target, actual)
        actual["intake"].append("unplanned_event")
        changed = projection.compare(self.target, actual)
        difference, = (row for row in changed if row.subject == "admit unplanned_event")
        waivers = {**self.waivers, 238: {**self.waivers.get(238, {}), difference.id: difference.subject}}

        self.assertEqual(projection.check(changed, waivers), [])
        self.assertEqual(projection.check(baseline, waivers),
                         [f"Stale waiver {difference.id} (#238): admit unplanned_event"])


if __name__ == "__main__":
    unittest.main()
