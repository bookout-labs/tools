"""Proposal writes, deduplication and durable halted-run recovery."""

import tests

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import get_args
import unittest
from unittest.mock import Mock, patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.adapters import Outcome, Usage
from foreman.cli import main, run_exit_code
from foreman.delivery import HaltReason, LiveRun
from foreman.dispatch import FailoverDispatcher
from foreman.github import CommandFailed, Forge, ForgeError, IssueSummary, LiveForge
from foreman.runner import run_live_delivery
from foreman.skill_rules import SkillMissing
from foreman.stage_output import Attempt
from foreman.watch import DEFAULT_WATCH_REPO, WATCH, watch
from foreman.workspace import WorkspaceError
from skill_fixture import fixture_skills, skill_fixture
from test_redaction import SYNTHETIC_CASES, SYNTHETIC_GITHUB
from test_runner import FakeGitHub, FakeWorkspace, worker_reply


FIXTURES = Path(__file__).with_name("fixtures") / "github"
RUN_KEY = "Example/Engine#8@checkpoint-a"
MARKER = "<!-- foreman-watch: invalid_output@src/foreman/stages/triage.md -->"
RUN_MARKER = f"<!-- foreman-watch-run: {RUN_KEY} -->"


def halted_state(reason="invalid_output", node="triage", model="gpt-6.1-sol-high"):
    return {"repo": "Example/Engine", "issue": 8, "check": "exit 0", "path": [node, "done"],
            "halt": reason, "stop": reason, "halt_detail": "Synthetic failure", "token_cap": 100,
            "attempts": [Attempt(stage=node, model=model, commit="a" * 40, methods=[],
                                 outcome="ran", usage=None).record()]}


def gh_response(name):
    return subprocess.CompletedProcess([], 0, (FIXTURES / name).read_text(), "")


