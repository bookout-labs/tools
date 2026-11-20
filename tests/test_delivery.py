import contextlib
import io
from pathlib import Path
import unittest

from dataclasses import replace

from foreman.cli import _print_run
from foreman.config import ConfigError, family, NodeConfig, load_config
from foreman.delivery import REQUIRED_NODES, compile_delivery, diagram, run_delivery, stage_text
from foreman.dispatch import CliMissing, FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.dry_run import ScriptHandler
from foreman.pointer import load_pointer
from foreman.resolve import Cmd
from foreman.save import config_from_pointer


SLICES = [
    {
        "title": "Board lists the deals",
        "acceptance": "The board shows the deals.",
        "depends_on": [],
        "screen": "docs/screens/deals.md",
        "properties": [{"name": "Visibility", "constraint": "Every open deal appears once."}],
    },
    {
        "title": "Board saves a move",
        "acceptance": "A move is stored.",
        "depends_on": ["Board lists the deals"],
        "screen": "",
        "properties": [],
    },
]


HEAD_SHA = "a" * 40
PUBLISHED = "b" * 40
ROOT = Path(__file__).parents[1]


def walk(handler, check="npm run check", issue_kind="slice", config=None, read_merge_hold=None):
    return run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check=check,
        issue_kind=issue_kind,
        config=config,
        read_merge_hold=read_merge_hold,
    )


