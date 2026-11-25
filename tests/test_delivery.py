"""Test delivery."""
import tests

import unittest

from dataclasses import replace
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from foreman.adapters import Outcome, Usage
from foreman.config import PSTACK_PIN, ModelSpec, NodeConfig, load_config
from foreman.cli import run_exit_code
from foreman.delivery import LiveRun, compile_delivery, run_delivery, stage_text
from foreman.runner import run_live_delivery
from foreman.stages import STAGES
from foreman.worker import assignment
from foreman.workspace import WorktreeState
from unittest.mock import patch
from foreman.dry_run import DryRun, ScriptHandler, SCRIPTED_USAGE
from skill_fixture import FixtureSkills, skill_fixture


BOARD = {"name": "deals-board", "exists": False, "path": None}
MOVE = {"name": "deal-move", "exists": False, "path": None}
SLICES = [
    {
        "title": "Board lists the deals",
        "acceptance": "The board shows the deals.",
        "builds": [BOARD],
        "uses": [],
        "screen": "docs/screens/deals.md",
        "properties": [{"name": "Deal external IDs are unique", "constraint": "deals_external_id_key"}],
        "stateful": False,
    },
    {
        "title": "Board saves a move",
        "acceptance": "A move is stored.",
        "builds": [MOVE],
        "uses": [BOARD],
        "screen": "",
        "properties": [],
        "stateful": False,
    },
]
EMPTY_SLICE = {**SLICES[1], "builds": [], "uses": []}


VERIFY_SKILL_PATH = ".claude/skills/verify-web/SKILL.md"


def contract_text(check="exit 0"):
    """A target repository's contract file naming the check and the verify skill."""
    return f"check: {json.dumps(check)}\nverify_skill: {VERIFY_SKILL_PATH}\n".encode()


def walk(handler, check="npm run check", config=None, *, slice=EMPTY_SLICE, commands=None, checkout=None):
    with skill_fixture(config) as (fixture_config, scratch):
        return run_delivery(
            handler, repo="DoctorCRE/doctorcre-engine", request="Add a field",
            contract=contract_text(check), slice=slice, config=fixture_config, scratch=scratch, commands=commands, checkout=checkout,
        )


