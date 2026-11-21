import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.cli import _write_check_log, main
from foreman.config import ConfigError, ModelSpec, NodeConfig, load_config
from foreman.delivery import LiveRun, halted, run_delivery
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.dry_run import DEFAULT_RISK, ScriptHandler
from foreman.github import CommandTimedOut, ForgeError, Issue, IssueBlocker, LiveForge, MergeHoldUnreadable, MergeNotArmed, PullRequest, render_slice_body

from foreman.runner import run_live_delivery
from foreman.stage_output import STAGE_OUTPUTS, ImplementOutput, ReviewOutput, Slice, parse_output
from foreman.worker import CliWorker, run_worker_command
from foreman.workspace import LiveWorkspace, PullRequestSize, ReviewDiff, Workspace, WorktreeInspectionFailed, WorktreeState
from test_adapters import fixture_process


HEAD = "a" * 40
SYNTHETIC_QUESTION_TOKEN = "ghp_" + "a" * 36

BASE = "b" * 40

BRANCH = "8-list-items"
CLEAN_WORKTREE = WorktreeState(HEAD, True, BRANCH)
SLICE = {
    "title": "List items once",
    "acceptance": "The view lists every item once.",
    "builds": [],
    "uses": [],
    "screen": "docs/screens/items.md",
    "properties": [{"name": "Unique", "constraint": "unique_item_id"}],
}
EMPTY_SLICE = {**SLICE, "screen": "", "properties": []}
BROWSER = {"screen": SLICE["screen"], "trace": "traces/items.zip", "passed": True}
FORMAL = {"ran": True, "constraints": [{"constraint": "unique_item_id", "rejected": True}]}
SEAM = {"name": "item-list", "exists": False, "path": None}
BUILDER = {**SLICE, "builds": [SEAM]}
CONSUMER = {**EMPTY_SLICE, "title": "Finish items", "uses": [SEAM]}
APPROVE = {"review": "approve", "risk": DEFAULT_RISK, "findings": [], "detail": "The changes meet the request.", "tool_facts": []}
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

    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize:
        return {"head": head, "merge_base": "b" * 40, "lines": 1}

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


class FixtureWorkspace(LiveWorkspace):
    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize:
        return {"head": head, "merge_base": BASE, "lines": 1}

    def review_diff(self, base, head):
        return ReviewDiff(f"git diff {base}...{head}", DIFF)