class WatchTests(unittest.TestCase):
    def test_every_halt_has_an_explicit_policy(self):
        def literals(value):
            return {value} if isinstance(value, str) else set().union(*(literals(arg) for arg in get_args(value)))
        self.assertEqual(set(WATCH), literals(HaltReason))

    def test_every_policy_produces_the_required_proposal_or_no_forge_calls(self):
        expected = {
            "invalid_output": ("Fix invalid output on review", "src/foreman/stages/review.md"),
            "verify_cap": ("Investigate repeated verify failures reaching cap", "src/foreman/stages/implement.md"),
            "review_cap": ("Investigate repeated review or proof blocks reaching cap", "src/foreman/stages/implement.md"),
            "spec_cap": ("Investigate rejected slice plans reaching cap", "src/foreman/stages/spec.md"),
            "dirty_worktree": ("Handle dirty worktree before verify", "src/foreman/stages/implement.md"),
            "insufficient_evidence": ("Required proof is missing after implement", "src/foreman/stages/implement.md"),
            "missing_slice": ("Slice issues carry a loadable slice", "src/foreman/github.py"),
            "review_unavailable": ("Review finds its pull request", "src/foreman/github.py"),
            "review_not_posted": ("Reviews post to the pull request", "src/foreman/github.py"),
            "pr_size_unavailable": ("Pull request size is measurable before review", "src/foreman/workspace.py"),
            "usage_unknown": ("codex attempts report usage", "src/foreman/adapters/codex.py"),
            "no_dispatcher": ("A live run always has a dispatcher", "src/foreman/cli.py"),
            "skill_missing": ("Pinned skills are available before dispatch", "src/foreman/skill_rules.py"),
            "git_failed": ("Publish handles git failures", "src/foreman/workspace.py"),
        }
        skipped = {"no_reviewer", "cli_missing", "merge_held", "merge_not_armed", "budget_exceeded",
                   "env_unavailable", "quota_exhausted", "publish_failed", "implement_quota", "failed", "design_rethink"}
        self.assertEqual(set(WATCH), expected.keys() | skipped)
        for reason in WATCH:
            with self.subTest(reason=reason):
                forge = Mock(spec=Forge)
                forge.open_issues.return_value = []
                forge.create_issue.return_value = 42
                result = watch(halted_state(reason, "review"), forge, DEFAULT_WATCH_REPO, RUN_KEY)
                if reason in skipped:
                    self.assertIsNone(result)
                    self.assertEqual(forge.mock_calls, [])
                else:
                    title, file = expected[reason]
                    self.assertEqual(result, {"issue": 42, "action": "filed"})
                    forge.create_issue.assert_called_once()
                    repo, actual_title, body, labels = forge.create_issue.call_args.args
                    self.assertEqual((repo, actual_title, labels), (DEFAULT_WATCH_REPO, title, []))
                    self.assertIn(f"File to change: {file}\n", body)
                    self.assertEqual(body.splitlines()[-1], f"<!-- foreman-watch: {reason}@{file} -->")

    def test_new_cause_body_contains_the_cause_and_attempt_evidence(self):
        forge = FakeGitHub()
        result = watch(halted_state(), forge, DEFAULT_WATCH_REPO, RUN_KEY)
        self.assertEqual(result, {"issue": 1001, "action": "filed"})
        self.assertEqual(forge.watch_calls[-1], (
            "create_issue", DEFAULT_WATCH_REPO, "Fix invalid output on triage",
            "Proposed graph fix: Fix invalid output on triage\n"
            "Cause: The triage stage returned invalid output.\n"
            "Evidence: Example/Engine#8; stop=invalid_output; detail=Synthetic failure; attempts=triage/gpt-6.1-sol-high=ran\n"
            "File to change: src/foreman/stages/triage.md\n"
            "This proposal does not change models.toml or max_review_rounds.\n"
            "It does not edit the graph, merge, or launch a coding agent.\n"
            "A person opens the pull request.\n" + RUN_MARKER + "\n" + MARKER, []))
        state = halted_state()
        state["watch"] = result
        before = list(forge.watch_calls)
        self.assertEqual(watch(state, forge, DEFAULT_WATCH_REPO, RUN_KEY), result)
        self.assertEqual(forge.watch_calls, before)

    def test_different_details_share_a_cause_and_a_repeat_is_one_comment(self):
        forge = FakeGitHub()
        watch(halted_state(), forge, DEFAULT_WATCH_REPO, RUN_KEY)
        second = {**halted_state(), "issue": 9, "halt_detail": "Another model failed at a different SHA"}
        record = watch(second, forge, DEFAULT_WATCH_REPO, "Example/Engine#9@checkpoint-b")
        self.assertEqual(record, {"issue": 1001, "action": "commented"})
        self.assertEqual([call[0] for call in forge.watch_calls].count("create_issue"), 1)
        self.assertEqual(forge.proposal_comments[DEFAULT_WATCH_REPO, 1001], [
            "Evidence: Example/Engine#9; stop=invalid_output; detail=Another model failed at a different SHA; attempts=triage/gpt-6.1-sol-high=ran\n"
            "<!-- foreman-watch-run: Example/Engine#9@checkpoint-b -->"])
        before = list(forge.proposal_comments[DEFAULT_WATCH_REPO, 1001])
        self.assertEqual(watch(second, forge, DEFAULT_WATCH_REPO, "Example/Engine#9@checkpoint-b"), record)
        self.assertEqual(forge.proposal_comments[DEFAULT_WATCH_REPO, 1001], before)

    def test_exact_marker_lines_and_different_nodes_are_separate_causes(self):
        forge = FakeGitHub()
        forge.proposals[DEFAULT_WATCH_REPO, 20] = IssueSummary(number=20, body="prefix " + MARKER)
        watch(halted_state(), forge, DEFAULT_WATCH_REPO, RUN_KEY)
        watch(halted_state(node="review"), forge, DEFAULT_WATCH_REPO, "Example/Engine#8@next")
        self.assertEqual([call[0] for call in forge.watch_calls].count("create_issue"), 2)

    def test_lowest_open_match_wins_and_crash_in_body_needs_no_write(self):
        forge = FakeGitHub()
        for number in (20, 10):
            forge.proposals[DEFAULT_WATCH_REPO, number] = IssueSummary(number=number, body=MARKER)
        self.assertEqual(watch(halted_state(), forge, DEFAULT_WATCH_REPO, RUN_KEY), {"issue": 10, "action": "commented"})
        self.assertEqual(list(forge.proposal_comments), [(DEFAULT_WATCH_REPO, 10)])
        forge.proposals[DEFAULT_WATCH_REPO, 10] = IssueSummary(number=10, body=RUN_MARKER + "\n" + MARKER)
        forge.watch_calls.clear()
        self.assertEqual(watch(halted_state(), forge, DEFAULT_WATCH_REPO, RUN_KEY), {"issue": 10, "action": "filed"})
        self.assertEqual(forge.watch_calls, [("open_issues", DEFAULT_WATCH_REPO)])
        forge.proposals[DEFAULT_WATCH_REPO, 10] = IssueSummary(number=10, body=MARKER)
        forge.proposals[DEFAULT_WATCH_REPO, 20] = IssueSummary(number=20, body=RUN_MARKER + "\n" + MARKER)
        forge.watch_calls.clear()
        self.assertEqual(watch(halted_state(), forge, DEFAULT_WATCH_REPO, RUN_KEY), {"issue": 20, "action": "filed"})
        self.assertEqual(forge.watch_calls, [("open_issues", DEFAULT_WATCH_REPO)])

    def test_adapter_file_uses_the_halted_attempt_model(self):
        for model, adapter in (("gpt-6.1-sol-high", "codex"), ("claude-opus-5-5-high", "claude"), ("grok-4.7-high", "grok")):
            with self.subTest(adapter=adapter):
                forge = FakeGitHub()
                state = halted_state("usage_unknown", "implement", model)
                state["models_used"] = {"implement": "gpt-6.1-sol-high"}
                watch(state, forge, DEFAULT_WATCH_REPO, RUN_KEY)
                self.assertEqual(forge.watch_calls[-1][2], f"{adapter} attempts report usage")
                self.assertIn(f"File to change: src/foreman/adapters/{adapter}.py\n", forge.watch_calls[-1][3])

    def test_fake_forge_redacts_halt_evidence_on_both_writes(self):
        forge = FakeGitHub()
        state = {**halted_state(), "halt_detail": SYNTHETIC_GITHUB}
        watch(state, forge, DEFAULT_WATCH_REPO, RUN_KEY)
        watch(state, forge, DEFAULT_WATCH_REPO, "Example/Engine#8@next")
        writes = [call for call in forge.watch_calls if call[0] in {"create_issue", "comment_issue"}]
        self.assertEqual(len(writes), 2)
        for call in writes:
            self.assertNotIn(SYNTHETIC_GITHUB, str(call))
            self.assertIn("[redacted:GitHub Token]", str(call))


