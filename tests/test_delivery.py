import unittest

from dataclasses import replace

from foreman.config import NodeConfig, load_config
from foreman.delivery import (
    REQUIRED_NODES,
    compile_delivery,
    diagram,
    render_pr_body,
    review_comment,
    run_delivery,
    stage_text,
)
from foreman.dispatch import CliMissing, FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.dry_run import DEFAULT_RISK, ScriptHandler


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
        self.assertEqual(result["models_used"]["implement"], "gpt-6.2-sol-high")
        self.assertEqual(result["models_used"]["review"], "gpt-6.2-sol-high")
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
        self.assertEqual(result["models_used"]["spec"], "claude-opus-5-5-high")
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
        self.assertEqual(result["models_used"]["implement"], "gpt-6.2-sol-high")

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
                self.assertEqual(result["models_used"]["implement"], "gpt-6.2-sol-high")
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
                self.assertEqual(result["models_used"]["triage"], "claude-opus-5-5-high")
                self.assertEqual(result["models_used"]["spec"], "claude-opus-5-5-high")

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
        self.assertEqual(
            [(item["verdict"], item["level"], item["model"]) for item in result["review_log"]],
            [("changes", "low", "gpt-6.2-sol-high"), ("approve", "low", "gpt-6.2-sol-high")],
        )

    def test_review_stays_on_sol_when_the_builder_changes_family(self):
        handler = ScriptHandler(reviews=["changes", "approve"], head_sha=HEAD_SHA)
        notes = []

        def switch_builder(node, model, state, instruction):
            if node == "implement" and state.get("review") == "changes":
                if model.startswith("gpt-"):
                    raise QuotaExhausted(model)
                notes.append(state.get("review_note"))
            return handler(node, model, state, instruction)

        result = walk(switch_builder)
        reviewers = [call["model"] for call in handler.calls if call["node"] == "review"]
        self.assertEqual(reviewers, ["gpt-6.2-sol-high", "gpt-6.2-sol-high"])
        self.assertEqual(result["models_used"]["implement"], "claude-opus-5-5-high")
        self.assertEqual(result["implement_models"], ["gpt-6.2-sol-high", "claude-opus-5-5-high"])
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
        self.assertEqual(
            offered,
            ["gpt-6.2-sol-high", "gpt-6.1-terra-high", "grok-4.7-high"],
        )
        self.assertEqual(result["stop"], "no_reviewer")
        self.assertIn("quota exhausted", result["halt_detail"])
        self.assertNotIn("merge", result["path"])

    def test_quota_uses_the_next_model(self):
        handler = ScriptHandler()

        def exhaust_claude(node, model, state, instruction):
            if model == "claude-opus-5-5-high":
                raise QuotaExhausted(model)
            return handler(node, model, state, instruction)

        result = walk(exhaust_claude)
        self.assertEqual(result["models_used"]["triage"], "grok-4.7-high")
        self.assertEqual(result["models_used"]["review"], "gpt-6.2-sol-high")

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
            if model == "claude-opus-5-5-high":
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
            if model == "claude-opus-5-5-high":
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

    def test_invalid_output_stops_immediately_without_failover(self):
        seen = []
        handler = ScriptHandler()

        def bad_triage(node, model, state, instruction):
            seen.append((node, model))
            if node == "triage":
                raise InvalidOutput("claude usage reported claude-haiku-5-5, expected claude-opus-5-5")
            return handler(node, model, state, instruction)

        result = walk(bad_triage)
        self.assertEqual(seen, [("triage", "claude-opus-5-5-high")])
        self.assertEqual(result["path"], ["triage", "done"])
        self.assertEqual(result["stop"], "invalid_output")
        self.assertEqual(
            result["attempts"],
            [{"node": "triage", "model": "claude-opus-5-5-high", "status": "stopped", "detail": result["halt_detail"]}],
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
                    "attempts": [],
                    "path": [],
                })
                self.assertEqual(len(gh_calls), 1)
                self.assertTrue(armed["merge_armed"])
                self.assertEqual(armed["stop"], "verified")

    def test_publish_precedes_merge_and_a_miss_stays_unpublished(self):
        payload = {
            "repo": "DoctorCRE/doctorcre-engine",
            "request": "Add a field",
            "check": "npm run check",
            "issue_kind": "slice",
            "review_rounds": 0,
            "models_used": {},
            "attempts": [],
            "path": [],
        }
        order = []

        def publish(sha):
            order.append(("publish", sha))
            return "https://github.com/DoctorCRE/doctorcre-engine/pull/875"

        def record(state, url):
            order.append(("record", url))
            self.assertEqual(state["review"], "approve")
            self.assertTrue(state["models_used"]["review"])

        def run_gh(args):
            order.append(("merge", tuple(args)))
            return 0

        armed = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=run_gh,
            publish_worktree=publish,
            record_review=record,
            read_merge_hold=lambda repo: None,
        ).invoke(payload)
        self.assertEqual(order, [
            ("publish", HEAD_SHA),
            ("record", "https://github.com/DoctorCRE/doctorcre-engine/pull/875"),
            ("merge", ("gh", "pr", "merge", "--auto", "--match-head-commit", HEAD_SHA)),
        ])
        self.assertEqual(armed["pull_request"], "https://github.com/DoctorCRE/doctorcre-engine/pull/875")
        self.assertTrue(armed["merge_armed"])
        self.assertEqual(armed["stop"], "verified")
        self.assertIsNone(armed["proposal"])

        def rejected(sha):
            raise InvalidOutput("could not push 875-health-release: rejected")

        missed = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
            publish_worktree=rejected,
            read_merge_hold=lambda repo: None,
        ).invoke(payload)
        self.assertEqual(missed["stop"], "unpublished")
        self.assertFalse(missed["merge_armed"])
        self.assertEqual(missed["halt_detail"], "could not push 875-health-release: rejected")
        self.assertEqual(missed["proposal"]["target"], "src/foreman/runner.py")
        self.assertTrue(missed["proposal"]["file_issue"])

        blank = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
            publish_worktree=lambda sha: "  ",
            read_merge_hold=lambda repo: None,
        ).invoke(payload)
        self.assertEqual(blank["stop"], "unpublished")
        self.assertEqual(blank["halt_detail"], "could not open the pull request")

        held = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
            publish_worktree=lambda sha: self.fail("published during a hold"),
            read_merge_hold=lambda repo: "production switch",
        ).invoke(payload)
        self.assertEqual(held["stop"], "merge_held")
        self.assertFalse(held["merge_armed"])
        self.assertIsNone(held["proposal"])

        failed_merge = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=lambda args: 1,
            publish_worktree=lambda sha: "https://github.com/DoctorCRE/doctorcre-engine/pull/875",
            read_merge_hold=lambda repo: None,
        ).invoke(payload)
        self.assertEqual(failed_merge["stop"], "unpublished")
        self.assertFalse(failed_merge["merge_armed"])
        self.assertEqual(failed_merge["pull_request"], "https://github.com/DoctorCRE/doctorcre-engine/pull/875")
        self.assertEqual(failed_merge["halt_detail"], "merge was not armed for the open pull request")

        def denied(state, url):
            raise InvalidOutput("could not record the review: denied")

        unrecorded = compile_delivery(
            FailoverDispatcher(ScriptHandler(head_sha=HEAD_SHA)),
            run_gh=lambda args: self.fail(f"unexpected merge command {args}"),
            publish_worktree=lambda sha: "https://github.com/DoctorCRE/doctorcre-engine/pull/875",
            record_review=denied,
            read_merge_hold=lambda repo: None,
        ).invoke(payload)
        self.assertEqual(unrecorded["stop"], "review_unrecorded")
        self.assertFalse(unrecorded["merge_armed"])
        self.assertEqual(unrecorded["pull_request"], "https://github.com/DoctorCRE/doctorcre-engine/pull/875")
        self.assertEqual(unrecorded["halt_detail"], "could not record the review: denied")
        self.assertEqual(unrecorded["proposal"]["target"], "src/foreman/runner.py")

    def test_review_comment_names_the_reviewer_and_leaves_the_request_out(self):
        text = review_comment({
            "request": "PRIVATE_REQUEST_TEXT_NEVER_LEAK",
            "workspace": "/private/workspace/path",
            "review": "approve",
            "review_note": "The date on decision 190 is later than this slice.",
            "models_used": {"implement": "gpt-6.2-sol-high", "review": "gpt-6.2-sol-high"},
            "risk": DEFAULT_RISK,
        })
        self.assertEqual(text.splitlines()[0], "Reviewed by gpt-6.2-sol-high.")
        self.assertIn("Verdict: approve.", text)
        self.assertIn("Risk: low.", text)
        self.assertIn("Alignment: The change matches the request.", text)
        self.assertIn("The date on decision 190 is later than this slice.", text)
        self.assertNotIn("PRIVATE_REQUEST_TEXT_NEVER_LEAK", text)
        self.assertNotIn("/private/workspace/path", text)
        with self.assertRaises(InvalidOutput):
            review_comment({"review": "approve", "risk": DEFAULT_RISK, "models_used": {}})

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
        for name in REQUIRED_NODES + ("merge", "done"):
            self.assertIn(name, picture)
        self.assertNotIn("claude-opus", picture)
        compile_delivery(FailoverDispatcher(ScriptHandler()), load_config())

    def test_verify_and_review_round_caps_are_independent_876_regression(self):
        verify_calls = 0
        review_calls = 0
        implement_calls = 0

        def handler(node, model, state, instruction):
            nonlocal review_calls, implement_calls
            if node == "triage":
                return {"triage": "implement"}
            if node == "implement":
                implement_calls += 1
                return {"implementation": f"impl-{implement_calls}", "head_sha": HEAD_SHA}
            if node == "review":
                review_calls += 1
                if review_calls == 1:
                    return {"review": "changes", "review_note": "Fix edge case", "risk": DEFAULT_RISK}
                return {"review": "approve", "risk": DEFAULT_RISK}
            return {}

        def run_check(cmd):
            nonlocal verify_calls
            verify_calls += 1
            if verify_calls <= 2:
                return 1, f"FAIL secret=tok-secret-example-value\nerror #{verify_calls}"
            return 0, "ok"

        with unittest.mock.patch.dict("os.environ", {"TEST_API_TOKEN": "tok-secret-example-value"}):
            result = compile_delivery(
                FailoverDispatcher(handler),
                load_config(),
                run_check=run_check,
            ).invoke({
                "repo": "DoctorCRE/doctorcre-engine",
                "issue": 876,
                "request": "Fix board",
                "check": "npm run check",
                "issue_kind": "slice",
                "verify_rounds": 0,
                "review_rounds": 0,
                "models_used": {},
                "sessions": {},
                "usage_records": [],
                "attempts": [],
                "path": [],
            })
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(implement_calls, 4)
        self.assertEqual(verify_calls, 4)
        self.assertEqual(review_calls, 2)
        self.assertEqual(result["verify_rounds"], 2)
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(result["check_output"], "")

    def test_verify_env_unavailable_and_preflight_halt_before_burning_rounds(self):
        preflight_calls = 0
        implement_calls = 0

        def handler(node, model, state, instruction):
            nonlocal implement_calls
            if node == "triage":
                return {"triage": "implement"}
            implement_calls += 1
            return {"implementation": "ok", "head_sha": HEAD_SHA}

        halted = compile_delivery(
            FailoverDispatcher(handler),
            load_config(),
            run_check=lambda cmd: (1, "No PostgreSQL server answered at localhost:5432"),
        ).invoke({
            "repo": "DoctorCRE/doctorcre-engine",
            "issue": 876,
            "request": "Run check",
            "check": "npm run check",
            "issue_kind": "slice",
            "verify_rounds": 0,
            "review_rounds": 0,
            "models_used": {},
            "sessions": {},
            "usage_records": [],
            "attempts": [],
            "path": [],
        })
        self.assertEqual(halted["stop"], "env_unavailable")
        self.assertEqual(halted["failure_phase"], "verify")
        self.assertEqual(halted["failure_reason"], "env_unavailable")
        self.assertEqual(implement_calls, 1)
        self.assertIn("No PostgreSQL server answered", halted["check_output"])

        def fail_preflight():
            nonlocal preflight_calls
            preflight_calls += 1
            return False, "db down for owner@example.invalid"

        implement_calls = 0
        pre = compile_delivery(
            FailoverDispatcher(handler),
            load_config(),
            preflight=fail_preflight,
        ).invoke({
            "repo": "DoctorCRE/doctorcre-engine",
            "issue": 876,
            "request": "Run check",
            "check": "npm run check",
            "issue_kind": "slice",
            "verify_rounds": 0,
            "review_rounds": 0,
            "models_used": {},
            "sessions": {},
            "usage_records": [],
            "attempts": [],
            "path": [],
        })
        self.assertEqual(preflight_calls, 1)
        self.assertEqual(implement_calls, 0)
        self.assertEqual(pre["stop"], "env_unavailable")
        self.assertEqual(pre["failure_phase"], "preflight")
        self.assertNotIn("owner@example.invalid", pre["halt_detail"])

    def test_render_pr_body_includes_verification_risk_and_redacts_pii(self):
        body = render_pr_body(
            {
                "pr_summary": "Updated query for owner@example.invalid",
                "check": "npm run check",
                "check_exit_code": 0,
                "head_sha": HEAD_SHA,
                "proof_labels": {"formal": "verified"},
                "risk": {**DEFAULT_RISK, "level": "medium"},
            },
            875,
        )
        self.assertIn("Closes #875", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Verification", body)
        self.assertIn("## Merge Risk", body)
        self.assertIn("**Level:** `medium`", body)
        self.assertNotIn("owner@example.invalid", body)


if __name__ == "__main__":
    unittest.main()
