import tests

import hashlib
import hmac
import io
import json
import os
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.accept import AcceptError, Accepted, Paused, accept_event
from foreman.cli import main
from foreman.contract import CONTRACT_PATH
from foreman.delivery import LiveRun
from foreman.receiver import listener, run_answer
from foreman.stage_output import HumanPrompt
from foreman.state import SCHEMA_VERSION, checkpoint_serializer
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


SECRET = b"synthetic webhook secret"


class ReceiverTest(unittest.TestCase):
    """The receiver verifies each delivery's signature, hands its body unchanged to accept and runs the one answer accept gives."""

    prompt = HumanPrompt(stage="implement", question="Keep the sort order?", revision="ab" * 32)

    def setUp(self):
        self.commands, self.output = [], {}
        paused = {("Example/Engine", 8): Paused("fixture-owner", self.prompt)}
        server = listener(("127.0.0.1", 0), SECRET, lambda repo, issue: paused.get((repo, issue)), self.command)
        self.enterContext(server)
        serving = threading.Thread(target=server.serve_forever)
        serving.start()
        self.addCleanup(serving.join)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}/"
        self.enterContext(redirect_stdout(io.StringIO()))

    def command(self, args):
        self.commands.append(args)
        return self.output.get(tuple(args), ["usage below is cumulative across all invocations\n"])

    def post(self, event, payload, secret=SECRET):
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        request = Request(self.url, body, {"X-GitHub-Event": event, "X-GitHub-Delivery": "d-1", "X-Hub-Signature-256": signature})
        try:
            with urlopen(request) as response:
                reply = response.status, response.read().decode().strip()
        except HTTPError as exc:
            reply = exc.code, exc.read().decode().strip()
        for run in [thread for thread in threading.enumerate() if thread.name.startswith("foreman ")]:
            run.join()
        return reply

    def test_only_a_signed_delivery_reaches_accept_and_only_its_answer_runs(self):
        """TR19: four deliveries with a valid signature: a comment on a pull request, a comment under another login on an issue
        whose run is paused, a check_suite completed for a commit no run owns, and a Ready label event. A fifth delivery has a
        bad signature. The receiver hands the first four to accept unchanged. The first three are ignored: no model is called,
        no session starts and nothing is written to GitHub. The fourth starts a run. The fifth is refused before accept."""
        answer = f"{self.prompt.answer_marker}\nKeep it."
        deliveries = [("issue_comment", comment("Looks good.", pull_request={"url": "https://example.test/pull/12"})),
                      ("issue_comment", comment(answer, login="someone-else")),
                      ("check_suite", {"action": "completed", "check_suite": {"head_sha": "cd" * 20, "pull_requests": []},
                                       "repository": {"full_name": "Example/Engine"}}),
                      ("issues", delivery())]
        with patch("foreman.receiver.accept_event", wraps=accept_event) as accept:
            replies = [self.post(event, payload) for event, payload in deliveries]
            self.assertEqual(replies, [(202, "ignored")] * 3 + [(202, "start Example/Engine#7")])
            self.assertEqual([(call.args[0], call.args[1]) for call in accept.call_args_list], deliveries)
            self.assertEqual(self.commands, [["start", "--repo", "Example/Engine", "--issue", "7"]])
            self.assertEqual(self.post("issues", delivery(), secret=b"another secret"), (401, "refused bad signature"))
            self.assertEqual(accept.call_count, 4)
        self.assertEqual(len(self.commands), 1)

    def test_a_delivery_without_a_signature_or_with_a_malformed_body_is_refused(self):
        request = Request(self.url, b"{}", {"X-GitHub-Event": "issues"})
        with self.assertRaises(HTTPError) as unsigned:
            urlopen(request)
        self.assertEqual(unsigned.exception.code, 401)
        self.assertEqual(self.post("issues", {"action": "labeled", "label": {"name": "Ready"}})[0], 400)
        self.assertEqual(self.commands, [])

    def test_a_wake_runs_and_each_issue_a_finished_run_releases_starts_in_order(self):
        """A wake's run prints the start lines merge.release returns, and the receiver starts each, then theirs."""
        self.output = {("wake", "--repo", "Example/Engine", "--issue", "8"): ["merged abc\n", "start Example/Engine#9\n", "start Example/Other#3\n"],
                       ("start", "--repo", "Example/Engine", "--issue", "9"): ["start Example/Engine#10\n"]}
        self.assertEqual(self.post("issue_comment", comment(f"{self.prompt.answer_marker}\nKeep it.")), (202, "wake Example/Engine#8"))
        self.assertEqual([args[0] + " " + args[2] + "#" + args[4] for args in self.commands],
                         ["wake Example/Engine#8", "start Example/Engine#9", "start Example/Other#3", "start Example/Engine#10"])

    def test_run_answer_reads_only_whole_start_lines(self):
        commands = []
        run_answer(Accepted("start", "Example/Engine", 7),
                   lambda args: commands.append(args) or ["start Example/Engine#0\n", "restored start Example/Engine#4\n"])
        self.assertEqual(commands, [["start", "--repo", "Example/Engine", "--issue", "7"]])


class WebhookSecretTest(unittest.TestCase):
    def receive(self, path):
        with patch.dict(os.environ, {"FOREMAN_WEBHOOK_SECRET_PATH": str(path)}), redirect_stderr(io.StringIO()) as err:
            code = main(["receive", "--port", "0"])
        return code, err.getvalue()

    def test_the_receiver_needs_an_owner_only_secret_and_never_shows_it(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "webhook-secret"
            self.assertEqual(self.receive(path)[0], 1)
            path.write_bytes(SECRET + b"\n")
            path.chmod(0o640)
            code, err = self.receive(path)
            self.assertEqual(code, 1)
            self.assertIn("owner-only", err)
            self.assertNotIn(SECRET.decode(), err)
            path.write_bytes(b"\n")
            path.chmod(0o600)
            self.assertIn("is empty", self.receive(path)[1])


if __name__ == "__main__":
    unittest.main()