class DeliveryTest(unittest.TestCase):
    def test_each_run_result_is_a_distinct_typed_exit(self):
        # DN1: every way a run ends is its own typed exit. A merge wait is an interrupt, never an exit.
        graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(DryRun(), DryRun(), human_login="fixture-owner"))
        exits = {key for branch in graph.builder.branches.values() for route in branch.values()
                 for key, target in (route.ends or {}).items() if target == "__end__"}
        self.assertEqual(exits, {"answered", "children_filed", "cancelled", "no_change", "closed", "merged", "halted"})
        # A queued pull request is pending: no completion until GitHub reports the merge.
        result = walk(ScriptHandler())
        self.assertEqual((result["stop"], result["merge"].status, run_exit_code(result)), ("merge_pending", "skipped", 75))
        self.assertEqual(result["path"][-1], "merge")

    def test_graph_receives_the_skill_source_as_an_input(self):
        source = FixtureSkills()

        with TemporaryDirectory() as directory:
            result = run_delivery(
                ScriptHandler(), repo="Example/Engine",
                request="List items", scratch=Path(directory), skills=source,
            )
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual(source.pins, [PSTACK_PIN])
        self.assertTrue(result["attempts"])

    def test_passing_a_slice_starts_a_slice_run(self):
        result = walk(ScriptHandler(triage="plan"), slice=SLICES[0])
        self.assertEqual(result["plan"].issue_kind, "slice")
        self.assertEqual(result["plan"].slice["acceptance"], SLICES[0]["acceptance"])
        self.assertEqual(result["stop"], "merge_pending")

    def test_clear_change_still_runs_the_checker(self):
        handler = ScriptHandler()

        def check_review_context(node, model, state, instruction):
            if node == "review":
                self.assertEqual(state["checks"].verify, "pass")
                self.assertEqual(state["work"].head_sha, state["checks"].verified_head)
            return handler(node, model, state, instruction)

        result = walk(check_review_context)
        self.assertEqual(
            result["path"],
            ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "review", "merge"],
        )
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual(result["merge"].status, "skipped")
        self.assertEqual(result["work"].head_sha, result["checks"].verified_head)
        self.assertEqual(result["models_used"]["implement"], "gpt-6.1-sol-high")
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertNotIn("verify", result["models_used"])

    def test_ready_spec_is_implemented(self):
        result = walk(ScriptHandler(triage="plan", spec_gate="ready"))
        self.assertEqual(
            result["path"],
            ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "review", "merge"],
        )
        self.assertEqual(result["stop"], "merge_pending")

    def test_ready_spec_retains_acceptance_specific_evidence(self):
        result = walk(ScriptHandler(triage="plan"), slice=SLICES[0])
        self.assertEqual(result["path"], ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "review", "merge"])
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual(result["plan"].slice["properties"], SLICES[0]["properties"])

    def test_worker_cannot_declare_depends_on(self):
        result = walk(
            lambda *_: Outcome("ran", {
                "triage": "supplied_slice", "work_kind": "feature", "human_question": None, "slice": {**SLICES[0], "depends_on": ["Other"]},
            }, usage=Usage(cache_read_input_tokens=0, cache_creation_input_tokens=0, input_tokens=1, output_tokens=1, total_cost_usd=0.0)),
            slice=None,
        )
        self.assertEqual(result["path"], ["triage"])
        self.assertEqual(result["stop"], "invalid_output")
        self.assertTrue(result["halt_detail"].startswith("triage output from claude-opus-5-5-high: slice.depends_on:"))

    def test_parent_questions_route_to_research_and_parent_plans_are_checked(self):
        result = walk(ScriptHandler(triage="question"), slice=None)
        self.assertEqual(result["path"], ["triage", "research"])
        result = walk(ScriptHandler(triage="plan", slices=SLICES), slice=None)
        self.assertEqual(result["path"], ["triage", "spec", "spec_check"])
        self.assertEqual(result["stop"], "children_filed")

    def test_single_slice_rejects_a_missing_dependency(self):
        handler = ScriptHandler(slice=SLICES[1], slices=[EMPTY_SLICE])
        result = walk(handler, slice=None)
        self.assertEqual(result["plan"].spec_failures, 1)
        self.assertEqual(result["path"], ["triage", "spec", "spec_check", "prepare", "implement", "check", "commit", "publish", "review", "merge"])

    def test_invalid_slice_plan_uses_a_round_and_returns_the_typed_reason_to_spec(self):
        first, second = SLICES
        cases = {
            "dependency_cycle": [{**first, "uses": [MOVE]}, second],
            "dangling_seam": [{**first, "builds": []}, second],
            "duplicate_builder": [first, {**second, "builds": [BOARD]}],
        }
        for kind, invalid in cases.items():
            with self.subTest(kind=kind):
                handler = ScriptHandler(triage="plan", slices=invalid)

                def repair(node, model, state, instruction):
                    if rejection := state["plan"].spec_rejection:
                        self.assertEqual(rejection["kind"], kind)
                        self.assertIn(BOARD["name"] if kind != "dependency_cycle" else first["title"], rejection["detail"])
                        handler.slices = SLICES
                    return handler(node, model, state, instruction)

                result = walk(repair, slice=None)
                self.assertEqual(result["path"], ["triage", "spec", "spec", "spec_check"])
                self.assertEqual(len(result["review"].review_findings), 0)
                self.assertEqual(result["plan"].spec_failures, 1)
                self.assertEqual(result["stop"], "children_filed")
                self.assertIsNone(result["plan"].spec_rejection)

    def test_seam_validation_for_single_and_multiple_slices(self):
        from foreman.stages import clean_slices, InvalidSlicePlan
        from foreman.stage_output import Slice
        from foreman.workspace import LiveWorkspace
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from foreman.skill_rules import git
            (root / "items.py").touch()
            git(root, "init", "-q")
            git(root, "add", "items.py")
            git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-qm", "Tracked items")
            workspace = LiveWorkspace(root)
            base = git(root, "rev-parse", "HEAD")
            new = {"name": "items", "exists": False, "path": None}
            existing = {**new, "exists": True, "path": "items.py"}
            for count in (1, 2):
                for use, kind in ((existing, "existing_seam_builder"), ({**existing, "path": "absent.py"}, "dangling_seam"),
                                  (new, None)):
                    item = {**EMPTY_SLICE, "builds": [new], "uses": [use]}
                    raw = [item] + ([{**item, "title": "Consumer", "builds": []}] if count == 2 else [])
                    if kind:
                        with self.assertRaises(InvalidSlicePlan) as raised:
                            clean_slices([Slice.model_validate(s) for s in raw], workspace, base)
                        self.assertEqual(raised.exception.reason["kind"], kind)
                    else:
                        self.assertEqual(clean_slices([Slice.model_validate(s) for s in raw], workspace, base)[0]["depends_on"], [])

    def test_common_validator_rejects_missing_dependencies_and_cycles(self):
        from foreman.stages import clean_slices, InvalidSlicePlan
        from foreman.stage_output import Slice
        first, second = SLICES
        cases = {
            "duplicate_builder": [first, {**second, "builds": [BOARD]}],
            "dangling_seam": [{**first, "builds": [], "uses": [MOVE]}, {**second, "builds": []}],
            "dependency_cycle": [{**first, "uses": [MOVE]}, second],
        }
        for kind, plan in cases.items():
            for count in (1, 2):
                raw = [Slice.model_validate(item) for item in plan[:count]]
                with self.subTest(kind=kind, count=count):
                    expected = ("dangling_seam" if kind == "dependency_cycle" else kind) if count == 1 else kind
                    if count == 1 and kind == "duplicate_builder":
                        self.assertEqual(clean_slices(raw, None)[0]["depends_on"], [])
                        continue
                    with self.assertRaises(InvalidSlicePlan) as raised:
                        clean_slices(raw, None)
                    self.assertEqual(raised.exception.reason["kind"], expected)

    def test_a_parent_stops_on_malformed_slices(self):
        first = SLICES[0]
        missing_key = {key: value for key, value in first.items() if key != "screen"}
        cases = {
            "missing key": [missing_key],
            "extra key": [{**first, "branch": "extra"}],
            "outside screens": [{**first, "screen": "docs/system.md"}],
            "traversal": [{**first, "screen": "docs/screens/../system.md"}],
            "double dot": [{**first, "screen": "docs/screens/deals..draft.md"}],
            "backslash": [{**first, "screen": r"docs/screens/other\deals.md"}],
            "nested screen": [{**first, "screen": "docs/screens/sub/deals.md"}],
            "blank constraint": [{**first, "properties": [{"name": "Deal external IDs are unique", "constraint": " "}]}],
            "missing name": [{**first, "properties": [{"constraint": "deals_external_id_key"}]}],
        }
        for case, slices in cases.items():
            with self.subTest(case=case):
                result = walk(ScriptHandler(triage="plan", slices=slices), slice=None)
                self.assertEqual(result["path"], ["triage", "spec"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"])

    def test_review_can_use_the_exact_builder_model(self):
        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], models=(ModelSpec.parse("gpt-6.1-sol-high"),))
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual(result["models_used"]["review"], result["models_used"]["implement"])

    def test_review_stops_when_the_builder_model_is_missing(self):
        config = load_config()
        config.nodes["implement"] = NodeConfig(role="check", models=())
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["stop"], "model_dependency_missing")
        self.assertIn("No model is configured for implement", result["halt_detail"])
        self.assertNotIn("review", [call["node"] for call in handler.calls])

    def test_model_selection_stops_when_its_dependency_is_missing(self):
        config = load_config()
        config.nodes["research"] = replace(config.nodes["research"], other_family_from="implement")
        result = walk(ScriptHandler(triage="question"), config=config, slice=None)
        self.assertEqual(result["stop"], "model_dependency_missing")

    def builder_answers(self, **answer):
        """The scripted run with the builder's answer changed, and the commands the run would issue."""
        script, commands = ScriptHandler(), []
        def handler(node, model, state, instruction):
            outcome = script(node, model, state, instruction)
            return replace(outcome, output={**outcome.output, **answer}) if node == "implement" else outcome
        return walk(handler, commands=commands), script, commands

    def test_typed_no_change_evidence_on_a_first_attempt_ends_no_change(self):
        # IM8: a clean tree with the builder's evidence that the acceptance already holds publishes nothing.
        evidence = "npm run check passed on the pinned base, and its board test shows each deal once."
        result, _, commands = self.builder_answers(no_change=evidence)
        self.assertEqual((result["stop"], result.get("halt"), result["halt_detail"]), ("no_change", None, evidence))
        self.assertEqual([attempt["stage"] for attempt in result["attempts"]].count("implement"), 1)
        self.assertNotIn("check", result["path"])
        self.assertEqual([command for command in commands if command[1] in {"commit", "push"}], [])

    def test_a_builder_question_on_a_contradicted_plan_fact_opens_the_gate(self):
        # IM9: the prompt tells the builder to challenge a plan fact, and its question waits with nothing committed.
        question = "The plan says GET /deals returns external_id, but routes/deals.py:41 returns only id and name. Which is right?"
        result, script, commands = self.builder_answers(human_question=question)
        instruction = " ".join(next(call["instruction"] for call in script.calls if call["node"] == "implement").split())
        for sentence in ("If the code contradicts a fact the plan states, do not build it.",
                         "Ask a question that cites the plan fact and the code.",
                         "On a first attempt whose acceptance already holds on the pinned base, edit nothing and set no_change "
                         "to the checks you ran and what they showed.",
                         "A clean tree without that evidence is an invalid answer."):
            self.assertIn(sentence, instruction)
        self.assertEqual(result["stop"], "needs_human")
        self.assertIn(question, result["halt_detail"])
        self.assertEqual([attempt["stage"] for attempt in result["attempts"]].count("implement"), 1)
        self.assertEqual([command for command in commands if command[1] in {"commit", "push"}], [])

    def test_open_spec_stops_for_a_person(self):
        result = walk(ScriptHandler(triage="plan", spec_gate="needs_human"), slice=None)
        self.assertEqual(result["path"], ["triage", "spec"])
        self.assertEqual(result["stop"], "needs_human")
        self.assertNotIn("implement", result["path"])

    def test_question_is_answered_without_a_change(self):
        result = walk(ScriptHandler(triage="question"), slice=None)
        self.assertEqual(result["path"], ["triage", "research"])
        self.assertEqual(result["stop"], "answered")

    def test_one_change_request_loops_once(self):
        result = walk(ScriptHandler(reviews=["changes", "approve"]))
        self.assertEqual(
            result["path"],
            ["triage", "spec_check", "prepare", "implement", "check", "commit", "publish", "review", "prepare", "implement", "check", "commit", "publish", "review", "merge"],
        )
        self.assertEqual(result["stop"], "merge_pending")

    def test_review_prefers_models_outside_every_builder_family(self):
        for review_models in (("claude-opus-5-5-high", "gpt-6.1-sol-high", "grok-4.7-high"),
                              ("claude-opus-5-5-high", "gpt-6.1-sol-high")):
            with self.subTest(review_models=tuple(ModelSpec.parse(model) for model in review_models)):
                config = load_config()
                config.nodes["review"] = replace(config.nodes["review"], models=tuple(ModelSpec.parse(model) for model in review_models))
                handler = ScriptHandler(reviews=["changes", "approve"])

                def switch_builder(node, model, state, instruction):
                    if node == "implement" and state["review"].review == "changes" and model and model.family == "gpt":
                        return Outcome("quota", usage=SCRIPTED_USAGE)
                    return handler(node, model, state, instruction)

                result = walk(switch_builder, config=config)
                reviewers = [call["model"] for call in handler.calls if call["node"] == "review"]
                self.assertEqual(result["models_used"]["implement"], "claude-opus-5-5-high")
                if "grok-4.7-high" in review_models:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high", "grok-4.7-high"])
                    self.assertEqual(result["stop"], "merge_pending")
                else:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high"] * 2)
                    self.assertEqual(result["stop"], "merge_pending")
                self.assertEqual(result["implement_models"], ["gpt-6.1-sol-high", "claude-opus-5-5-high"])

    def test_review_cap_stops_without_arming(self):
        result = walk(ScriptHandler(reviews=[]))
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertEqual(result["path"].count("check"), 3)
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "review_cap")

    def test_verify_cap_stops_without_review(self):
        with patch.object(DryRun, "run", return_value=1):
            result = walk(ScriptHandler())
        self.assertEqual(result["path"].count("check"), 3)
        self.assertNotIn("review", result["path"])
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "verify_cap")

    def test_needs_human_stops_at_triage(self):
        result = walk(ScriptHandler(triage="needs_human"))
        self.assertEqual(result["path"], ["triage"])
        self.assertEqual(result["stop"], "needs_human")

    def test_review_without_findings_is_rejected(self):
        handler = ScriptHandler()

        def drop_findings(node, model, state, instruction):
            raw = handler(node, model, state, instruction)
            if node == "review":
                raw.output.pop("findings")
            return raw

        result = walk(drop_findings)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("review output from claude-opus-5-5-high: findings:", result["halt_detail"])
        self.assertEqual(result["path"].count("review"), 1)
        self.assertNotIn("merge", result["path"])

    def test_review_exhausts_the_whole_configured_list(self):
        handler = ScriptHandler()
        offered = []

        def exhaust_review(node, model, state, instruction):
            if node == "review":
                offered.append(model.name)
                return Outcome("quota", usage=SCRIPTED_USAGE)
            return handler(node, model, state, instruction)

        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], other_family_from=None)
        result = walk(exhaust_review, config=config)
        self.assertEqual(offered, ["claude-opus-5-5-high", "gpt-6.1-sol-high"])
        self.assertEqual(result["stop"], "quota_exhausted")
        self.assertIn("quota", result["halt_detail"])
        self.assertNotIn("merge", result["path"])

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if node == "triage" and model and model.name == "claude-opus-5-5-high":
                return Outcome("quota", usage=SCRIPTED_USAGE)
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["models_used"]["triage"], "grok-4.7-high")
        triage = [item for item in result["attempts"] if item["stage"] == "triage"]
        self.assertEqual([item["model"] for item in triage], ["claude-opus-5-5-high", "grok-4.7-high"])
        self.assertEqual([item["outcome"] for item in triage], ["quota", "ran"])
        self.assertEqual([item["methods"] for item in triage], [[]] * 2)
        self.assertEqual(result["models_used"]["review"], "claude-opus-5-5-high")
        self.assertEqual(result["stop"], "merge_pending")

    def test_every_model_exhausted_halts(self):
        def exhaust(node, model, state, instruction):
            return Outcome("quota", usage=SCRIPTED_USAGE)

        result = walk(exhaust)
        self.assertEqual(result["stop"], "quota_exhausted")

    def test_node_instruction_comes_from_the_stage_file(self):
        handler = ScriptHandler(triage="plan")
        walk(handler)
        by_node = {call["node"]: call["instruction"] for call in handler.calls}
        self.assertTrue(by_node["triage"].startswith(stage_text("triage")))
        for instruction in by_node.values():
            self.assertNotIn("Talk through the work with the person.", instruction)
            self.assertNotIn("The Ready label starts the delivery graph.", instruction)
        self.assertTrue(by_node["implement"].startswith(stage_text("implement")))
        self.assertIn("review-quality.md", by_node["review"])
        self.assertNotIn("verify", by_node)
        self.assertNotIn("gpt-6.1-sol-high", " ".join(by_node.values()))

    def test_work_kind_selects_implement_and_research_methods(self):
        for kind in ("feature", "bug_fix", "refactoring", "perf_issue"):
            with self.subTest(kind=kind):
                built = walk(ScriptHandler(work_kind=kind))
                methods = next(record["methods"] for record in built["attempts"] if record["stage"] == "implement")
                self.assertEqual("tdd/SKILL.md" in methods, kind == "bug_fix")
                self.assertEqual("benchmark-checklist/SKILL.md" in methods, kind == "perf_issue")

                researched = walk(ScriptHandler(triage="question", work_kind=kind), slice=None)
                research = researched["attempts"][-1]["methods"]
                self.assertIn("how/SKILL.md", research)
                self.assertEqual("why/SKILL.md" in research, kind == "bug_fix")

    def test_draft_uses_statechart_when_revising_a_stored_stateful_slice(self):
        absent = {"name": "policy", "exists": True, "path": "missing.py"}
        invalid = {**EMPTY_SLICE, "uses": [absent]}
        for item in (None, invalid, {**invalid, "stateful": True}):
            with self.subTest(item=item):
                result = walk(ScriptHandler(triage="plan", slices=[{**EMPTY_SLICE, "stateful": True}]), slice=item)
                methods = next(record["methods"] for record in result["attempts"] if record["stage"] == "spec")
                self.assertEqual("statechart/SKILL.md" in methods, item is not None and item["stateful"])

    def test_worker_gets_absolute_read_only_method_paths(self):
        script = ScriptHandler(triage="plan")

        def read_methods(node, model, state, instruction):
            paths = re.findall(r"^Read (/.*?\.md) ", instruction, re.M)
            # Triage draws no skills, and implement's verify skill lives in the target repository.
            if node not in {"triage", "implement"}:
                self.assertTrue(paths)
            for path in paths:
                method = Path(path)
                text = method.read_text().strip()
                self.assertTrue(text)
                if text.startswith("Synthetic method."):
                    self.assertEqual(method.stat().st_mode & 0o222, 0)
            return script(node, model, state, instruction)

        result = walk(read_methods)
        self.assertEqual(result["stop"], "merge_pending")
        self.assertEqual([item["stage"] for item in result["attempts"]], ["triage", "spec_check", "implement", "review"])


