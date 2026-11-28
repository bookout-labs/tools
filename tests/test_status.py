"""The issue's status comment and its run report (rules.reporting): TR17, TR20 and a crash between a checkpoint and the comment."""

from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml

from foreman.config import load_config
from foreman.delivery import LiveRun
from foreman.github import BranchProbe, DOING_LABEL, DONE_LABEL, NEEDS_ANSWER_LABEL, STOPPED_LABEL, ForgeError, LiveForge
from foreman.outcomes import HALTS, PAUSES, RELEASED_STOPS, Halt
from foreman.readiness import Escape, MergeState
from foreman.report import BLUEPRINT_VERSION, COLUMNS
from foreman.stage_output import HumanPrompt, VerifyState
from foreman.stages import STAGES
from foreman.state import ChecksState, DeliveryResult, PlanState, ReviewState, RunState
from foreman.status import EVIDENCE_LIMIT, STATUS_MARKER, Status, shows, status_of, write_status
from foreman.runner import run_live_delivery
from foreman.steps import CANCELLATION
from foreman.worker import CliWorker
from foreman.workspace import WorkspaceError
from publication_fixture import SUBSCRIBED
from skill_fixture import checker_reply, fixture_skills
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, WakeTest, comment, fixture_process, priced_quota, stage_text, worker_reply
from container_double import container_route

HEAD, MERGE = "4f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d", "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d"


def result(stop, *, base="b" * 40, **fields) -> DeliveryResult:
    return {"run": RunState(repo="Example/Engine", issue=8, base_sha=base), "plan": PlanState(), "checks": ChecksState(verified_head=HEAD),
            "review": ReviewState(), "path": ["merge"], "stop": stop, **fields}