class FakeGitHub:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else ["slice"]
        self.body = slice_body(EMPTY_SLICE) if "slice" in self.labels else "Issue body"
        self.created: list[tuple[str, str, list[str]]] = []
        self.closed = set()
        self.merged = set()
        self.label_calls = []
        self.issue_labels: dict[int, list[str]] = {}
        self.merge_calls = []
        self.dependencies = {}
        self.base_branch = "main"
        self.reviews = []
        self.base = BASE
        self.hold: str | None = None
        self.hold_reads = []

    def pull_request_base(self, repo: str, branch: str) -> str:
        return self.base_branch

    def review_target(self, repo, issue, branch, sha):
        return PullRequest.model_validate({
            "number": 12, "state": "OPEN", "headRefOid": sha, "headRefName": branch,
            "baseRefName": "main", "baseRefOid": self.base, "autoMergeRequest": None,
        })

    def post_review(self, repo, target, body, comments):
        self.reviews.append((repo, target.number, target.headRefOid, body, comments))
    def merge_hold(self, repo):
        self.hold_reads.append(repo)
        return self.hold

    def arm_auto_merge(self, repo, issue, branch, sha):
        self.merge_calls.append((repo, issue, branch, sha))

    def load_issue(self, repo, number) -> Issue:
        if number in self.issue_labels:
            title, body, _ = self.created[number - 101]
            return {"title": title, "body": body, "labels": self.issue_labels[number]}
        return {"title": "Issue title", "body": self.body, "labels": self.labels}

    def create_issue(self, repo, title, body, labels, *, child=None):
        if child is not None:
            body = render_slice_body(body, child)
        self.created.append((title, body, labels))
        number = 100 + len(self.created)
        self.issue_labels[number] = list(labels)
        return number

    def is_merged(self, repo, number):
        return number in self.merged

    def add_blocker(self, repo, number, blocker):
        self.dependencies.setdefault(number, []).append(blocker)

    def blockers(self, repo, number):
        return [
            IssueBlocker(number=blocker, title=self.created[blocker - 101][0],
                         repository_url=f"https://api.github.com/repos/{repo}",
                         html_url=f"https://github.com/{repo}/issues/{blocker}",
                         state="closed" if blocker in self.closed or blocker in self.merged else "open")
            for blocker in self.dependencies.get(number, ())
        ]

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

    def walk(self, replies, *, issue=8, check="exit 0", snapshots=(CLEAN_WORKTREE,), workspace: Workspace | None = None):
        outputs = iter(replies)

        def run(args, cwd, env):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            self.prompts.append(args[-1])
            return worker_reply(args, next(outputs))

        return run_live_delivery(
            repo="Example/Engine", issue=issue, check=check,
            checkpoint=self.checkpoint, dispatcher=FailoverDispatcher(CliWorker(self.cwd, run)),
            run=LiveRun(self.github, workspace if workspace is not None else FakeWorkspace(self.cwd, snapshots)),
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

    def test_review_halts_when_git_fetch_times_out(self):
        outputs = {
            ("git", "rev-parse", "HEAD"): HEAD + "\n",
            ("git", "status", "--porcelain"): "",
            ("git", "branch", "--show-current"): BRANCH + "\n",
            ("exit 0",): "",
        }

        def run(
            command: str | list[str], *, timeout: float | None = None, **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if command == ["git", "fetch", "--no-tags", "origin", BASE, HEAD]:
                self.assertEqual(timeout, 30)
                raise subprocess.TimeoutExpired(command, 30)
            self.assertIsNone(timeout)
            key = (command,) if isinstance(command, str) else tuple(command)
            return subprocess.CompletedProcess(command, 0, outputs[key].encode())

        with patch.object(LiveWorkspace, "pull_request_size", return_value={"head": HEAD, "merge_base": BASE, "lines": 1}), patch("foreman.workspace.subprocess.run", side_effect=run):
            result = self.walk(
                [{"triage": "implement"}, {"implementation": "Built."}],
                workspace=LiveWorkspace(self.cwd),
            )

        self.assertEqual(result.get("stop"), "review_unavailable")
        self.assertRegex(result.get("halt_detail", ""), r"^git fetch .*timed out after 30 seconds")
        self.assertEqual(self.github.reviews, [])
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
        result = self.walk([{"triage": "spec"}, {
            "spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER],
        }])
        self.assertEqual(result["issue_kind"], "parent")
        self.assertEqual(result["request"], "Issue title\n\nIssue body")
        self.assertEqual(result["child_issue_numbers"], [101, 102])
        self.assertEqual([x[0] for x in self.github.created], [BUILDER["title"], CONSUMER["title"]])
        self.assertEqual(self.github.created[0][2], ["slice", "factory"])
        self.assertEqual(self.github.created[1][2], ["slice"])
        for text in (SLICE["acceptance"], SLICE["screen"], "unique_item_id"):
            self.assertIn(text, self.github.created[0][1])
        self.assertIn(SLICE["title"], self.github.created[1][1])
        child_payload = {**CONSUMER, "depends_on": [BUILDER["title"]], "blocker_numbers": [101]}
        expected = [{**BUILDER, "depends_on": [], "blocker_numbers": []}, child_payload]
        for item, (_, body, _) in zip(expected, self.github.created):
            prose, fence, encoded = body.partition("\n\n```json\n")
            self.assertEqual(fence, "\n\n```json\n")
            self.assertEqual(json.loads(encoded.removesuffix("\n```")), item)
            self.assertTrue(prose.startswith("Acceptance:\n" + item["acceptance"]))
        self.assertEqual(self.github.dependencies, {102: [101]})
        resumed = self.walk([])
        self.assertEqual(self.github.dependencies, {102: [101]})
        self.assertEqual(resumed["child_issue_numbers"], [101, 102])
        self.assertEqual(len(self.github.created), 2)
        self.assertEqual(self.github.label_calls, [])
        self.assertEqual([blocker.number for blocker in self.github.blockers("Example/Engine", 102)], [101])
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "102", "--check", "exit 0"],
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
                worker_runner=lambda *_: self.fail("An open blocker must prevent dispatch."),
            )
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "done blocked\nOpen blockers: https://github.com/Example/Engine/issues/101\n")
        self.github.closed.add(101)
        self.assertIn("no merged pull request", self.walk([], issue=102)["halt_detail"])
        self.github.merged.add(101)
        self.walk([])
        self.walk([])
        self.assertEqual(self.github.dependencies, {102: [101]})
        self.assertEqual(self.github.label_calls, [("Example/Engine", 102, "factory")])
        child = self.walk([{"triage": "question"}, {"research": "answer"}], issue=102)
        self.assertEqual(child["slice"], child_payload)

    def test_existing_seam_resolves_in_workspace_and_skips_a_dependency(self):
        self.github = FakeGitHub(["factory"])
        (self.cwd / "seam.py").touch()
        consumer = {**CONSUMER, "uses": [{"name": "local", "exists": True, "path": "seam.py"}]}
        result = self.walk([{"triage": "spec"}, {
            "spec": "Plan", "spec_gate": "ready", "slices": [BUILDER, consumer],
        }])
        self.assertEqual(result["stop"], "sliced")
        self.assertEqual(result["slices"][1]["depends_on"], [])
        self.assertEqual(self.github.issue_labels[102], ["slice", "factory"])

    def test_self_built_seam_is_not_a_dependency(self):
        self.github = FakeGitHub(["factory"])
        builder = {**BUILDER, "uses": [SEAM]}
        result = self.walk([{"triage": "spec"}, {
            "spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, builder],
        }])
        self.assertEqual(result["stop"], "sliced")
        self.assertEqual(result["slices"][0]["depends_on"], [])
        self.assertEqual(self.github.dependencies, {102: [101]})

    def test_existing_seam_with_a_builder_is_rejected_before_filing(self):
        self.github = FakeGitHub(["factory"])
        (self.cwd / "seam.py").touch()
        consumer = {**CONSUMER, "uses": [{**SEAM, "exists": True, "path": "seam.py"}]}
        spec = {"spec": "Plan", "spec_gate": "ready", "slices": [BUILDER, consumer]}
        result = self.walk([{"triage": "spec"}, spec, spec, spec])
        self.assertEqual(result["stop"], "spec_cap")
        self.assertEqual(result["spec_rejection"]["kind"], "existing_seam_builder")
        self.assertEqual(self.github.created, [])

    def test_workspace_file_paths_resolve_inside_the_root(self):
        (self.cwd / "api.py").touch()
        (self.cwd / "sub").mkdir()
        workspace = LiveWorkspace(self.cwd)
        self.assertTrue(workspace.has_file("sub/../api.py"))
        with TemporaryDirectory() as directory:
            outside = Path(directory) / "api.py"
            outside.touch()
            (self.cwd / "escape.py").symlink_to(outside)
            for path in (str(outside), os.path.relpath(outside, self.cwd), "escape.py"):
                with self.subTest(path=path):
                    self.assertFalse(workspace.has_file(path))

    def test_existing_seam_outside_workspace_reaches_the_cap_without_filing(self):
        self.github = FakeGitHub(["factory"])
        with TemporaryDirectory() as directory:
            outside = Path(directory) / "seam.py"
            outside.touch()
            (self.cwd / "escape.py").symlink_to(outside)
            consumer = {**CONSUMER, "uses": [{"name": "local", "exists": True, "path": "escape.py"}]}
            spec = {"spec": "Plan", "spec_gate": "ready", "slices": [BUILDER, consumer]}
            result = self.walk([{"triage": "spec"}, spec, spec, spec])
        self.assertEqual(result["stop"], "spec_cap")
        self.assertEqual(result["review_rounds"], 3)
        self.assertEqual(result["spec_rejection"]["kind"], "dangling_seam")
        self.assertEqual(self.github.created, [])

    def test_link_failure_leaves_child_blocked_and_parent_retry_repairs_it(self):
        self.github = FakeGitHub(["factory"])
        with patch.object(self.github, "add_blocker", side_effect=CommandTimedOut("gh api")):
            with self.assertRaises(CommandTimedOut):
                self.walk([{"triage": "spec"}, {
                    "spec": "Plan", "spec_gate": "ready", "slices": [CONSUMER, BUILDER],
                }])
            self.assertEqual(self.walk([], issue=102)["halt_detail"], "Planned blocker links are missing.")
        self.walk([])
        self.assertEqual(len(self.github.created), 2)
        self.assertEqual([blocker.number for blocker in self.github.blockers("Example/Engine", 102)], [101])

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

    def review_tool_fact(self, fixture: str | None, *, entry: str | None = None, record_during_review: bool = False):
        root = self.cwd / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", BRANCH], cwd=root, check=True)
        fixtures = root / "tests/fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "result.txt").write_text("debug output\n")

        def manifest(path: str) -> str:
            return (
                f'[fixtures."{path}"]\n'
                'kind = "captured_run"\n'
                'tool = "example"\n'
                'version = "1"\n'
                'argv = ["example", "--debug"]\n'
            )

        manifest_path = fixtures / "manifest.toml"
        manifest_path.write_text(manifest(entry) if entry is not None else "[fixtures]\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgSign=false",
             "commit", "-qm", "Add fixture"],
            cwd=root, check=True,
        )
        built = {"implementation": "Built.", "head_sha": HEAD}
        review = {
            **APPROVE,
            "tool_facts": [{"claim": "The CLI accepts --debug.", "fixture": fixture}],
        }
        replies = iter([{"triage": "implement"}] + [built, review] * 3)

        def worker(args, cwd, env):
            reply = next(replies)
            if record_during_review and "review" in reply:
                manifest_path.write_text(manifest("result.txt"))
            return worker_reply(args, reply)

        return run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
            dispatcher=FailoverDispatcher(CliWorker(root, worker)),
            run=LiveRun(self.github, FixtureWorkspace(root)),
        )

    def test_approve_with_unrecorded_tool_fact_becomes_changes(self):
        result = self.review_tool_fact("result.txt", record_during_review=True)
        self.assertEqual(result.get("review"), "changes")
        self.assertIn("The CLI accepts --debug.", result.get("detail") or "")
        self.assertEqual(result.get("review_rounds"), 1)
        self.assertEqual(self.github.merge_calls, [])

    def test_approve_with_recorded_tool_fact_stays_approve(self):
        result = self.review_tool_fact("result.txt", entry="result.txt")
        self.assertEqual(result.get("review"), "approve")
        self.assertEqual(result.get("findings"), [])
        self.assertEqual(result.get("head_sha"), HEAD)
        self.assertNotEqual(result.get("verified_head"), HEAD)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, result.get("verified_head"))])

    def test_dangling_tool_fact_citation_blocks_approval(self):
        result = self.review_tool_fact("absent.txt", entry="absent.txt")
        self.assertEqual(result.get("review"), "changes")
        self.assertIn("absent.txt", result.get("detail") or "")
        self.assertEqual(result.get("stop"), "review_cap")
        self.assertEqual(self.github.merge_calls, [])

    def test_tool_fact_without_citation_blocks_approval(self):
        result = self.review_tool_fact(None, entry="result.txt")
        self.assertEqual(result.get("review"), "changes")
        self.assertIn("The CLI accepts --debug.", result.get("detail") or "")
        self.assertEqual(self.github.merge_calls, [])
    def test_cli_redacts_the_check_subprocess_stdout_and_stderr(self):
        check = self.cwd / "verify.sh"
        check.write_text(
            f"echo 'Check stdout {SYNTHETIC_QUESTION_TOKEN}'\n"
            f"echo 'Check stderr {SYNTHETIC_QUESTION_TOKEN}' >&2\n"
        )
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "sh verify.sh"],
                worker_runner=lambda args, cwd, env: worker_reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 0)
        self.assertIn("done verified\n", stdout.getvalue())
        self.assertIn("Check stdout [redacted:GitHub Token]\n", stdout.getvalue())
        self.assertIn("Check stderr [redacted:GitHub Token]\n", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, stdout.getvalue() + stderr.getvalue())

    def test_check_streams_in_write_order_before_the_command_exits(self):
        check = self.cwd / "stream.py"
        acknowledgement = self.cwd / "logged"
        check.write_text(
            "import os, pathlib, time\n"
            f"os.write(1, b'First synthetic {SYNTHETIC_QUESTION_TOKEN}\\n')\n"
            f"ack = pathlib.Path({str(acknowledgement)!r})\n"
            "deadline = time.monotonic() + 5\n"
            "while not ack.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
            "if not ack.exists(): raise SystemExit(9)\n"
            f"os.write(2, b'Second synthetic {SYNTHETIC_QUESTION_TOKEN}\\n')\n"
            "os.write(1, b'Third\\n')\n"
            "os.write(2, b'Fourth')\n"
            "raise SystemExit(7)\n"
        )

        def log(line):
            _write_check_log(line)
            acknowledgement.touch()

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = LiveWorkspace(self.cwd).run(shlex.join([sys.executable, str(check)]), log=log)
        self.assertEqual(code, 7)
        self.assertEqual(stdout.getvalue(), (
            "First synthetic [redacted:GitHub Token]\n"
            "Second synthetic [redacted:GitHub Token]\nThird\nFourth"
        ))

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
        self.assertEqual(code, 1)
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
            (
                WorktreeState("b" * 40, True, BRANCH),
                f"git reset --keep {HEAD}\nThen rerun the same command. New commits need a new run.",
            ),
            (WorktreeState(HEAD, True, None), "HEAD is detached"),
        ):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
                    snapshots=(CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, snapshot),
                )
                self.assertEqual(result["check_exit_code"], 0)
                self.assertEqual(result["merge"], "not_armed")
                self.assertEqual(result["stop"], "merge_not_armed")
                self.assertIn(detail, result["halt_detail"])
                self.assertEqual(self.github.merge_calls, [])

    def test_head_moved_before_review_uses_the_existing_merge_stop(self):
        moved = WorktreeState("b" * 40, True, BRANCH)
        for snapshots in ((CLEAN_WORKTREE, moved), (CLEAN_WORKTREE, CLEAN_WORKTREE, moved)):
            with self.subTest(snapshots=snapshots), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "run.sqlite3"
                result = self.walk([{"triage": "implement"}, {"implementation": "built"}], snapshots=snapshots)
                self.assertEqual(result.get("stop"), "merge_not_armed")
                self.assertEqual(result.get("merge"), "not_armed")
                self.assertIn(f"HEAD is {moved.head}, verification checked {HEAD}.", result.get("halt_detail", ""))
                self.assertNotIn("review", result.get("models_used", {}))
                self.assertEqual(self.github.merge_calls, [])
                resumed = self.walk([APPROVE])
                self.assertIn("review", resumed.get("models_used", {}))
                self.assertEqual(resumed.get("merge"), "armed")
                self.github.merge_calls.clear()

    def test_retry_after_failed_hold_read_arms_once(self):
        reason = "HTTP 503: Service Unavailable"
        with patch.object(self.github, "merge_hold", side_effect=MergeHoldUnreadable(reason)):
            held = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(held["stop"], "merge_held")
        self.assertEqual(held["merge"], "not_armed")
        self.assertEqual(held["halt_detail"], f"could not read FOREMAN_MERGE_HOLD: {reason}")
        self.assertEqual(held["merge_command"], [
            "gh", "pr", "merge", "<number>", "--repo", "Example/Engine", "--squash", "--auto", "--match-head-commit", HEAD,
        ])
        self.assertEqual(self.github.merge_calls, [])
        released = self.walk([])
        self.assertEqual(released["stop"], "verified")
        self.assertEqual(released["merge"], "armed")
        self.assertEqual(released["path"], held["path"])
        self.assertNotIn("halt_detail", released)
        self.assertEqual(self.walk([]), released)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_retry_after_worktree_is_cleaned_arms_once(self):
        halted = self.walk(
            [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
            snapshots=(CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, WorktreeState(HEAD, False, BRANCH)),
        )
        self.assertEqual(halted["stop"], "merge_not_armed")
        self.assertEqual(halted["merge"], "not_armed")
        self.assertEqual(self.github.hold_reads, [])
        self.assertEqual(self.github.merge_calls, [])
        released = self.walk([])
        self.assertEqual(released["stop"], "verified")
        self.assertEqual(released["merge"], "armed")
        self.assertEqual(released["path"], halted["path"])
        self.assertEqual(self.walk([]), released)
        self.assertEqual(self.github.hold_reads, ["Example/Engine"])
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_retry_after_reset_to_verified_head_arms_merge(self):
        repo = self.cwd / "engine"
        repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-c", "user.name=Foreman Test", "-c", "user.email=foreman@example.com", *args],
                cwd=repo, check=True, capture_output=True, text=True,
            ).stdout.strip()

        git("init", "-b", BRANCH)
        git("commit", "--allow-empty", "-m", "Verified commit")
        git("branch", "main")
        git("remote", "add", "origin", ".")
        verified_head = git("rev-parse", "HEAD")
        self.github.base = verified_head
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])

        def run_worker(args, cwd, env=None):
            reply = next(replies)
            if reply is APPROVE:
                git("commit", "--allow-empty", "-m", "Commit after verification")
            return worker_reply(args, reply)

        def run_delivery():
            return run_live_delivery(
                repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
                dispatcher=FailoverDispatcher(CliWorker(repo, run_worker)),
                run=LiveRun(self.github, LiveWorkspace(repo)),
            )

        halted = run_delivery()
        self.assertEqual(halted["stop"], "merge_not_armed")
        self.assertEqual(self.github.merge_calls, [])
        reset = re.search(rf"^  git reset --keep {verified_head}$", halted["halt_detail"], re.MULTILINE)
        self.assertIsNotNone(reset, halted["halt_detail"])
        git(*reset.group().split()[1:])

        resumed = run_delivery()
        self.assertEqual(resumed["stop"], "verified")
        self.assertEqual(resumed["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, verified_head)])

    def test_live_workspace_reports_head_status_and_branch(self):
        for status, branch in (("", BRANCH + "\n"), ("?? untracked\n", ""), ("", "")):
            with self.subTest(status=status, branch=branch), patch("foreman.workspace.subprocess.run", side_effect=[
                subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, (HEAD + "\n").encode()),
                subprocess.CompletedProcess(["git", "status", "--porcelain"], 0, status.encode()),
                subprocess.CompletedProcess(["git", "branch", "--show-current"], 0, branch.encode()),
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
                return_value=subprocess.CompletedProcess(["git", "rev-parse", "HEAD"], 0, (head + "\n").encode(), b""),
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
        self.assertEqual(code, 75)
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

    def test_main_rerun_arms_once_after_the_repository_hold_is_cleared(self):
        self.github.hold = "  Maintenance window "
        repo = "Example/Target"
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        live_run = LiveRun(self.github, FakeWorkspace(self.cwd))

        def invoke():
            with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    ["run", "delivery", "--repo", repo, "--issue", "8", "--check", "exit 0"],
                    worker_runner=lambda args, cwd, env=None: worker_reply(args, next(replies)),
                    checkpoint=self.checkpoint, live_run=live_run,
                )
            self.assertEqual(code, 75 if self.github.hold else 0)
            return stdout.getvalue()

        held = invoke()
        self.assertIn(f"done merge_held\nFOREMAN_MERGE_HOLD is set: {self.github.hold}\n", held)
        self.assertEqual(invoke(), held)
        self.assertEqual(self.github.merge_calls, [])
        self.github.hold = ""
        armed = invoke()
        self.assertIn("merge armed\ncheck exit 0\ndone verified", armed)
        self.assertEqual(invoke(), armed)
        self.assertEqual(self.github.hold_reads, [repo, repo, repo])
        self.assertEqual(self.github.merge_calls, [(repo, 8, BRANCH, HEAD)])

    def test_whitespace_merge_hold_reports_done(self):
        self.github.hold = " "
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8", "--check", "exit 0"],
                worker_runner=lambda args, cwd, env=None: worker_reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 75)
        self.assertIn("done merge_held\nFOREMAN_MERGE_HOLD is set.\n", stdout.getvalue())

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import _write_check_log, main
from foreman.delivery import LiveRun
from test_runner import APPROVE, HEAD, SYNTHETIC_QUESTION_TOKEN, FakeGitHub, FakeWorkspace, worker_reply

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
                "spec": "A decision is open", "spec_gate": "needs_human",
                "human_question": f"Which items? {SYNTHETIC_QUESTION_TOKEN}",
            })
        assert state["human_answers"] == ["Only open items"]
        assert state["request"] == "List items"
        assert state["human_question"] == f"Which items? {SYNTHETIC_QUESTION_TOKEN}"
        return worker_reply(args, {"spec": "List open items", "spec_gate": "ready"})
    if schema["title"] == "ImplementOutput":
        return worker_reply(args, {"implementation": "built", "head_sha": HEAD})
    return worker_reply(args, APPROVE)

