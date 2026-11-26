import tests

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from foreman.accept import AcceptError, Accepted, Paused, accept_event
from foreman.cli import main
from foreman.contract import CONTRACT_PATH
from foreman.delivery import LiveRun
from foreman.stage_output import HumanPrompt
from foreman.steps import CANCELLATION
from test_delivery import contract_text
import test_runner
from test_runner import APPROVE, FakeGitHub, FakeWorkspace, checker_reply


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


def comment(body, *, login="fixture-owner", action="created", **issue):
    return {"action": action, "issue": {"number": 8, **issue}, "repository": {"full_name": "Example/Engine"},
            "comment": {"user": {"login": login}, "body": body}, "sender": {"login": login}}


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

    def test_a_comment_wakes_only_the_paused_run_it_answers(self):
        gate, waiting = Paused("Fixture-Owner", self.prompt), Paused("fixture-owner", None)
        answer = f"{self.prompt.answer_marker}\nKeep it."
        for run, payload, wakes in (
                (gate, comment(answer), True), (gate, comment(answer, action="edited"), False),
                (gate, comment(answer, login="someone-else"), False), (gate, comment("Keep it."), False),
                (gate, comment(f"{self.prompt.answer_marker}\n"), False), (gate, comment(answer, pull_request={}), False),
                (None, comment(answer), False), (waiting, comment("foreman stop\nNo longer needed."), True),
                (waiting, comment("foreman stop", login="someone-else"), False), (waiting, comment(answer), False)):
            with self.subTest(run=run, payload=payload):
                decision = accept_event("issue_comment", payload, lambda repo, issue: run)
                self.assertEqual(decision, Accepted("wake", "Example/Engine", 8) if wakes else None)
        with self.assertRaises(AcceptError):
            accept_event("issue_comment", {"action": "created", "issue": {"number": 8}}, lambda repo, issue: gate)


class AnswerWakeTest(unittest.TestCase):
    """The App's issue comment webhook wakes a run paused at a gate on its reply marker, and a waiting run on a stop
    (rules.gates.continuation). The wake is no start, so it runs no doctor and keeps the login and caps the start saved."""

    # The runner tests' fixtures, borrowed without inheriting their tests.
    setUpClass = classmethod(test_runner.LiveRunnerTest.setUpClass.__func__)
    walk = test_runner.LiveRunnerTest.walk

    def setUp(self):
        test_runner.LiveRunnerTest.setUp(self)
        self.enterContext(patch.dict("os.environ", {"XDG_RUNTIME_DIR": str(self.cwd / "runtime"), "FOREMAN_APP_ID": "1"}))
        self.enterContext(patch("foreman.cli.credentials.environment", return_value={"GH_TOKEN": "synthetic"}))

    def command(self, argv, replies=()):
        workspace = FakeWorkspace(self.cwd)
        workspace.remote, workspace.base_files[CONTRACT_PATH] = self.github.branches, contract_text("exit 0")
        outputs, results = iter(replies), []
        with redirect_stdout(io.StringIO()) as out:
            code = main(argv, checkpoint=self.checkpoint, live_run=LiveRun(self.github, workspace), report=results.append,
                        worker_runner=lambda args, cwd, env: workspace.reply(args, checker_reply(args) or next(outputs)))
        return code, out.getvalue(), results[-1] if results else None

    def deliver(self, event, payload):
        path = self.cwd / "delivery.json"
        path.write_text(json.dumps(payload))
        code, out, _ = self.command(["accept", "--event", event, str(path)])
        self.assertEqual(code, 0)
        return out.strip()

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
        self.assertEqual((woken["human_login"], woken["run"].max_budget_usd), ("fixture-owner", "3"))
        self.assertEqual(self.github.variable_reads, [*reads, "Example/Engine"])
        merged = len(self.github.merge_calls)
        self.assertEqual(self.deliver("issue_comment", comment(answer)), "ignored")
        self.assertEqual(self.deliver("issue_comment", comment(answer + "\nIn place.", action="edited")), "ignored")
        self.assertEqual(self.command(["wake", "--repo", "Example/Engine", "--issue", "8"])[:2], (0, "not waiting example/engine#8\n"))
        self.assertEqual(len(self.github.merge_calls), merged)

        self.checkpoint, self.github = self.cwd / "second.sqlite3", FakeGitHub()
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
        code, _, stopped = self.command(["wake", "--repo", "Example/Engine", "--issue", "8"])
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
        """A wake that finds the run halted, as when an earlier wake continued it, resumes nothing. Only Ready resumes it."""
        self.github.hold = "Holding merges."
        self.assertEqual(self.walk([{"triage": "supplied_slice"}, {"implementation": "built"}, APPROVE])["stop"], "merge_held")
        reads, writes = list(self.github.variable_reads), list(self.github.status_writes)
        self.assertEqual(self.command(["wake", "--repo", "Example/Engine", "--issue", "8"])[:2], (0, "not waiting example/engine#8\n"))
        self.assertEqual((self.github.variable_reads, self.github.status_writes), (reads, writes))


if __name__ == "__main__":
    unittest.main()
