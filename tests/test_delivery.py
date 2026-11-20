import contextlib
import io
from pathlib import Path
import unittest

from dataclasses import replace

from foreman.cli import _print_run
from foreman.config import family, NodeConfig, load_config
from foreman.delivery import REQUIRED_NODES, compile_delivery, diagram, run_delivery, stage_text
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
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


def walk(handler, check="npm run check", issue_kind="slice", config=None):
    return run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check=check,
        issue_kind=issue_kind,
        config=config,
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
        self.assertEqual(result["models_used"]["review"], "opus")
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

    def test_a_parent_routes_to_spec_even_when_triage_says_implement(self):
        for triage in ("implement", "spec"):
            with self.subTest(triage=triage):
                result = walk(ScriptHandler(triage=triage, slices=SLICES), issue_kind="parent")
                self.assertEqual(result["path"], ["triage", "spec", "done"])
                self.assertEqual(result["stop"], "sliced")
                self.assertEqual(result["slices"], SLICES)

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
                result = walk(ScriptHandler(slices=slices), issue_kind="parent")
                self.assertEqual(result["path"], ["triage", "spec", "done"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"])

    def test_review_stops_when_no_other_family_remains(self):
        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], models=("gpt-6.1-terra-high",))
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

    def test_review_excludes_every_builder_family(self):
        for review_models in (("claude-opus-5-5-high", "gpt-6.1-sol-high", "grok-4.7-high"),
                              ("claude-opus-5-5-high", "gpt-6.1-sol-high")):
            with self.subTest(review_models=review_models):
                config = load_config()
                config.nodes["review"] = replace(config.nodes["review"], models=review_models)
                handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)

                def switch_builder(node, model, state, instruction):
                    if node == "implement" and state.get("review") == "changes" and family(model) == "gpt":
                        raise QuotaExhausted(model)
                    return handler(node, model, state, instruction)

                result = walk(switch_builder, config=config)
                reviewers = [call["model"] for call in handler.calls if call["node"] == "review"]
                self.assertEqual(result["models_used"]["implement"], "opus")
                if "grok-4.7-high" in review_models:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high", "grok-4.7-high"])
                    self.assertEqual(result["stop"], "verified")
                else:
                    self.assertEqual(reviewers, ["claude-opus-5-5-high"])
                    self.assertEqual(result["stop"], "no_reviewer")
                    self.assertNotIn("merge", result["path"])
                self.assertEqual(result["implement_models"], ["terra", "opus"])

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

    def test_review_quota_never_falls_back_to_the_builder_family(self):
        handler = ScriptHandler(head_sha=HEAD_SHA)
        offered = []

        def exhaust_review(node, model, state, instruction):
            if node == "review":
                offered.append(model)
                raise QuotaExhausted(model)
            return handler(node, model, state, instruction)

        config = load_config()
        config.nodes["review"] = replace(config.nodes["review"], other_family_from=None)
        result = walk(exhaust_review, config=config)
        self.assertEqual(offered, ["opus", "grok"])
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
        self.assertEqual(result["models_used"]["review"], "grok")
        self.assertNotEqual(
            family(result["models_used"]["implement"]),
            family(result["models_used"]["review"]),
        )

    def test_every_model_exhausted_raises(self):
        def exhaust(node, model, state, instruction):
            raise QuotaExhausted(model)

        with self.assertRaises(QuotaExhausted):
            walk(exhaust)

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
