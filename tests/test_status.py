"""The issue's status comment and its run report (rules.reporting): TR17, TR20 and a crash between a checkpoint and the comment."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import yaml

from foreman.config import load_config
from foreman.github import DOING_LABEL, DONE_LABEL, NEEDS_ANSWER_LABEL, STOPPED_LABEL, ForgeError
from foreman.outcomes import HALTS, PAUSES, RELEASED_STOPS, Halt
from foreman.readiness import MergeState
from foreman.report import BLUEPRINT_VERSION, COLUMNS
from foreman.stage_output import HumanPrompt
from foreman.stages import STAGES
from foreman.state import ChecksState, DeliveryResult, PlanState, ReviewState, RunState
from foreman.status import EVIDENCE_LIMIT, STATUS_MARKER, Status, shows, status_of, write_status
from foreman.steps import CANCELLATION
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, WakeTest, comment, fixture_process, stage_text

HEAD, MERGE = "4f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d", "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d"


def result(stop, *, base="b" * 40, **fields) -> DeliveryResult:
    return {"run": RunState(repo="Example/Engine", issue=8, base_sha=base), "plan": PlanState(), "checks": ChecksState(verified_head=HEAD),
            "review": ReviewState(), "path": ["merge"], "stop": stop, **fields}


class StatusTest(unittest.TestCase):
    def test_every_checkpoint_maps_to_one_label(self):
        self.assertIsNone(status_of(result("blocked"), "triage", None, "bookout-labs/tools"))
        self.assertIsNone(status_of(result(""), "spec", None, "bookout-labs/tools"))
        for stop in RELEASED_STOPS:
            with self.subTest(stop=stop):
                self.assertEqual(status_of(result(stop), "merge", None, "bookout-labs/tools").label, DONE_LABEL)
        for stop in PAUSES:
            with self.subTest(stop=stop):
                self.assertEqual(status_of(result(stop), "merge", None, "bookout-labs/tools").label, DOING_LABEL)
        prompt = HumanPrompt(stage="spec", question="Which items?", revision="a" * 64)
        asked = status_of(result("needs_human"), "spec", prompt, "bookout-labs/tools")
        self.assertEqual((asked.label, asked.facts), (NEEDS_ANSWER_LABEL, [prompt.reply]))
        for code in Halt:
            with self.subTest(code=code):
                halted = result(code.value, halt=code.value, halt_detail="Synthetic detail.", watch={"issue": 26, "action": "filed"})
                admitted = status_of(halted, "merge", None, "bookout-labs/tools")
                self.assertEqual(admitted.label, STOPPED_LABEL)
                self.assertIn(f"Foreman halted with {code}. {HALTS[code].reason}", admitted.text)
                self.assertIn("Synthetic detail.", admitted.text)
                self.assertEqual("Rerun Foreman on this issue to resume." in admitted.text, HALTS[code].recovery == "retry")
                self.assertEqual(admitted.facts, ["Watch: bookout-labs/tools#26"])
                # Before admission Foreman owns no label.
                self.assertIsNone(status_of(result(code.value, base="", halt=code.value, halt_detail="Synthetic detail."), "triage", None, "x/y").label)

    def test_outcomes_carry_their_links(self):
        merged = status_of(result("merged", merge=MergeState(merge_commit_sha=MERGE, observation={"pr": {"number": 12}}),
                                  released=["Example/Engine#9"]), "merge", None, "bookout-labs/tools")
        self.assertEqual(merged.facts, ["Pull request: #12", f"Verified head: {HEAD}", f"Merge commit: {MERGE}", "Released: Example/Engine#9"])
        closed = status_of(result("closed", dispatch_plan={"target": {"number": 14}}), "merge", None, "bookout-labs/tools")
        self.assertEqual(closed.facts, ["Pull request: #14"])
        filed = status_of(result("children_filed", released=["Example/Engine#101"]), "spec", None, "bookout-labs/tools")
        self.assertEqual(filed.facts, ["Released: Example/Engine#101"])

    def test_long_evidence_is_cut_and_says_so_while_the_report_is_never_cut(self):
        github, report = FakeGitHub(["Ready"]), "Run report\n\n" + "\n".join(f"| implement | edit | m{n} |" for n in range(300))
        write_status(github, "Example/Engine", 8, Status(STOPPED_LABEL, "Foreman halted with git_failed. " + "e " * 40_000, report=report))
        body = github.status["Example/Engine", 8]
        self.assertLess(len(body), 65_536)
        self.assertIn(f"Foreman cut this text at {EVIDENCE_LIMIT} characters.", body)
        self.assertIn(report, body)


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

    def failing_once(self, printed: str) -> str:
        """A check command that prints this text and fails the first time it runs, then passes."""
        counter = self.cwd.parent / "checks"
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

    def test_tr17_a_stop_before_implement_ends_the_run_with_no_rewrite_before_its_stop_read(self):
        """TR17's second issue: a stop posted after spec's stop read ends the run at implement's entry, and no rewrite comes
        between that entry and its stop read."""
        self.cancel_at_implement()
        bodies = self.reports()
        entered = [body for body in bodies if facts(body).get("Stage") == "implement"]
        self.assertEqual(entered, bodies[-1:])
        self.assertTrue(bodies[-1].startswith("The run was cancelled."))
        self.assertEqual(facts(bodies[-1])["Step"], "prepare")

    def halted_in_implement(self):
        """A run halted env_unavailable in implement. The failed-check cap that TR20 names is still the terminal verify_cap
        (waivers V2-83a091f4050035ec, V2-bd4e3c00cdabb593), so this is the halt in implement that Ready resumes today."""
        with patch.object(FakeWorkspace, "run", return_value=9):
            halted = self.walk([{"triage": "supplied_slice"}], preflight="true")
        self.assertEqual(halted["stop"], "env_unavailable")
        self.github.ready_again(8)

    def test_tr20_a_ready_resume_rewrites_only_after_the_stop_read_it_makes(self):
        """TR20: a run halted in implement resumes when Ready is added, re-entering implement at prepare, and no rewrite of
        the comment comes before prepare's stop read."""
        self.halted_in_implement()
        log: list[str] = []
        records, upsert = self.github.issue_comment_records, self.github.upsert_comment
        self.github.issue_comment_records = lambda *args: (log.append("read"), records(*args))[1]
        self.enterContext(patch.object(self.github, "upsert_comment", lambda *a, **k: (log.append("write"), upsert(*a, **k))[1]))
        written = len(self.reports())
        self.assertEqual(self.walk([BUILT, APPROVE], preflight="true")["stop"], "merged")
        self.assertEqual(log[0], "read")
        first = self.reports()[written]
        self.assertEqual((facts(first)["Stage"], facts(first)["Steps taken"].split()[-1]), ("implement", "implement.prepare"))

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

    def test_tr20_failed_wakes_leave_one_git_failed_report_that_the_cancelling_rewrite_drops(self):
        self.capped()
        self.github.ruling("foreman stop")
        records = self.github.issue_comment_records
        self.github.issue_comment_records = Mock(side_effect=ForgeError("gh api timed out."))
        self.wake()
        self.wake()
        shown = self.github.status["Example/Engine", 8]
        self.assertEqual(shown.count("Foreman did not continue this run (git_failed)."), 1)
        self.assertIn("Foreman halted with verify_cap.", shown)
        self.assertEqual(facts(shown)["Stage"], "implement")
        self.github.issue_comment_records = records
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        stopped = self.wake()[2]
        self.assertEqual((stopped["stop"], self.github.pr_closes[-1]), ("cancelled", ("Example/Engine", 12, CANCELLATION)))
        shown = self.github.status["Example/Engine", 8]
        self.assertNotIn("git_failed", shown)
        self.assertIn("Run report", shown)

    def test_tr20_a_refused_resume_shows_no_run_report_until_the_next_rewrite(self):
        self.halted_in_implement()
        self.github.settings["FOREMAN_MAX_BUDGET_USD"] = "4.5o"
        refused = self.walk([], preflight="true")
        self.assertEqual(refused["stop"], "not_ready")
        shown = self.github.status["Example/Engine", 8]
        self.assertTrue(shown.startswith("Foreman did not resume this run (not_ready)."), shown)
        self.assertNotIn("Run report", shown)
        del self.github.settings["FOREMAN_MAX_BUDGET_USD"]
        self.assertEqual(self.walk([BUILT, APPROVE], preflight="true")["stop"], "merged")
        self.assertIn("Run report", self.github.status["Example/Engine", 8])

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
