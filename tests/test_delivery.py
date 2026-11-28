"""Test delivery."""
import tests

import unittest

from dataclasses import replace
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from foreman.adapters import Outcome
from foreman.config import PSTACK_PIN, ModelSpec, NodeConfig, load_config
from foreman.cli import run_exit_code
from foreman.delivery import LiveRun, compile_delivery, run_delivery, stage_text
from foreman.runner import run_live_delivery
from foreman.stages import STAGES
from foreman.worker import assignment
from foreman.state import ChecksState
from foreman.workspace import WorktreeState
from unittest.mock import patch
from foreman.dry_run import DryRun, ScriptHandler, SCRIPTED_USAGE
from skill_fixture import FixtureSkills, skill_fixture
from foreman.contract import CONTRACT_PATH
from publication_fixture import PublicationFixture, contract_text, run_subprocess


SLICES = [
    {
        "title": "Board lists the deals", "kind": "feature",
        "criteria": [{"text": "The board shows the deals.", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": [],
        "files": [],
        "depends_on": [],
        "screen": "docs/screens/deals.md",
        "properties": [{"name": "Deal external IDs are unique", "constraint": "deals_external_id_key"}],
        "stateful": False,
    },
    {
        "title": "Board saves a move", "kind": "feature",
        "criteria": [{"text": "A move is stored.", "proof": {"kind": "test"}}], "non_goals": [], "removed_tests": [],
        "files": [],
        "depends_on": ["Board lists the deals"],
        "screen": "",
        "properties": [],
        "stateful": False,
    },
]
EMPTY_SLICE = {**SLICES[1], "depends_on": []}


def walk(handler, check="npm run check", config=None, *, slice=EMPTY_SLICE, commands=None, checkout=None):
    with skill_fixture(config) as (fixture_config, scratch):
        return run_delivery(
            handler, repo="DoctorCRE/doctorcre-engine", request="Add a field",
            contract=contract_text(check), slice=slice, config=fixture_config, scratch=scratch, commands=commands, checkout=checkout,
        )


class DeliveryTest(unittest.TestCase):
    def test_each_run_result_is_a_distinct_typed_exit(self):
        # DN1: every way a run ends is its own typed exit. A merge wait is an interrupt, never an exit.
        graph = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(DryRun(), DryRun()))
        exits = {key for branch in graph.builder.branches.values() for route in branch.values()
                 for key, target in (route.ends or {}).items() if target == "__end__"}
        self.assertEqual(exits, {"answered", "children_filed", "cancelled", "no_change", "closed", "merged", "reverted", "halted"})
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
        self.assertEqual(result["plan"].slice["criteria"], SLICES[0]["criteria"])
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

    def test_parent_questions_route_to_research_and_parent_plans_are_checked(self):
        result = walk(ScriptHandler(triage="question"), slice=None)
        self.assertEqual(result["path"], ["triage", "research"])
        result = walk(ScriptHandler(triage="plan", slices=SLICES), slice=None)
        self.assertEqual(result["path"], ["triage", "spec", "spec_check"])
        self.assertEqual(result["stop"], "children_filed")
        handler = ScriptHandler(triage="supplied_slice", slice=EMPTY_SLICE)
        result = walk(handler, slice=None)
        self.assertEqual((result["plan"].issue_kind, result["plan"].triage), ("parent", "supplied_slice"))
        self.assertEqual(result["plan"].slice["criteria"], EMPTY_SLICE["criteria"])
        self.assertEqual(result["path"][:2], ["triage", "spec_check"])
        self.assertNotIn("spec", [call["node"] for call in handler.calls])

    def test_single_slice_rejects_a_missing_dependency(self):
        handler = ScriptHandler(slice=SLICES[1], slices=[EMPTY_SLICE])
        result = walk(handler, slice=None)
        self.assertEqual(result["plan"].spec_failures, 1)
        self.assertEqual(result["path"], ["triage", "spec", "spec_check", "prepare", "implement", "check", "commit", "publish", "review", "merge"])

    def test_invalid_slice_plan_uses_a_round_and_returns_the_typed_reason_to_spec(self):
        first, second = SLICES
        cases = {
            "dependency_cycle": ([{**first, "depends_on": [second["title"]]}, second], first["title"]),
            "unknown_dependency": ([{**first, "depends_on": ["Board archives"]}, second], "Board archives"),
            "duplicate_title": ([first, {**second, "title": first["title"]}], "unique"),
            "file_path": ([{**first, "files": ["../deals.py"]}, second], "../deals.py"),
        }
        for kind, (invalid, named) in cases.items():
            with self.subTest(kind=kind):
                handler = ScriptHandler(triage="plan", slices=invalid)

                def repair(node, model, state, instruction):
                    if rejection := state["plan"].spec_rejection:
                        self.assertEqual(rejection["kind"], kind)
                        self.assertIn(named, rejection["detail"])
                        handler.slices = SLICES
                    return handler(node, model, state, instruction)

                result = walk(repair, slice=None)
                self.assertEqual(result["path"], ["triage", "spec", "spec", "spec_check"])
                self.assertEqual(len(result["review"].review_findings), 0)
                self.assertEqual(result["plan"].spec_failures, 1)
                self.assertEqual(result["stop"], "children_filed")
                self.assertIsNone(result["plan"].spec_rejection)

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

    def related_walk(self, script, *, slice=EMPTY_SLICE):
        """A walk whose contract names the engine as a related repository, so admission pins its checkout."""
        contract = contract_text("npm run check") + b"related_repositories: [{repository: DoctorCRE/doctorcre-engine, start: 'true'}]\n"
        with skill_fixture() as (config, scratch):
            return run_delivery(script, repo="DoctorCRE/doctorcre-app", request="Show the deal's external ID", contract=contract,
                                slice=slice, config=config, scratch=scratch)

    def test_the_builder_asks_when_the_related_checkout_contradicts_the_plan(self):
        # IM9, related half: the builder reads the engine at its pin and cites the engine file that contradicts the plan.
        question = ("The plan says GET /deals returns external_id, but DoctorCRE/doctorcre-engine routes/deals.py:41 at the pinned "
                    "commit returns only id and name. Which is right?")
        script = ScriptHandler()
        def handler(node, model, state, instruction):
            outcome = script(node, model, state, instruction)
            return replace(outcome, output={**outcome.output, "human_question": question}) if node == "implement" else outcome
        result = self.related_walk(handler)
        instruction = next(call["instruction"] for call in script.calls if call["node"] == "implement")
        self.assertIn("Related repository checkouts, read-only at the commits pinned at admission.", instruction)
        self.assertIn("DoctorCRE/doctorcre-engine at <sha>: <checkout of DoctorCRE/doctorcre-engine>", instruction)
        self.assertEqual(result["stop"], "needs_human")
        self.assertIn(question, result["halt_detail"])
        self.assertNotIn("publish", result["path"])

    def test_research_answers_from_the_related_checkout(self):
        answer = "The engine's routes/deals.py at the pinned commit returns id and name, so the board cannot show external_id."
        script = ScriptHandler(triage="question", research=answer)
        result = self.related_walk(script, slice=None)
        instruction = " ".join(next(call["instruction"] for call in script.calls if call["node"] == "research").split())
        self.assertIn("Answer the originating question from the issue, the pinned target checkout, the related repository "
                      "checkouts and cited sources.", instruction)
        self.assertIn("DoctorCRE/doctorcre-engine at <sha>: <checkout of DoctorCRE/doctorcre-engine>", instruction)
        self.assertEqual((result["stop"], result["research"]), ("answered", answer))

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
                    # Each of a round's three passes prefers a family outside every builder's (RV5).
                    self.assertEqual(reviewers, ["claude-opus-5-5-high"] * 3 + ["grok-4.7-high"] * 3)
                    self.assertEqual(result["stop"], "merge_pending")
                else:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high"] * 6)
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
        self.assertEqual(result["stop"], "worker_unavailable")
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
        self.assertEqual(result["stop"], "worker_unavailable")

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
                # A refactor or perf slice's criteria already hold at the base, so a drive proves them.
                driven = {**EMPTY_SLICE, "criteria": [{"text": "A move is stored.", "proof": {"kind": "drive", "not_drivable": None}}]}
                built = walk(ScriptHandler(work_kind=kind), slice=driven)
                methods = next(record["methods"] for record in built["attempts"] if record["stage"] == "implement")
                self.assertEqual("tdd/SKILL.md" in methods, kind == "bug_fix")
                self.assertEqual("benchmark-checklist/SKILL.md" in methods, kind == "perf_issue")

                researched = walk(ScriptHandler(triage="question", work_kind=kind), slice=None)
                research = researched["attempts"][-1]["methods"]
                self.assertIn("how/SKILL.md", research)
                self.assertEqual("why/SKILL.md" in research, kind == "bug_fix")

    def test_draft_uses_statechart_when_revising_a_stored_stateful_slice(self):
        invalid = {**EMPTY_SLICE, "files": ["/policy.py"]}
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
        self.assertEqual([item["stage"] for item in result["attempts"]], ["triage", "spec_check", "implement", "review", "review", "review"])


class WorkerInputTests(unittest.TestCase):
    def test_each_worker_input_holds_only_its_procedures_inputs(self):
        """DP10: Scripted runs that together call every procedure's worker, each started from repository variables. Each
        worker's input holds the inputs its procedure names and nothing else from the run. The login, the caps, the ID of the
        builder's saved session and the record dispatch keeps of each paid call are in none of them. The builder's session
        is resumed by dispatch, not passed as input."""
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
                    calls.append((node, set(state), assignment(node, state, instruction, None)))
                    if node == "implement":
                        workspace.current = WorktreeState(workspace.current.head, False, workspace.current.branch)
                        return replace(script(node, model, state, instruction), session_id=session)
                    return script(node, model, state, instruction)

                result = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=Path(folder) / "run.sqlite3",
                                           dispatcher=handler, run=LiveRun(github, workspace))
            self.assertIn(result["stop"], {"merged", "children_filed", "answered"}, result.get("halt_detail"))
            self.assertEqual((result["start"].human_login, result["start"].max_tokens, result["start"].max_budget_usd),
                             ("fixture-owner", 987654, "43.21"))
        self.assertEqual(set(STAGES), {node for node, _, _ in calls})
        # The second builder call is sent after the first one's session was saved and attempts were recorded. No step holds the
        # start, whose login and caps every reader takes from the run context.
        second = [channels for node, channels, _ in calls if node == "implement"][1]
        self.assertLessEqual({"builder_session", "attempts", "dispatch_plan"}, second)
        self.assertNotIn("start", set().union(*(channels for _, channels, _ in calls)))
        for node, channels, prompt in calls:
            with self.subTest(node=node):
                given = json.loads(prompt.split("\n\nDelivery state:\n", 1)[1])
                self.assertEqual(set(given), set(STAGES[node].input_fields) & channels)
                for secret in ("fixture-owner", "987654", "43.21", session):
                    self.assertNotIn(secret, prompt)



