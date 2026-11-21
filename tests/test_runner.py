import tests

import contextlib
from dataclasses import replace
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
from unittest.mock import Mock, patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Outcome, Usage
from foreman.cli import _write_check_log, main, run_exit_code
from foreman.config import ConfigError, ModelSpec, NodeConfig, load_config
from foreman.outcomes import Halt
from foreman.delivery import LiveRun, carried_tool_facts, halted, run_delivery
from foreman.dispatch import InvalidOutput
from foreman.dry_run import DEFAULT_RISK, ScriptHandler
from foreman.review_context import SCRIPTED_REVIEW_CONTEXT
from foreman.github import CommandTimedOut, ForgeError, Issue, IssueBlocker, LinkedIssue, LiveForge, MergeHoldUnreadable, MergeNotArmed, HandoffChanged, QueueRejected, OpenIssue, PullRequest, render_slice_body

from foreman.readiness import MergeReceipt, Observation, PullSnapshot
from foreman.runner import AnswerError, RunPresentation, run_live_delivery
from foreman.state import CheckpointVersionError
from foreman.skill_rules import export_skills, git
from foreman.stage_output import STAGE_OUTPUTS, ImplementOutput, ReviewOutput, Slice, parse_output
from foreman.worker import CliWorker, run_worker_command
from foreman.workspace import LiveWorkspace, PullRequestSize, ReviewDiff, Workspace, WorktreeInspectionFailed, WorktreeState
from skill_fixture import FixtureSkills, fixture_pin, fixture_skills, skill_fixture
from test_adapters import codex_events, fixture_process


HEAD = "a" * 40
SYNTHETIC_QUESTION_TOKEN = "ghp_" + "a" * 36

BASE = "b" * 40

BRANCH = "8-list-items-once"
CLEAN_WORKTREE = WorktreeState(HEAD, True, BRANCH)
SLICE = {
    "title": "List items once",
    "acceptance": "The view lists every item once.",
    "builds": [],
    "uses": [],
    "screen": "docs/screens/items.md",
    "properties": [{"name": "Unique", "constraint": "unique_item_id"}],
    "stateful": False,
}
EMPTY_SLICE = {**SLICE, "screen": "", "properties": []}
BROWSER = {"screen": SLICE["screen"], "trace": "traces/items.zip", "passed": True}
FORMAL = {"ran": True, "constraints": [{"constraint": "unique_item_id", "rejected": True}], "typecheck": None}
SEAM = {"name": "item-list", "exists": False, "path": None}
BUILDER = {**SLICE, "builds": [SEAM]}
CONSUMER = {**EMPTY_SLICE, "title": "Finish items", "uses": [SEAM]}
APPROVE = {"resolutions": [], "risk": DEFAULT_RISK, "findings": [], "tool_facts": []}
FINDING = {"id": "f1", "kind": "correctness", "fix": "Add a check.", "ruling_id": None, "file": "items.txt", "line": 2, "severity": "blocking", "summary": "Add a check."}
DIFF = Path(__file__).with_name("fixtures").joinpath("review.diff").read_text()


def slice_body(item):
    return "Issue body\n\n```json\n" + json.dumps(item) + "\n```"


class FakeWorkspace(LiveWorkspace):
    def __init__(self, cwd, snapshots=()):
        super().__init__(cwd)
        self.snapshots = iter(snapshots)
        self.current = CLEAN_WORKTREE
        self.published = False

    def inspect(self):
        snapshot = next(self.snapshots, self.current) if self.published else self.current
        if isinstance(snapshot, WorktreeInspectionFailed):
            raise snapshot
        return snapshot

    def fingerprint(self):
        return "synthetic-edits"

    def reconcile(self, parent, branch, base_branch, fact_record):
        return None if self.current.clean else {"disposition": "edits", "fingerprint": self.fingerprint()}

    def create_branch(self, branch, base):
        self.current = WorktreeState(self.current.head, True, branch)

    def commit(self, message):
        head = HEAD
        self.current = WorktreeState(head, True, self.current.branch)

    def push(self, branch, repo):
        self.published = True

    def reply(self, args, reply):
        if "implementation" in reply:
            self.published = False
            self.current = WorktreeState(self.current.head, False, self.current.branch)
        return worker_reply(args, reply)

    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize:
        return {"head": head, "merge_base": "b" * 40, "lines": 1}

    def review_context(self, base, head):
        return SCRIPTED_REVIEW_CONTEXT

    def review_diff(self, base, head, *, interdiff=False):
        return ReviewDiff(f"git diff {base}{'..' if interdiff else '...'}{head}", DIFF)


def worker_reply(args, reply):
    if isinstance(reply, dict):
        if "triage" in reply:
            reply = {"work_kind": "feature", "slice": None, "human_question": None, **reply}
        if "spec" in reply:
            reply = {"slices": None, "human_question": None, **reply}
        if "implementation" in reply and args[0] == "codex":
            reply = {"browser_proof": None, "formal_proof": None, **reply}
    if args[0] == "codex":
        events = codex_events()
        events[-2]["item"]["text"] = json.dumps(reply) if isinstance(reply, dict) else reply
        return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")
    recorded = fixture_process(args, "success", 0)
    envelope = json.loads(recorded.stdout)
    field = "structured_output" if args[0] == "claude" else "structuredOutput"
    envelope[field] = reply
    return subprocess.CompletedProcess(args, recorded.returncode, json.dumps(envelope), recorded.stderr)


class FixtureWorkspace(LiveWorkspace):
    def push(self, branch, repo):
        pass

    def pull_request_size(self, head: str, base_branch: str) -> PullRequestSize:
        return {"head": head, "merge_base": BASE, "lines": 1}

    def review_context(self, base, head):
        return SCRIPTED_REVIEW_CONTEXT

    def review_diff(self, base, head, *, interdiff=False):
        return ReviewDiff(f"git diff {base}{'..' if interdiff else '...'}{head}", DIFF)


