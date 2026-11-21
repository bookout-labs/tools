import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import main
from foreman.config import ConfigError, ModelSpec, NodeConfig, load_config
from foreman.delivery import LiveRun, run_delivery
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.dry_run import DEFAULT_RISK
from foreman.github import ForgeError, MergeNotArmed, PullRequest
from foreman.runner import run_live_delivery
from foreman.stage_output import STAGE_OUTPUTS, ImplementOutput, ReviewOutput, Slice, parse_output
from foreman.worker import CliWorker, run_worker_command
from foreman.workspace import LiveWorkspace, ReviewDiff, WorktreeInspectionFailed, WorktreeState
from test_adapters import fixture_process


HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "8-list-items"
CLEAN_WORKTREE = WorktreeState(HEAD, True, BRANCH)
SLICE = {
    "title": "List items once",
    "acceptance": "The view lists every item once.",
    "depends_on": [],
    "screen": "docs/screens/items.md",
    "properties": [{"name": "Unique", "constraint": "unique_item_id"}],
}
EMPTY_SLICE = {**SLICE, "screen": "", "properties": []}
BROWSER = {"screen": SLICE["screen"], "trace": "traces/items.zip", "passed": True}
FORMAL = {"ran": True, "constraints": [{"constraint": "unique_item_id", "rejected": True}]}
APPROVE = {"review": "approve", "risk": DEFAULT_RISK, "findings": [], "detail": "The changes meet the request."}
FINDING = {"file": "items.txt", "line": 2, "severity": "blocking", "summary": "Add a check."}
DIFF = Path(__file__).with_name("fixtures").joinpath("review.diff").read_text()


def slice_body(item):
    return "Issue body\n\n```json\n" + json.dumps(item) + "\n```"


class FakeWorkspace(LiveWorkspace):
    def __init__(self, cwd, snapshots=(CLEAN_WORKTREE,)):
        super().__init__(cwd)
        self.snapshots = iter(snapshots)
        self.last = CLEAN_WORKTREE

    def inspect(self):
        self.last = next(self.snapshots, self.last)
        if isinstance(self.last, WorktreeInspectionFailed):
            raise self.last
        return self.last

    def review_diff(self, base, head):
        return ReviewDiff(f"git diff {base}...{head}", DIFF)


def worker_reply(args, reply):
    recorded = fixture_process(args, "success", 0)
    if args[0] == "codex":
        stdout = json.dumps(reply) if isinstance(reply, dict) else reply
        return subprocess.CompletedProcess(args, recorded.returncode, stdout, recorded.stderr)
    if "triage" in reply:
        reply = {"slice": None, "human_question": None, **reply}
    if "spec" in reply:
        reply = {"slices": None, "human_question": None, **reply}
    envelope = json.loads(recorded.stdout)
    field = "structured_output" if args[0] == "claude" else "structuredOutput"
    envelope[field] = reply
    return subprocess.CompletedProcess(args, recorded.returncode, json.dumps(envelope), recorded.stderr)

class FakeGitHub:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else ["slice"]
        self.body = slice_body(EMPTY_SLICE) if "slice" in self.labels else "Issue body"
        self.created = []
        self.merged = set()
        self.label_calls = []
        self.issue_labels = {}
        self.merge_calls = []
        self.reviews = []

    def review_target(self, repo, issue, branch, sha):
        return PullRequest.model_validate({
            "number": 12, "state": "OPEN", "headRefOid": sha, "headRefName": branch,
            "baseRefName": "main", "baseRefOid": BASE, "autoMergeRequest": None,
        })

    def post_review(self, repo, target, body, comments):
        self.reviews.append((repo, target.number, target.headRefOid, body, comments))

    def arm_auto_merge(self, repo, issue, branch, sha):
        self.merge_calls.append((repo, issue, branch, sha))

    def load_issue(self, repo, number):
        if number in self.issue_labels:
            title, body, _ = self.created[number - 101]
            return {"title": title, "body": body, "labels": self.issue_labels[number]}
        return {"title": "Issue title", "body": self.body, "labels": self.labels}

    def create_issue(self, repo, title, body, labels):
        self.created.append((title, body, labels))
        number = 100 + len(self.created)
        self.issue_labels[number] = list(labels)
        return number

    def is_merged(self, repo, number):
        return number in self.merged

    def add_label(self, repo, number, label):
        self.label_calls.append((repo, number, label))
        self.issue_labels[number].append(label)