class DeliveryTest(unittest.TestCase):
    def test_clear_change_skips_spec(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)

        def check_review_context(node, model, state, instruction):
            if node == "review":
                self.assertEqual(state["verify"], "pass")
                self.assertEqual(state["head_sha"], HEAD_SHA)
            return handler(node, model, state, instruction)

        result = walk(check_review_context)
        self.assertEqual(
            result["path"],
            ["triage", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["risk"]["level"], "low")
        self.assertTrue(result["merge_armed"])
        self.assertEqual(result["head_sha"], HEAD_SHA)
        self.assertEqual(result["models_used"]["implement"], "terra")
        self.assertEqual(result["models_used"]["review"], "sol")
        implement = next(choice for choice in result["model_choices"] if choice["node"] == "implement")
        self.assertEqual(implement["reason"], "small scoped build")
        self.assertNotIn("verify", result["models_used"])

    def test_ready_spec_is_implemented(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="ready"))
        self.assertEqual(
            result["path"],
            ["triage", "spec", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_small_parent_that_triages_implement_skips_spec(self):
        result = walk(ScriptHandler(triage="implement", head_sha=HEAD_SHA), issue_kind="parent")
        self.assertEqual(
            result["path"],
            ["triage", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertNotIn("slices", result)
        self.assertTrue(result["merge_armed"])
        self.assertIsNone(result["proposal"])

    def test_parent_that_triages_spec_slices_and_stops(self):
        result = walk(ScriptHandler(triage="spec", slices=SLICES), issue_kind="parent")
        self.assertEqual(result["path"], ["triage", "spec", "done"])
        self.assertEqual(result["stop"], "sliced")
        self.assertEqual(result["slices"], SLICES)

    def test_parent_that_triages_question_still_routes_to_spec(self):
        result = walk(ScriptHandler(triage="question", spec_gate="ready", slices=SLICES), issue_kind="parent")
        self.assertEqual(result["path"], ["triage", "spec", "done"])
        self.assertEqual(result["stop"], "sliced")

    def test_a_parent_stops_on_malformed_slices(self):
        first, second = SLICES
        missing_key = {key: value for key, value in first.items() if key != "screen"}
        cases = {
            "cycle": [{**first, "depends_on": [second["title"]]}, second],
            "unknown dependency": [{**first, "depends_on": ["Unknown"]}],
            "missing key": [missing_key],
            "extra key": [{**first, "branch": "extra"}],
            "outside screens": [{**first, "screen": "docs/system.md"}],
            "traversal": [{**first, "screen": "docs/screens/../system.md"}],
            "backslash": [{**first, "screen": r"docs/screens/other\deals.md"}],
            "nested screen": [{**first, "screen": "docs/screens/sub/deals.md"}],
            "blank constraint": [{**first, "properties": [{"name": "Visibility", "constraint": " "}]}],
            "missing name": [{**first, "properties": [{"constraint": "Every deal appears once."}]}],
        }
        for case, slices in cases.items():
            with self.subTest(case=case):
                result = walk(ScriptHandler(triage="spec", slices=slices), issue_kind="parent")
                self.assertEqual(result["path"], ["triage", "spec", "done"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"])

    def test_review_stops_when_no_other_family_remains(self):
        config = load_config()
        config.nodes["review"] = replace(
            config.nodes["review"],
            models=("gpt-6.1-terra-high",),
            other_family_from="implement",
        )
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "done"])
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("gpt family", result["halt_detail"])
        self.assertNotIn("review", [call["node"] for call in handler.calls])

    def test_review_stops_when_the_builder_model_is_missing(self):
        config = load_config()
        config.nodes["implement"] = NodeConfig(role="check", models=())
        handler = ScriptHandler()
        result = walk(handler, config=config)
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("implement model", result["halt_detail"])
        self.assertNotIn("review", [call["node"] for call in handler.calls])

    def test_open_spec_stops_for_a_person(self):
        result = walk(ScriptHandler(triage="spec", spec_gate="needs_human"))
        self.assertEqual(result["path"], ["triage", "spec", "done"])
        self.assertEqual(result["stop"], "needs_human")
        self.assertNotIn("implement", result["path"])

    def test_a_sectioned_spec_is_stored_as_one_document(self):
        def handler(node, model, state, instruction):
            if node == "triage":
                return {"triage": "spec"}
            self.assertIn("one document", instruction)
            return {
                "spec": {
                    "behavior": "Keep GET /health public.",
                    "current_state": "src/worker.ts already returns version.",
                },
                "spec_gate": "needs_human",
                "human_question": "Which clock?",
            }

        result = walk(handler, issue_kind="parent")
        self.assertEqual(result["path"], ["triage", "spec", "done"])
        self.assertEqual(result["stop"], "needs_human")
        self.assertEqual(
            result["spec"],
            "behavior\nKeep GET /health public.\n\ncurrent_state\nsrc/worker.ts already returns version.",
        )
        self.assertEqual(result["human_question"], "Which clock?")
        self.assertEqual(result["models_used"]["spec"], "opus")
        self.assertNotIn("implement", result["path"])

    def test_a_sectioned_implementation_is_stored_as_one_document(self):
        script = ScriptHandler(head_sha=HEAD_SHA)

        def handler(node, model, state, instruction):
            if node == "implement":
                self.assertIn("one document", instruction)
                self.assertIn("The runner commits them on that branch.", instruction)
                return {
                    "implementation": {
                        "summary": "Health reports the release.",
                        "validation": "Worker tests pass.",
                    },
                    "head_sha": HEAD_SHA,
                }
            return script(node, model, state, instruction)

        result = walk(handler)
        self.assertEqual(
            result["path"],
            ["triage", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(
            result["implementation"],
            "summary\nHealth reports the release.\n\nvalidation\nWorker tests pass.",
        )
        self.assertEqual(result["head_sha"], HEAD_SHA)
        self.assertEqual(result["models_used"]["implement"], "terra")

    def test_an_implementation_that_is_not_a_document_records_the_model(self):
        shapes = (
            ["Health reports the release."],
            {"summary": {"text": "Health reports the release."}},
            {"summary": "  "},
            {},
            "  ",
        )
        for implementation in shapes:
            with self.subTest(implementation=implementation):
                def handler(node, model, state, instruction, implementation=implementation):
                    if node == "implement":
                        return {"implementation": implementation, "head_sha": HEAD_SHA}
                    return {"triage": "implement"}

                result = walk(handler)
                self.assertEqual(result["stop"], "invalid_output")
                self.assertEqual(result["halt_detail"], "implement did not return text")
                self.assertEqual(result["models_used"]["implement"], "terra")
                self.assertNotIn("verify", result["path"])

    def test_a_sectioned_research_answer_is_stored_as_one_document(self):
        def handler(node, model, state, instruction):
            if node == "triage":
                return {"triage": "question"}
            self.assertIn("one document", instruction)
            return {"research": {"answer": "The release comes from the tag.", "source": "src/worker.ts"}}

        result = walk(handler)
        self.assertEqual(result["path"], ["triage", "research", "done"])
        self.assertEqual(result["stop"], "answered")
        self.assertEqual(
            result["research"],
            "answer\nThe release comes from the tag.\n\nsource\nsrc/worker.ts",
        )

    def test_a_spec_that_is_not_a_document_records_the_model(self):
        shapes = (
            ["Keep GET /health public."],
            {"behavior": {"text": "Keep GET /health public."}},
            {"behavior": "  "},
            {},
            "  ",
        )
        for spec in shapes:
            with self.subTest(spec=spec):
                def handler(node, model, state, instruction, spec=spec):
                    if node == "triage":
                        return {"triage": "spec"}
                    return {"spec": spec, "spec_gate": "needs_human", "human_question": "Which clock?"}

                result = walk(handler, issue_kind="parent")
                self.assertEqual(result["stop"], "invalid_output")
                self.assertEqual(result["halt_detail"], "spec did not return text")
                self.assertEqual(result["models_used"]["triage"], "opus")
                self.assertEqual(result["models_used"]["spec"], "opus")

    def test_question_is_answered_without_a_change(self):
        result = walk(ScriptHandler(triage="question"))
        self.assertEqual(result["path"], ["triage", "research", "done"])
        self.assertEqual(result["stop"], "answered")

    def test_one_change_request_loops_once(self):
        result = walk(ScriptHandler(reviews=["changes", "approve"]))
        self.assertEqual(
            result["path"],
            ["triage", "implement", "verify", "review", "implement", "verify", "review", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")

    def test_review_stays_on_sol_when_the_builder_changes_family(self):
        handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)
        notes = []

        def switch_builder(node, model, state, instruction):
            if node == "implement" and state.get("review") == "changes":
                if family(model) == "gpt":
                    raise QuotaExhausted(model)
                notes.append(state.get("review_note"))
            return handler(node, model, state, instruction)

        result = walk(switch_builder)
        reviewers = [call["model"] for call in handler.calls if call["node"] == "review"]
        self.assertEqual(reviewers, ["sol", "sol"])
        self.assertEqual(result["models_used"]["implement"], "opus")
        self.assertEqual(result["implement_models"], ["terra", "opus"])
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(notes, [""])

    def test_a_review_sentence_keeps_the_verdict_and_the_note(self):
        handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)
        seen = []

        def sentence(node, model, state, instruction):
            if node == "review":
                self.assertIn("The review value is approve or changes.", instruction)
            if node == "implement" and state.get("review") == "changes":
                seen.append(state.get("review_note"))
            raw = handler(node, model, state, instruction)
            if node == "review" and raw["review"] == "changes":
                raw["review"] = "changes. Decision 190 is dated 2026-11-25."
            return raw

        result = walk(sentence)
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(seen, ["Decision 190 is dated 2026-11-25."])
        self.assertEqual(result["review"], "approve")

    def test_a_review_that_is_not_a_verdict_stops(self):
        samples = (
            "approved",
            "approve this if the date is fixed",
            "changes are required",
            "change.",
            "The review is approve.",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                handler = ScriptHandler(head_sha=HEAD_SHA)

                def bad(node, model, state, instruction, sample=sample):
                    raw = handler(node, model, state, instruction)
                    if node == "review":
                        raw["review"] = sample
                    return raw

                result = walk(bad)
                self.assertEqual(result["stop"], "invalid_output")
                self.assertEqual(result["halt_detail"], "review must be one of approve, changes")
                self.assertNotIn("merge", result["path"])

    def test_review_cap_stops_without_arming(self):
        result = walk(ScriptHandler(reviews=[], head_sha=HEAD_SHA))
        self.assertEqual(result["path"].count("implement"), 3)
        self.assertEqual(result["path"].count("verify"), 3)
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "review_cap")

    def test_verify_cap_stops_without_review(self):
        result = walk(ScriptHandler(verify="fail", head_sha=HEAD_SHA))
        self.assertEqual(result["path"].count("verify"), 3)
        self.assertNotIn("review", result["path"])
        self.assertNotIn("merge", result["path"])
        self.assertEqual(result["stop"], "verify_cap")

    def test_merge_requires_the_latest_build_head(self):
        handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)

        def drop_second_head(node, model, state, instruction):
            output = handler(node, model, state, instruction)
            if node == "implement" and state.get("review") == "changes":
                output.pop("head_sha")
            return output

        result = walk(drop_second_head)
        self.assertIn("merge", result["path"])
        self.assertFalse(result["merge_armed"])
        self.assertIsNone(result["head_sha"])

    def test_needs_human_stops_at_triage(self):
        result = walk(ScriptHandler(triage="needs_human"))
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "needs_human")

    def test_review_without_risk_is_rejected(self):
        handler = ScriptHandler()

        def drop_risk(node, model, state, instruction):
            raw = handler(node, model, state, instruction)
            if node == "review":
                raw = {"review": raw["review"]}
            return raw

        result = walk(drop_risk)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("merge risk", result["halt_detail"])
        self.assertEqual(result["path"].count("review"), 1)
        self.assertNotIn("merge", result["path"])

    def test_non_string_risk_level_stops_as_invalid_output(self):
        handler = ScriptHandler()
        handler.risk["level"] = ["low"]
        result = walk(handler)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("risk level", result["halt_detail"])

    def test_review_quota_tries_each_configured_model(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)
        offered = []

        def exhaust_review(node, model, state, instruction):
            if node == "review":
                offered.append(model)
                raise QuotaExhausted(model)
            return handler(node, model, state, instruction)

        result = walk(exhaust_review)
        self.assertEqual(offered, ["sol", "terra", "grok"])
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("quota exhausted", result["halt_detail"])
        self.assertNotIn("merge", result["path"])

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if model == "opus":
                raise QuotaExhausted(model)
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["models_used"]["triage"], "grok")
        self.assertEqual(result["models_used"]["review"], "sol")

    def test_every_model_exhausted_halts_so_watcher_sees_it(self):
        def exhaust(node, model, state, instruction):
            raise QuotaExhausted(model, f"{model} hit 429")

        result = walk(exhaust)
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "quota_exhausted")
        self.assertEqual([item["status"] for item in result["attempts"]], ["quota", "quota", "quota"])
        self.assertEqual(result["proposal"]["kind"], "quota")
        self.assertFalse(result["proposal"]["file_issue"])
        self.assertEqual(result["proposal"]["target"], "")

    def test_attempts_are_recorded_on_clean_run(self):
        result = walk(ScriptHandler(head_sha=HEAD_SHA))
        self.assertEqual(result["stop"], "verified")
        self.assertIsNone(result["proposal"])
        self.assertEqual(
            [(item["node"], item["status"]) for item in result["attempts"]],
            [("triage", "ran"), ("implement", "ran"), ("verify", "ran"), ("review", "ran")],
        )

    def test_quota_failover_records_attempts_and_quota_proposal(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)

        def exhaust_claude(node, model, state, instruction):
            if model == "opus":
                raise QuotaExhausted(model, "429 quota")
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["stop"], "verified")
        self.assertTrue(any(a["status"] == "quota" for a in result["attempts"]))
        self.assertEqual(result["proposal"]["kind"], "quota")
        self.assertFalse(result["proposal"]["file_issue"])
        self.assertIn("No graph change.", result["proposal"]["summary"])

    def test_cli_missing_failover_and_all_missing_halt(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)

        def missing_claude(node, model, state, instruction):
            if model == "opus":
                raise CliMissing(model, "claude CLI is not installed")
            return handler(node, model, state, instruction)

        recovered = walk(missing_claude)
        self.assertEqual(recovered["stop"], "verified")
        self.assertTrue(any(a["status"] == "cli_missing" for a in recovered["attempts"]))
        self.assertEqual(recovered["proposal"]["kind"], "graph_fix")
        self.assertEqual(recovered["proposal"]["target"], "src/foreman/worker.py")
        self.assertTrue(recovered["proposal"]["file_issue"])

        def all_missing(node, model, state, instruction):
            raise CliMissing(model, f"{model} CLI is not installed")

        halted = walk(all_missing)
        self.assertEqual(halted["path"], ["triage", "done"])
        self.assertEqual(halted["stop"], "cli_missing")
        self.assertEqual([a["status"] for a in halted["attempts"]], ["cli_missing", "cli_missing", "cli_missing"])
        self.assertEqual(halted["proposal"]["kind"], "graph_fix")
        self.assertEqual(halted["proposal"]["target"], "src/foreman/worker.py")

    def test_missing_cli_during_route_resolution_records_cli_missing_and_falls_back(self):
        def resolve(route, effort):
            if route == "opus":
                raise ConfigError("claude CLI is not installed")
            return route

        result = run_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            repo="DoctorCRE/doctorcre-engine",
            request="Add a field",
            check="npm run check",
            resolve=resolve,
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["models_used"]["triage"], "grok")
        self.assertEqual(result["models_used"]["review"], "sol")
        self.assertTrue(any(a["status"] == "cli_missing" and a["model"] == "opus" for a in result["attempts"]))
        self.assertEqual(result["proposal"]["kind"], "graph_fix")
        self.assertEqual(result["proposal"]["target"], "src/foreman/worker.py")
        triage_choice = next(c for c in result["model_choices"] if c["node"] == "triage")
        self.assertEqual(triage_choice["reason"], "cli_missing fallback")

    def test_invalid_output_stops_immediately_without_failover(self):
        seen = []
        handler = ScriptHandler()

        def bad_triage(node, model, state, instruction):
            seen.append((node, model))
            if node == "triage":
                raise InvalidOutput("claude usage reported claude-haiku-5-5, expected opus")
            return handler(node, model, state, instruction)

        result = walk(bad_triage)
        self.assertEqual(seen, [("triage", "opus")])
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "invalid_output")
        self.assertEqual(
            result["attempts"],
            [{"node": "triage", "model": "opus", "status": "stopped", "detail": result["halt_detail"]}],
        )
        self.assertEqual(result["proposal"]["target"], "src/foreman/worker.py")
        self.assertTrue(result["proposal"]["file_issue"])

    def test_merge_hold_stops_before_arming_and_empty_hold_arms(self):
        gh_calls = []

        def run_gh(args):
            gh_calls.append(args)
            return 0

        graph = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=run_gh,
            read_merge_hold=lambda repo: "production switch",
        )
        held = graph.invoke({
            "repo": "DoctorCRE/doctorcre-engine",
            "request": "Add a field",
            "check": "npm run check",
            "issue_kind": "slice",
            "review_rounds": 0,
            "models_used": {},
            "model_choices": [],
            "attempts": [],
            "path": [],
        })
        self.assertEqual(gh_calls, [])
        self.assertFalse(held["merge_armed"])
        self.assertEqual(held["stop"], "merge_held")
        self.assertEqual(held["halt_detail"], "production switch")
        self.assertEqual(held["merge_args"], ["gh", "pr", "merge", "--auto", "--match-head-commit", HEAD_SHA])
        self.assertIsNone(held["proposal"])

        for empty in ("", "   ", None):
            with self.subTest(hold=empty):
                gh_calls.clear()
                unheld_graph = compile_delivery(
                    FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
                    run_gh=run_gh,
                    read_merge_hold=lambda repo, value=empty: value,
                )
                armed = unheld_graph.invoke({
                    "repo": "DoctorCRE/doctorcre-engine",
                    "request": "Add a field",
                    "check": "npm run check",
                    "issue_kind": "slice",
                    "review_rounds": 0,
                    "models_used": {},
                    "model_choices": [],
                    "attempts": [],
                    "path": [],
                })
                self.assertEqual(len(gh_calls), 1)
                self.assertTrue(armed["merge_armed"])
                self.assertEqual(armed["stop"], "verified")

    def test_stage_prompts_cover_small_parent_and_postgres_properties(self):
        triage_prompt = stage_text("triage")
        self.assertIn("even on a parent issue", triage_prompt)
        self.assertNotIn("Parent issues and changes that need multiple pull requests go to spec even when", triage_prompt)
        spec_prompt = stage_text("spec")
        self.assertIn("named PostgreSQL constraints only", spec_prompt)
        self.assertIn("A response check stays in `acceptance`", spec_prompt)

    def test_node_instruction_comes_from_the_stage_file(self):
        handler = ScriptHandler(triage="spec")
        walk(handler)
        by_node = {call["node"]: call["instruction"] for call in handler.calls}
        self.assertEqual(by_node["triage"], stage_text("triage"))
        self.assertIn("Do not edit it.", by_node["implement"])
        self.assertIn("Do not add a test that restates the code.", by_node["implement"])
        self.assertIn("Alignment", by_node["review"])
        self.assertNotIn("insufficient_evidence", by_node["implement"])
        self.assertIn("formal method", by_node["review"])
        self.assertIn("Command: npm run check", by_node["verify"])
        self.assertNotIn("gpt-6.1-sol-high", " ".join(by_node.values()))

    def test_diagram_names_the_nodes(self):
        picture = diagram()
        for name in REQUIRED_NODES + ("merge", "done", "save"):
            self.assertIn(name, picture)
        self.assertNotIn("claude-opus", picture)
        compile_delivery(FailoverDispatcher(ScriptHandler()), load_config())

    def publisher(self, sha=PUBLISHED, branch="feature", porcelain="", names="src/field.ts\0", ancestor=0):
        calls = []

        def run(args, _env):
            calls.append(list(args))
            if args[:2] == ["git", "branch"]:
                return Cmd(0, branch + "\n")
            if args[:2] == ["git", "fetch"]:
                return Cmd(0, "")
            if args[:2] == ["git", "show-ref"]:
                return Cmd(1, "")
            if args[:4] == ["git", "rev-parse", "-q", "--verify"]:
                return Cmd(1, "")
            if args[:2] == ["git", "merge-base"]:
                return Cmd(ancestor, "")
            if args[:2] == ["git", "status"]:
                return Cmd(0, porcelain)
            if args[:2] == ["git", "rev-parse"]:
                return Cmd(0, sha + "\n")
            if args[:2] == ["git", "diff"] and "--name-only" in args:
                return Cmd(0, names)
            if args[:2] == ["git", "push"]:
                return Cmd(0, "")
            if args[:3] == ["gh", "pr", "list"]:
                return Cmd(0, "[]")
            if args[:3] == ["gh", "pr", "create"]:
                return Cmd(0, "https://example.test/pull/9\n")
            return Cmd(1, "", "unexpected %s" % args)

        return calls, run

    def test_approval_publishes_the_reviewed_sha_then_arms_it(self):
        calls, run = self.publisher()
        config = config_from_pointer(load_pointer(ROOT / "examples" / "doctorcre-engine.yaml"))
        self.assertTrue(config.queue)
        result = run_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=PUBLISHED)),
            repo="DoctorCRE/doctorcre-engine",
            request="Add a field",
            check="npm run check",
            save_config=config,
            save_run=run,
        )
        self.assertEqual(
            result["path"],
            ["triage", "implement", "verify", "review", "save", "merge", "done"],
        )
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["head_sha"], PUBLISHED)
        self.assertTrue(result["head_checked"])
        self.assertEqual(
            result["merge_args"],
            ["gh", "pr", "merge", "--auto", "--match-head-commit", PUBLISHED],
        )
        self.assertTrue(result["merge_armed"])
        self.assertIn("https://example.test/pull/9", result["save_report"])
        created = next(args for args in calls if args[:3] == ["gh", "pr", "create"])
        self.assertEqual(created[created.index("--title") + 1], "Add a field")
        self.assertIn("DoctorCRE/doctorcre-engine", created)
        self.assertTrue(any(args[:2] == ["git", "push"] for args in calls))
        self.assertFalse(any(args[:2] == ["git", "commit"] for args in calls))
        self.assertFalse(any(args[:2] == ["git", "merge"] for args in calls))
        self.assertFalse(any(args[:3] == ["gh", "pr", "merge"] for args in calls))
        self.assertFalse(any(arg == "token" for args in calls for arg in args))
        self.assertTrue(config.queue)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            _print_run(result)
        self.assertIn("save\n1/2 pushed feature", stdout.getvalue())
        self.assertIn("merge armed", stdout.getvalue())

    def test_publish_uses_the_slice_title_and_issue(self):
        calls, run = self.publisher()
        config = config_from_pointer(load_pointer(ROOT / "examples" / "doctorcre-engine.yaml"))
        graph = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=PUBLISHED)),
            save_config=config,
            save_run=run,
        )
        result = graph.invoke({
            "repo": "DoctorCRE/doctorcre-engine",
            "request": "Add a field\n\nDetails.",
            "check": "npm run check",
            "issue_kind": "slice",
            "issue": 42,
            "slice": {
                "title": "Name the release",
                "acceptance": "The health check names the release.",
                "depends_on": [],
                "screen": "",
                "properties": [],
            },
            "review_rounds": 0,
            "models_used": {},
            "model_choices": [],
            "path": [],
        })
        created = next(args for args in calls if args[:3] == ["gh", "pr", "create"])
        self.assertEqual(created[created.index("--title") + 1], "Name the release")
        self.assertIn("Board: #42", created[created.index("--body") + 1])
        self.assertNotIn("Proof:", created[created.index("--body") + 1])
        self.assertEqual(result["head_sha"], PUBLISHED)

    def test_a_different_head_stays_unpublished(self):
        calls, run = self.publisher(sha="c" * 40)
        config = config_from_pointer(load_pointer(ROOT / "examples" / "doctorcre-engine.yaml"))
        result = run_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=PUBLISHED)),
            repo="DoctorCRE/doctorcre-engine",
            request="Add a field",
            check="npm run check",
            save_config=config,
            save_run=run,
        )
        self.assertEqual(result["stop"], "save_failed")
        self.assertIn("reviewed commit", result["halt_detail"])
        self.assertNotIn("merge", result["path"])
        self.assertFalse(any(args[:2] == ["git", "push"] for args in calls))

    def test_save_failure_stops_before_merge(self):
        calls, run = self.publisher(branch="main")
        config = config_from_pointer(load_pointer(ROOT / "examples" / "doctorcre-engine.yaml"))
        result = run_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=PUBLISHED)),
            repo="DoctorCRE/doctorcre-engine",
            request="Add a field",
            check="npm run check",
            save_config=config,
            save_run=run,
        )
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "save", "done"])
        self.assertEqual(result["stop"], "save_failed")
        self.assertIn("never from main", result["halt_detail"])
        self.assertFalse(any(args[:2] == ["git", "push"] for args in calls))

    def test_review_changes_do_not_publish(self):
        calls, run = self.publisher()
        config = config_from_pointer(load_pointer(ROOT / "examples" / "doctorcre-engine.yaml"))
        result = run_delivery(
            FailoverDispatcher(ScriptHandler(reviews=["changes"], head_sha=PUBLISHED)),
            repo="DoctorCRE/doctorcre-engine",
            request="Add a field",
            check="npm run check",
            save_config=config,
            save_run=run,
        )
        self.assertNotIn("save", result["path"])
        self.assertNotIn("merge", result["path"])
        self.assertEqual(calls, [])
        self.assertEqual(result["stop"], "review_cap")


if __name__ == "__main__":
    unittest.main()