class FakeGitHub:
    def __init__(self, labels=None):
        self.labels = labels if labels is not None else ["slice"]
        self.body = slice_body(EMPTY_SLICE) if "slice" in self.labels else "Issue body"
        self.created: list[tuple[str, str, list[str]]] = []
        self.comments: dict[tuple[str, int], list[str]] = {}
        self.closed = set()
        self.merged = set()
        self.label_calls = []
        self.issue_labels: dict[int, list[str]] = {}
        self.issue_repos: dict[int, str] = {}
        self.bodies: dict[tuple[str, int], str] = {}
        self.parents: dict[tuple[str, int], int] = {}
        self.failures: dict[tuple[str, bool], list[BaseException]] = {}
        self.merge_calls = []
        self.dependencies = {}
        self.base_branch = "main"
        self.reviews = []
        self.base = BASE
        self.hold: str | None = None
        self.hold_reads = []

    def default_branch(self, repo):
        return "main"

    def open_pull_request(self, *args):
        pass

    def pull_request_base(self, repo: str, branch: str) -> str:
        return self.base_branch

    def review_target(self, repo, issue, branch, sha):
        return PullRequest.model_validate({
            "number": 12, "state": "OPEN", "headRefOid": sha, "headRefName": branch,
            "baseRefName": "main", "baseRefOid": self.base, "autoMergeRequest": None,
        })

    def post_review(self, repo, target, body, comments, *, marker):
        self.reviews.append((repo, target.number, target.headRefOid, body, comments))
        return {"id": len(self.reviews), "state": "COMMENTED", "commit_id": target.headRefOid}
    def readiness(self, repo, issue, branch, sha):
        return Observation(repo=repo, pr=PullSnapshot(number=12, state="open", draft=False,
            head={"sha": sha, "ref": branch}, base={"sha": self.base, "ref": "main"},
            mergeable=True, merge_commit_sha=None, mergeable_state="clean"),
            checks=[], outcome="ready", recovery="Eligible.", queue_policy={"merge_method": "SQUASH"})

    def reconcile_merge(self, receipt):
        return receipt

    def merge_hold(self, repo):
        self.hold_reads.append(repo)
        return self.hold

    def arm_auto_merge(self, repo, issue, branch, sha, observation):
        self.merge_calls.append((repo, issue, branch, sha))
        return MergeReceipt(repo=repo, pr=12, source_head=sha, base=self.base,
                            base_ref="main", method="SQUASH", queue_policy={"merge_method": "SQUASH"}, identity="queue-test", status="QUEUED")

    def load_issue(self, repo, number) -> Issue:
        if number in self.issue_labels:
            title, body, _ = self.created[number - 101]
            return {"title": title, "body": self.bodies.get((repo, number), body), "labels": self.issue_labels[number]}
        return {"title": "Issue title", "body": self.bodies.get((repo, number), self.body), "labels": self.labels}

    def open_issues(self, repo) -> list[OpenIssue]:
        return [OpenIssue(number=number, body=self.bodies[repo, number])
                for number in self.issue_labels if self.issue_repos[number] == repo
                and number not in self.closed and number not in self.merged]

    def marked_issues(self, repo, marker) -> list[int]:
        numbers = dict.fromkeys([number for number, issue_repo in self.issue_repos.items() if issue_repo == repo]
                                + [number for issue_repo, number in self.bodies if issue_repo == repo])
        return [number for number in numbers if marker in self.load_issue(repo, number)["body"]]

    def _linked_issue(self, repo, number) -> LinkedIssue:
        return LinkedIssue(id=number + 10000, number=number, title=self.load_issue(repo, number)["title"],
                           repository_url=f"https://api.github.com/repos/{repo}",
                           html_url=f"https://github.com/{repo}/issues/{number}",
                           state="closed" if number in self.closed or number in self.merged else "open")

    def sub_issues(self, repo, number) -> list[LinkedIssue]:
        return [self._linked_issue(repo, child) for (issue_repo, child), parent in self.parents.items()
                if issue_repo == repo and parent == number]

    def parent_issue(self, repo, number) -> LinkedIssue | None:
        parent = self.parents.get((repo, number))
        return self._linked_issue(repo, parent) if parent is not None else None

    def preset_sub_issues(self, repo, number, count):
        start = max(19999, max((child for issue_repo, child in self.parents if issue_repo == repo), default=0)) + 1
        for child in range(start, start + count):
            self.parents[repo, child] = number

    def add_sub_issue(self, repo, number, child):
        self._fail("add_sub_issue", after_write=False)
        if (repo, child) in self.parents or len(self.sub_issues(repo, number)) >= 100:
            raise ForgeError("Child already has a parent or parent is full.")
        self.parents[repo, child] = number
        self._fail("add_sub_issue", after_write=True)

    def update_issue_body(self, repo, number, body):
        self._fail("update_issue_body", after_write=False)
        self.bodies[repo, number] = body
        self._fail("update_issue_body", after_write=True)

    def fail_next(self, operation, *, after_write=False, error=None):
        self.failures.setdefault((operation, after_write), []).append(
            error if error is not None else ForgeError("Injected write failure."))

    def _fail(self, operation, *, after_write):
        failures = self.failures.get((operation, after_write), [])
        if failures:
            raise failures.pop(0)

    def issue_comments(self, repo, number) -> list[str]:
        return list(self.comments.get((repo, number), []))

    def comment_issue(self, repo, number, body):
        self.comments.setdefault((repo, number), []).append(body)

    def create_issue(self, repo, title, body, labels, *, child=None):
        self._fail("create_issue", after_write=False)
        if child is not None:
            body = render_slice_body(body, child)
        self.created.append((title, body, labels))
        number = 100 + len(self.created)
        self.issue_labels[number] = list(labels)
        self.issue_repos[number] = repo
        self.bodies[repo, number] = body
        self._fail("create_issue", after_write=True)
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
                schema_paths = []

                def run(args, cwd, env):
                    self.assertEqual(args[:len(prefix)], prefix)
                    self.assertEqual(cwd, Path("engine"))
                    self.assertIn('{"issue": 12}', args[-1])
                    expected_env = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"} if args[0] == "claude" else {}
                    self.assertEqual(env, {**expected_env, "TMPDIR": env["TMPDIR"]})
                    self.assertTrue(Path(env["TMPDIR"]).is_dir())
                    self.assertNotIn("Output schema:", args[-1])
                    if args[0] == "codex":
                        self.assertIn("--json", args)
                        self.assertIn("--ephemeral", args)
                        schema_path = Path(args[args.index("--output-schema") + 1])
                        schema_paths.append(schema_path)
                        schema = json.loads(schema_path.read_text())
                    else:
                        schema = json.loads(args[args.index("--json-schema") + 1])
                    self.assertEqual(schema, STAGE_OUTPUTS["triage"].model_json_schema())
                    return worker_reply(args, {"triage": "implement"})

                result = CliWorker(Path("engine"), run)(
                    "triage", ModelSpec.parse(model), {"issue": 12}, "Read the issue.",
                )
                self.assertEqual(result.kind, "ran")
                self.assertEqual(result.output["triage"], "implement")
                for path in schema_paths:
                    self.assertFalse(path.exists())

    def test_each_family_can_use_scratch_outside_checkout(self):
        with TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            checkout.mkdir()
            paths = []
            for model in ("gpt-6.1-sol-high", "claude-opus-5-5-high", "grok-4.7-high"):
                with self.subTest(model=model):
                    def run(args, cwd, env):
                        scratch = Path(env["TMPDIR"])
                        paths.append(scratch)
                        self.assertTrue(scratch.is_absolute())
                        self.assertFalse(scratch.resolve().is_relative_to(checkout.resolve()))
                        self.assertEqual(list(scratch.iterdir()), [])
                        if args[0] == "codex":
                            schema_path = Path(args[args.index("--output-schema") + 1])
                            self.assertFalse(schema_path.resolve().is_relative_to(scratch.resolve()))
                            self.assertTrue(schema_path.is_file())
                        self.assertNotIn(str(scratch), args[-1])
                        process = run_worker_command([
                            sys.executable, "-c",
                            "import os, pathlib; "
                            "p = pathlib.Path(os.environ['TMPDIR']) / 'scratch.txt'; "
                            "p.write_text('worker scratch'); print(p.read_text())",
                        ], cwd, env)
                        self.assertEqual(process.returncode, 0, process.stderr)
                        self.assertEqual(process.stdout.strip(), "worker scratch")
                        return worker_reply(args, {"triage": "implement"})

                    result = CliWorker(checkout, run)("triage", ModelSpec.parse(model), {}, "Read the issue.")
                    self.assertEqual(result.kind, "ran")
                    self.assertFalse(paths[-1].exists())
            self.assertEqual(len(set(paths)), 3)
            self.assertEqual(list(checkout.iterdir()), [])

    def test_inherited_checkout_tmpdir_is_overridden_without_changing_environment(self):
        script = """
import os
from pathlib import Path
import subprocess
import tempfile
from foreman.config import ModelSpec
from foreman.worker import CliWorker

checkout = Path.cwd().resolve()
assert Path(tempfile.gettempdir()).resolve().is_relative_to(checkout)
inherited = dict(os.environ)
paths = []
def run(args, cwd, env):
    scratch = Path(env["TMPDIR"])
    assert not scratch.resolve().is_relative_to(checkout)
    assert list(scratch.iterdir()) == []
    paths.append(scratch)
    return subprocess.CompletedProcess(args, 1, "", "")
CliWorker(checkout, run)("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "Read.")
assert paths and all(not path.parent.exists() for path in paths)
assert dict(os.environ) == inherited
"""
        with TemporaryDirectory() as root:
            checkout = Path(root) / "checkout"
            checkout.mkdir()
            inherited_temp = checkout / "temp"
            inherited_temp.mkdir()
            env = {**os.environ, "TMPDIR": str(inherited_temp),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            process = subprocess.run([sys.executable, "-c", script], cwd=checkout,
                                     env=env, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(list(checkout.iterdir()), [inherited_temp])

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
        replies = (
            ('Here is the result: {"triage": "implement"}', "did not return valid JSON"),
            ('["implement"]', "JSON object"),
        )
        for reply, detail in replies:
            with self.subTest(reply=reply):
                worker = CliWorker(Path("."), lambda args, cwd, env: worker_reply(args, reply))
                outcome = worker("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "")
                self.assertEqual(outcome.kind, "failed")
                self.assertIn(detail, outcome.detail)

    def walk_triage(self, models, run):
        config = load_config()
        config.nodes["triage"] = NodeConfig("coordinator", tuple(ModelSpec.parse(model) for model in models))
        with skill_fixture(config) as (fixture_config, scratch):
            return run_delivery(CliWorker(Path("."), run), repo="Example/Engine",
                                request="List items", check="exit 0", config=fixture_config, slice=EMPTY_SLICE, scratch=scratch)

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
                    expected = [case, "ran"] if stop == "needs_human" else ["failed"]
                    self.assertEqual([item["outcome"] for item in result["attempts"]], expected)
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

    def test_codex_cli_missing_removes_the_schema_file(self):
        paths = []

        def run(args, cwd, env):
            path = Path(args[args.index("--output-schema") + 1])
            self.assertTrue(path.is_file())
            paths.append(path)
            raise FileNotFoundError(args[0])

        outcome = CliWorker(Path("."), run)("triage", ModelSpec.parse("gpt-6.1-sol-high"), {}, "")
        self.assertEqual((outcome.kind, outcome.detail), ("cli_missing", "codex executable is unavailable"))
        self.assertFalse(paths[0].exists())


class StageOutputTest(unittest.TestCase):
    def test_cli_rejects_invalid_fact_record_before_running(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["run", "delivery", "--repo", "Example/Engine", "--fact-record", "../facts.toml"])
        self.assertEqual(raised.exception.code, 2)

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
            "implementation": "Done", "formal_proof": None,
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
                    "browser_proof": None,
                    "formal_proof": {"ran": None, "typecheck": None,
                                     "constraints": [{"constraint": "unique_item_id", **fields}]},
                }
                with self.assertRaisesRegex(InvalidOutput, r"^formal_proof\.constraints\.0\.rejected:"):
                    parse_output(ImplementOutput, raw)

    def test_implementation_rejects_worker_head(self):
        with self.assertRaisesRegex(InvalidOutput, r"^head_sha:"):
            parse_output(ImplementOutput, {"implementation": "Done", "head_sha": HEAD, "browser_proof": None, "formal_proof": None})

    def test_hedged_approval_is_invalid(self):
        with self.assertRaisesRegex(InvalidOutput, "^review:"):
            parse_output(ReviewOutput, {**APPROVE, "review": "approve, but please fix X first"}, context=SCRIPTED_REVIEW_CONTEXT)

    def test_review_requires_an_exact_verdict(self):
        for verdict in ("Approve.", "approve.", "changes.", "changes. Add a check."):
            with self.subTest(verdict=verdict), self.assertRaisesRegex(InvalidOutput, "^review:"):
                parse_output(ReviewOutput, {**APPROVE, "review": verdict}, context=SCRIPTED_REVIEW_CONTEXT)

    def test_review_summary_is_limited_to_200_characters(self):
        self.assertEqual(ReviewOutput.model_json_schema()["$defs"]["ReviewFinding"]["properties"]["summary"]["maxLength"], 200)
        raw = {**APPROVE, "findings": [{**FINDING, "summary": "a" * 201}]}
        with self.assertRaisesRegex(InvalidOutput, r"^findings\.0\.summary:"):
            parse_output(ReviewOutput, raw, context=SCRIPTED_REVIEW_CONTEXT)


class LiveRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(TemporaryDirectory()))
        cls.pin = fixture_pin(root / "fixture")

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.cwd = Path(directory)
        self.checkpoint = self.cwd / "runs.sqlite3"
        self.calls = []
        self.prompts = []
        self.github = FakeGitHub()
        self.enterContext(patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=self.pin)))

    def walk(self, replies, *, issue=8, check="exit 0", preflight="", answer=None, snapshots=(CLEAN_WORKTREE,), workspace: Workspace | None = None, skills=export_skills, presentation=None):
        outputs = iter(replies)
        active = workspace if workspace is not None else FakeWorkspace(self.cwd, snapshots)

        def run(args, cwd, env):
            self.assertEqual(cwd, self.cwd)
            self.calls.append(args[0])
            self.prompts.append(args[-1])
            reply = next(outputs)
            return active.reply(args, reply) if isinstance(active, FakeWorkspace) else worker_reply(args, reply)

        return run_live_delivery(
            repo="Example/Engine", issue=issue, check=check, preflight=preflight, answer=answer,
            checkpoint=self.checkpoint, dispatcher=CliWorker(self.cwd, run), presentation=presentation,
            run=LiveRun(self.github, active, skills=skills),
        )

    def test_readiness_halts_resume_without_repeating_workers(self):
        for outcome, reason in [("pending", "ci_pending"), ("red", "ci_red"), ("conflict", "pr_conflict")]:
            self.checkpoint = self.cwd / (outcome + ".sqlite3")
            observed = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
            observed.outcome = outcome
            observed.recovery = "Repair or wait, then resume."
            with patch.object(self.github, "readiness", return_value=observed):
                result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
            self.assertEqual(result["stop"], reason)
            self.assertIsInstance(result["readiness_observation"], dict)
            self.assertIn("PR #12 head=", result["halt_detail"])
            self.assertIn("base=main@", result["halt_detail"])
            self.assertEqual(run_exit_code(result), 75)
            count = len(self.calls)
            resumed = self.walk([])
            self.assertEqual(resumed["stop"], "verified")
            self.assertEqual(len(self.calls), count)
            self.assertEqual(resumed["review_receipt"], result["review_receipt"])

    def test_resume_rejects_historical_schema_before_missing_usage(self):
        self.github.hold = "Maintenance"
        result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["stop"], "merge_held")
        config = {"configurable": {"thread_id": "example/engine#8"}}
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            historical = next(item for item in saver.list(config)
                              if item.checkpoint["channel_values"].get("path", [])[-1:] == ["review"])
            historical.checkpoint["channel_values"].pop("schema_version")
            historical.checkpoint["channel_values"].pop("token_cap")
            saver.put(historical.config, historical.checkpoint, historical.metadata, {})
        with self.assertRaises(CheckpointVersionError) as raised:
            self.walk([])
        self.assertIn(str(self.checkpoint), str(raised.exception))
        self.assertIn("schema_version missing", str(raised.exception))

    def test_queue_receipt_is_persisted_and_completion_does_not_reobserve(self):
        result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        receipt = result["merge_receipt"]
        self.assertEqual((receipt["source_head"], receipt["identity"]), (HEAD, "queue-test"))
        with patch.object(self.github, "readiness", side_effect=AssertionError("completed run reobserved")):
            resumed = self.walk([])
        self.assertEqual(resumed["merge_receipt"], result["merge_receipt"])

    def test_saved_receipt_reconciles_before_readiness_without_reenqueue(self):
        for status in ["missing", "merged"]:
            self.checkpoint = self.cwd / (status + ".sqlite3")
            ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
            with patch.object(self.github, "readiness", side_effect=[ready, ForgeError("interrupted readback")]):
                first = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
            count = len(self.calls)
            receipt = MergeReceipt.model_validate(first["merge_receipt"])
            effect = QueueRejected(f"Queue receipt {receipt.identity} disappeared") if status == "missing" else None
            with patch.object(self.github, "readiness", side_effect=AssertionError("readiness ran before receipt")), patch.object(self.github, "arm_auto_merge", side_effect=AssertionError("re-enqueued")), patch.object(self.github, "reconcile_merge", side_effect=effect, return_value=receipt.model_copy(update={"status": "MERGED"})) as read:
                result = self.walk([])
            read.assert_called_once_with(receipt)
            self.assertEqual(len(self.calls), count)
            self.assertEqual(result["stop"], "queue_rejected" if effect else "verified")
            if effect:
                self.assertEqual(run_exit_code(result), 75)
                self.assertIn(receipt.identity, result["halt_detail"])

    def test_changed_saved_handoff_invalidates_receipt_for_renewed_proof(self):
        ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        with patch.object(self.github, "readiness", side_effect=[ready, ForgeError("interrupted readback")]):
            self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        with patch.object(self.github, "reconcile_merge", side_effect=HandoffChanged("PR head changed; resume to renew proof")):
            result = self.walk([])
        self.assertEqual(result["stop"], "merge_not_armed")
        self.assertIsNone(result["merge_receipt"])
        with patch.object(self.github, "reconcile_merge", side_effect=AssertionError("stale receipt resurrected")):
            self.assertEqual(self.walk([])["stop"], "verified")

    def test_fresh_process_reconciles_crash_after_write_or_merge(self):
        script = r"""
import json, os, sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from foreman.cli import main
from foreman.config import PstackPin, load_config
from foreman.delivery import LiveRun
from foreman.github import LiveForge
from foreman.readiness import QueueSnapshot
from test_runner import APPROVE, BASE, BRANCH, HEAD, FakeGitHub, FakeWorkspace, worker_reply

class Forge(FakeGitHub):
    arm_auto_merge = LiveForge.arm_auto_merge
    _arm_auto_merge = LiveForge._arm_auto_merge
    reconcile_merge = LiveForge.reconcile_merge
    def readiness(self, *args):
        value = super().readiness(*args)
        if Path("merges.log").exists() and sys.argv[3] == "merged":
            value.pr.state, value.pr.merged = "closed", True
        return value
    def _queue_snapshot(self, repo, number):
        written = Path("merges.log").exists()
        return QueueSnapshot(number=12, state="MERGED" if written and sys.argv[3] == "merged" else "OPEN",
            headRefOid=HEAD, baseRefOid=BASE, baseRefName="main", isDraft=False, autoMergeRequest=None,
            mergeQueueEntry={"id": "queue-crash", "state": "QUEUED"} if written else None)
    def _run_command(self, repo, command):
        assert command[1:3] == ["pr", "merge"]
        with Path("merges.log").open("a") as log:
            log.write("merge\n")
        os._exit(17)

workspace = FakeWorkspace(Path.cwd())
def worker(args, cwd, env):
    schema = json.loads(Path(args[args.index("--output-schema")+1]).read_text()) if args[0] == "codex" else json.loads(args[args.index("--json-schema")+1])
    with Path("workers.log").open("a") as log:
        log.write(schema["title"] + "\n")
    if schema["title"] == "TriageOutput":
        return worker_reply(args, {"triage": "implement"})
    if schema["title"] == "ImplementOutput":
        return workspace.reply(args, {"implementation": "built"})
    return worker_reply(args, APPROVE)
pin = PstackPin.model_validate_json(sys.argv[2])
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin)):
    sys.exit(main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                  checkpoint=Path(sys.argv[1]), worker_runner=worker, live_run=LiveRun(Forge(), workspace)))
"""
        bootstrap = f"import sys; sys.path[:0] = {[str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]!r}\n"
        for mode in ["queued", "merged"]:
            directory = self.cwd / mode
            directory.mkdir()
            command = [sys.executable, "-c", bootstrap + script, str(directory / "checkpoint.sqlite3"), self.pin.model_dump_json(), mode]
            first = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            self.assertEqual(first.returncode, 17, first.stderr)
            resumed = subprocess.run(command, cwd=directory, capture_output=True, text=True)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("done verified", resumed.stdout)
            self.assertEqual((directory / "merges.log").read_text(), "merge\n")
            self.assertEqual((directory / "workers.log").read_text().splitlines(), ["TriageOutput", "ImplementOutput", "ReviewOutput"])

    def test_readiness_is_reconciled_after_arming(self):
        ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        for outcome, halt in [("conflict", "pr_conflict"), ("red", "ci_red"), ("pending", "ci_pending")]:
            self.checkpoint = self.cwd / (outcome + "-reconcile.sqlite3")
            changed = ready.model_copy(update={"outcome": outcome})
            with patch.object(self.github, "readiness", side_effect=[ready, changed]), patch.object(self.github, "arm_auto_merge", wraps=self.github.arm_auto_merge) as arm:
                result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
            arm.assert_called_once()
            self.assertEqual((result["stop"], result["merge"], run_exit_code(result)), (halt, "armed", 75))
        self.checkpoint = self.cwd / "advanced-base.sqlite3"
        changed = ready.model_copy(deep=True)
        changed.pr.base.sha = "c" * 40
        changed.pr.merge_commit_sha = "d" * 40
        with patch.object(self.github, "readiness", side_effect=[ready, changed]):
            self.assertEqual(self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])["stop"], "verified")

    def test_unavailable_and_policy_outcomes_fail_closed(self):
        ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        for index, sequence in enumerate([[ForgeError("HTTP 503")], [ready, ForgeError("HTTP 503")],
                                          [ready.model_copy(update={"outcome": "policy"})]]):
            self.checkpoint = self.cwd / f"unavailable-{index}.sqlite3"
            with patch.object(self.github, "readiness", side_effect=sequence):
                result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
            self.assertEqual((result["stop"], run_exit_code(result)), ("readiness_unavailable", 75))
            self.assertEqual(result["merge"], "armed" if index == 1 else "not_armed")

    def test_receipt_and_ledger_guard_prevent_arming(self):
        from foreman.review_round import ReviewHistory
        for kind in ["receipt", "ledger"]:
            self.checkpoint = self.cwd / (kind + ".sqlite3")
            def posted(*args, **kwargs):
                if kind == "ledger":
                    self.enterContext(patch.object(ReviewHistory, "unresolved", return_value=[FINDING]))
                return {"id": 1, "state": "COMMENTED", "commit_id": "d"*40 if kind == "receipt" else HEAD}
            with patch.object(self.github, "post_review", side_effect=posted), patch.object(self.github, "arm_auto_merge", wraps=self.github.arm_auto_merge) as arm:
                result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
            self.assertEqual(result["stop"], "merge_not_armed")
            self.assertIn("Renew review", result["halt_detail"])
            arm.assert_not_called()

    def test_remote_only_repair_fetches_and_renews_proof(self):
        workspace = FakeWorkspace(self.cwd)
        ready = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        with patch.object(self.github, "readiness", return_value=ready.model_copy(update={"outcome": "conflict"})):
            self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE], workspace=workspace)
        remote = ready.model_copy(deep=True)
        remote.outcome, remote.pr.head.sha = "head_changed", "d"*40
        observe = self.github.readiness
        def refreshed(branch, head):
            workspace.current = WorktreeState(head, True, branch)
        with patch.object(self.github, "readiness", side_effect=lambda repo, issue, branch, sha: remote if sha == HEAD else observe(repo, issue, branch, sha)), patch.object(workspace, "refresh", side_effect=refreshed) as fetch, patch.object(workspace, "commit", side_effect=lambda message: refreshed(BRANCH, "d"*40)):
            result = self.walk([{"implementation": "renewed"}, APPROVE], workspace=workspace)
        fetch.assert_called_once_with(BRANCH, "d"*40)
        self.assertEqual(result["stop"], "verified", result)
        self.assertEqual((result["verified_head"], result["reviewed_head"], result["review_receipt"]["commit_id"]), ("d"*40,)*3)

    def test_external_conflict_repair_renews_entire_lifecycle(self):
        workspace = FakeWorkspace(self.cwd)
        conflict = self.github.readiness("Example/Engine", 8, BRANCH, HEAD)
        conflict.outcome = "conflict"
        with patch.object(self.github, "readiness", return_value=conflict):
            result = self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE], workspace=workspace)
        self.assertEqual(result["stop"], "pr_conflict")
        repaired = "d" * 40
        workspace.current = WorktreeState(repaired, True, BRANCH)
        def commit(message):
            workspace.current = WorktreeState(repaired, True, BRANCH)
        with patch.object(workspace, "commit", side_effect=commit):
            resumed = self.walk([{"implementation": "renewed"}, APPROVE], workspace=workspace)
        self.assertEqual(resumed["stop"], "verified")
        self.assertEqual(resumed["reviewed_head"], repaired)
        self.assertEqual(resumed["verified_head"], repaired)
        self.assertEqual(resumed["review_receipt"]["commit_id"], repaired)
        self.assertEqual(len(self.github.reviews), 2)

    def test_findings_and_change_kind_reach_the_next_implement_prompt(self):
        built = {"implementation": "Built."}
        changes = dict(APPROVE)
        for severity, kind in (("nit", "nits"), ("blocking", "blocking")):
            with self.subTest(severity=severity):
                self.checkpoint = self.cwd / f"{severity}.sqlite3"
                finding = {**FINDING, "severity": severity}
                self.walk([{"triage": "implement"}, built, {**changes, "findings": [finding]}, built, {**APPROVE, "resolutions": [{"id": finding["id"], "status": "resolved"}]}])
                state = json.loads(self.prompts[-2].split("Delivery state:\n")[1])
                self.assertEqual(state["findings"], [finding])
                self.assertEqual(state["detail"], ReviewOutput.model_validate(changes, context=SCRIPTED_REVIEW_CONTEXT).risk.render())
                self.assertEqual(state["review_change_kind"], kind)

    def test_review_posts_only_changed_lines_inline(self):
        findings = [
            FINDING,
            {**FINDING, "id": "f2", "line": 1, "summary": "Untouched context."},
            {**FINDING, "id": "f3", "file": "README.md", "line": 1, "summary": "Unchanged dependency."},
        ]
        built = {"implementation": "Built."}
        changes = {**APPROVE, "findings": findings}
        self.walk([{"triage": "implement"}, built, changes, built, {**APPROVE, "resolutions": [{"id": f["id"], "status": "resolved"} for f in findings]}])
        self.assertIn(f"git diff {BASE}...{HEAD}", self.prompts[2])
        selection = '\n\nReviewer claude-opus-5-5-high; implemented by gpt-6.1-sol-high; same-family fallback: not needed.'
        self.assertEqual(self.github.reviews, [(
            "Example/Engine", 12, HEAD,
            "Graph verdict: changes. Review event: COMMENT (Foreman authors this PR).\n\n" + ReviewOutput.model_validate(APPROVE, context=SCRIPTED_REVIEW_CONTEXT).risk.render() + selection + "\n\nitems.txt:1 [blocking] Untouched context.\n\nREADME.md:1 [blocking] Unchanged dependency.",
            [{"path": "items.txt", "line": 2, "side": "RIGHT", "body": "[blocking] Add a check."}],
        ), ("Example/Engine", 12, HEAD, "Graph verdict: approve. Review event: COMMENT (Foreman authors this PR).\n\n" + ReviewOutput.model_validate(APPROVE, context=SCRIPTED_REVIEW_CONTEXT).risk.render() + selection, [])])

    def test_review_failures_halt_without_merging(self):
        changes = {**APPROVE, "findings": [FINDING]}
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

    def test_posting_retry_preserves_round_and_does_not_dispatch(self):
        with patch.object(self.github, "post_review", side_effect=ForgeError("Unavailable")):
            stopped = self.walk([{"triage": "implement"}, {"implementation": "Built."}, APPROVE])
        self.assertEqual(stopped["halt"], "review_not_posted")
        self.assertEqual(stopped["review_rounds"], 1)
        calls = self.calls.copy()
        result = self.walk([])
        self.assertEqual(self.calls, calls)
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(len(result["review_findings"]), 1)
        self.assertEqual(result["review_receipt"]["state"], "COMMENTED")
        self.assertEqual(result["merge"], "armed")

    def test_review_halts_when_git_fetch_times_out(self):
        real_run = subprocess.run
        def fetch(command, **kwargs):
            if command[:2] == ["git", "fetch"]:
                raise subprocess.TimeoutExpired(command, 30)
            return real_run(command, **kwargs)

        workspace = FakeWorkspace(self.cwd)
        workspace.review_diff = lambda base, head: LiveWorkspace(self.cwd).review_diff(base, head)
        with patch("foreman.workspace.subprocess.run", side_effect=fetch):
            result = self.walk([{"triage": "implement"}, {"implementation": "Built."}], workspace=workspace)
        self.assertEqual(result.get("stop"), "review_unavailable")
        self.assertRegex(result.get("halt_detail", ""), r"^git fetch timed out after 30 seconds")
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

    def test_missing_inactive_method_halts_the_cli_before_loading_the_issue(self):
        pin = fixture_pin(self.cwd / "incomplete")
        source = Path(pin.repository)
        absent = "benchmark-checklist/SKILL.md"
        (source / "pstack" / "skills" / absent).unlink()
        git(source, "add", "--", "pstack")
        git(source, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
            "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Synthetic skill fixture")
        commit = git(source, "rev-parse", "HEAD")
        config = replace(load_config(), pstack=pin.model_copy(update={"commit": commit}))
        worker = Mock()
        with (
            patch("foreman.runner.load_config", return_value=config),
            patch.object(self.github, "load_issue") as load_issue,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                worker_runner=worker, checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 1)
        self.assertIn("done skill_missing", stdout.getvalue())
        self.assertIn(f"method missing at pin ({absent})", stdout.getvalue())
        self.assertIn(commit, stdout.getvalue())
        load_issue.assert_not_called()
        worker.assert_not_called()

    def test_empty_or_truncated_method_halts_before_dispatch(self):
        for method, content, detail in (
            ("poteto-mode/SKILL.md", "", "method is empty"),
            ("poteto-mode/SKILL.md", "Synthetic method.\n" * 109, "requires lines 110 through 110, has 109"),
            ("why/SKILL.md", "", "method is empty"),
        ):
            with self.subTest(method=method, detail=detail), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                pin = fixture_pin(Path(directory) / "fixture")
                source = Path(pin.repository)
                (source / "pstack" / "skills" / method).write_text(content, encoding="utf-8")
                git(source, "add", "--", "pstack")
                git(source, "-c", "user.name=Foreman", "-c", "user.email=foreman@example.invalid",
                    "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "Synthetic invalid method")
                commit = git(source, "rev-parse", "HEAD")
                config = replace(load_config(), pstack=pin.model_copy(update={"commit": commit}))
                with patch("foreman.runner.load_config", return_value=config), patch.object(self.github, "load_issue") as load_issue:
                    result = self.walk([])
                self.assertEqual(result.get("stop", ""), "skill_missing")
                self.assertEqual(run_exit_code(result), 1)
                self.assertEqual(result.get("skill_reason", ""), "method_missing")
                self.assertIn(method, result.get("halt_detail", ""))
                self.assertIn(commit, result.get("halt_detail", ""))
                self.assertIn(detail, result.get("halt_detail", ""))
                self.assertEqual(self.calls, [])
                load_issue.assert_not_called()

    def test_unavailable_pin_has_a_distinct_reason(self):
        commit = "f" * 40
        pin = self.pin.model_copy(update={"commit": commit})
        with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin)):
            result = self.walk([])
        self.assertEqual(result["stop"], "skill_missing")
        self.assertEqual(run_exit_code(result), 1)
        self.assertEqual(result["skill_reason"], "pin_unavailable")
        self.assertEqual(result["halt_detail"], f"pstack pin unavailable, commit {commit}")
        self.assertEqual(self.calls, [])

    def test_quota_pause_returns_the_resumable_cli_exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                worker_runner=lambda args, cwd, env: fixture_process(args, "quota", 1),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 75)
        self.assertIn("paused quota_exhausted", stdout.getvalue())
        self.assertIn("triage quota exhausted", stdout.getvalue())

    def test_resume_passes_checkpoint_pin_to_the_run_skill_source(self):
        source = FixtureSkills()
        paused = self.walk([{"triage": "needs_human", "human_question": "Which items?"}], skills=source)
        self.assertEqual(paused["stop"], "needs_human")
        changed = self.pin.model_copy(update={"commit": "f" * 40})
        with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=changed)):
            resumed = self.walk([{"triage": "question"}, {"research": "Only open items."}],
                                answer="Only open items", skills=source)
        self.assertEqual(resumed["stop"], "answered")
        self.assertEqual(source.pins, [self.pin, self.pin])

    def test_resume_uses_saved_custom_fact_record_when_omitted(self):
        run = LiveRun(self.github, FakeWorkspace(self.cwd))
        first = run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", fact_record="evidence/facts.toml",
            checkpoint=self.checkpoint, run=run,
            dispatcher=lambda *args: Outcome("quota"),
        )
        self.assertEqual(first["fact_record"], "evidence/facts.toml")
        resumed = run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint, run=run,
            dispatcher=lambda *args: Outcome("quota"),
        )
        self.assertEqual(resumed["fact_record"], "evidence/facts.toml")

    def test_resume_rejects_a_different_fact_record(self):
        self.walk([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        with self.assertRaisesRegex(ValueError, "--fact-record differs"):
            run_live_delivery(repo="Example/Engine", issue=8, check="exit 0",
                              fact_record="evidence/other.toml", checkpoint=self.checkpoint, dispatcher=ScriptHandler(),
                              run=LiveRun(self.github, FakeWorkspace(self.cwd)))
        self.assertEqual(self.walk([])["stop"], "verified")

    def test_resume_allocates_fresh_scratch_without_checkpointing_paths(self):
        paths = []

        def quota(args, cwd, env):
            scratch = Path(env["TMPDIR"])
            self.assertFalse(scratch.resolve().is_relative_to(self.cwd.resolve()))
            self.assertNotIn(scratch, paths)
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertTrue(all(not path.exists() for path in paths))
            (scratch / "scratch.txt").write_text("discard on quota")
            paths.append(scratch)
            return fixture_process(args, "quota", 1)

        paused = run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
            dispatcher=CliWorker(self.cwd, quota), run=LiveRun(self.github, FakeWorkspace(self.cwd)),
        )
        self.assertEqual(paused["stop"], "quota_exhausted")
        self.assertTrue(all(not path.exists() for path in paths))
        before_resume = len(paths)
        self.assertGreater(before_resume, 1)
        replies = iter([{"triage": "question"}, {"research": "Only open items."}])

        def answer(args, cwd, env):
            quota(args, cwd, env)
            return worker_reply(args, next(replies))

        resumed = run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
            dispatcher=CliWorker(self.cwd, answer), run=LiveRun(self.github, FakeWorkspace(self.cwd)),
        )
        self.assertEqual(resumed["stop"], "answered")
        self.assertGreater(len(paths), before_resume)
        self.assertTrue(all(not path.exists() for path in paths))
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            checkpoints = repr(list(saver.list({"configurable": {"thread_id": "example/engine#8"}})))
        for path in paths:
            self.assertNotIn(str(path), checkpoints)

    def test_quota_pause_keeps_attempts_and_resumes_the_same_stage(self):
        def exhaust(node, model, state, instruction):
            if node == "triage":
                return Outcome("ran", {"triage": "implement", "work_kind": "feature", "human_question": None, "slice": None}, usage=Usage(input_tokens=1, output_tokens=1))
            return Outcome("quota")

        paused = run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
            dispatcher=exhaust, run=LiveRun(self.github, FakeWorkspace(self.cwd)),
        )
        self.assertEqual(paused["stop"], "quota_exhausted")
        self.assertEqual(run_exit_code(paused), 75)
        self.assertNotIn("done", paused["path"])
        self.assertEqual(paused["pstack_pin"], self.pin.model_dump())
        attempts = paused["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["ran"] + ["quota"] * 4)
        self.assertTrue(all(a["stage"] == "implement" and a["commit"] == self.pin.commit for a in attempts[1:]))

        resumed = self.walk([{"implementation": "built"}, APPROVE])
        self.assertEqual(resumed["stop"], "verified")
        self.assertEqual(resumed["path"].count("triage"), 1)
        self.assertEqual(resumed["attempts"][:len(attempts)], attempts)
        self.assertEqual([a["stage"] for a in resumed["attempts"][len(attempts):]], ["implement", "review"])

    def test_load_parent_and_file_slices_only_once(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "spec", "work_kind": "bug_fix"}, {
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
            stored = json.loads(encoded.removesuffix("\n```"))
            self.assertEqual(stored.pop("provenance"), {"author": result["spec_candidate"]["author"], "revision": result["spec_candidate"]["revision"]})
            self.assertEqual(stored, {**item, "work_kind": "bug_fix"})
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
        self.assertEqual(stdout.getvalue(), "tokens 0 of 20000000\ndone blocked\nOpen blockers: https://github.com/Example/Engine/issues/101\n")
        self.github.closed.add(101)
        self.assertIn("no merged pull request", self.walk([], issue=102)["halt_detail"])
        self.github.merged.add(101)
        self.walk([])
        self.walk([])
        self.assertEqual(self.github.dependencies, {102: [101]})
        self.assertEqual(self.github.label_calls, [("Example/Engine", 102, "factory")])
        child = self.walk([{"triage": "question"}, {"research": "answer"}], issue=102)
        self.assertEqual(child["slice"], {**child_payload, "provenance": {"author": result["spec_candidate"]["author"], "revision": result["spec_candidate"]["revision"]}})
        self.assertEqual(child["work_kind"], "bug_fix")
        self.assertIn("why/SKILL.md", self.prompts[-1])

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
        self.assertEqual([labels for _, _, labels in self.github.created if "slice" in labels], [])

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
        self.assertEqual(result["review_rounds"], 0)
        self.assertEqual(result["spec_failures"], 3)
        self.assertEqual(result["spec_rejection"]["kind"], "dangling_seam")
        self.assertEqual([labels for _, _, labels in self.github.created if "slice" in labels], [])

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
            [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
            check="test -f expected",
        )
        self.assertEqual(self.calls, ["claude", "codex", "claude"])
        self.assertEqual(result["path"], ["triage", "implement", "publish", "verify", "review", "merge", "done"])
        self.assertEqual(result["check_exit_code"], 0)
        self.assertEqual(result["verified_head"], HEAD)
        self.assertEqual(result["head_sha"], HEAD)
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(result["merge"], "armed")

    def review_tool_fact(self, fixture: str | None, *, entry: str | None = None, record_during_review: bool = False, recover: bool = False, no_changes: bool = False, record_path: str = "tests/fixtures/manifest.toml", paraphrase: bool = False, worker_commits: bool = False, residual: bool = False):
        root = self.cwd / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", BRANCH], cwd=root, check=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.cwd / "remote.git")], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.cwd / "remote.git")], cwd=root, check=True)
        fixtures = (root / record_path).parent
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

        manifest_path = root / record_path
        if not (recover or no_changes):
            manifest_path.write_text(manifest(entry) if entry is not None else "[fixtures]\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgSign=false",
             "commit", "-qm", "Add fixture"],
            cwd=root, check=True,
        )
        subprocess.run(["git", "branch", "main"], cwd=root, check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Synthetic Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "synthetic@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)
        self.github.base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        built = {"implementation": "Built.", "browser_proof": BROWSER, "formal_proof": FORMAL}
        review = {
            **APPROVE,
            "tool_facts": [{"claim": "The CLI accepts --debug.", "fixture": fixture}],
        }
        replies = iter([{"triage": "implement"}] + [built, review] * 3)

        implementations = 0

        def worker(args, cwd, env):
            nonlocal implementations
            reply = next(replies)
            if "implementation" in reply:
                implementations += 1
                if implementations > 1 and (recover or no_changes):
                    if recover:
                        self.assertIn("captured_run", args[-1])
                        self.assertIn("quoted_source", args[-1])
                        self.assertIn("The CLI accepts --debug.", args[-1])
                        worker_state = json.loads(args[-1].split("Delivery state:\n", 1)[1])
                        self.assertNotIn("tool_facts", worker_state)
                        claims = carried_tool_facts(worker_state)
                        self.assertIn("Carried tool facts:\n" + json.dumps([fact.model_dump() for fact in claims]), args[-1])
                        self.assertIn("claim_ids", args[-1])
                        self.assertIn("Foreman owns commits and publishing", args[-1])
                        self.assertIn(record_path, args[-1])
                        manifest_path.write_text(manifest("result.txt") + f'claim_ids = {json.dumps([] if residual else [f.claim_id for f in claims])}\n# round {implementations}\n')
                        if worker_commits:
                            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
                            subprocess.run(["git", "commit", "-qm", "Record evidence\n\nCo-Authored-By: gpt-6.1-sol-high <noreply@openai.com>"], cwd=root, check=True)
                    return worker_reply(args, reply)
                file = root / "items.txt"
                file.write_text((file.read_text() if file.exists() else "") + "synthetic edit\n")
            if paraphrase and implementations > 1 and "tool_facts" in reply:
                reply = {**reply, "tool_facts": [{"claim": "The debug flag is accepted.", "fixture": "result.txt"}]}
            if record_during_review and "tool_facts" in reply:
                manifest_path.write_text(manifest("result.txt"))
            return worker_reply(args, reply)

        return run_live_delivery(
            repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
            **({"fact_record": record_path} if record_path != "tests/fixtures/manifest.toml" else {}),
            dispatcher=CliWorker(root, worker),
            run=LiveRun(self.github, FixtureWorkspace(root)),
        )

    def test_fact_repair_adopts_worker_commits_and_rechecks_exact_head(self):
        for committed, record, residual in ((False, "tests/fixtures/manifest.toml", False),
                                             (True, "tests/fixtures/manifest.toml", False),
                                             (True, "evidence/facts.toml", False),
                                             (True, "evidence/facts.toml", True)):
            with self.subTest(committed=committed, record=record, residual=residual), TemporaryDirectory() as directory:
                self.cwd = Path(directory)
                self.checkpoint = self.cwd / "runs.sqlite3"
                self.github.body = slice_body(SLICE)
                result = self.review_tool_fact(None, recover=True, record_path=record,
                                               worker_commits=committed, residual=residual)
                self.assertEqual(result["stop"], "review_cap" if residual else "verified")
                rounds = result["review_findings"]
                self.assertFalse(rounds[0]["graph_check"]["passed"])
                self.assertEqual(rounds[-1]["graph_check"]["passed"], not residual)
                self.assertIn(rounds[0]["reviewed_head"], rounds[-1]["graph_check"]["failing_heads"])
                if not residual:
                    self.assertEqual(result["path"].count("implement"), 2)
                    self.assertEqual(result["path"].count("verify"), 2)
                    self.assertEqual(result["path"].count("review"), 2)
                    self.assertEqual(result["path"].count("proof"), 2)
                    self.assertEqual(result["pr_size"]["head"], result["verified_head"])
                    self.assertEqual(rounds[-1]["reviewed_head"], result["verified_head"])
                    self.assertEqual(self.github.merge_calls[-1][-1], result["verified_head"])
                    self.assertEqual(result["implementation_candidate"]["disposition"], "adopted" if committed else "edits")

    def test_missing_manifest_can_be_recorded_by_implement(self):
        result = self.review_tool_fact("result.txt", recover=True)
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual(result.get("merge"), "armed")
        self.assertEqual(result.get("fact_record"), "tests/fixtures/manifest.toml")
        self.assertNotIn("tool_facts", result)

    def test_null_claim_resolves_by_id_when_reviewer_paraphrases(self):
        result = self.review_tool_fact(None, recover=True, paraphrase=True)
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual(result.get("merge"), "armed")
        self.assertEqual(result["path"].count("review"), 2)

    def test_custom_fact_record_reaches_approval(self):
        result = self.review_tool_fact("result.txt", recover=True, record_path="evidence/facts.toml")
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual(result.get("fact_record"), "evidence/facts.toml")

    def test_no_changes_names_unresolved_fact_gate(self):
        result = self.review_tool_fact(None, no_changes=True)
        self.assertIn("tool-facts", result.get("halt_detail", ""))

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
        self.assertEqual(result.get("head_sha"), result.get("verified_head"))
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
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "sh verify.sh"],
                worker_runner=lambda args, cwd, env: fake.reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, fake),
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
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])

        def worker(args, cwd, env):
            self.assertEqual(cwd, root)
            return live_run.workspace.reply(args, next(replies))

        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "touch checked"],
                worker_runner=worker, checkpoint=self.checkpoint, live_run=live_run,
            )
        self.assertEqual(code, 0)
        self.assertTrue((root / "checked").exists())
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        rerun = {"repo": "Example/Engine", "issue": 8, "check": "exit 0", "checkpoint": self.checkpoint, "dispatcher": ScriptHandler()}
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
            {"implementation": "built", "browser_proof": BROWSER, "formal_proof": FORMAL},
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
        self.assertEqual(result["path"], ["triage", "implement", "publish", "verify", "proof", "done"])
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
                self.assertTrue(result["halt_detail"].startswith("triage output from claude-opus-5-5-high: slice:"))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.github.merge_calls, [])
                self.assertEqual([labels for _, _, labels in self.github.created if "slice" in labels], [])

    def test_browser_block_rebuilds_and_rechecks_before_review(self):
        self.github.body = slice_body(SLICE)
        blocked = {"implementation": "built", "formal_proof": FORMAL, "browser_proof": {**BROWSER, "passed": False}}
        fixed = {**blocked, "browser_proof": BROWSER}
        result = self.walk([{"triage": "implement"}, blocked, fixed, APPROVE])
        self.assertEqual(result["path"], [
            "triage", "implement", "publish", "verify", "proof", "implement", "publish", "verify", "proof", "review", "merge", "done",
        ])
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])

    def test_formal_block_consumes_the_review_cap(self):
        self.github.body = slice_body(SLICE)
        blocked = {
            "implementation": "built", "browser_proof": BROWSER,
            "formal_proof": {**FORMAL, "constraints": [{"constraint": "unique_item_id", "rejected": False}]},
        }
        result = self.walk([{"triage": "implement"}] + [blocked] * 3)
        self.assertEqual(result["stop"], "review_cap")
        self.assertEqual(result["proof_labels"], {"formal": "block"})
        self.assertEqual(result["review_rounds"], 0)
        self.assertEqual(result["proof_failures"], 3)
        self.assertNotIn("review", result["path"])

    def test_cli_preflight_stops_before_dispatch_and_uses_engine_checkout(self):
        root = self.cwd / "checkout"
        root.mkdir()
        preflight = "echo probed > preflight.log; exit 9"
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                 "--check", "touch checked", "--preflight", preflight],
                worker_runner=lambda args, cwd, env: worker_reply(args, {"triage": "implement"}),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(root)),
            )
        self.assertEqual(code, 75)
        self.assertIn("done env_unavailable", stdout.getvalue())
        self.assertIn(f"Preflight {preflight!r} exited with 9", stdout.getvalue())
        self.assertEqual((root / "preflight.log").read_text(), "probed\n")
        self.assertFalse((self.cwd / "preflight.log").exists())
        self.assertFalse((root / "checked").exists())

    def test_cli_preflight_output_streams_through_redacted_log(self):
        preflight = f"echo {SYNTHETIC_QUESTION_TOKEN}; echo warning >&2; exit 9"
        stdout = io.StringIO()
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(stdout):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                 "--check", "exit 0", "--preflight", preflight],
                worker_runner=lambda args, cwd, env: worker_reply(args, {"triage": "implement"}),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 75)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, stdout.getvalue())
        self.assertIn("[redacted:GitHub Token]", stdout.getvalue())
        self.assertIn("warning", stdout.getvalue())

    def test_cli_rejects_changed_answer_without_traceback(self):
        self.walk([{"triage": "needs_human", "human_question": "Which items?"}], preflight="exit 9")
        self.walk([{"triage": "implement"}], answer="Only open")
        stderr = io.StringIO()
        with contextlib.chdir(self.cwd), contextlib.redirect_stderr(stderr):
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                 "--check", "exit 0", "--answer", "All items"],
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 1)
        self.assertIn("already answered differently", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_rerunning_same_command_after_preflight_recovers_without_using_a_round(self):
        preflight = "test -f ready"
        halted = self.walk([{"triage": "implement"}], preflight=preflight)
        self.assertEqual(halted.get("stop"), "env_unavailable")
        self.assertEqual(halted.get("review_rounds"), 0)
        self.assertEqual(halted.get("path"), ["triage", "implement", "done"])
        self.assertEqual(self.calls, ["claude"])

        (self.cwd / "ready").touch()
        result = self.walk(
            [{"implementation": "built"}, APPROVE],
            preflight=preflight,
        )
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual(result.get("review_rounds"), 1)
        self.assertNotIn("halt_detail", result)
        self.assertEqual(result.get("path"), ["triage", "implement", "publish", "verify", "review", "merge", "done"])

    def test_answer_resume_is_idempotent_after_environment_halt(self):
        preflight = "test -f ready"
        question = self.walk(
            [{"triage": "needs_human", "human_question": "Which items?"}], preflight=preflight,
        )
        self.assertEqual(question.get("stop"), "needs_human")
        halted = self.walk([{"triage": "implement"}], answer="Only open")
        self.assertEqual(halted.get("stop"), "env_unavailable")
        self.assertEqual(halted.get("human_answers"), ["Only open"])
        with self.assertRaisesRegex(AnswerError, "already answered differently"):
            self.walk([], answer="All items")
        (self.cwd / "ready").touch()
        resumed = self.walk([{"implementation": "built"}, APPROVE], answer="Only open")
        self.assertEqual(resumed.get("stop"), "verified")
        self.assertEqual(resumed.get("human_answers"), ["Only open"])
        self.assertEqual(self.walk([], answer="Only open").get("stop"), "verified")

    def test_non_implement_slice_routes_skip_preflight(self):
        for issue, replies, stop in (
            (8, [{"triage": "question"}, {"research": "answer"}], "answered"),
            (9, [{"triage": "spec"}, {"spec": "not ready", "spec_gate": "needs_human",
                                       "human_question": "Which items?"}], "needs_human"),
        ):
            with self.subTest(stop=stop):
                result = self.walk(replies, issue=issue, preflight="touch probed; exit 9")
                self.assertEqual(result.get("stop"), stop)
                self.assertFalse((self.cwd / "probed").exists())

    def test_parent_and_slice_probe_once_plus_failed_check_reprobe(self):
        for issue, labels in ((8, ["factory"]), (9, ["slice"])):
            with self.subTest(labels=labels):
                self.github = FakeGitHub(labels)
                marker = f"checked-{issue}"
                probe_log = f"probes-{issue}"
                result = self.walk(
                    [{"triage": "implement", "slice": EMPTY_SLICE},
                     {"implementation": "built"}, {"implementation": "fixed"}, APPROVE], issue=issue,
                    check=f"test -f {marker} || {{ touch {marker}; exit 1; }}",
                    preflight=f"echo probed >> {probe_log}",
                )
                self.assertEqual(result.get("stop"), "verified")
                self.assertEqual(result.get("review_rounds"), 1)
                self.assertEqual(result.get("path", []).count("implement"), 2)
                self.assertEqual((self.cwd / probe_log).read_text().splitlines(), ["probed", "probed"])

    def test_successful_slice_probes_only_once(self):
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built"}, APPROVE],
            preflight="echo probed >> probes",
        )
        self.assertEqual(result.get("stop"), "verified")
        self.assertEqual((self.cwd / "probes").read_text(), "probed\n")

    def test_parent_preflight_halts_at_implement(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "implement", "slice": EMPTY_SLICE}], preflight="exit 9")
        self.assertEqual(result.get("stop"), "env_unavailable")
        self.assertEqual(result.get("path"), ["triage", "implement", "done"])
        self.assertEqual(self.calls, ["claude"])

    def test_parent_stops_sliced_without_running_preflight(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk(
            [{"triage": "spec"}, {"spec": "Plan", "spec_gate": "ready", "slices": [EMPTY_SLICE]}],
            preflight="touch probed; exit 9",
        )
        self.assertEqual(result.get("stop"), "sliced")
        self.assertFalse((self.cwd / "probed").exists())

    def test_dry_run_does_not_run_preflight(self):
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--dry-run", "--request", "List items",
                 "--preflight", "touch preflight.log; exit 9"],
            )
        self.assertEqual(code, 0)
        self.assertIn("done verified", stdout.getvalue())
        self.assertFalse((self.cwd / "preflight.log").exists())

    def test_failed_check_with_failed_preflight_preserves_rounds(self):
        (self.cwd / "ready").touch()
        result = self.walk(
            [{"triage": "implement"}, {"implementation": "built"}],
            check="test -f checked || { touch checked; rm ready; exit 1; }", preflight="test -f ready",
        )
        self.assertEqual(result.get("stop"), "env_unavailable")
        self.assertEqual(result.get("review_rounds"), 0)
        self.assertEqual(result.get("check_exit_code"), 1)
        self.assertIn("test -f ready", result.get("halt_detail", ""))
        self.assertIn("exited with 1", result.get("halt_detail", ""))
        self.assertEqual(result.get("path"), ["triage", "implement", "publish", "verify", "done"])

        (self.cwd / "ready").touch()
        resumed = self.walk([APPROVE])
        self.assertEqual(resumed.get("stop"), "verified")
        self.assertEqual(resumed.get("check_exit_code"), 0)
        self.assertEqual(resumed.get("review_rounds"), 1)
        self.assertEqual(self.calls, ["claude", "codex", "claude"])

    def test_failed_check_consumes_round_with_healthy_or_unspecified_environment(self):
        for issue, preflight in enumerate(("", "echo probed >> preflight.log"), start=8):
            with self.subTest(preflight=preflight):
                marker = f"checked-{issue}"
                built = {"implementation": "built"}
                result = self.walk(
                    [{"triage": "implement"}, built, built, APPROVE], issue=issue,
                    check=f"test -f {marker} || {{ touch {marker}; exit 1; }}", preflight=preflight,
                )
                self.assertEqual(result.get("stop"), "verified")
                self.assertEqual(result.get("review_rounds"), 1)
                self.assertEqual(result.get("path", []).count("implement"), 2)
        self.assertEqual((self.cwd / "preflight.log").read_text(), "probed\nprobed\n")

    def test_check_that_cannot_start_stops_without_spending_a_round(self):
        (self.cwd / "non-executable").write_text("#!/bin/sh\nexit 0\n")
        cases = (
            ("./non-executable", 126, "command is not executable", ""),
            ("./missing-check", 127, "command not found", "echo probed >> preflight.log"),
        )
        for issue, (command, exit_code, reason, preflight) in enumerate(cases, start=8):
            with self.subTest(exit_code=exit_code):
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}],
                    issue=issue, check=command, preflight=preflight,
                )
                self.assertEqual(result.get("stop"), "env_unavailable")
                self.assertEqual(result.get("review_rounds"), 0)
                self.assertEqual(result.get("check_exit_code"), exit_code)
                for detail in (command, str(exit_code), reason):
                    self.assertIn(detail, result.get("halt_detail", ""))
        self.assertEqual((self.cwd / "preflight.log").read_text(), "probed\n")

    def test_interrupt_during_setup_has_no_pending_usage(self):
        with patch("foreman.delivery.EnvironmentProbe.error", new_callable=unittest.mock.PropertyMock,
                   side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "implement"}])
        result = self.walk([{"implementation": "built"}, APPROVE])
        self.assertEqual(result["stop"], "verified")
        self.assertNotIn("interrupted", [a["outcome"] for a in result["attempts"]])

    def test_env_unavailable_resumes_before_implement(self):
        with patch("foreman.delivery.EnvironmentProbe.error", new_callable=unittest.mock.PropertyMock,
                   return_value="temporarily unavailable"):
            result = self.walk([{"triage": "implement"}])
        self.assertEqual(result["stop"], "env_unavailable")
        self.assertIsNone(result.get("pending_dispatch"))
        self.assertEqual(self.walk([{"implementation": "built"}, APPROVE])["stop"], "verified")

    def test_resume_at_interrupted_dispatch_stops_with_unknown_usage(self):
        def interrupted_run(node, model, state, instruction):
            if node == "triage":
                return Outcome("ran", {"triage": "implement", "work_kind": "feature", "slice": None, "human_question": None}, usage=Usage(input_tokens=1, output_tokens=1))
            self.assertEqual(node, "implement")
            raise RuntimeError("worker stopped")

        ready = self.cwd / "ready"
        ready.touch()
        with self.assertRaisesRegex(RuntimeError, "worker stopped"):
            run_live_delivery(
                repo="Example/Engine", issue=8, check="exit 0", preflight="test -f ready",
                checkpoint=self.checkpoint, run=LiveRun(self.github, FakeWorkspace(self.cwd)),
                dispatcher=interrupted_run,
            )
        ready.unlink()
        result = self.walk([])
        self.assertEqual(result.get("stop"), "usage_unknown")
        self.assertEqual(result.get("review_rounds"), 0)
        self.assertEqual(self.calls, [])

    def test_dirty_or_invalid_worktree_skips_the_check(self):
        for snapshot in (WorktreeState(HEAD, False, BRANCH), WorktreeInspectionFailed("Invalid HEAD")):
            with self.subTest(snapshot=snapshot), TemporaryDirectory(dir=self.cwd) as directory:
                self.checkpoint = Path(directory) / "runs.sqlite3"
                result = self.walk(
                    [{"triage": "implement"}, {"implementation": "built"}],
                    check="touch ran", snapshots=(snapshot,),
                )
                self.assertEqual(result["stop"], "dirty_worktree")
                self.assertEqual(result["path"], ["triage", "implement", "publish", "verify", "done"])
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
            "gh", "pr", "merge", "<number>", "--repo", "Example/Engine", "--auto", "--squash", "--match-head-commit", HEAD,
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
        git("config", "user.name", "Synthetic Test")
        git("config", "user.email", "synthetic@example.invalid")
        git("config", "commit.gpgsign", "false")
        git("commit", "--allow-empty", "-m", "Verified commit")
        git("branch", "main")
        git("remote", "add", "origin", ".")
        verified_head = git("rev-parse", "HEAD")
        self.github.base = verified_head
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])

        def run_worker(args, cwd, env=None):
            reply = next(replies)
            if "implementation" in reply:
                (repo / "items.txt").write_text("synthetic edits")
            if reply is APPROVE:
                git("commit", "--allow-empty", "-m", "Commit after verification")
            return worker_reply(args, reply)

        def run_delivery():
            return run_live_delivery(
                repo="Example/Engine", issue=8, check="exit 0", checkpoint=self.checkpoint,
                dispatcher=CliWorker(repo, run_worker),
                run=LiveRun(self.github, LiveWorkspace(repo)),
            )

        with patch.object(LiveWorkspace, "push"):
            halted = run_delivery()
        self.assertEqual(halted["stop"], "merge_not_armed", halted)
        verified_head = halted["verified_head"]
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
        interdiff = LiveWorkspace(checkout).review_diff(base, head, interdiff=True)
        self.assertIn(f"{base}..{head}", interdiff.command)
        self.assertIn("main.txt", interdiff.patch)
        self.assertEqual(LiveWorkspace(checkout).review_diff(head, head, interdiff=True).patch, "")

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
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        detail = "GraphQL: Auto-merge is disabled"
        with (
            contextlib.chdir(self.cwd),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            patch.object(self.github, "arm_auto_merge", side_effect=MergeNotArmed(detail)) as arm,
        ):
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8", "--check", "exit 0"],
                worker_runner=lambda args, cwd, env: fake.reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, fake),
            )
        self.assertEqual(code, 75)
        self.assertEqual(arm.call_args.args[:4], ("Example/Target", 8, BRANCH, HEAD))
        self.assertIn(f"merge not armed\ncheck exit 0\ntokens 47606 of 20000000\ndone merge_not_armed\n{detail}", stdout.getvalue())

    def test_parent_arms_its_pull_request_without_filing_slices(self):
        self.github = FakeGitHub(["factory"])
        result = self.walk([{"triage": "implement", "slice": EMPTY_SLICE}, {"implementation": "built"}, APPROVE])
        self.assertEqual(result["path"], ["triage", "implement", "publish", "verify", "review", "merge", "done"])
        self.assertEqual(result["merge"], "armed")
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(self.github.merge_calls, [("Example/Engine", 8, BRANCH, HEAD)])
        self.assertEqual(self.github.created, [])

    def test_main_dry_run_reports_a_skipped_merge(self):
        worker = Mock()
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            patch.object(FakeGitHub, "arm_auto_merge") as arm,
            patch("foreman.skill_rules.git") as git_call,
        ):
            code = main(["run", "delivery", "--repo", "Example/Target", "--request", "List items", "--dry-run"],
                        worker_runner=worker, live_run=LiveRun(self.github, FakeWorkspace(self.cwd)))
        self.assertEqual(code, 0)
        self.assertIn(
            "merge skipped (dry run): gh pr merge '<number>' --repo Example/Target --auto --squash --match-head-commit '<sha>'\n"
            "check exit 0\ntokens 6 of 20000000\ndone verified",
            stdout.getvalue(),
        )
        self.assertIn("pstack pin not checked (dry run)", stdout.getvalue())
        worker.assert_not_called()
        arm.assert_not_called()
        git_call.assert_not_called()

    def test_main_rerun_arms_once_after_the_repository_hold_is_cleared(self):
        self.github.hold = "  Maintenance window "
        repo = "Example/Target"
        fake = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        live_run = LiveRun(self.github, FakeWorkspace(self.cwd))

        def invoke():
            with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = main(
                    ["run", "delivery", "--repo", repo, "--issue", "8", "--check", "exit 0"],
                    worker_runner=lambda args, cwd, env=None: live_run.workspace.reply(args, next(replies)),
                    checkpoint=self.checkpoint, live_run=live_run,
                )
            self.assertEqual(code, 75 if self.github.hold else 0)
            return stdout.getvalue()

        held = invoke()
        self.assertIn(f"done merge_held\nFOREMAN_MERGE_HOLD is set: {self.github.hold}\n", held)
        retried = invoke()
        self.assertIn("this invocation:\nmerge not armed\ndone merge_held", retried)
        self.assertEqual(self.github.merge_calls, [])
        self.github.hold = ""
        armed = invoke()
        self.assertIn("merge armed\ndone verified", armed)
        terminal = invoke()
        self.assertIn("restored tokens 47606 of 20000000", terminal)
        self.assertTrue(terminal.endswith("this invocation: no nodes executed\n"))
        self.assertEqual(self.github.hold_reads, [repo, repo, repo])
        self.assertEqual(self.github.merge_calls, [(repo, 8, BRANCH, HEAD)])

    def test_whitespace_merge_hold_reports_done(self):
        self.github.hold = " "
        workspace = FakeWorkspace(self.cwd)
        replies = iter([{"triage": "implement"}, {"implementation": "built"}, APPROVE])
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = main(
                ["run", "delivery", "--repo", "Example/Target", "--issue", "8", "--check", "exit 0"],
                worker_runner=lambda args, cwd, env=None: workspace.reply(args, next(replies)),
                checkpoint=self.checkpoint, live_run=LiveRun(self.github, workspace),
            )
        self.assertEqual(code, 75)
        self.assertIn("done merge_held\nFOREMAN_MERGE_HOLD is set.\n", stdout.getvalue())

    def test_second_process_sees_the_saved_stop_and_answer_resumes_spec(self):
        script = """
import json
from pathlib import Path
import sys
from foreman.cli import _write_check_log, main, run_exit_code
from foreman.delivery import LiveRun
from dataclasses import replace
from unittest.mock import patch
from foreman.config import PstackPin, load_config
from test_runner import APPROVE, HEAD, SYNTHETIC_QUESTION_TOKEN, FakeGitHub, FakeWorkspace, worker_reply

def run(args, cwd, env):
    prompt = args[-1]
    state = json.loads(prompt.split("Delivery state:\\n")[1])
    if args[0] == "codex":
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    else:
        schema = json.loads(args[args.index("--json-schema") + 1])
    with Path("workers.log").open("a") as log:
        log.write(schema["title"] + "\\n")
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
        assert state["human_question"] == "Which items? [redacted:GitHub Token]"
        return worker_reply(args, {"spec": "List open items", "spec_gate": "ready"})
    if schema["title"] == "ImplementOutput":
        return workspace.reply(args, {"implementation": "built"})
    return worker_reply(args, APPROVE)

class Forge(FakeGitHub):
    def arm_auto_merge(self, *args):
        with Path("merges.log").open("a") as log:
            log.write("merge\\n")
        return super().arm_auto_merge(*args)

workspace = FakeWorkspace(Path.cwd())
pin = PstackPin.model_validate_json(sys.argv[2])
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin)):
    sys.exit(main(
        sys.argv[3:], worker_runner=run, checkpoint=Path(sys.argv[1]),
        live_run=LiveRun(Forge(), workspace),
    ))
"""
        def invoke(*flags, pin=None, expected=0):
            bootstrap = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
                         f"sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n")
            process = subprocess.run(
                [sys.executable, "-c", bootstrap + script, str(self.checkpoint), (pin or self.pin).model_dump_json(), "run", "delivery",
                 "--repo", "Example/Engine", "--issue", "8", "--check", "echo checked >> check.log", *flags],
                cwd=self.cwd, text=True, capture_output=True, check=False,
            )
            self.assertEqual(process.returncode, expected, process.stderr)
            return process.stdout

        first = invoke("--request", "List items", expected=3)
        self.assertIn("paused needs_human\nWhich items? [redacted:GitHub Token]", first)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, first)
        waiting = invoke(expected=3)
        self.assertIn("restored triage ", waiting)
        self.assertIn("restored spec ", waiting)
        self.assertIn("restored paused needs_human\nrestored Which items? [redacted:GitHub Token]", waiting)
        self.assertIn("this invocation: no nodes executed", waiting)
        self.assertNotIn(SYNTHETIC_QUESTION_TOKEN, waiting)
        self.assertFalse((self.cwd / "check.log").exists())
        completed = invoke("--answer", "Only open items", pin=self.pin.model_copy(update={"commit": "f" * 40}))
        restored, current = completed.split("this invocation:\n")
        self.assertEqual(restored.count("restored triage "), 1)
        self.assertEqual(restored.count("restored spec "), 1)
        self.assertNotIn("triage", current)
        self.assertEqual(sum(line.startswith("spec ") for line in current.splitlines()), 1)
        self.assertIn("merge armed", completed)
        self.assertIn("done verified", completed)
        terminal = invoke()
        self.assertIn("restored done verified", terminal)
        self.assertIn("restored merge armed", terminal)
        self.assertIn("restored check exit 0", terminal)
        self.assertIn("this invocation: no nodes executed", terminal)
        self.assertEqual((self.cwd / "workers.log").read_text().splitlines(),
                         ["TriageOutput", "SpecOutput", "SpecOutput", "ImplementOutput", "ReviewOutput"])
        self.assertEqual((self.cwd / "merges.log").read_text(), "merge\n")
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            stored = saver.get({"configurable": {"thread_id": "example/engine#8"}})
        self.assertEqual(stored["channel_values"]["path"],
                         ["triage", "spec", "spec", "implement", "publish", "verify", "review", "merge", "done"])
        self.assertEqual((self.cwd / "check.log").read_text(), "checked\n")

    def test_restored_snapshot_matches_result_for_legacy_merge_and_interrupts(self):
        for replies in ([{"triage": "needs_human", "human_question": "Which items?"}],
                        [{"triage": "implement"}, {"implementation": "built"}, APPROVE]):
            with self.subTest(replies=replies):
                self.checkpoint = self.cwd / f"{len(replies)}-{type(replies[0]).__name__}.sqlite3"
                expected = self.walk(replies)
                if expected["stop"] == "verified":
                    with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
                        stored = saver.get_tuple({"configurable": {"thread_id": "example/engine#8"}})
                        stored.checkpoint["channel_values"].pop("merge")
                        stored.checkpoint["channel_values"]["merge_armed"] = True
                        saver.put(stored.config, stored.checkpoint, stored.metadata, {})
                presentation = RunPresentation()
                self.assertEqual(self.walk([], presentation=presentation), expected)
                self.assertEqual(presentation.restored, expected)
                self.assertEqual(presentation.updates, [])

    def test_terminal_proof_failure_is_entirely_restored(self):
        self.github.body = slice_body(SLICE)
        self.walk([{"triage": "implement"}, {"implementation": "built"}])
        worker = Mock(side_effect=AssertionError("terminal checkpoint dispatched a worker"))
        with contextlib.chdir(self.cwd), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"],
                worker_runner=worker, checkpoint=self.checkpoint,
                live_run=LiveRun(self.github, FakeWorkspace(self.cwd)),
            )
        self.assertEqual(code, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[-1], "this invocation: no nodes executed")
        self.assertTrue(all(line.startswith("restored ") for line in lines[:-1]))
        self.assertIn("restored browser proof insufficient_evidence", lines)
        self.assertIn("restored formal proof insufficient_evidence", lines)
        self.assertIn("restored check exit 0", lines)
        self.assertIn("restored done insufficient_evidence", lines)
        worker.assert_not_called()
        self.assertEqual(self.github.merge_calls, [])

    def test_fresh_process_retries_quota_and_an_older_merge_checkpoint(self):
        script = r'''
import json
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch
from foreman.cli import main
from foreman.config import PstackPin, load_config
from foreman.delivery import LiveRun
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, worker_reply, fixture_process

phase = sys.argv[3]

def record(event):
    with Path("effects.log").open("a") as log:
        log.write(event + "\n")

class Workspace(FakeWorkspace):
    def commit(self, message):
        record("commit")
        super().commit(message)

    def push(self, branch, repo):
        record("push")
        super().push(branch, repo)

class Forge(FakeGitHub):
    def arm_auto_merge(self, *args):
        record("merge")
        return super().arm_auto_merge(*args)

    def post_review(self, *args, marker):
        record("post_review")
        return super().post_review(*args, marker=marker)

def worker(args, cwd, env):
    if args[0] == "codex":
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    else:
        schema = json.loads(args[args.index("--json-schema") + 1])
    stage = schema["title"]
    record(stage)
    if stage == "TriageOutput":
        return worker_reply(args, {"triage": "implement"})
    if stage == "ImplementOutput":
        if phase == "quota":
            return fixture_process(args, "quota", 1)
        return workspace.reply(args, {"implementation": "built"})
    assert stage == "ReviewOutput", stage
    return worker_reply(args, APPROVE)

workspace = Workspace(Path.cwd())
forge = Forge()
forge.hold = "Maintenance" if phase == "held" else ""
pin = PstackPin.model_validate_json(sys.argv[2])
with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin)):
    sys.exit(main(sys.argv[4:], worker_runner=worker, checkpoint=Path(sys.argv[1]),
                  live_run=LiveRun(forge, workspace)))
'''
        bootstrap = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
                     f"sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})\n")
        for scenario in ("held", "quota"):
            with self.subTest(scenario=scenario):
                root = self.cwd / scenario
                root.mkdir()
                checkpoint = root / "runs.sqlite3"

                def invoke(phase, expected):
                    process = subprocess.run(
                        [sys.executable, "-c", bootstrap + script, str(checkpoint), self.pin.model_dump_json(),
                         phase, "run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                         "--check", "echo checked >> check.log"],
                        cwd=root, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(process.returncode, expected, process.stderr)
                    return process.stdout

                def saved_path():
                    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                        saved = saver.get({"configurable": {"thread_id": "example/engine#8"}})
                    return saved["channel_values"]["path"]

                first = invoke(scenario, 75)
                previous_path = saved_path()
                if scenario == "held":
                    # Replaying the older pre-merge checkpoint ends with exactly the same path.
                    retried = invoke("held", 75)
                    restored, current = retried.split("this invocation:\n")
                    self.assertEqual(saved_path(), previous_path)
                    self.assertIn("restored review ", restored)
                    self.assertIn("risk low", restored)
                    self.assertEqual(current, "merge not armed\ndone merge_held\nFOREMAN_MERGE_HOLD is set: Maintenance\n")
                    self.assertEqual((root / "effects.log").read_text().splitlines(),
                                     ["TriageOutput", "ImplementOutput", "commit", "push", "ReviewOutput", "post_review"])
                    self.assertEqual((root / "check.log").read_text(), "checked\n")
                else:
                    self.assertIn("paused quota_exhausted", first)
                    self.assertFalse((root / "check.log").exists())
                    attempts = len(load_config().nodes["implement"].models)
                    self.assertEqual((root / "effects.log").read_text().splitlines(),
                                     ["TriageOutput"] + ["ImplementOutput"] * attempts)

                resumed = invoke("ready", 0)
                restored, current = resumed.split("this invocation:\n")
                self.assertNotIn("triage", current)
                if scenario == "held":
                    self.assertEqual(current, "merge armed\ndone verified\n")
                    expected_workers = ["TriageOutput", "ImplementOutput", "commit", "push", "ReviewOutput", "post_review", "merge"]
                else:
                    self.assertRegex(restored, r"restored implement\nrestored tokens \d+ of 20000000\nrestored paused quota_exhausted")
                    self.assertEqual(sum(line.startswith("implement ") for line in current.splitlines()), 1)
                    self.assertEqual(saved_path(), ["triage", "implement", "implement", "publish", "verify", "review", "merge", "done"])
                    expected_workers = (["TriageOutput"] + ["ImplementOutput"] * attempts
                                        + ["ImplementOutput", "commit", "push", "ReviewOutput", "post_review", "merge"])
                terminal = invoke("ready", 0)
                self.assertIn("restored merge armed", terminal)
                self.assertIn("restored check exit 0", terminal)
                self.assertIn("restored done verified", terminal)
                self.assertTrue(terminal.endswith("this invocation: no nodes executed\n"))
                self.assertEqual((root / "effects.log").read_text().splitlines(), expected_workers)
                self.assertEqual((root / "check.log").read_text(), "checked\n")

class HaltExitCodeTest(unittest.TestCase):
    def live(self, replies=(), *, snapshots=(CLEAN_WORKTREE,), body=None, check="exit 0", preflight="", hold=None):
        directory = self.enterContext(TemporaryDirectory())
        root = Path(directory)
        forge = FakeGitHub()
        forge.hold = hold
        if body is not None:
            forge.body = body
        outputs = iter(replies)
        workspace = FakeWorkspace(root, snapshots)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(
                ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", check, "--preflight", preflight],
                checkpoint=root / "runs.sqlite3", live_run=LiveRun(forge, workspace, skills=fixture_skills),
                worker_runner=(lambda args, cwd, env: workspace.reply(args, next(outputs))),
            )
        return code, output.getvalue()

    def assert_halt(self, result, reason, detail, code=1):
        self.assertEqual(result[0], code, result[1])
        self.assertIn(f"done {reason}\n{detail}\n", result[1])

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
        changes = {**APPROVE, "findings": [FINDING]}
        unresolved = {**APPROVE, "resolutions": [{"id": "f1", "status": "unresolved"}]}
        self.assert_halt(self.live(
            [{"triage": "implement"}] + [{"implementation": "built"}, changes]
            + [{"implementation": "built"}, unresolved] * 2,
        ), "review_cap", "Review requested changes after 3 rounds. Last review: " + ReviewOutput.model_validate(APPROVE, context=SCRIPTED_REVIEW_CONTEXT).risk.render())

    def test_missing_slice_has_no_fake_stage(self):
        result = self.live(body="No JSON")
        self.assert_halt(result, "missing_slice", "slice issue needs a fenced JSON object")
        self.assertEqual(result[1], "tokens 0 of 20000000\ndone missing_slice\nslice issue needs a fenced JSON object\n")

    def test_resumable_halts(self):
        replies = [{"triage": "implement"}, {"implementation": "built"}, APPROVE]
        self.assert_halt(self.live(replies, hold="Maintenance"), "merge_held", "FOREMAN_MERGE_HOLD is set: Maintenance", 75)
        self.assert_halt(self.live(replies, snapshots=(CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, CLEAN_WORKTREE, WorktreeState(HEAD, True, None))),
                         "merge_not_armed", "HEAD is detached. Merge requires a checked-out branch.", 75)

    def test_environment_halts_have_detail_and_resumable_exit(self):
        self.assert_halt(self.live(
            [{"triage": "implement"}], preflight="exit 9",
        ), "env_unavailable", "Preflight 'exit 9' exited with 9", 75)
        for command, code, reason in (
            ("./missing-check", 127, "command not found"),
            ("./non-executable", 126, "command is not executable"),
        ):
            with self.subTest(code=code):
                result = self.live(
                    [{"triage": "implement"}, {"implementation": "built"}], check=command,
                    preflight="touch non-executable" if code == 126 else "",
                )
                self.assert_halt(result, "env_unavailable", f"Check {command!r} exited with {code} ({reason})", 75)

    def test_invalid_merge_sha_halts_with_detail_and_resumable_exit(self):
        detail = "Merge arming requires a full 40-hex commit SHA."
        with self.assertRaisesRegex(MergeNotArmed, detail):
            LiveForge().arm_auto_merge("Example/Engine", 8, BRANCH, "invalid", FakeGitHub().readiness("Example/Engine", 8, BRANCH, HEAD))
        result = {**halted(Halt.merge_not_armed, detail, "merge"), "stop": "merge_not_armed", "path": ["merge", "done"], "merge": "not_armed"}
        with patch("foreman.cli.run_live_delivery", side_effect=lambda **kwargs: (kwargs["report"](result), result)[1]), contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"])
        self.assert_halt((code, output.getvalue()), "merge_not_armed", detail, 75)

    def test_answered_is_success(self):
        code, output = self.live([{"triage": "question"}, {"research": "The answer."}])
        self.assertEqual(code, 0, output)
        self.assertIn("done answered\n", output)

    def test_empty_details_are_rejected(self):
        for detail in ("", " ", "\n"):
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, "requires a nonempty halt detail"):
                halted(Halt.failed, detail, "triage")

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