class WatchLiveForgeTests(unittest.TestCase):
    @patch("foreman.github.subprocess.run")
    def test_list_argv_parse_all_pages_and_drop_prs_even_with_null_key(self, gh):
        gh.side_effect = [gh_response("watch-issues.json"), gh_response("watch-comments.json")]
        forge = LiveForge()
        self.assertEqual([(issue.number, issue.body) for issue in forge.open_issues("Example/Target")], [
            (20, "Evidence: Example/Engine#7\n" + MARKER), (10, "Evidence: Example/Engine#6\n" + MARKER), (21, None)])
        self.assertEqual(forge.issue_comments("Example/Target", 10), [
            "Evidence: Example/Engine#7; stop=invalid_output",
            "Evidence: Example/Engine#8; stop=invalid_output\n" + RUN_MARKER])
        self.assertEqual([call.args[0] for call in gh.call_args_list], [
            ["gh", "api", "repos/Example/Target/issues?state=open&per_page=100", "--paginate", "--slurp"],
            ["gh", "api", "repos/Example/Target/issues/10/comments?per_page=100", "--paginate", "--slurp"]])

    @patch("foreman.github.subprocess.run")
    def test_page_two_run_marker_recovers_a_comment_without_writing(self, gh):
        gh.side_effect = [gh_response("watch-issues.json"), gh_response("watch-comments.json")]
        self.assertEqual(watch(halted_state(), LiveForge(), "Example/Target", RUN_KEY), {"issue": 10, "action": "commented"})
        self.assertEqual(len(gh.call_args_list), 2)
        self.assertTrue(all("POST" not in call.args[0] for call in gh.call_args_list))

    @patch("foreman.github.subprocess.run")
    def test_closed_issues_and_prs_do_not_prevent_filing(self, gh):
        records = json.loads((FIXTURES / "watch-issues.json").read_text())
        records = [[issue for page in records for issue in page if issue["number"] in {2, 3, 4}]]
        gh.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(records), ""), subprocess.CompletedProcess([], 0, '{"number":42}', "")]
        self.assertEqual(watch(halted_state(), LiveForge(), "Example/Target", RUN_KEY), {"issue": 42, "action": "filed"})
        self.assertIn("POST", gh.call_args.args[0])
        self.assertNotIn("comments", gh.call_args.args[0][2])

    @patch("foreman.github.subprocess.run")
    def test_failed_or_malformed_page_is_an_error_and_never_a_write(self, gh):
        for output in (subprocess.CompletedProcess([], 1, '[[]]', "page two failed"),
                       subprocess.CompletedProcess([], 0, '[[], {"message":"failure"}]', "")):
            for method in ("open_issues", "issue_comments"):
                with self.subTest(method=method, output=output):
                    gh.reset_mock()
                    gh.return_value = output
                    args = ("Example/Target", 10) if method == "issue_comments" else ("Example/Target",)
                    with self.assertRaises(ForgeError):
                        getattr(LiveForge(), method)(*args)
                    self.assertEqual(gh.call_count, 1)
        gh.reset_mock()
        gh.side_effect = [gh_response("watch-issues.json"), subprocess.CompletedProcess([], 1, '[[]]', "page two failed")]
        with self.assertRaises(CommandFailed):
            watch(halted_state(), LiveForge(), "Example/Target", RUN_KEY)
        self.assertTrue(all("POST" not in call.args[0] for call in gh.call_args_list))

    @patch("foreman.github.subprocess.run")
    def test_comment_create_argv_and_response(self, gh):
        gh.return_value = gh_response("watch-comment-created.json")
        self.assertIsNone(LiveForge().comment_issue("Example/Target", 10, "Evidence: synthetic repeat"))
        self.assertEqual(gh.call_args.args[0], [
            "gh", "api", "repos/Example/Target/issues/10/comments", "--method", "POST", "-f", "body=Evidence: synthetic repeat"])

    @patch("foreman.github.subprocess.run")
    def test_real_forge_redacts_every_secret_kind_in_new_and_repeat_evidence(self, gh):
        for kind, (secret, surrounding) in SYNTHETIC_CASES.items():
            for repeat in (False, True):
                with self.subTest(kind=kind, repeat=repeat):
                    gh.side_effect = ([gh_response("watch-issues.json"), subprocess.CompletedProcess([], 0, '[[]]', ""),
                                       subprocess.CompletedProcess([], 0, '[[]]', ""),
                                       gh_response("watch-comment-created.json")] if repeat else [
                                       subprocess.CompletedProcess([], 0, '[[]]', ""), subprocess.CompletedProcess([], 0, '{"number":42}', "")])
                    state = {**halted_state(), "halt_detail": surrounding.format(secret)}
                    watch(state, LiveForge(), "Example/Target", "Example/Engine#8@new")
                    argv = gh.call_args.args[0]
                    self.assertNotIn(secret, "\n".join(argv))
                    self.assertIn(f"[redacted:{kind}]", "\n".join(argv))


class WatchRunnerTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.config, _ = self.enterContext(skill_fixture())
        self.enterContext(patch("foreman.runner.load_config", return_value=self.config))
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.checkpoint = self.root / "watch.sqlite"
        self.forge = FakeGitHub()
        self.workspace = FakeWorkspace(self.root)
        self.live = LiveRun(self.forge, self.workspace, skills=fixture_skills)

    def run_live(self, **overrides):
        return run_live_delivery(**{"repo": "Example/Engine", "issue": 8, "check": "exit 0",
                                    "checkpoint": self.checkpoint, "run": self.live, **overrides})

    def saved(self):
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            return saver.get({"configurable": {"thread_id": "example/engine#8"}})["channel_values"]

    def test_same_halted_checkpoint_reopened_from_sqlite_writes_once(self):
        first = self.run_live()
        self.assertEqual(first["halt"], "no_dispatcher")
        self.assertEqual(first["watch"], {"issue": 1001, "action": "filed"})
        self.assertEqual(self.saved()["watch"], first["watch"])
        before = list(self.forge.watch_calls)
        again = self.run_live()
        self.assertEqual(again, first)
        self.assertEqual(self.forge.watch_calls, before)

    def test_restarted_resumable_run_adds_new_evidence_to_the_existing_issue(self):
        calls = []
        def handler(*args):
            calls.append(args[0])
            return Outcome("ran", {"triage": "implement", "work_kind": "feature", "slice": None, "human_question": None},
                           usage=Usage(input_tokens=1, output_tokens=1))
        with patch.object(self.workspace, "create_branch", side_effect=WorkspaceError("Synthetic git failure")):
            first = self.run_live(dispatcher=FailoverDispatcher(handler))
            second = self.run_live(dispatcher=FailoverDispatcher(handler))
        self.assertEqual(first["halt"], "git_failed")
        self.assertEqual(second["halt"], "git_failed")
        self.assertEqual(first["watch"], {"issue": 1001, "action": "filed"})
        self.assertEqual(second["watch"], {"issue": 1001, "action": "commented"})
        self.assertEqual(calls, ["triage"])
        self.assertEqual([call[0] for call in self.forge.watch_calls].count("create_issue"), 1)
        comments = self.forge.proposal_comments[DEFAULT_WATCH_REPO, 1001]
        self.assertEqual(len(comments), 1)
        marker = comments[0].splitlines()[-1]
        self.assertTrue(marker.startswith("<!-- foreman-watch-run: Example/Engine#8@"))
        self.assertNotIn(marker, self.forge.proposals[DEFAULT_WATCH_REPO, 1001].body)
        self.assertEqual(self.saved()["watch"], second["watch"])

    def test_crash_after_write_recovers_from_github_and_records_without_writing(self):
        def crash_after_write(*args):
            watch(*args)
            raise SystemExit("Synthetic crash after the GitHub write")
        with patch("foreman.runner.watch", side_effect=crash_after_write), self.assertRaises(SystemExit):
            self.run_live()
        self.assertNotIn("watch", self.saved())
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            identity = saver.get_tuple({"configurable": {"thread_id": "example/engine#8"}}).config["configurable"]["checkpoint_id"]
        body = self.forge.proposals[DEFAULT_WATCH_REPO, 1001].body
        self.assertIn(f"<!-- foreman-watch-run: Example/Engine#8@{identity} -->", body.splitlines())
        result = self.run_live()
        self.assertEqual(result["watch"], {"issue": 1001, "action": "filed"})
        self.assertEqual(self.saved()["watch"], result["watch"])
        self.assertEqual([call[0] for call in self.forge.watch_calls].count("create_issue"), 1)
        self.assertEqual(self.forge.proposal_comments, {})

    def test_skill_missing_is_durable_before_any_graph_dispatch_or_issue_load(self):
        source = Mock(side_effect=SkillMissing(self.config.pstack))
        live = replace(self.live, skills=source)
        with patch.object(self.forge, "load_issue") as load_issue:
            first = self.run_live(run=live)
            second = self.run_live(run=live)
        self.assertEqual(first["halt"], "skill_missing")
        self.assertEqual(first["issue"], 8)
        self.assertEqual(first["watch"], {"issue": 1001, "action": "filed"})
        self.assertEqual(self.saved()["watch"], first["watch"])
        self.assertEqual(second, first)
        source.assert_called_once()
        load_issue.assert_not_called()
        self.assertEqual([call[0] for call in self.forge.watch_calls].count("create_issue"), 1)

    def test_skill_failure_on_restart_gets_its_own_proposal_and_durable_stop(self):
        def handler(*args):
            return Outcome("ran", {"triage": "implement", "work_kind": "feature", "slice": None, "human_question": None},
                           usage=Usage(input_tokens=1, output_tokens=1))
        with patch.object(self.workspace, "create_branch", side_effect=WorkspaceError("Synthetic git failure")):
            first = self.run_live(dispatcher=FailoverDispatcher(handler))
        self.assertEqual(first["watch"], {"issue": 1001, "action": "filed"})
        live = replace(self.live, skills=Mock(side_effect=SkillMissing(self.config.pstack)))
        restarted = self.run_live(run=live)
        self.assertEqual(restarted["halt"], "skill_missing")
        self.assertEqual(restarted["watch"], {"issue": 1002, "action": "filed"})
        self.assertIn("File to change: src/foreman/skill_rules.py\n", self.forge.proposals[DEFAULT_WATCH_REPO, 1002].body)
        self.assertEqual(self.run_live(run=live), restarted)

    def test_cli_watcher_failure_is_secondary_redacted_retryable_and_keeps_exit_codes(self):
        argv = ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"]
        for git_failure, expected_code in ((False, 1), (True, 75)):
            with self.subTest(git_failure=git_failure):
                self.checkpoint = self.root / f"failure-{git_failure}.sqlite"
                output = StringIO()
                with (patch.object(self.forge, "open_issues", side_effect=ForgeError(SYNTHETIC_GITHUB)),
                      patch.object(self.workspace, "create_branch", side_effect=WorkspaceError("Synthetic git failure")),
                      redirect_stdout(output), redirect_stderr(output)):
                    code = main(argv, checkpoint=self.checkpoint, live_run=self.live,
                                worker_runner=(lambda args, cwd, env: worker_reply(args, {"triage": "implement"})) if git_failure else None)
                self.assertEqual(code, expected_code, output.getvalue())
                self.assertLess(output.getvalue().index("done "), output.getvalue().index("watch failed:"))
                self.assertIn("watch failed: [redacted:GitHub Token]", output.getvalue())
                self.assertNotIn(SYNTHETIC_GITHUB, output.getvalue())
                self.assertNotIn("watch", self.saved())
                if not git_failure:
                    retried = self.run_live()
                    self.assertEqual(retried["watch"], {"issue": 1001, "action": "filed"})
                    self.assertEqual(run_exit_code(retried), expected_code)

    def test_cli_default_and_selected_watch_repository_and_halt_first_log(self):
        for selected in (None, "Example/Proposals"):
            with self.subTest(selected=selected):
                self.checkpoint = self.root / ("default.sqlite" if selected is None else "selected.sqlite")
                argv = ["run", "delivery", "--repo", "Example/Engine", "--issue", "8", "--check", "exit 0"]
                if selected:
                    argv.extend(["--watch-repo", selected])
                with redirect_stdout(StringIO()) as output:
                    code = main(argv, checkpoint=self.checkpoint, live_run=self.live, worker_runner=None)
                self.assertEqual(code, 1)
                repo = selected or "bookout-labs/tools"
                self.assertEqual(self.forge.watch_calls[-1][1], repo)
                self.assertLess(output.getvalue().index("done no_dispatcher"), output.getvalue().index("watch filed #"))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main([*argv, "--watch-repo", "invalid"], checkpoint=self.checkpoint, live_run=self.live, worker_runner=None)

    def test_skipped_halt_logs_and_never_reads_or_writes_forge(self):
        def missing_cli(*args):
            return Outcome("cli_missing", detail="Synthetic missing CLI")
        with redirect_stdout(StringIO()) as output:
            result = self.run_live(dispatcher=FailoverDispatcher(missing_cli), report=lambda result: None)
        self.assertEqual(result["halt"], "cli_missing")
        self.assertEqual(self.forge.watch_calls, [])
        self.assertIn("watch skipped cli_missing", output.getvalue())
        self.assertNotIn("watch", self.saved())

    def test_dry_run_never_reads_or_writes_forge(self):
        with patch.object(LiveForge, "open_issues") as issues, patch.object(LiveForge, "create_issue") as create, redirect_stdout(StringIO()):
            self.assertEqual(main(["run", "delivery", "--repo", "Example/Engine", "--dry-run", "--request", "Synthetic", "--max-tokens", "1"]), 1)
        issues.assert_not_called()
        create.assert_not_called()