class StatusTest(unittest.TestCase):
    def test_every_checkpoint_maps_to_one_label(self):
        self.assertIsNone(status_of(result("blocked"), "triage", None))
        self.assertIsNone(status_of(result(""), "spec", None))
        for stop in RELEASED_STOPS:
            with self.subTest(stop=stop):
                self.assertEqual(status_of(result(stop), "merge", None).label, DONE_LABEL)
        for stop in PAUSES:
            with self.subTest(stop=stop):
                self.assertEqual(status_of(result(stop), "merge", None).label, DOING_LABEL)
        prompt = HumanPrompt(stage="spec", question="Which items?", revision="a" * 64)
        asked = status_of(result("needs_human"), "spec", prompt)
        self.assertEqual((asked.label, asked.facts), (NEEDS_ANSWER_LABEL, [prompt.reply]))
        for code in Halt:
            with self.subTest(code=code):
                halted = result(code.value, halt=code.value, halt_detail="Synthetic detail.")
                admitted = status_of(halted, "merge", None)
                self.assertEqual(admitted.label, STOPPED_LABEL)
                self.assertIn(f"Foreman halted with {code}. {HALTS[code].reason}", admitted.text)
                self.assertIn("Synthetic detail.", admitted.text)
                self.assertEqual("Rerun Foreman on this issue to resume." in admitted.text, HALTS[code].recovery == "retry")
                self.assertEqual(admitted.facts, [])
                # Before admission Foreman owns no label.
                self.assertIsNone(status_of(result(code.value, base="", halt=code.value, halt_detail="Synthetic detail."), "triage", None).label)

    def test_outcomes_carry_their_links(self):
        evidence = "https://github.com/Example/Engine/pull/12#issuecomment-91"
        merged = status_of(result("merged", merge=MergeState(merge_commit_sha=MERGE, observation={"pr": {"number": 12}}),
                                  released=["Example/Engine#9"], verify=VerifyState(evidence_url=evidence)), "merge", None)
        self.assertEqual(merged.facts, ["Pull request: #12", f"Verified head: {HEAD}", f"Merge commit: {MERGE}",
                                        f"Verify evidence: {evidence}", "Released: Example/Engine#9"])
        closed = status_of(result("closed", dispatch_plan={"target": {"number": 14}}), "merge", None)
        self.assertEqual(closed.facts, ["Pull request: #14"])
        filed = status_of(result("children_filed", released=["Example/Engine#101"]), "spec", None)
        self.assertEqual(filed.facts, ["Released: Example/Engine#101"])

    def test_the_terminal_report_lists_each_escape_with_its_evidence(self):
        escapes = [Escape(kind="hand_fix", key="e" * 40, evidence="Commit by octocat on the pull request's branch."),
                   Escape(kind="production_check", key=MERGE, evidence="The production check exited 1."),
                   Escape(kind="revert", key=MERGE, evidence="Revert issue #42 and regression issue #41.")]
        for stop, extra in (("reverted", {}), ("merged", {}), ("production_unconfirmed", {"halt": "production_unconfirmed", "halt_detail": "Failed."})):
            with self.subTest(stop):
                status = status_of(result(stop, merge=MergeState(merge_commit_sha=MERGE, escapes=escapes), **extra), "merge", None)
                for name, escape in zip(("Hand fix", "Failed production check", "Revert"), escapes):
                    self.assertIn(f"Escape: {name} ({escape.key}).\n\n{escape.evidence}", status.text)

    def test_long_evidence_is_cut_and_says_so_while_the_report_is_never_cut(self):
        github, report = FakeGitHub(["Ready"]), "Run report\n\n" + "\n".join(f"| implement | edit | m{n} |" for n in range(300))
        write_status(github, "Example/Engine", 8, Status(STOPPED_LABEL, "Foreman halted with git_failed. " + "e " * 40_000, report=report))
        body = github.status["Example/Engine", 8]
        self.assertLess(len(body), 65_536)
        self.assertIn(f"Foreman cut this text at {EVIDENCE_LIMIT} characters.", body)
        self.assertIn(report, body)


    def test_a_live_halts_detail_is_redacted_on_the_runs_own_status_comment(self):
        secret, status, fake = "ghp_" + "a" * 36, list[str](), FakeGitHub()

        def gh(args, **kwargs):
            endpoint = args[2]
            fields = dict(arg.split("=", 1) for arg in args if "=" in arg and not arg.startswith("repos/"))
            if args[1] == "issue" or endpoint.startswith(("repos/Example/Engine/labels", "repos/Example/Engine/issues/8/comments")):
                if "--method" in args and "body" in fields:
                    status.append(fields["body"])
                result = [[]] if "?" in endpoint else {}  # Foreman's status mirror on the run's own issue.
            elif endpoint == "repos/Example/Engine/issues/8":
                result = {"number": 8, "repository_url": "https://api.github.com/repos/Example/Engine", "title": "Issue", "body": fake.body,
                          "labels": [{"name": "slice"}, {"name": "Ready"}], "user": {"login": "octocat"}}
            elif args[1] == "repo":
                result = {"defaultBranchRef": {"name": "main"}}
            elif args[1] == "variable":
                result = [{"name": "FOREMAN_HUMAN_LOGIN", "value": "fixture-owner"}]
            elif endpoint.endswith("/collaborators/fixture-owner/permission"):
                result = {"permission": "write", "role_name": "write"}
            elif "/rules/branches/" in endpoint:
                result = [{"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}}]
            elif endpoint.endswith("/dependencies/blocked_by"):
                result = [[]]
            else:
                raise AssertionError(f"The run made a GitHub call outside its own issue: {args}")
            return subprocess.CompletedProcess(args, 0, json.dumps(result), "")
        root = Path(self.enterContext(TemporaryDirectory())) / "checkout"
        root.mkdir()
        checkpoint = root.parent / "runs.sqlite3"
        workspace = FakeWorkspace(root)
        workspace.create_branch = Mock(side_effect=WorkspaceError("#48 " + secret))
        dispatcher = CliWorker(root, lambda args, *unused: worker_reply(args, checker_reply(args) or {"triage": "supplied_slice"}), scratch_root=root.parent, containers=container_route)
        real_run = subprocess.run
        with (patch("foreman.github.subprocess.run", side_effect=lambda args, **kwargs: gh(args, **kwargs) if args[0] == "gh" else real_run(args, **kwargs)),
              patch.object(LiveForge, "app_installation", return_value="installed"), patch.object(LiveForge, "app_subscription", return_value=SUBSCRIBED),
              # Admission's full check runs the branch probe (doctor.full), which this run's GitHub double does not serve.
              patch.object(LiveForge, "branch_probe", return_value=BranchProbe(dequeued=True, head_read=True))):
            for _ in range(2):
                halted = run_live_delivery(repo="Example/Engine", issue=8, checkpoint=checkpoint, dispatcher=dispatcher,
                                           run=LiveRun(LiveForge(), workspace, skills=fixture_skills))
                self.assertEqual(halted["halt"], "git_failed")
        self.assertTrue(status)
        for body in status:
            self.assertNotIn(secret, body)
        self.assertTrue(any("#48 [redacted:GitHub Token]" in body for body in status))

TARGET = Path(__file__).parents[1] / "docs" / "graph" / "blueprint" / "target.yaml"
BUILT = {"implementation": "built"}


def facts(body: str) -> dict[str, str]:
    """The run report's fact lines, by name."""
    report = body.partition("Run report\n")[2]
    return dict(line.split(": ", 1) for line in report.splitlines() if ": " in line and not line.startswith("|"))


def rows(body: str) -> list[list[str]]:
    """The worker-call table's rows under its header, each as its seven cells."""
    lines = [line for line in body.partition("Run report\n")[2].splitlines() if line.startswith("| ")]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines[2:]]


class RunReportTest(WakeTest):
    def reports(self, issue=8) -> list[str]:
        return [body for _, number, body in self.github.status_writes if number == issue]

    def assert_one_stage(self):
        """One comment, one stage: the state text above each report names the stage its Stage line names."""
        reported = [body for body in self.reports() if "Run report" in body]
        self.assertTrue(reported)
        for body in reported:
            if named := re.search(r"Foreman (?:is running|needs an answer in) (\w+)", body.partition("Run report")[0]):
                self.assertEqual(named[1], facts(body)["Stage"], body)

    def failing_once(self, printed: str) -> str:
        """A check command that prints this text and fails the first time it runs, then passes."""
        counter = self.checkpoint.parent / "checks"
        return (f"{sys.executable} -c \"import pathlib, sys; p = pathlib.Path('{counter}'); n = p.exists(); p.touch(); "
                f"print('{printed}'); sys.exit(0 if n else 7)\"")

    def test_tr17_every_rewrite_reports_the_step_under_way_and_only_what_foreman_names(self):
        """TR17: a run from triage to a published pull request with one failed check in implement's edit step, through verify
        and review, waits in arm. The check's output, the builder's answer and a provider's error text hold marked strings,
        and one rewrite in the middle fails."""
        marks = ("MARKEDCHECKOUTPUT", "MARKEDBUILDERANSWER", "MARKEDPROVIDERERROR")
        reply, quota = FakeWorkspace.reply, []

        def provider_error_once(workspace, args, output):
            if "implementation" in output and not quota:
                quota.append(args[0])
                process = fixture_process(args, "quota", 1)
                return subprocess.CompletedProcess(args, 1, process.stdout, f"{marks[2]} {process.stderr}")
            return reply(workspace, args, output)
        self.enterContext(patch.object(FakeWorkspace, "reply", provider_error_once))
        upsert, writes = self.github.upsert_comment, []

        def fails_fifth(*args, **kwargs):
            writes.append(args)
            if len(writes) == 5:
                raise ForgeError("Injected status failure.")
            return upsert(*args, **kwargs)
        self.enterContext(patch.object(self.github, "upsert_comment", fails_fifth))
        readiness = self.github.readiness
        self.github.readiness = lambda *args: readiness(*args).model_copy(update={"outcome": "pending"})
        answer = {"implementation": f"built {marks[1]}"}
        waiting = self.walk([{"triage": "supplied_slice"}, answer, answer, answer, APPROVE], check=self.failing_once(marks[0]))
        self.assertEqual(waiting["stop"], "merge_pending", waiting.get("halt_detail"))
        self.assertTrue(quota)
        bodies = self.reports()
        pins = (waiting["pstack_pin"]["commit"], waiting["matt_skills_pin"]["commit"])
        taken: list[str] = []
        for body in bodies:
            fact = facts(body)
            for name in ("Repository", "Issue", "Blueprint version", "Stage", "Step", "Steps taken", "Skill pins",
                         "Plan revisions", "Failed checks", "Blocking rounds", "Tokens", "Dollars"):
                self.assertIn(name, fact, body)
            steps = fact["Steps taken"].split()
            self.assertEqual(steps[-1], f"{fact['Stage']}.{fact['Step']}")
            self.assertEqual(steps[:len(taken)], taken)
            taken = steps
            self.assertEqual(fact["Blueprint version"], str(BLUEPRINT_VERSION))
            self.assertTrue(all(pin in fact["Skill pins"] for pin in pins))
            self.assertRegex(fact["Tokens"], r"^\d+ of \d+$")
            self.assertRegex(fact["Dollars"], r"^(\$[\d.]+|unknown) of \$[\d.]+$")
            for mark in marks:
                self.assertNotIn(mark, body)
            self.assertNotIn(str(self.cwd.parent), body)
            self.assertNotIn(self.pin.repository, body)
            self.assertNotIn("/Users/", body.partition("Run report")[2])
            self.assertTrue(body.endswith(STATUS_MARKER))
        self.assertEqual(facts(bodies[0])["Steps taken"], "triage.admit triage.route")
        running = [body for body in bodies if rows(body) and rows(body)[-1][4] == "running"]
        self.assertTrue(running)
        for body in running:
            self.assertEqual(rows(body)[-1][5:], ["", ""])
        final = bodies[-1]
        self.assertEqual((facts(final)["Stage"], facts(final)["Step"], facts(final)["Failed checks"]), ("merge", "arm", "1"))
        self.assertEqual(len(rows(final)), len(waiting["attempts"]))
        for row, attempt in zip(rows(final), waiting["attempts"]):
            self.assertEqual(len(row), len(COLUMNS))
            self.assertEqual(f"{row[0]}.{row[1]}", STAGES[attempt["stage"]].step)
            self.assertEqual((row[2], row[4]), (attempt["model"], attempt["outcome"]))
        self.assertIn("quota", [row[4] for row in rows(final)])
        self.assertTrue(any("@" in row[3] and "(" in row[3] for row in rows(final)))
        self.assertEqual(self.github.status["Example/Engine", 8], final)
        self.assert_one_stage()

    def cancel_at_implement(self):
        """Post the human's stop while spec's check runs, after spec's stop read, so implement's entry reads it."""
        reply, posted = FakeWorkspace.reply, []

        def stop_during_check(workspace, args, output):
            if args[-1].startswith(stage_text("spec_check")) and not posted:
                self.github.ruling("foreman stop")
                posted.append(True)
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", stop_during_check):
            cancelled = self.walk([{"triage": "supplied_slice"}])
        self.assertEqual(cancelled["stop"], "cancelled")

    def test_a_looped_edit_that_pauses_keeps_both_edits_and_its_resume_adds_none(self):
        """The list never loses an entry: an edit that loops on its failed check and pauses at its gate or halts
        worker_unavailable shows both implement.edit entries. The gate's answer resumes the edit and adds no third. Ready
        resumes the halt through prepare, the stage's first step, and makes the interrupted call again as a third edit."""
        reply = FakeWorkspace.reply
        for pause in ("gate", "quota"):
            with self.subTest(pause=pause), TemporaryDirectory(dir=self.cwd.parent) as directory:
                self.checkpoint, self.github = Path(directory) / "runs.sqlite3", FakeGitHub()
                edits, quota = [], [pause == "quota"]

                def quota_on_second_edit(workspace, args, output):
                    if "implementation" in output:
                        edits.append(args[0])
                        if len(edits) > 1 and quota[0]:
                            return priced_quota(args)
                    return reply(workspace, args, output)
                second = {**BUILT, "human_question": "Which order?"} if pause == "gate" else BUILT
                with patch.object(FakeWorkspace, "reply", quota_on_second_edit):
                    paused = self.walk([{"triage": "supplied_slice"}, BUILT, second, *[BUILT] * 6], check=self.failing_once("failed"))
                    self.assertEqual(paused["stop"], "needs_human" if pause == "gate" else "worker_unavailable", paused.get("halt_detail"))
                    taken = facts(self.reports()[-1])["Steps taken"].split()
                    self.assertEqual((paused["steps"].count("implement.edit"), taken), (2, paused["steps"]))
                    self.assertEqual(paused["steps"][-2:], ["implement.edit", "implement.edit"])
                    quota[0] = False
                    written = len(self.reports())
                    resumed = self.walk([BUILT, APPROVE], answer="Oldest first." if pause == "gate" else None)
                self.assertEqual(resumed["stop"], "merged", resumed.get("halt_detail"))
                again = [] if pause == "gate" else ["implement.prepare"]
                self.assertEqual(facts(self.reports()[written])["Steps taken"].split(), [*paused["steps"], *again])
                self.assertEqual(resumed["steps"][:len(paused["steps"]) + len(again)], [*paused["steps"], *again])
                self.assertEqual(resumed["steps"].count("implement.edit"), 2 if pause == "gate" else 3)

    def test_tr17_a_stop_before_implement_ends_the_run_with_no_rewrite_before_its_stop_read(self):
        """TR17's second issue: a stop posted after spec's stop read ends the run at implement's entry, and no rewrite comes
        between that entry and its stop read."""
        self.cancel_at_implement()
        bodies = self.reports()
        entered = [body for body in bodies if facts(body).get("Stage") == "implement"]
        self.assertEqual(entered, bodies[-1:])
        self.assertTrue(bodies[-1].startswith("The run was cancelled."))
        self.assertEqual(facts(bodies[-1])["Step"], "prepare")
        self.assert_one_stage()

    def halted_in_implement(self):
        """A run halted env_unavailable in implement. The failed-check cap that TR20 names is still the terminal failed_check_cap
        (waivers V2-83a091f4050035ec, V2-bd4e3c00cdabb593), so this is the halt in implement that Ready resumes today."""
        halted = self.walk([{"triage": "supplied_slice"}], setup=["exit 9"])
        self.assertEqual(halted["stop"], "env_unavailable")
        self.github.ready_again(8)
        return halted

    def test_tr20_a_ready_resume_rewrites_only_after_the_stop_read_it_makes(self):
        """TR20: a run halted in implement resumes when Ready is added, re-entering implement at prepare, and no rewrite of
        the comment comes before prepare's stop read."""
        halted = self.halted_in_implement()
        log: list[str] = []
        records, upsert = self.github.issue_comment_records, self.github.upsert_comment
        self.github.issue_comment_records = lambda *args: (log.append("read"), records(*args))[1]
        self.enterContext(patch.object(self.github, "upsert_comment", lambda *a, **k: (log.append("write"), upsert(*a, **k))[1]))
        written = len(self.reports())
        self.assertEqual(self.walk([BUILT, APPROVE])["stop"], "merged")
        self.assertEqual(log[0], "read")
        first = self.reports()[written]
        self.assertEqual((facts(first)["Stage"], facts(first)["Steps taken"].split()[-1]), ("implement", "implement.prepare"))
        # The resume keeps every step the halted run saved, implement's own included.
        self.assertEqual(facts(first)["Steps taken"].split(), [*halted["steps"], "implement.prepare"])
        self.assert_one_stage()

    def test_tr20_a_halt_before_admission_shows_no_run_report_and_keeps_ready(self):
        pin = self.pin.model_copy(update={"commit": "f" * 40})
        with patch("foreman.runner.load_config", return_value=replace(load_config(), pstack=pin, matt_skills=pin.model_copy(update={"directory": "skills"}))):
            halted = self.walk([])
        self.assertEqual(halted["stop"], "skill_missing")
        status = self.github.status["Example/Engine", 8]
        self.assertIn("Foreman halted with skill_missing.", status)
        self.assertNotIn("Run report", status)
        self.assertIn("Ready", self.github.load_issue("Example/Engine", 8)["labels"])

    def test_tr20_a_new_run_after_a_cancelled_one_replaces_its_report_at_admission(self):
        self.cancel_at_implement()
        self.github.rulings.clear()
        self.github.ready_again(8)
        written = len(self.reports())
        code, _, _ = self.command(["start", "--repo", "Example/Engine", "--issue", "8"], [{"triage": "supplied_slice"}, BUILT, APPROVE])
        self.assertEqual(code, 0)
        first = self.reports()[written]
        self.assertEqual(facts(first)["Steps taken"], "triage.admit triage.route")
        self.assert_one_stage()

    def test_tr20_failed_wakes_leave_one_git_failed_report_that_the_cancelling_rewrite_drops(self):
        self.capped()
        self.github.ruling("foreman stop")
        records = self.github.issue_comment_records
        self.github.issue_comment_records = Mock(side_effect=ForgeError("gh api timed out."))
        self.wake()
        self.wake()
        shown = self.github.status["Example/Engine", 8]
        self.assertEqual(shown.count("Foreman did not continue this run (git_failed)."), 1)
        self.assertIn("Foreman halted with failed_check_cap.", shown)
        self.assertEqual(facts(shown)["Stage"], "implement")
        self.github.issue_comment_records = records
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        stopped = self.wake()[2]
        self.assertEqual((stopped["stop"], self.github.pr_closes[-1]), ("cancelled", ("Example/Engine", 12, CANCELLATION)))
        shown = self.github.status["Example/Engine", 8]
        self.assertNotIn("git_failed", shown)
        self.assertIn("Run report", shown)
        self.assert_one_stage()

    def test_tr20_a_refused_resume_shows_no_run_report_until_the_next_rewrite(self):
        self.halted_in_implement()
        self.github.settings["FOREMAN_MAX_BUDGET_USD"] = "4.5o"
        refused = self.walk([])
        self.assertEqual(refused["stop"], "not_ready")
        shown = self.github.status["Example/Engine", 8]
        self.assertTrue(shown.startswith("Foreman did not resume this run (not_ready)."), shown)
        self.assertNotIn("Run report", shown)
        del self.github.settings["FOREMAN_MAX_BUDGET_USD"]
        self.assertEqual(self.walk([BUILT, APPROVE])["stop"], "merged")
        self.assertIn("Run report", self.github.status["Example/Engine", 8])
        self.assert_one_stage()

    def test_a_crash_between_a_checkpoint_and_its_comment_adopts_the_comment(self):
        """A process that dies after a comment write landed and before its checkpoint moved on leaves a comment the next
        process finds by its marker and rewrites whole from the checkpoint, never a second one."""
        self.github.queue_merges = False
        upsert, crashed = self.github.upsert_comment, []

        def dies_after_landing(*args, **kwargs):
            upsert(*args, **kwargs)
            if len(self.reports()) == 3 and not crashed:
                crashed.append(True)
                raise KeyboardInterrupt("synthetic crash after the comment landed")
        with patch.object(self.github, "upsert_comment", dies_after_landing), self.assertRaises(KeyboardInterrupt):
            self.walk([{"triage": "supplied_slice"}, BUILT, APPROVE])
        waiting = self.walk([BUILT, APPROVE])
        self.assertEqual(waiting["stop"], "merge_pending", waiting.get("halt_detail"))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Doing"))
        self.assertEqual(facts(self.github.status["Example/Engine", 8])["Step"], "observe")

    def test_the_blueprint_version_is_the_drawing_s(self):
        self.assertEqual(BLUEPRINT_VERSION, yaml.safe_load(TARGET.read_text())["blueprint_version"])