sys.exit(main(
    sys.argv[2:], worker_runner=run, checkpoint=Path(sys.argv[1]),
    live_run=LiveRun(FakeGitHub(), FakeWorkspace(Path.cwd())),
))
"""
        def invoke(*flags, expected=0):
            bootstrap = f"import sys; sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n"
            process = subprocess.run(
                [sys.executable, "-c", bootstrap + script, str(self.checkpoint), "run", "delivery",
                 "--repo", "Example/Engine", "--issue", "8", "--check", "echo checked >> check.log", *flags],
                cwd=self.cwd, text=True, capture_output=True, check=False,
            )
            self.assertEqual(process.returncode, expected, process.stderr)
            return process.stdout

        first = invoke("--request", "List items", expected=3)
        self.assertIn("paused needs_human\nWhich items? [redacted:GitHub Token]", first)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, first)
        self.assertEqual(invoke(expected=3), first)
        self.assertFalse((self.cwd / "check.log").exists())
        completed = invoke("--answer", "Only open items")
        self.assertEqual(completed.count("triage "), 1)
        self.assertEqual(completed.count("spec "), 2)
        self.assertIn("merge armed", completed)
        self.assertIn("done verified", completed)
        self.assertEqual(invoke(), completed)
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")

class HaltExitCodeTest(unittest.TestCase):
    def live(self, replies=(), *, snapshots=(CLEAN_WORKTREE,), body=None, check="exit 0", dispatcher=True, hold=None):
        directory = self.enterContext(TemporaryDirectory())
        root = Path(directory)
        forge = FakeGitHub()
        forge.hold = hold
        if body is not None:
            forge.body = body
        outputs = iter(replies)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", check],
                checkpoint=root / "runs.sqlite3", live_run=LiveRun(forge, FakeWorkspace(root, snapshots)),
                worker_runner=(lambda args, cwd, env: worker_reply(args, next(outputs))) if dispatcher else None,
            )
        return code, output.getvalue()

    def assert_halt(self, result, reason, detail, code=1):
        self.assertEqual(result[0], code, result[1])
        self.assertIn(f"done {reason}\n{detail}\n", result[1])

    def test_no_dispatcher(self):
        self.assert_halt(self.live(dispatcher=False), "no_dispatcher", "No dispatcher is configured.")

    def test_dirty_worktree_changes(self):
        self.assert_halt(self.live(
            [{"triage": "implement"}, {"implementation": "built"}],
            snapshots=(WorktreeState(HEAD, False, BRANCH),),
        ), "dirty_worktree", "The worktree has changes.")

    def test_dirty_worktree_inspection(self):
        self.assert_halt(self.live(
            [{"triage": "implement"}, {"implementation": "built"}],
            snapshots=(WorktreeInspectionFailed("Invalid HEAD"),),
        ), "dirty_worktree", "Invalid HEAD")

    def test_insufficient_evidence(self):
        self.assert_halt(self.live(
            [{"triage": "implement"}, {"implementation": "built"}], body=slice_body(SLICE),
        ), "insufficient_evidence", "Required proof is missing.")

    def test_proof_review_cap(self):
        blocked = {"implementation": "built", "browser_proof": {**BROWSER, "passed": False}, "formal_proof": FORMAL}
        self.assert_halt(self.live(
            [{"triage": "implement"}] + [blocked] * 3, body=slice_body(SLICE),
        ), "review_cap", "Proof failed after 3 rounds: {'browser': 'block'}.")

    def test_verify_cap(self):
        self.assert_halt(self.live(
            [{"triage": "implement"}] + [{"implementation": "built"}] * 3, check="exit 7",
        ), "verify_cap", "Verification failed after 3 rounds. Check exit 7 failed.")

    def test_review_cap(self):
        changes = {**APPROVE, "review": "changes", "findings": [FINDING], "detail": "Add a boundary check."}
        self.assert_halt(self.live(
            [{"triage": "implement"}] + [{"implementation": "built"}, changes] * 3,
        ), "review_cap", "Review requested changes after 3 rounds. Last review: Add a boundary check.")

    def test_missing_slice_has_no_fake_stage(self):
        result = self.live(body="No JSON")
        self.assert_halt(result, "missing_slice", "slice issue needs a fenced JSON object")
        self.assertEqual(result[1], "done missing_slice\nslice issue needs a fenced JSON object\n")

    def test_resumable_halts(self):
        replies = [{"triage": "implement"}, {"implementation": "built"}, APPROVE]
        self.assert_halt(self.live(replies, hold="Maintenance"), "merge_held", "FOREMAN_MERGE_HOLD is set: Maintenance", 75)
        self.assert_halt(self.live(replies, snapshots=(CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, WorktreeState(HEAD, True, None))),
                         "merge_not_armed", "HEAD is detached. Merge requires a checked-out branch.", 75)

    def test_invalid_merge_sha_halts_with_detail_and_resumable_exit(self):
        invalid = WorktreeState("invalid", True, BRANCH)
        with patch.object(FakeGitHub, "arm_auto_merge", LiveForge.arm_auto_merge), \
             patch.object(FakeGitHub, "_arm_auto_merge", LiveForge._arm_auto_merge, create=True):
            result = self.live(
                [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
                snapshots=(invalid, invalid, invalid),
            )
        self.assert_halt(result, "merge_not_armed", "Merge arming requires a full 40-hex commit SHA.", 75)

    def test_answered_is_success(self):
        code, output = self.live([{"triage": "question"}, {"research": "The answer."}])
        self.assertEqual(code, 0, output)
        self.assertIn("done answered\n", output)

    def test_empty_details_are_rejected(self):
        for detail in ("", " ", "\n"):
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, "requires a nonempty halt detail"):
                halted("no_dispatcher", detail, "triage")

    def test_dry_run_success_and_human_stops(self):
        for handler, stop, expected in (
            (ScriptHandler(), "verified", 0),
            (ScriptHandler(triage="spec", slices=[EMPTY_SLICE]), "sliced", 0),
            (ScriptHandler(triage="needs_human"), "needs_human", 3),
            (ScriptHandler(triage="spec", spec_gate="needs_human"), "needs_human", 3),
        ):
            with self.subTest(stop=stop), patch("foreman.cli.ScriptHandler", return_value=handler):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = main(["run", "delivery", "--repo", "Example/Engine", "--request", "test", "--dry-run"])
                self.assertEqual(code, expected, output.getvalue())
                self.assertIn(f"done {stop}\n", output.getvalue())
                if stop == "needs_human":
                    self.assertIn("done needs_human\nWhat should Foreman know to continue?\n", output.getvalue())