class WorkerInputTests(unittest.TestCase):
    def test_each_worker_input_holds_only_its_procedures_inputs(self):
        """DP10: a live run started from repository variables calls every procedure's worker. Each worker's input holds the
        inputs its procedure names and nothing else from the run. The login, the caps, the builder's saved session and the
        attempt records are in none of them."""
        from test_runner import FakeGitHub, FakeWorkspace

        settings = {"FOREMAN_HUMAN_LOGIN": "fixture-owner", "FOREMAN_MAX_TOKENS": "987654", "FOREMAN_MAX_BUDGET_USD": "43.21"}
        session = "0d10d10d-0000-4000-8000-00000000d010"
        calls = []
        # A slice issue goes to the builder twice. A Ready issue is split or answered.
        for labels, script in ((None, ScriptHandler(reviews=["changes", "approve"])),
                               (["Ready"], ScriptHandler(triage="plan", slices=[EMPTY_SLICE])), (["Ready"], ScriptHandler(triage="question"))):
            with TemporaryDirectory() as folder, skill_fixture() as (config, _), patch("foreman.runner.load_config", return_value=config):
                workspace, github = FakeWorkspace(Path(folder)), FakeGitHub(labels)
                github.settings = settings

                def handler(node, model, state, instruction):
                    calls.append((node, set(state), assignment(node, model, state, instruction, None)))
                    if node == "implement":
                        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
                        return replace(script(node, model, state, instruction), session_id=session)
                    return script(node, model, state, instruction)

                result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=Path(folder) / "run.sqlite3",
                                           dispatcher=handler, run=LiveRun(github, workspace))
            self.assertIn(result["stop"], {"merged", "children_filed", "answered"}, result.get("halt_detail"))
            self.assertEqual((result["human_login"], result["run"].token_cap, result["run"].max_budget_usd),
                             ("fixture-owner", 987654, "43.21"))
        self.assertEqual(set(STAGES), {node for node, _, _ in calls})
        # The second builder call is sent after the first one's session was saved and attempts were recorded.
        self.assertLessEqual({"builder_session", "attempts", "dispatch_plan", "human_login"},
                             [channels for node, channels, _ in calls if node == "implement"][1])
        for node, channels, prompt in calls:
            with self.subTest(node=node):
                given = json.loads(prompt.split("\n\nDelivery state:\n", 1)[1])
                self.assertEqual(set(given), set(STAGES[node].input_fields) & channels)
                self.assertFalse({"token_cap", "max_budget_usd"} & set(given["run"]))
                for secret in ("fixture-owner", "987654", "43.21", session):
                    self.assertNotIn(secret, prompt)


if __name__ == "__main__":
    unittest.main()
