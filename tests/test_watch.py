import tests

import ast
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from foreman.state import RunState
from foreman.outcomes import HALTS
from foreman.github import Forge, ForgeError, LiveForge, marked
from foreman.redaction import redact
from foreman.watch import run_key, watch
from test_runner import FakeGitHub


REPO = "Example/Engine"
TARGET = "Example/Tools"
KEY = run_key(REPO, 8, 42)
RUN_MARKER = "<!-- foreman-watch-run: Example/Engine#8@42 -->"
MARKER = "<!-- foreman-watch: invalid_output@src/foreman/stages/review.md -->"
FILING = {
    "invalid_output": ("Fix invalid output on review", "stages/review.md"),
    "verify_cap": ("Investigate repeated verify failures reaching cap", "stages/implement.md"),
    "review_cap": ("Investigate repeated review blocks reaching cap", "stages/implement.md"),
    "spec_cap": ("Investigate rejected slice plans reaching cap", "stages/spec.md"),
    "dirty_worktree": ("Handle dirty worktree before verify", "stages/implement.md"),
    "missing_slice": ("Slice issues carry a loadable slice", "github.py"),
    "review_not_posted": ("Reviews post to the pull request", "github.py"),
    "git_failed": ("Git operations complete before publication", "workspace.py"),
    "skill_missing": ("Pinned skills are available before dispatch", "skill_rules.py"),
    "merged_unreviewed": ("Investigate a merge at an unreviewed head", "steps/merge.py"),
}
SKIPPED = {"base_check_failed", "base_protected_changed", "model_dependency_missing", "publication_model_missing", "merge_held", "merge_not_armed", "budget_exceeded",
           "env_unavailable", "publish_failed", "publication_unreconciled",
           "implementation_rejected", "readiness_unavailable", "not_ready"}


