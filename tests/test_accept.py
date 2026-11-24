import tests

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from contextlib import redirect_stdout

from foreman.accept import AcceptError, accept_event
from foreman.cli import main


def delivery(label="Ready", action="labeled", **issue):
    return {"action": action, "label": {"name": label},
            "issue": {"number": 7, "title": "Encode the timecode", "labels": [{"name": "bug"}, {"name": label}], **issue},
            "repository": {"full_name": "Example/Engine"}, "sender": {"login": "octocat"}}


class AcceptTest(unittest.TestCase):
    def test_adding_ready_to_an_issue_starts_its_run(self):
        self.assertEqual(accept_event("issues", delivery()), {"start": True, "repo": "Example/Engine", "issue": 7})

    def test_the_added_label_decides_not_the_issue_labels(self):
        # An unrelated label added to an issue that already carries Ready does not start it again.
        payload = delivery(label="bug")
        payload["issue"]["labels"].append({"name": "Ready"})
        self.assertEqual(accept_event("issues", payload), {"start": False})

    def test_other_events_actions_and_pull_requests_are_ignored(self):
        self.assertEqual(accept_event("issue_comment", delivery()), {"start": False})
        self.assertEqual(accept_event("issues", delivery(action="unlabeled")), {"start": False})
        self.assertEqual(accept_event("issues", delivery(pull_request={"url": "https://example.test/pull/7"})), {"start": False})

    def test_foreman_own_label_writes_never_start_a_run(self):
        # Spec labels each child Ready as the App, and the child starts once, from its start line.
        for login in ("bookout-labs-foreman[bot]", "Bookout-Labs-Foreman[bot]"):
            with self.subTest(login=login):
                self.assertEqual(accept_event("issues", {**delivery(), "sender": {"login": login}}), {"start": False})
        # A delivery without a sender, like the blueprint's synthetic one, is decided by its label.
        payload = delivery()
        del payload["sender"]
        self.assertEqual(accept_event("issues", payload), {"start": True, "repo": "Example/Engine", "issue": 7})

    def test_the_retired_factory_label_is_ignored(self):
        self.assertEqual(accept_event("issues", delivery(label="factory")), {"start": False})

    def test_a_malformed_delivery_is_rejected(self):
        for payload in ([], {"action": "labeled", "label": {"name": "Ready"}}):
            with self.subTest(payload=payload), self.assertRaises(AcceptError):
                accept_event("issues", payload)
        for repository, number in (("not a repo", 7), ("Example/Engine", 0), ("Example/Engine", True)):
            payload = delivery()
            payload["repository"]["full_name"], payload["issue"]["number"] = repository, number
            with self.subTest(repository=repository, number=number), self.assertRaises(AcceptError):
                accept_event("issues", payload)

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


if __name__ == "__main__":
    unittest.main()