class WatchIdentityTests(unittest.TestCase):
    @patch("foreman.github.subprocess.run")
    def test_real_forge_preserves_uuid_identity_for_crash_recovery_on_both_writes(self, gh):
        key = "Example/Engine#8@1f1e5777-9a8b-6b54-8002-46d0b9af3677"
        expected_marker = f"<!-- foreman-watch-run: {key} -->"
        for existing in (False, True):
            with self.subTest(existing=existing):
                issues = [{"number": 42, "state": "open", "body": MARKER}] if existing else []
                comments = []
                writes = []
                def api(argv, **kwargs):
                    if "--method" in argv:
                        body = next(arg.removeprefix("body=") for arg in argv if arg.startswith("body="))
                        writes.append(body)
                        if argv[2].endswith("/comments"):
                            comments.append({"body": body})
                            response = {"id": 100, "body": body}
                        else:
                            issues.append({"number": 42, "state": "open", "body": body})
                            response = {"number": 42}
                    else:
                        response = [comments if "/comments?" in argv[2] else issues]
                    return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")
                gh.side_effect = api
                state = {**halted_state(), "halt_detail": f"Synthetic failure {SYNTHETIC_GITHUB}"}
                expected = {"issue": 42, "action": "commented" if existing else "filed"}
                self.assertEqual(watch(state, LiveForge(), "Example/Target", key), expected)
                self.assertEqual(watch(state, LiveForge(), "Example/Target", key), expected)
                self.assertEqual(len(writes), 1)
                self.assertIn(expected_marker, writes[0].splitlines())
                self.assertIn("[redacted:GitHub Token]", writes[0])
                self.assertNotIn(SYNTHETIC_GITHUB, writes[0])

    @patch("foreman.github.subprocess.run")
    def test_unterminated_key_cannot_consume_recovery_markers(self, gh):
        key = "Example/Engine#8@1f1e5777-9a8b-6b54-8002-46d0b9af3677"
        state = {**halted_state(), "halt_detail": "-----BEGIN " + "PRIVATE KEY-----\nsynthetic key material"}
        for repeat in (False, True):
            with self.subTest(repeat=repeat):
                gh.side_effect = ([gh_response("watch-issues.json"), subprocess.CompletedProcess([], 0, '[[]]', ""),
                                       subprocess.CompletedProcess([], 0, '[[]]', ""),
                                   gh_response("watch-comment-created.json")] if repeat else [
                                   subprocess.CompletedProcess([], 0, '[[]]', ""), subprocess.CompletedProcess([], 0, '{"number":42}', "")])
                watch(state, LiveForge(), "Example/Target", key)
                body = next(arg.removeprefix("body=") for arg in gh.call_args.args[0] if arg.startswith("body="))
                self.assertIn(f"<!-- foreman-watch-run: {key} -->", body.splitlines())
                self.assertIn("[redacted:Private Key]", body)
                self.assertNotIn("synthetic key material", body)
                if not repeat:
                    self.assertEqual(body.splitlines()[-1], MARKER)
