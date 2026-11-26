import tests

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.accept import AcceptError, Accepted, OpenRun, accept_event
from foreman.cli import main
from foreman.stage_output import HumanPrompt
from foreman.state import SCHEMA_VERSION, checkpoint_serializer
from foreman.status import shows
from foreman.steps import CANCELLATION
from foreman.workspace import WorkspaceError
from test_runner import APPROVE, BRANCH, FINDING, FakeGitHub, FakeWorkspace, WakeTest, comment


def delivery(label="Ready", action="labeled", **issue):
    return {"action": action, "label": {"name": label},
            "issue": {"number": 7, "title": "Encode the timecode", "labels": [{"name": "bug"}, {"name": label}], **issue},
            "repository": {"full_name": "Example/Engine"}, "sender": {"login": "octocat"}}


def NO_RUN(repo, issue):
    return None


class AcceptTest(unittest.TestCase):
    def test_adding_ready_to_an_issue_starts_its_run(self):
        self.assertEqual(accept_event("issues", delivery(), NO_RUN), Accepted("start", "Example/Engine", 7))

    def test_the_added_label_decides_not_the_issue_labels(self):
        # An unrelated label added to an issue that already carries Ready does not start it again.
        payload = delivery(label="bug")
        payload["issue"]["labels"].append({"name": "Ready"})
        self.assertIsNone(accept_event("issues", payload, NO_RUN))

    def test_other_events_actions_and_pull_requests_are_ignored(self):
        self.assertIsNone(accept_event("issue_comment", delivery(), NO_RUN))
        self.assertIsNone(accept_event("issues", delivery(action="unlabeled"), NO_RUN))
        self.assertIsNone(accept_event("issues", delivery(pull_request={"url": "https://example.test/pull/7"}), NO_RUN))

    def test_foreman_own_label_writes_never_start_a_run(self):
        # Spec labels each child Ready as the App, and the child starts once, from its start line.
        for login in ("bookout-labs-foreman[bot]", "Bookout-Labs-Foreman[bot]"):
            with self.subTest(login=login):
                self.assertIsNone(accept_event("issues", {**delivery(), "sender": {"login": login}}, NO_RUN))
        # A delivery without a sender, like the blueprint's synthetic one, is decided by its label.
        payload = delivery()
        del payload["sender"]
        self.assertEqual(accept_event("issues", payload, NO_RUN), Accepted("start", "Example/Engine", 7))

    def test_the_retired_factory_label_is_ignored(self):
        self.assertIsNone(accept_event("issues", delivery(label="factory"), NO_RUN))

    def test_a_malformed_delivery_is_rejected(self):
        for payload in ([], {"action": "labeled", "label": {"name": "Ready"}}):
            with self.subTest(payload=payload), self.assertRaises(AcceptError):
                accept_event("issues", payload, NO_RUN)
        for repository, number in (("not a repo", 7), ("Example/Engine", 0), ("Example/Engine", True)):
            payload = delivery()
            payload["repository"]["full_name"], payload["issue"]["number"] = repository, number
            with self.subTest(repository=repository, number=number), self.assertRaises(AcceptError):
                accept_event("issues", payload, NO_RUN)

    def test_the_command_reads_the_event_header_and_json_body(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            for name, payload, line in (("issues", delivery(), "start Example/Engine#7"), ("issues", delivery(label="bug"), "ignored")):
                path.write_text(json.dumps(payload))
                output = io.StringIO()
                with self.subTest(line=line), redirect_stdout(output):
                    self.assertEqual(main(["accept", "--event", name, str(path)]), 0)
                    self.assertEqual(output.getvalue().strip(), line)
            path.write_text("{")
            self.assertEqual(main(["accept", "--event", "issues", str(path)]), 1)


class ReplyMarkerTest(unittest.TestCase):
    prompt = HumanPrompt(stage="implement", question="Keep the sort order?", revision="ab" * 32)

    def test_an_answer_is_the_text_under_a_first_line_that_is_exactly_the_marker(self):
        marker = self.prompt.answer_marker
        for body, answer in ((f"{marker}\nKeep it.", "Keep it."), (f"  {marker}\r\nKeep it.\r\nIn place.\r\n", "Keep it.\r\nIn place."),
                             (f"{marker}\ncancel", "cancel"), (f"{marker}\n", None), (f"{marker} Keep it.", None),
                             (f"Re: {marker}\nKeep it.", None), (f"Keep it.\n{marker}", None)):
            with self.subTest(body=body):
                self.assertEqual(self.prompt.answer_in(body), answer)
        self.assertIsNone(self.prompt.model_copy(update={"revision": None}).answer_in(f"{marker}\nKeep it."))

    def test_a_comment_wakes_only_the_paused_run_it_answers_and_a_stop_wakes_any_open_run(self):
        """An answer wakes only the run paused on its question. A stop wakes a run that is running, waiting, halted or paused
        at a gate. A run that ended with a result other than a halt is no open run, so nothing wakes it."""
        gate, other = OpenRun("Fixture-Owner", self.prompt), OpenRun("fixture-owner", None)
        answer, stop = f"{self.prompt.answer_marker}\nKeep it.", "foreman stop\nNo longer needed."
        for run, payload, wakes in (
                (gate, comment(answer), "answer"), (gate, comment(answer, action="edited"), None),
                (gate, comment(answer, login="someone-else"), None), (gate, comment("Keep it."), None),
                (gate, comment(f"{self.prompt.answer_marker}\n"), None), (gate, comment(answer, pull_request={}), None),
                (gate, comment(stop), "stop"), (None, comment(answer), None), (None, comment(stop), None),
                (other, comment(stop), "stop"), (other, comment(stop, action="edited"), None),
                (other, comment("foreman stop", login="someone-else"), None), (other, comment(answer), None)):
            with self.subTest(run=run, payload=payload):
                decision = accept_event("issue_comment", payload, lambda repo, issue: run)
                self.assertEqual(decision, Accepted("wake", "Example/Engine", 8) if wakes else None)
        with self.assertRaises(AcceptError):
            accept_event("issue_comment", {"action": "created", "issue": {"number": 8}}, lambda repo, issue: gate)


class AnswerWakeTest(WakeTest):
    """The App's issue comment webhook wakes a run paused at a gate on its reply marker, and any open run on a stop
    (rules.gates.continuation). The wake is no start, so it runs no doctor and keeps the login and caps the start saved."""

    def test_an_answer_continues_the_paused_run_once_and_nothing_else_wakes_it(self):
        """TR14: a run paused at implement's gate. The saved login's comment whose first line is the reply marker is delivered
        twice and then edited. On a second paused run, Ready is added, another login posts the marker and the saved login
        posts a comment without it. Only the first delivery wakes a run, and it continues at edit without doctor."""
        question = "Keep the sort order?"
        paused = self.walk([{"triage": "supplied_slice"}, {"implementation": "built", "human_question": question}])
        self.assertEqual(paused["stop"], "needs_human")
        reads = list(self.github.variable_reads)
        self.github.settings.update({"FOREMAN_HUMAN_LOGIN": "someone-else", "FOREMAN_MAX_BUDGET_USD": "4.5o"})
        self.github.answer(self.question, "Keep it.")
        answer = self.github.rulings[-1].body
        self.assertEqual(self.deliver("issue_comment", comment(answer)), "wake Example/Engine#8")
        code, _, woken = self.command(["wake", "--repo", "Example/Engine", "--issue", "8"], [{"implementation": "built"}, APPROVE])
        self.assertEqual((code, woken["stop"], woken["human_answers"]), (0, "merged", ["Keep it."]))
        self.assertEqual(woken["plan"].human_qa[-1]["stage"], "implement")
        self.assertEqual(woken["path"][len(paused["path"]):][:2], ["implement", "check"])
        # A start would stop not_ready on the cap that won't parse. Only merge.arm reads a variable, its merge hold.
        self.assertEqual((woken["start"].human_login, woken["start"].max_budget_usd), ("fixture-owner", "3"))
        self.assertEqual(self.github.variable_reads, [*reads, "Example/Engine"])
        merged = len(self.github.merge_calls)
        self.assertEqual(self.deliver("issue_comment", comment(answer)), "ignored")
        self.assertEqual(self.deliver("issue_comment", comment(answer + "\nIn place.", action="edited")), "ignored")
        self.assertEqual(self.command(["wake", "--repo", "Example/Engine", "--issue", "8"])[:2], (0, "not waiting example/engine#8\n"))
        self.assertEqual(len(self.github.merge_calls), merged)

        self.checkpoint, self.github = self.cwd.parent / "second.sqlite3", FakeGitHub()
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built", "human_question": question}])["stop"], "needs_human")
        reads, writes, calls = list(self.github.variable_reads), list(self.github.status_writes), len(self.calls)
        ready = {"action": "labeled", "label": {"name": "Ready"}, "issue": {"number": 8}, "repository": {"full_name": "Example/Engine"}}
        self.assertEqual(self.deliver("issues", ready), "start Example/Engine#8")
        self.assertEqual(self.command(["start", "--repo", "Example/Engine", "--issue", "8"])[:2], (0, "already started example/engine#8\n"))
        self.github.answer(self.question, "Keep it.", login="someone-else")
        self.assertEqual(self.deliver("issue_comment", comment(self.github.rulings[-1].body, login="someone-else")), "ignored")
        self.github.ruling("Keep it.")
        self.assertEqual(self.deliver("issue_comment", comment("Keep it.")), "ignored")
        self.github.answer(self.question, "Keep it.")
        self.assertEqual(self.deliver("issue_comment", comment(self.github.rulings[-1].body, action="edited")), "ignored")
        self.assertEqual((self.github.variable_reads, self.github.status_writes, len(self.calls)), (reads, writes, calls))

    def test_an_answer_to_an_older_schema_checkpoint_is_refused_with_the_schema_error(self):
        """A run paused by an older release has no saved login to match, so accept reports the checkpoint's schema and wakes nothing."""
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built", "human_question": "Keep the sort order?"}])["stop"],
                         "needs_human")
        with SqliteSaver.from_conn_string(str(self.checkpoint)) as saver:
            saver.serde = checkpoint_serializer()
            saved = saver.get_tuple({"configurable": {"thread_id": "example/engine#8"}})
            channels = saved.checkpoint["channel_values"]
            channels["schema_version"] = SCHEMA_VERSION - 1
            del channels["start"]
            saver.put(saved.config, saved.checkpoint, saved.metadata, {})
        self.github.answer(self.question, "Keep it.")
        path = self.cwd / "delivery.json"
        path.write_text(json.dumps(comment(self.github.rulings[-1].body)))
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = main(["accept", "--event", "issue_comment", str(path)], checkpoint=self.checkpoint)
        self.assertEqual((code, out.getvalue()), (1, ""))
        for text in (str(self.checkpoint), "example/engine#8", f"schema_version {SCHEMA_VERSION - 1}; expected {SCHEMA_VERSION}", "older release"):
            self.assertIn(text, err.getvalue())

    def test_a_stop_wakes_a_waiting_run_which_ends_cancelled_at_the_next_stage_entry(self):
        """A stop under the saved login wakes a run waiting on the merge queue, and merge's stop read ends it cancelled.
        Another login's stop, and a comment that is not a stop, wake nothing."""
        self.github.queue_merges = False
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_pending")
        reads = list(self.github.variable_reads)
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop", login="someone-else")), "ignored")
        self.assertEqual(self.deliver("issue_comment", comment("Please stop soon.")), "ignored")
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        code, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], self.github.pr_closes), ("cancelled", [("Example/Engine", 12, CANCELLATION)]))
        self.assertEqual(self.github.variable_reads, reads)
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "ignored")

    def test_a_wake_never_starts_a_run(self):
        """A wake for an issue with no checkpoint starts nothing. Only Ready starts a run."""
        reads, writes = list(self.github.variable_reads), list(self.github.status_writes)
        self.assertEqual(self.command(["wake", "--repo", "Example/Engine", "--issue", "8"])[:2], (0, "not waiting example/engine#8\n"))
        self.assertEqual((self.github.variable_reads, self.github.status_writes, self.calls), (reads, writes, []))
        self.assertFalse(self.checkpoint.exists())

    def test_a_wake_never_resumes_a_halted_run(self):
        """A wake that finds the run halted, as when an earlier wake continued it, reads the issue for a stop and resumes
        nothing when it finds none. Only Ready resumes it."""
        self.github.hold = "Holding merges."
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_held")
        reads, writes, calls, arms = list(self.github.variable_reads), list(self.github.status_writes), len(self.calls), len(self.github.merge_calls)
        code, _, held = self.wake()
        self.assertEqual((code, held["stop"]), (75, "merge_held"))
        self.assertEqual((self.github.variable_reads, self.github.status_writes, len(self.calls), len(self.github.merge_calls)),
                         (reads, writes, calls, arms))

    def test_a_stop_wakes_a_gate_paused_or_halted_run_and_ends_it_cancelled(self):
        """TR15: a run paused at implement's gate with its PR open, and one halted at its verify cap with its PR open. Each stop
        comment wakes its run, which ends cancelled and closes its PR with no worker call. The halted run resumes no step, and
        Ready added after it admits a new run at triage. The run waiting on worker capacity is left out: #379 deletes that wait."""
        built = {"implementation": "built"}
        asked = self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]},
                           {**built, "human_question": "Keep the sort order?"}])
        self.assertEqual((asked["stop"], self.github.opened[BRANCH].state), ("needs_human", "OPEN"))
        self.checkpoint, gate = self.cwd.parent / "halted.sqlite3", self.github
        self.github = FakeGitHub()
        halted = self.capped()
        calls = len(self.calls)
        for checkpoint, github, stage in ((self.cwd.parent / "runs.sqlite3", gate, "implement"), (self.checkpoint, self.github, "verify")):
            with self.subTest(stage):
                self.checkpoint, self.github = checkpoint, github
                self.github.ruling("foreman stop")
                self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
                _, _, stopped = self.wake()
                self.assertEqual((stopped["stop"], stopped.get("halt"), self.github.pr_closes),
                                 ("cancelled", None, [("Example/Engine", 12, CANCELLATION)]))
                self.assertEqual(stopped["path"][-1], halted["path"][-1] if stage == "verify" else "implement")
                self.assertTrue(shows(self.github, "Example/Engine", 8, "Done"))
        self.assertEqual(len(self.calls), calls)
        self.github.ready_again(8)
        ready = {"action": "labeled", "label": {"name": "Ready"}, "issue": {"number": 8}, "repository": {"full_name": "Example/Engine"}}
        self.assertEqual(self.deliver("issues", ready), "start Example/Engine#8")
        _, _, again = self.command(["start", "--repo", "Example/Engine", "--issue", "8"], [{"triage": "supplied_slice"}, built])
        # The earlier stops are older than the new Ready, so the new run is admitted and goes on past its stage entries.
        self.assertEqual((again["path"][0], bool(again["run"].base_sha), "prepare" in again["path"]), ("triage", True, True))
        self.assertNotEqual(again["stop"], "cancelled")


    def test_a_stop_that_ends_the_run_ends_the_wake(self):
        """A stop that ends a gate-paused run cancelled ends the wake there. The resumed run's identity is not checked again,
        so a checkout whose origin no longer matches cannot replace the cancelled result."""
        built = {"implementation": "built"}
        self.walk([{"triage": "supplied_slice"}, built, {**APPROVE, "findings": [FINDING]}, {**built, "human_question": "Keep the sort order?"}])
        self.github.ruling("foreman stop")
        with patch.object(FakeWorkspace, "validate_origin", side_effect=WorkspaceError("The checkout's origin moved.")):
            _, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], self.github.pr_closes), ("cancelled", [("Example/Engine", 12, CANCELLATION)]))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Done"))
        self.assertEqual(self.wake()[:2], (0, "not waiting example/engine#8\n"))

    def test_a_wake_resumes_no_run_whose_process_died_and_a_stop_ends_it(self):
        """A run whose process died during the builder's call is not waiting, paused or halted. A wake that finds no stop
        resumes nothing and writes nothing, and the stop's wake ends the run cancelled with no worker call."""
        reply = FakeWorkspace.reply

        def killed(workspace, args, output):
            if "implementation" in output:
                raise RuntimeError("Foreman's process was killed.")
            return reply(workspace, args, output)
        with patch.object(FakeWorkspace, "reply", killed), self.assertRaisesRegex(RuntimeError, "killed"):
            self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}])
        calls, writes = len(self.calls), list(self.github.status_writes)
        _, _, unchanged = self.wake()
        self.assertEqual((unchanged["stop"], len(self.calls), self.github.status_writes), ("", calls, writes))
        self.github.ruling("foreman stop")
        self.assertEqual(self.deliver("issue_comment", comment("foreman stop")), "wake Example/Engine#8")
        _, _, stopped = self.wake()
        self.assertEqual((stopped["stop"], len(self.calls)), ("cancelled", calls))
        self.assertTrue(shows(self.github, "Example/Engine", 8, "Done"))

    def test_ready_after_a_run_ended_otherwise_than_cancelled_starts_nothing(self):
        """Only a run that ended cancelled is replaced when Ready is added again (intake.policy). Ready added to an answered
        question prints already started and calls no worker."""
        self.github = FakeGitHub(["Ready"])
        self.assertEqual(self.walk([{"triage": "question"}, {"research": "Yes, it sorts."}])["stop"], "answered")
        calls = len(self.calls)
        self.github.ready_again(8)
        self.assertEqual(self.command(["start", "--repo", "Example/Engine", "--issue", "8"])[:2], (0, "already started example/engine#8\n"))
        self.assertEqual(len(self.calls), calls)


if __name__ == "__main__":
    unittest.main()