class CliWorkerTest(unittest.TestCase):
    def test_commands_use_the_configured_models_and_parse_the_final_result(self):
        models = {
            "gpt-6.1-sol-high": ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high"],
            "claude-opus-5-5-high": ["claude", "--model", "claude-opus-5-5", "--effort", "high"],
            "grok-4.7-high": ["grok", "-m", "grok-4.7", "--reasoning-effort", "high"],
        }
        for model, prefix in models.items():
            with self.subTest(model=model):
                def run(args, cwd, env):
                    self.assertEqual(args[:len(prefix)], prefix)
                    self.assertEqual(cwd, Path("engine"))
                    self.assertIn('{"issue": 12}', args[-1])
                    expected_env = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"} if args[0] == "claude" else {}
                    self.assertEqual(env, expected_env)
                    if args[0] == "codex":
                        self.assertIn("Output schema:", args[-1])
                    else:
                        self.assertNotIn("Output schema:", args[-1])
                        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]),
                                         STAGE_OUTPUTS["triage"].model_json_schema())
                    return worker_reply(args, {"triage": "implement"})

                result = CliWorker(Path("engine"), run)(
                    "triage", ModelSpec.parse(model), {"issue": 12}, "Read the issue.",
                )
                self.assertEqual(result.kind, "ran")
                self.assertEqual(result.output["triage"], "implement")

    def test_subprocess_merges_adapter_environment_with_inherited_environment(self):
        inherited = {"PATH": "/bin", "CLAUDE_CODE_NO_MODEL_FALLBACK": "0"}
        adapter_env = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"}
        with patch.dict("os.environ", inherited, clear=True), patch("foreman.worker.subprocess.run") as run:
            run_worker_command(["claude"], Path("engine"), adapter_env)
        run.assert_called_once_with(
            ["claude"], cwd=Path("engine"), env={**inherited, **adapter_env},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )

    def test_worker_without_a_model_raises_a_config_error(self):
        worker = CliWorker(Path("."), lambda *_: self.fail("CLI must not run"))
        with self.assertRaisesRegex(ConfigError, "triage has no model configured"):
            worker("triage", None, {}, "")

    def test_non_object_replies_are_invalid(self):
        replies = ('Here is the result: {"triage": "implement"}', '["implement"]')
        for reply in replies:
            with self.subTest(reply=reply):
                worker = CliWorker(Path("."), lambda args, cwd, env: worker_reply(args, reply))
                outcome = worker("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "")
                self.assertEqual(outcome.kind, "failed")
                self.assertIn("did not return a JSON object", outcome.detail)

    def walk_triage(self, models, run):
        config = load_config()
        config.nodes["triage"] = NodeConfig("coordinator", tuple(ModelSpec.parse(model) for model in models))
        return run_delivery(FailoverDispatcher(CliWorker(Path("."), run)), repo="Example/Engine",
                            request="List items", check="exit 0", config=config, slice=EMPTY_SLICE)

    def test_delivery_falls_through_only_for_quota_or_missing_cli(self):
        for cli, model in (("claude", "claude-opus-5-5-high"), ("grok", "grok-4.7-high")):
            cases = [("quota", "needs_human"), ("cli_missing", "needs_human"),
                     ("is_error", "invalid_output"), ("missing_model_usage", "invalid_output")]
            if cli == "claude":
                cases.append(("wrong_model", "invalid_output"))
            for case, stop in cases:
                with self.subTest(cli=cli, case=case):
                    calls = []

                    def run(args, cwd, env):
                        calls.append(args[0])
                        if args[0] == "codex":
                            return worker_reply(args, {"triage": "needs_human", "human_question": "Which items?"})
                        if case == "cli_missing":
                            raise FileNotFoundError(args[0])
                        exit_code = 1 if case in {"quota", "is_error"} else 0
                        return fixture_process(args, case, exit_code)

                    result = self.walk_triage((model, "gpt-6.1-sol-high"), run)
                    self.assertEqual(result["stop"], stop)
                    self.assertEqual(calls, [cli, "codex"] if stop == "needs_human" else [cli])
                    if case == "is_error":
                        message = "turn_handoff:" if cli == "claude" else "unknown model id"
                        self.assertIn(message, result["halt_detail"])

    def test_delivery_stops_for_an_incomplete_native_stage_reply(self):
        def run(args, cwd, env):
            self.assertEqual(args[0], "claude")
            recorded = fixture_process(args, "success", 0)
            envelope = json.loads(recorded.stdout)
            envelope["structured_output"] = {"triage": "question"}
            return subprocess.CompletedProcess(args, 0, json.dumps(envelope), recorded.stderr)

        result = self.walk_triage(("claude-opus-5-5-high", "gpt-6.1-sol-high"), run)
        self.assertEqual(result["stop"], "invalid_output")
        self.assertIn("Field required", result["halt_detail"])

    def test_delivery_stops_when_every_cli_is_missing(self):
        calls = []

        def run(args, cwd, env):
            calls.append(args[0])
            raise FileNotFoundError(args[0])

        result = self.walk_triage(("claude-test", "grok-test", "gpt-test"), run)
        self.assertEqual(calls, ["claude", "grok", "codex"])
        self.assertEqual(result["stop"], "cli_missing")
        self.assertIn("codex executable is unavailable", result["halt_detail"])

    def test_codex_reroute_is_rejected_even_with_the_requested_banner(self):
        def run(args, cwd, env):
            return subprocess.CompletedProcess(
                args, 0, '{"triage":"implement"}', "model: gpt-first\nmodel rerouted: gpt-first -> gpt-other\n"
            )

        outcome = CliWorker(Path("."), run)("triage", ModelSpec.parse("gpt-first"), {}, "")
        self.assertEqual(outcome.kind, "quota")


class StageOutputTest(unittest.TestCase):
    def test_schema_snapshot(self):
        snapshot = Path(__file__).with_name("stage-schemas.json").read_text()
        schemas = {name: model.model_json_schema() for name, model in STAGE_OUTPUTS.items()}
        self.assertEqual(schemas, json.loads(snapshot))

    def test_every_object_in_the_schema_is_strict(self):
        def check(schema):
            if schema.get("type") == "object":
                self.assertCountEqual(schema["required"], schema["properties"])
                self.assertIs(schema["additionalProperties"], False)
            return schema

        for model in STAGE_OUTPUTS.values():
            schema = model.model_json_schema()
            self.assertEqual(schema["type"], "object")
            self.assertNotIn("anyOf", schema)
            self.assertNotIn("oneOf", schema)
            json.loads(json.dumps(schema), object_hook=check)

    def test_extra_field_reports_its_nested_path(self):
        raw = {
            "implementation": "Done", "head_sha": None, "formal_proof": None,
            "browser_proof": {"screen": None, "trace": None, "passed": None, "extra": True},
        }
        with self.assertRaisesRegex(InvalidOutput, r"^browser_proof\.extra:"):
            parse_output(ImplementOutput, raw)

    def test_screen_rejects_control_characters_and_subdirectories(self):
        for screen in ("docs/screens/na\x01me.md", "docs/screens/a/b.md"):
            with self.subTest(screen=screen), self.assertRaisesRegex(InvalidOutput, r"^screen:"):
                parse_output(Slice, {**SLICE, "screen": screen})

    def test_formal_rejection_requires_a_boolean(self):
        for fields in ({}, {"rejected": "true"}, {"rejected": 1}):
            with self.subTest(fields=fields):
                raw = {
                    "implementation": "Done",
                    "head_sha": None, "browser_proof": None,
                    "formal_proof": {"ran": None, "typecheck": None,
                                     "constraints": [{"constraint": "unique_item_id", **fields}]},
                }
                with self.assertRaisesRegex(InvalidOutput, r"^formal_proof\.constraints\.0\.rejected:"):
                    parse_output(ImplementOutput, raw)

    def test_head_sha_requires_a_full_hex_commit_or_null(self):
        implementation = {"implementation": "Done", "browser_proof": None, "formal_proof": None}
        for sha in ("", "a" * 39, "z" * 40, " " + HEAD, HEAD + "\n"):
            with self.subTest(sha=sha), self.assertRaisesRegex(InvalidOutput, r"^head_sha:"):
                parse_output(ImplementOutput, {**implementation, "head_sha": sha})
        for sha in (None, HEAD, HEAD.upper()):
            with self.subTest(sha=sha):
                self.assertEqual(parse_output(ImplementOutput, {**implementation, "head_sha": sha}).head_sha, sha)

    def test_hedged_approval_is_invalid(self):
        with self.assertRaisesRegex(InvalidOutput, "^review:"):
            parse_output(ReviewOutput, {**APPROVE, "review": "approve, but please fix X first"})

    def test_review_requires_an_exact_verdict(self):
        for verdict in ("Approve.", "approve.", "changes.", "changes. Add a check."):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(InvalidOutput, "^review:"):
                parse_output(ReviewOutput, {**APPROVE, "review": verdict})

    def test_review_summary_is_limited_to_200_characters(self):
        self.assertEqual(ReviewOutput.model_json_schema()["$defs"]["Finding"]["properties"]["summary"]["maxLength"], 200)
        raw = {**APPROVE, "review": "changes", "findings": [{**FINDING, "summary": "a" * 201}]}
        with self.assertRaisesRegex(InvalidOutput, r"^findings\.0\.summary:"):
            parse_output(ReviewOutput, raw)

    def test_review_verdict_requires_matching_findings(self):
        for raw in (
            {**APPROVE, "review": "changes"},
            {**APPROVE, "findings": [{**FINDING, "severity": "nit"}]},
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(InvalidOutput, "changes requires at least one finding"):
                parse_output(ReviewOutput, raw)


class LiveRunnerTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.cwd = Path(directory)
        self.checkpoint = self.cwd / "runs.sqlite3"
        self.calls = []
        self.prompts = []
        self.github = FakeGitHub()

    def walk(self, replies, *, issue=8, check="exit 0", snapshots=(CLEAN_WORKTREE,)):
        outputs = iter(replies)

        def run(args, cwd, env):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            self.prompts.append(args[-1])
            return worker_reply(args, next(outputs))

        return run_live_delivery(
            repo="Example/Engine", issue=issue, check=check,
            checkpoint=self.checkpoint, dispatcher=FailoverDispatcher(CliWorker(self.cwd, run)),
            run=LiveRun(self.github, FakeWorkspace(self.cwd, snapshots)),
        )

    def test_findings_and_change_kind_reach_the_next_implement_prompt(self):
        built = {"implementation": "Built."}
        changes = {**APPROVE, "review": "changes", "detail": "Check the bounds."}
        for severity, kind in (("nit", "nits"), ("blocking", "blocking")):
            with self.subTest(severity=severity):
                self.checkpoint = self.cwd / f"{severity}.sqlite3"
                finding = {**FINDING, "severity": severity}
                self.walk([{"triage": "implement"}, built, {**changes, "findings": [finding]}, built, APPROVE])
                state = json.loads(self.prompts[-2].split("Delivery state:\n")[1])
                self.assertEqual(state["findings"], [finding])
                self.assertEqual(state["detail"], changes["detail"])
                self.assertEqual(state["review_change_kind"], kind)

    def test_review_posts_only_changed_lines_inline(self):
        findings = [
            FINDING,
            {**FINDING, "line": 1, "summary": "Untouched context."},
            {**FINDING, "file": "deleted.txt", "line": 1, "summary": "Deleted file."},
        ]
        built = {"implementation": "Built."}
        changes = {**APPROVE, "review": "changes", "findings": findings, "detail": "Check the bounds."}
        self.walk([{"triage": "implement"}, built, changes, built, APPROVE])
        self.assertIn(f"git diff {BASE}...{HEAD}", self.prompts[2])
        self.assertEqual(self.github.reviews, [(
            "Example/Engine", 12, HEAD,
            "Check the bounds.\n\nitems.txt:1 [blocking] Untouched context.\n\ndeleted.txt:1 [blocking] Deleted file.",
            [{"path": "items.txt", "line": 2, "side": "RIGHT", "body": "[blocking] Add a check."}],
        ), ("Example/Engine", 12, HEAD, APPROVE["detail"], [])])

    def test_review_failures_halt_without_merging(self):
        changes = {**APPROVE, "review": "changes", "findings": [FINDING]}
        for method, stop, replies in (
            ("review_target", "review_unavailable", []),
            ("post_review", "review_not_posted", [changes]),
        ):
            with self.subTest(method=method), patch.object(self.github, method, side_effect=ForgeError("Unavailable")):
                self.checkpoint = self.cwd / f"{method}.sqlite3"
                result = self.walk([{"triage": "implement"}, {"implementation": "Built."}, *replies])
                self.assertEqual(result.get("stop"), stop)
                self.assertEqual(result.get("halt_detail"), "Unavailable")
                if replies:
                    self.assertEqual(result.get("findings"), [FINDING])
                self.assertEqual(self.github.merge_calls, [])

    def test_load_slice_issue(self):
        result = self.walk([{"triage": "question"}, {"research": "answer"}])
        self.assertEqual(result["request"], "Issue title\n\n" + slice_body(EMPTY_SLICE))
        self.assertEqual(result["issue_kind"], "slice")
        self.assertEqual(result["slice"], EMPTY_SLICE)

    def test_missing_or_invalid_slice_stops_before_triage(self):
        for body in ("Issue body", "Issue body\n\n```json\n{\n```", slice_body({"screen": ""})):
            with self.subTest(body=body), TemporaryDirectory(dir=self.cwd) as directory:
                self.github.body = body
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([])
                self.assertEqual(result["stop"], "missing_slice")
                self.assertEqual(self.calls, [])
                self.assertEqual(self.walk([])["stop"], "missing_slice")

    def test_load_parent_and_file_slices_only_once(self):
        self.github = FakeGitHub(["factory"])
        second = {**SLICE, "title": "Finish items", "depends_on": [SLICE["title"]]}
        result = self.walk([{"triage": "spec"}, {
            "spec": "Plan", "spec_gate": "ready", "slices": [SLICE, second],
        }])
        self.assertEqual(result["issue_kind"], "parent")
        self.assertEqual(result["request"], "Issue title\n\nIssue body")
        self.assertEqual(result["child_issue_numbers"], [101, 102])
        self.assertEqual([x[0] for x in self.github.created], [SLICE["title"], second["title"]])
        self.assertEqual(self.github.created[0][2], ["slice", "factory"])
        self.assertEqual(self.github.created[1][2], ["slice"])
        for text in (SLICE["acceptance"], SLICE["screen"], "unique_item_id"):
            self.assertIn(text, self.github.created[0][1])
        self.assertIn(SLICE["title"], self.github.created[1][1])
        for item, (_, body, _) in zip([SLICE, second], self.github.created):
            prose, fence, encoded = body.partition("\n\n```json\n")
            self.assertEqual(fence, "\n\n```json\n")
            self.assertEqual(json.loads(encoded.removesuffix("\n```")), item)
            self.assertTrue(prose.startswith("Acceptance:\n" + item["acceptance"]))
        resumed = self.walk([])
        self.assertEqual(resumed["child_issue_numbers"], [101, 102])
        self.assertEqual(len(self.github.created), 2)
        self.assertEqual(self.github.label_calls, [])
        self.github.merged.add(101)
        self.walk([])
        self.walk([])
        self.assertEqual(self.github.label_calls, [("Example/Engine", 102, "factory")])
        child = self.walk([{"triage": "question"}, {"research": "answer"}], issue=102)
        self.assertEqual(child["slice"], second)

    def test_unknown_dependency_does_not_label(self):
        self.github = FakeGitHub(["factory"])
        second = {**SLICE, "title": "Finish items", "depends_on": ["Missing"]}
        with patch("foreman.delivery.clean_slices", return_value=[SLICE, second]):
            self.walk([{"triage": "spec"}, {"spec": "Plan", "spec_gate": "ready", "slices": [SLICE, second]}])
        self.walk([])
        self.assertEqual(self.github.label_calls, [])

    def test_injected_workers_reach_merge_after_a_real_check_in_the_repo(self):
        (self.cwd / "expected").touch()
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built", "head_sha": "b" * 40}, APPROVE],
            check="test -f expected",
        )
        self.assertEqual(self.calls, ["claude", "codex", "claude"])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["check_exit_code"], 0)
        self.assertEqual(result["verified_head"], HEAD)
        self.assertEqual(result["head_sha"], "b" * 40)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(result["merge"], "armed")

    def test_cli_uses_one_workspace_root_for_the_worker_check_and_checkpoint(self):
        root = self.cwd / "checkout"
        root.mkdir()
        live_run = LiveRun(self.github, FakeWorkspace(root))
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])

        def worker(args, cwd, env):
            self.assertEqual(cwd, root)
            return worker_reply(args, next(replies))

        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "touch checked"],
                worker_runner=worker, checkpoint=self.checkpoint, live_run=live_run,
            )
        self.assertEqual(code, 0)
        self.assertTrue((root / "checked").exists())
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        rerun = {"repo": "Example/Engine", "issue": 8, "check": "exit 0", "checkpoint": self.checkpoint}
        self.assertEqual(run_live_delivery(**rerun, run=live_run)["workspace"], str(root.resolve()))
        with self.assertRaisesRegex(ValueError, "Run this issue from"):
            run_live_delivery(**rerun, run=LiveRun(self.github, FakeWorkspace(self.cwd)))

    def test_nonzero_check_exit_consumes_the_cap_without_dispatched_verify_or_review(self):
        result = self.walk([{"triage": "implement"}] + [{"implementation": "built"}] * 3, check="exit 17")
        self.assertEqual(result["check_exit_code"], 17)
        self.assertEqual(result["stop"], "verify_cap")
        self.assertEqual(self.calls, ["claude", "codex", "codex", "codex"])
        self.assertNotIn("merge", result["path"])

    def test_both_proofs_pass_before_review_and_merge(self):
        self.github.body = slice_body(SLICE)
        result = self.walk([
            {"triage": "spec"},
            {"spec_gate": "ready", "spec": "Make IDs unique"},
            {"implementation": "built", "head_sha": HEAD, "browser_proof": BROWSER, "formal_proof": FORMAL},
            APPROVE,
        ])
        self.assertEqual(result["path"][-5:], ["verify", "proof", "review", "merge", "done"])
        self.assertEqual(result["slice"], SLICE)
        self.assertEqual(result["proof_labels"], {})
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_missing_proof_stops_before_review(self):
        self.github.body = slice_body(SLICE)
        for field, artifact, missing in (("formal_proof", FORMAL, "browser"), ("browser_proof", BROWSER, "formal")):
            with self.subTest(missing=missing), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk([
                    {"triage": "implement", "slice": EMPTY_SLICE}, {"implementation": "built", field: artifact},
                ])
                self.assertEqual(result["stop"], "insufficient_evidence")
                self.assertEqual(result["proof_labels"], {missing: "insufficient_evidence"})
                self.assertNotIn("review", result["path"])

    def test_parent_stops_before_review_when_its_proof_is_missing(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "implement", "slice": SLICE}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "proof", "done"])
        self.assertEqual(result["stop"], "insufficient_evidence")
        self.assertEqual(result["proof_labels"], {"browser": "insufficient_evidence", "formal": "insufficient_evidence"})

    def test_parent_cannot_implement_without_a_slice(self):
        self.github = FakeGitHub(["factory"])
        for triage in ({"triage": "implement"}, {"triage": "implement", "slice": None}):
            with self.subTest(triage=triage), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "run.sqlite3"
                self.calls.clear()
                result = self.walk([triage, {"implementation": "built"}, APPROVE])
                self.assertEqual(result["path"], ["triage", "done"])
                self.assertEqual(result["stop"], "invalid_output")
                self.assertTrue(result["halt_detail"].startswith("slice:"))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.github.merge_calls, [])
                self.assertEqual(self.github.created, [])

    def test_browser_block_rebuilds_and_rechecks_before_review(self):
        self.github.body = slice_body(SLICE)
        blocked = {"implementation": "built", "formal_proof": FORMAL, "browser_proof": {**BROWSER, "passed": False}}
        fixed = {**blocked, "browser_proof": BROWSER}
        result = self.walk([{"triage": "implement"}, blocked, fixed, APPROVE])
        self.assertEqual(result["path"], [
            "triage", "implement", "verify", "proof", "implement", "verify", "proof", "review", "merge", "done",
        ])
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_formal_block_consumes_the_review_cap(self):
        self.github.body = slice_body(SLICE)
        blocked = {
            "implementation": "built", "browser_proof": BROWSER,
            "formal_proof": {"constraints": [{"constraint": "unique_item_id", "rejected": False}]},
        }
        result = self.walk([{"triage": "implement"}] + [blocked] * 3)
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["proof_labels"], {"formal": "block"})
        self.assertEqual(result["review_rounds"], 3)
        self.assertNotIn("review", result["path"])

    def test_unconfigured_worker_stops_without_running_the_check(self):
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "touch ran"],
                worker_runner=None, checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 0)
        self.assertIn("done no_dispatcher", stdout.getvalue())
        self.assertFalse((self.cwd / "ran").exists())

    def test_dirty_or_invalid_worktree_skips_the_check(self):
        for snapshot in (WorktreeState(HEAD, False, BRANCH), WorktreeInspectionFailed("Invalid HEAD")):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built", "head_sha": HEAD}],
                    check="touch ran", snapshots=(snapshot,),
                )
                self.assertEqual(result["stop"], "dirty_worktree")
                self.assertEqual(result["path"], ["triage", "implement", "verify", "done"])
                self.assertFalse((self.cwd / "ran").exists())
                self.assertNotIn("check_exit_code", result)
                self.assertEqual(self.github.merge_calls, [])

    def test_worktree_changes_after_check_leave_the_command_unarmed(self):
        for snapshot, detail in (
            (WorktreeState(HEAD, False, BRANCH), "worktree has changes"),
            (WorktreeState("b" * 40, True, BRANCH), f"HEAD is {'b' * 40}, verification checked {HEAD}. This run is finished"),
            (WorktreeState(HEAD, True, None), "HEAD is detached"),
        ):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
                    snapshots=(CLEAN_WORKTREE, CLEAN_WORKTREE, snapshot),
                )
                self.assertEqual(result["check_exit_code"], 0)
                self.assertEqual(result["merge"], "not_armed")
                self.assertEqual(result["stop"], "merge_not_armed")
                self.assertIn(detail, result["halt_detail"])
                self.assertEqual(self.github.merge_calls, [])

    def test_live_workspace_reports_head_status_and_branch(self):
        for status, branch in (("", BRANCH + "\n"), ("?? untracked\n", ""), ("", "")):
            with self.subTest(status=status, branch=branch), patch("foreman.workspace.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, HEAD + "\n"),
                subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, status),
                subprocess.CompletedProcess(["git", "branch", "--show-current"], 0, branch),
            ]) as run:
                self.assertEqual(LiveWorkspace(self.cwd).inspect(), WorktreeState(HEAD, not status, branch.strip() or None))
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["git", "rev-parse", "HEAD"], ["git", "status", "--porcelain"], ["git", "branch", "--show-current"],
                ])
                self.assertTrue(all(call.kwargs["cwd"] == self.cwd for call in run.call_args_list))

    def test_review_diff_fetches_an_unrelated_new_base_commit(self):
        upstream = self.cwd / "upstream"
        checkout = self.cwd / "checkout"

        def git(root: Path, *args: str) -> str:
            return subprocess.check_output(
                ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", *args],
                cwd=root, text=True, stderr=subprocess.PIPE,
            ).strip()

        git(self.cwd, "init", "-b", "main", str(upstream))
        (upstream / "items.txt").write_text("old\n")
        git(upstream, "add", "items.txt")
        git(upstream, "commit", "-m", "Base")
        git(self.cwd, "clone", "--no-local", str(upstream), str(checkout))
        git(checkout, "switch", "-c", BRANCH)
        (checkout / "items.txt").write_text("new\n")
        git(checkout, "commit", "-am", "Review head")
        git(checkout, "push", "origin", BRANCH)
        head = git(checkout, "rev-parse", "HEAD")
        (upstream / "main.txt").write_text("Unrelated change\n")
        git(upstream, "add", "main.txt")
        git(upstream, "commit", "-m", "Advance main")
        base = git(upstream, "rev-parse", "HEAD")
        with self.assertRaises(subprocess.CalledProcessError):
            git(checkout, "cat-file", "-e", base)

        reviewed = LiveWorkspace(checkout).review_diff(base, head)
        self.assertEqual(git(checkout, "cat-file", "-t", base), "commit")
        self.assertIn(f"{base}...{head}", reviewed.command)
        self.assertIn("-old\n+new\n", reviewed.patch)
        self.assertNotIn("main.txt", reviewed.patch)

    def test_live_workspace_rejects_invalid_head_before_the_remaining_inspection(self):
        for head in ("", "abc123", "z" * 40):
            with self.subTest(head=head), patch(
                "foreman.workspace.subprocess.run",
                return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, head + "\n", ""),
            ) as run:
                with self.assertRaisesRegex(WorktreeInspectionFailed, "full 40-hex HEAD SHA"):
                    LiveWorkspace(self.cwd).inspect()
                run.assert_called_once()

    def test_main_displays_the_forge_failure(self):
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        detail = "GraphQL: Auto-merge is disabled"
        with (
            contextlib.chdir(self.cwd),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            patch.object(self.github, "arm_auto_merge", side_effect=MergeNotArmed(detail)) as arm,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8", "--check", "exit 0"],
                worker_runner=lambda args, cwd, env: worker_reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 0)
        arm.assert_called_once_with("Example/Target", 8, BRANCH, HEAD)
        self.assertIn(f"merge not armed\ncheck exit 0\ndone merge_not_armed\n{detail}", stdout.getvalue())

    def test_parent_arms_its_pull_request_without_filing_slices(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "implement", "slice": EMPTY_SLICE}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["path"], ["triage", "implement", "verify", "review", "merge", "done"])
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(self.github.created, [])

    def test_main_dry_run_reports_a_skipped_merge(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, patch("foreman.github.subprocess.run") as run:
            code = main(["run", "delivery", "--repo", "Example/Target", "--request", "List items", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(
            "merge skipped (dry run): gh pr merge '<number>' --repo Example/Target --squash --auto --match-head-commit '<sha>'\n"
            "done verified",
            stdout.getvalue(),
        )
        run.assert_not_called()

    def test_completed_legacy_checkpoint_keeps_its_merge_outcome(self):
        result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        config = {"configurable": {"thread_id": "example/engine#8"}}
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saved = saver.get_tuple(config)
            channels = saved.checkpoint["channel_values"]
            channels["merge_armed"] = channels.pop("merge") == "armed"
            saver.put(saved.config, saved.checkpoint, saved.metadata, {})
        self.assertEqual(self.walk([]), result)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import main
from foreman.delivery import LiveRun
from test_runner import APPROVE, HEAD, FakeGitHub, FakeWorkspace, worker_reply

def run(args, cwd, env):
    prompt = args[-1]
    state = json.loads(prompt.split("Delivery state:\\n")[1])
    if args[0] == "codex":
        schema = json.loads(prompt.split("Output schema:\\n")[1].split("\\n\\nDelivery state:")[0])
    else:
        schema = json.loads(args[args.index("--json-schema") + 1])
    if schema["title"] == "TriageOutput":
        assert not state.get("human_answers")
        assert state["request"] == "List items"
        return worker_reply(args, {"triage": "spec"})
    if schema["title"] == "SpecOutput":
        if not state.get("human_answers"):
            return worker_reply(args, {
                "spec": "A decision is open", "spec_gate": "needs_human", "human_question": "Which items?"
            })
        assert state["human_answers"] == ["Only open items"]
        assert state["request"] == "List items"
        return worker_reply(args, {"spec": "List open items", "spec_gate": "ready"})
    if schema["title"] == "ImplementOutput":
        return worker_reply(args, {"implementation": "built", "head_sha": HEAD})
    return worker_reply(args, APPROVE)

sys.exit(main(
    sys.argv[2:], worker_runner=run, checkpoint=Path(sys.argv[1]),
    live_run=LiveRun(FakeGitHub(), FakeWorkspace(Path.cwd())),
))
"""
        def invoke(*flags):
            bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
            return subprocess.run(
                [sys.executable, "-c", bootstrap + script, str(self.checkpoint), "run", "delivery",
                 "--repo", "Example/Engine", "--issue", "8", "--check", "echo checked >> check.log", *flags],
                cwd=self.cwd, text=True, capture_output=True, check=True,
            ).stdout

        first = invoke("--request", "List items")
        self.assertIn("paused needs_human\nWhich items?", first)
        self.assertEqual(invoke(), first)
        self.assertFalse((self.cwd / "check.log").exists())
        completed = invoke("--answer", "Only open items")
        self.assertEqual(completed.count("triage "), 1)
        self.assertEqual(completed.count("spec "), 2)
        self.assertIn("merge armed", completed)
        self.assertIn("done verified", completed)
        self.assertEqual(invoke(), completed)
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")