class BaseMergeFixture(PublicationFixture):
    """Merge B: on a confirmed conflict Foreman merges the base head into the owned branch, and the builder resolves only the
    files Git could not merge (implement invariants). GitHub reports the conflict until the PR head contains the base head."""

    def reset_fixture(self, check="! grep -qs '^<<<<<<<' items.txt notes.txt"):
        super().reset_fixture(check)
        # The pinned base holds the two files the slice changes and a test the base later deletes.
        (self.checkout / "tests").mkdir()
        for name, text in (("items.txt", "base\n"), ("notes.txt", "base\n"), ("tests/old_test.txt", "old\n")):
            (self.checkout / name).write_text(text)
        self.git("add", "--all")
        self.git("commit", "-m", "Base files")
        self.git("push", "origin", self.base)
        self.mergeable = False

    def advance_base(self, files, *, deleted=()):
        """Main moves while the PR is open, in a separate clone, so the run's checkout is untouched."""
        other = self.checkout.parent / "other"
        if not other.exists():
            run_subprocess(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        for args in (("fetch", "-q", "origin"), ("checkout", "-q", "-B", self.base, f"origin/{self.base}")):
            run_subprocess(["git", *args], cwd=other, check=True)
        for name, text in files.items():
            (other / name).write_text(text)
        for name in deleted:
            (other / name).unlink()
        for args in (("add", "--all"), ("-c", "user.name=Base", "-c", "user.email=base@example.com", "-c", "commit.gpgsign=false",
                                        "commit", "-qm", "Base moves"), ("push", "-q", "origin", self.base)):
            run_subprocess(["git", *args], cwd=other, check=True)
        return self.git("--git-dir", str(self.remote), "rev-parse", self.base)

    def builder(self, files, resolve, *, deleted=()):
        """The first call builds the slice while main moves. A later call is the conflict repair."""
        seen = {}

        def edit(model, state, instruction):
            if "base" not in seen:
                for name in ("items.txt", "notes.txt"):
                    (self.checkout / name).write_text("one\n")
                seen["base"] = self.advance_base(files, deleted=deleted)
                return None
            seen["merge"] = state["work"].base_merge
            seen["merging"] = self.git("rev-parse", "MERGE_HEAD")
            seen["instruction"] = instruction
            return resolve()
        return edit, seen

    def resolve(self):
        for name in ("items.txt", "notes.txt"):
            (self.checkout / name).write_text("both\n")

    def merge_parents(self):
        return self.git("--git-dir", str(self.remote), "rev-list", "--parents", "-n", "1", self.ref).split()[1:]

    def later_stages(self, result):
        """The stages after the first review round, each round's three review passes counted once."""
        stages = [attempt["stage"] for attempt in result["attempts"]]
        stages = stages[stages.index("review") + 3:]
        return [stage for previous, stage in zip([None, *stages], stages) if stage != "review" or previous != "review"]


class BaseMergeTest(BaseMergeFixture):
    def test_IM15_a_confirmed_conflict_is_merged_by_foreman_and_resolved_by_the_builder(self):
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, self.resolve, deleted=("tests/old_test.txt",))
        result = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        first, merged = self.published_heads[0], self.remote_head()
        # Prepare named the two files Git could not merge, and the builder answered with the merge still in progress.
        self.assertEqual((seen["merge"].head, seen["merge"].unmerged, seen["merging"]),
                         (seen["base"], ["items.txt", "notes.txt"], seen["base"]))
        self.assertIn("Run no Git command that writes", seen["instruction"])
        # Commit made a merge commit with the base head as its second parent, and publish pushed it without force.
        self.assertEqual(self.merge_parents(), [first, seen["base"]])
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{self.ref}:items.txt"), "both")
        self.assertNotIn("tests/old_test.txt", self.git("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", self.ref))
        # Verify and review ran again at the new head, and no question gate opened.
        self.assertEqual(self.published_heads, [first, merged])
        self.assertEqual(self.later_stages(result), ["implement", "verify", "review"])
        self.assertEqual(result["checks"].verified_head, merged)

    def test_IM15_when_git_merges_every_file_no_builder_is_called(self):
        """The base moves a file the slice did not change. Foreman checks the merged tree and commits it."""
        edit, seen = self.builder({"other.txt": "main\n"}, lambda: self.fail("Git merged every file, so no builder is called"))
        result = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        self.assertEqual(self.merge_parents(), [self.published_heads[0], seen["base"]])
        self.assertEqual(self.later_stages(result), ["verify", "review"])

    def test_IM16_a_base_that_changed_the_contract_file_merges_nothing(self):
        """The run keeps its admitted contract. Ready checks the base again without a worker. A stop older than the latest
        Ready no longer counts. A stop after it ends the run cancelled and closes the PR, leaving the branch. Waking a halted
        run on the stop comment is B2's, so here the stop is read when the run resumes."""
        contract = (self.checkout / CONTRACT_PATH).read_text() + "# The base's new contract\n"
        edit, _ = self.builder({CONTRACT_PATH: contract, "items.txt": "main\n"},
                               lambda: self.fail("No worker runs when the base changed the contract file"))
        halted = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual((halted["stop"], halted["path"][-1]), ("base_protected_changed", "prepare"))
        self.assertIn(CONTRACT_PATH, halted["halt_detail"])
        published = self.remote_head()
        self.assertEqual([attempt["stage"] for attempt in halted["attempts"]].count("implement"), 1)
        # The checkout is unchanged: clean, at the published head, with no merge started.
        self.assertEqual((self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), ("", published))
        self.assertNotEqual(run_subprocess(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=self.checkout).returncode, 0)
        assert self.status is not None
        for words in ("Adding Ready checks the base again without calling a worker", "post a foreman stop comment", "Done label"):
            self.assertIn(words, self.status)
        # Ready halts the same way with no paid call.
        again = self.walk(self.no_worker)
        self.assertEqual((again["stop"], again["attempts"]), ("base_protected_changed", halted["attempts"]))
        # A stop posted before Ready was added again no longer counts, so the run halts again with its PR open.
        stale, stop = "foreman stop", "foreman stop\nStart over with the base's contract."
        self.comments.append(stale)
        self.comment_times[stale], self.ready_at = "2026-11-25T10:00:00Z", "2026-11-25T10:00:05Z"
        kept = self.walk(self.no_worker)
        self.assertEqual((kept["stop"], kept["attempts"], self.closed), ("base_protected_changed", halted["attempts"], False))
        # A stop after the latest Ready ends the run cancelled and closes its PR, leaving the branch.
        self.comments.append(stop)
        self.comment_times[stop] = "2026-11-25T10:01:00Z"
        cancelled = self.walk(self.no_worker)
        self.assertEqual((cancelled["stop"], self.closed, self.remote_head()), ("cancelled", True, published))
        self.assertEqual(cancelled["attempts"], halted["attempts"])


