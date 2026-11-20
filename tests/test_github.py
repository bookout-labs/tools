import json
import unittest
from unittest.mock import patch

from foreman.github import GhClient


class GhClientTest(unittest.TestCase):
    @patch("foreman.github.subprocess.check_output")
    def test_only_closed_issue_with_merged_closing_pull_request_is_merged(self, run):
        client = GhClient()
        for closed, refs, expected in (
            (False, [{"number": 3, "state": "MERGED"}], False),
            (True, [], False),
            (True, [{"number": 3, "state": "CLOSED"}], False),
            (True, [{"number": 3, "state": "MERGED"}], True),
        ):
            with self.subTest(closed=closed, refs=refs):
                run.return_value = json.dumps({"closed": closed, "stateReason": "COMPLETED", "closedByPullRequestsReferences": refs})
                self.assertEqual(client.is_merged("Example/Engine", 1), expected)
        run.side_effect = [json.dumps({"closed": True, "stateReason": "COMPLETED", "closedByPullRequestsReferences": [{"number": 3}]}), json.dumps({"state": "MERGED"})]
        self.assertTrue(client.is_merged("Example/Engine", 1))
        self.assertEqual(run.call_args.args[0], ["gh", "pr", "view", "3", "--repo", "Example/Engine", "--json", "state"])

    @patch("foreman.github.subprocess.check_output")
    def test_add_label_preserves_existing_labels(self, run):
        GhClient().add_label("Example/Engine", 2, "factory")
        run.assert_called_once_with(["gh", "issue", "edit", "2", "--repo", "Example/Engine", "--add-label", "factory"], text=True)