def state(halt="invalid_output", detail="bad output", node="review", model="gpt-6.1-sol-high", repo=REPO):
    return dict(run=RunState(repo=repo, issue=8), check="true", path=["triage", node], stop=halt,
                halt=halt, halt_detail=detail, attempts=[dict(stage=node, model=model, outcome="ran",
                                                           commit="pin", methods=[], usage=None)])


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGitHub()
        self.forge = Mock(spec=Forge, wraps=self.fake)

    def test_every_halt_has_an_explicit_policy(self):
        self.assertEqual(set(HALTS), set(FILING) | SKIPPED)

    def test_every_row_files_the_expected_proposal_or_makes_no_forge_calls(self):
        self.assertEqual(KEY, "Example/Engine#8@42")
        self.assertEqual(redact(KEY), KEY)
        self.assertEqual(redact(RUN_MARKER), RUN_MARKER)
        for halt in HALTS:
            with self.subTest(halt=halt):
                forge = Mock(spec=Forge, wraps=FakeGitHub())
                result = watch(state(halt), forge, TARGET, KEY)
                if halt in SKIPPED:
                    self.assertIsNone(result)
                    self.assertEqual(forge.mock_calls, [])
                    continue
                title, file = FILING[halt]
                file = "src/foreman/" + file
                self.assertEqual(result, {"issue": 101, "action": "filed"})
                forge.create_issue.assert_called_once()
                repo, actual_title, body, labels = forge.create_issue.call_args.args
                body = marked(body, forge.create_issue.call_args.kwargs["marker"])
                self.assertEqual((repo, actual_title, labels), (TARGET, title, []))
                self.assertIn(f"File to change: {file}", body.splitlines())
                self.assertTrue(body.startswith(f"Proposed graph fix: {title}\nCause: "))
                marker = f"<!-- foreman-watch: {halt}@{file} -->"
                self.assertEqual(body.splitlines()[-2:], [RUN_MARKER, marker])
                self.assertEqual(redact(marker), marker)

    def test_no_halt_makes_no_forge_calls(self):
        self.assertIsNone(watch(state(None), self.forge, TARGET, KEY))
        self.assertEqual(self.forge.mock_calls, [])

    def test_new_cause_and_repeated_runs_write_once_each(self):
        self.assertEqual(watch(state(), self.forge, TARGET, KEY), {"issue": 101, "action": "filed"})
        self.forge.create_issue.assert_called_once()
        self.forge.comment_issue.assert_not_called()
        self.assertEqual(watch(state(), self.forge, TARGET, KEY), {"issue": 101, "action": "filed"})
        other = run_key(REPO, 9, 43)
        self.assertEqual(watch(state(detail="another detail"), self.forge, TARGET, other), {"issue": 101, "action": "commented"})
        self.assertEqual(watch(state(detail="yet another detail"), self.forge, TARGET, other), {"issue": 101, "action": "commented"})
        self.forge.create_issue.assert_called_once()
        self.forge.comment_issue.assert_called_once_with(TARGET, 101,
            "Evidence: Example/Engine#8; stop=invalid_output; attempts=review/gpt-6.1-sol-high=ran\n", marker="<!-- foreman-watch-run: Example/Engine#9@43 -->")

    def test_detail_stays_out_of_a_watch_repo_other_than_the_runs(self):
        for repo, shown in ((REPO, False), (TARGET, True), (TARGET.lower(), True)):
            with self.subTest(repo=repo):
                fake = FakeGitHub()
                watch(state(detail="private detail", repo=repo), fake, TARGET, KEY)
                watch(state(detail="private detail", repo=repo), fake, TARGET, run_key(REPO, 8, 43))
                for body in (fake.created[0][1], fake.comments[TARGET, 101][0]):
                    self.assertIn(f"Evidence: {repo}#8; stop=invalid_output; attempts=review/gpt-6.1-sol-high=ran\n", body)
                    self.assertEqual("private detail" in body, shown)
                    self.assertEqual("Detail:" in body, shown)

    def test_different_nodes_are_different_causes(self):
        watch(state(), self.forge, TARGET, KEY)
        watch(state(node="implement"), self.forge, TARGET, KEY)
        self.assertEqual(self.forge.create_issue.call_count, 2)
        self.forge.comment_issue.assert_not_called()
        self.assertEqual(self.fake.created[1][0], "Fix invalid output on implement")

    def test_substring_cause_markers_do_not_match(self):
        for body in ("prefix " + MARKER, MARKER + " suffix"):
            with self.subTest(body=body):
                forge = FakeGitHub()
                forge.create_issue(TARGET, "unrelated", body, [], marker="")
                self.assertEqual(watch(state(), forge, TARGET, KEY), {"issue": 102, "action": "filed"})

    def test_lowest_matching_issue_gets_the_comment(self):
        self.fake.create_issue(TARGET, "first", MARKER, [], marker="")
        self.fake.create_issue(TARGET, "second", MARKER, [], marker="")
        self.forge.open_issues.return_value = self.fake.open_issues(TARGET)[::-1]
        self.assertEqual(watch(state(), self.forge, TARGET, KEY), {"issue": 101, "action": "commented"})
        self.forge.create_issue.assert_not_called()
        self.assertEqual(self.forge.comment_issue.call_args.args[:2], (TARGET, 101))

    def test_crash_marker_in_any_matching_body_prevents_a_write(self):
        self.fake.create_issue(TARGET, "first", MARKER, [], marker="")
        self.fake.create_issue(TARGET, "second", RUN_MARKER + "\n" + MARKER, [], marker="")
        self.assertEqual(watch(state(), self.forge, TARGET, KEY), {"issue": 102, "action": "filed"})
        self.forge.create_issue.assert_not_called()
        self.forge.comment_issue.assert_not_called()

    def test_run_markers_match_whole_lines_in_bodies_and_comments(self):
        self.fake.create_issue(TARGET, "first", MARKER + "\nprefix " + RUN_MARKER, [], marker="")
        self.fake.comment_issue(TARGET, 101, RUN_MARKER + " suffix", marker="")
        self.assertEqual(watch(state(), self.forge, TARGET, KEY), {"issue": 101, "action": "commented"})
        self.forge.comment_issue.assert_called_once()

    def test_closed_issue_causes_a_new_proposal(self):
        watch(state(), self.forge, TARGET, KEY)
        self.fake.closed.add(101)
        self.assertEqual(watch(state(), self.forge, TARGET, KEY), {"issue": 102, "action": "filed"})
        self.assertEqual(self.forge.create_issue.call_count, 2)

    def test_multiline_detail_keeps_line_breaks_in_both_writes(self):
        halted = state(detail="first\r\nsecond\nthird with literal `\\n`", repo=TARGET)
        watch(halted, self.forge, TARGET, KEY)
        watch(halted, self.forge, TARGET, run_key(REPO, 8, 43))
        for body in (self.fake.created[0][1], self.fake.comments[TARGET, 101][0]):
            self.assertIn("Detail:\n\n    first\r\n    second\n    third with literal `\\n`\n\n", body)
            self.assertNotIn(r"first\r\nsecond\nthird", body)

    def test_marker_in_detail_does_not_suppress_a_later_run(self):
        other = run_key(REPO, 8, 43)
        marker = f"<!-- foreman-watch-run: {other} -->"
        halted = state(detail=f"first\n{marker}\nthird", repo=TARGET)
        watch(halted, self.forge, TARGET, KEY)
        self.assertEqual(watch(halted, self.forge, TARGET, other), {"issue": 101, "action": "commented"})
        for body in (self.fake.created[0][1], self.fake.comments[TARGET, 101][0]):
            self.assertIn(f"\n    {marker}\n", body)
        self.assertEqual(watch(halted, self.forge, TARGET, other), {"issue": 101, "action": "commented"})
        self.assertEqual(len(self.fake.comments[TARGET, 101]), 1)

    @patch("foreman.github.subprocess.run")
    def test_live_boundary_redacts_both_writes_and_finds_page_two_crash_marker(self, gh):
        def respond(args, **kwargs):
            endpoint = args[2]
            fields = dict(arg.split("=", 1) for arg in args if "=" in arg and not arg.startswith("repos/"))
            if "--method" in args:
                if endpoint.endswith("/comments"):
                    self.fake.comment_issue(TARGET, int(endpoint.split("/")[-2]), fields["body"], marker="")
                    result = {}
                else:
                    result = {"number": self.fake.create_issue(TARGET, fields["title"], fields["body"], [], marker="")}
            elif "/comments?" in endpoint:
                number = int(endpoint.split("/")[-2])
                result = [[{"body": "page one"}], [{"body": body} for body in self.fake.issue_comments(TARGET, number)]]
            else:
                result = [[issue.model_dump() for issue in self.fake.open_issues(TARGET)]]
            return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

        gh.side_effect = respond
        forge = LiveForge()
        secret = "ghp_" + "a" * 36
        halted = state(detail="#48 " + secret, repo=TARGET)
        watch(halted, forge, TARGET, KEY)
        self.assertEqual(watch(halted, forge, TARGET, run_key(REPO, 8, 43)), {"issue": 101, "action": "commented"})
        for body in (self.fake.created[0][1], self.fake.comments[TARGET, 101][0]):
            self.assertNotIn(secret, body)
            self.assertIn("#48 [redacted:GitHub Token]", body)
        self.fake.create_issue(TARGET, "duplicate", MARKER, [], marker="")
        self.assertEqual(watch(halted, forge, TARGET, run_key(REPO, 8, 43)), {"issue": 101, "action": "commented"})
        self.assertEqual(len(self.fake.comments[TARGET, 101]), 1)
        self.assertNotIn((TARGET, 102), self.fake.comments)
        self.assertEqual(len(self.fake.created), 2)

    def test_failed_comment_lookup_does_not_write(self):
        self.fake.create_issue(TARGET, "first", MARKER, [], marker="")
        self.forge.issue_comments.side_effect = ForgeError("page two failed")
        with self.assertRaises(ForgeError):
            watch(state(), self.forge, TARGET, KEY)
        self.forge.create_issue.assert_not_called()
        self.forge.comment_issue.assert_not_called()

    def test_watcher_has_no_workspace_worker_or_adapter_import(self):
        source = Path(__file__).resolve().parents[1] / "src/foreman/watch.py"
        forbidden = ("foreman.workspace", "foreman.worker", "foreman.adapters")
        for node in ast.walk(ast.parse(source.read_text())):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                       else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            self.assertFalse(any(module == name or module.startswith(name + ".") for module in modules for name in forbidden))