class ContainerLimitTest(PublicationFixture):
    """IM21, first and third runs: a container has thirty minutes to be ready, and each check run its contract's
    check_timeout_seconds. The double reports a command that never ends as ended at the limit Workspace gave it, so no test
    waits."""

    def test_IM21_a_setup_command_still_running_at_thirty_minutes_stops_env_unavailable_and_ready_builds_again(self):
        self.set_contract(setup=["echo first", "echo never ends"])
        self.containers.hangs = {"echo never ends"}
        halted = self.walk(self.no_worker)
        self.assertEqual((halted["stop"], halted["path"][-1]), ("env_unavailable", "prepare"))
        self.assertIn("Setup command 'echo never ends' was still running when the thirty minutes a container has to be ready "
                      "ran out", halted["halt_detail"])
        # The thirty minutes count from the start of the pull, which docker run makes, to the end of the last setup command.
        (built, allowed), = self.containers.created
        self.assertTrue(1799 < allowed <= 1800, allowed)
        self.assertLessEqual(self.containers.ran[-1].timeout, allowed)
        self.assertEqual((self.containers.live, self.containers.stopped), (set(), [built]))
        # Ready added again builds the container again.
        self.containers.hangs = set()
        resumed = self.walk(self.edit)
        self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
        self.assertEqual(list(self.containers.specs), ["container-1", "container-2"])

    def test_IM21_a_check_that_does_not_end_in_its_limit_is_a_failed_check_and_its_container_is_built_again(self):
        self.set_contract(extra="check_timeout_seconds: 600\n")
        self.containers.hangs = {self.check}
        calls = []

        def edit(model, state, instruction):
            calls.append((ChecksState.model_validate(state["checks"]), set(self.containers.live)))
            if len(calls) > 1:
                self.containers.hangs = set()
            self.edit()
        result = self.walk(model_edit=edit)
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        (_, before_first), (second, before_second) = calls
        # Edit ended the check after its 600 seconds, counted one failed check and returned that evidence to the builder.
        self.assertEqual([ran.timeout for ran in self.containers.ran if ran.command == self.check][:1], [600])
        self.assertEqual((second.verify, second.verify_failures, second.check_exit_code), ("fail", 1, None))
        self.assertIn(f"Check {self.check!r} did not end within 600 seconds, so Foreman ended it.", second.failure_output)
        # The run's container was removed when the check was ended, and built again before the builder's next call.
        self.assertEqual((before_first, before_second, self.containers.stopped[:1]), ({"container-1"}, {"container-2"}, ["container-1"]))


class BaseHeadContainerTest(BaseMergeFixture):
    """IM21, second and fourth runs: the check at a base head runs in a container of its own whose setup ran on that head's
    checkout, and it is gone when the check ends."""

    def test_IM21_the_check_at_a_base_head_runs_in_a_container_of_its_own_set_up_on_that_head(self):
        self.reset_fixture(check="test -s installed && ! grep -qs '^<<<<<<<' items.txt notes.txt")
        installs = self.checkout.parent / "installs.log"
        (self.checkout / "lock").write_text("pinned\n")
        (self.checkout / ".gitignore").write_text("installed\n")
        self.git("add", "lock", ".gitignore")
        self.git("commit", "-qm", "Lock")
        self.set_contract(setup=[f"cp lock installed && cat lock >> {installs}"])
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n", "lock": "moved\n"}, self.resolve)
        result = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        run, base = self.containers.specs["container-1"], self.containers.specs["container-2"]
        # Setup ran on the pinned base's lockfile for the run, and on the base head's for the base head's own container.
        self.assertEqual(installs.read_text().splitlines(), ["pinned", "moved"])
        # That checkout sits beside the run's, outside Git's directory, and is gone with its container.
        self.assertEqual((base.workdir.parent.parent, base.workdir.exists()), (self.checkout.parent, False))
        self.assertEqual([ran.cwd for ran in self.containers.setups("container-2")], [base.workdir] * 2)
        self.assertEqual(self.containers.stopped, ["container-2"])
        # The run's own container and checkout are unchanged by it.
        self.assertEqual((run.workdir, (self.checkout / "installed").read_text()), (self.checkout, "pinned\n"))
        self.assertEqual(self.containers.live, {"container-1"})

    def test_IM21_a_base_check_that_does_not_end_stops_base_check_failed_after_1800_seconds(self):
        edit, seen = self.builder({"items.txt": "main\n", "notes.txt": "main\n"}, lambda: self.fail("nothing is merged"))
        self.containers.hangs, self.containers.hang_in = {self.check}, lambda cwd: cwd != self.checkout
        halted = self.walk(model_edit=edit, reviews=["approve", "approve"])
        self.assertEqual((halted["stop"], halted["path"][-1]), ("base_check_failed", "prepare"))
        self.assertIn(f"Check {self.check!r} did not end within 1800 seconds at the base head {seen['base']}. Nothing was merged.",
                      halted["halt_detail"])
        self.assertEqual([ran.timeout for ran in self.containers.ran if ran.command == self.check][-1], 1800)
        self.assertEqual(self.containers.stopped, ["container-2"])

if __name__ == "__main__":
    unittest.main()
