import tests

import unittest

from foreman.accept import AcceptError, accept_event


class AcceptTest(unittest.TestCase):
    def test_factory_label_starts_a_run(self):
        decision = accept_event(
            {
                "source": "github",
                "event": "issue_labeled",
                "labels": ["bug", "factory"],
                "title": "Encode the timecode",
                "body": "The comment thread crashes.",
            }
        )
        self.assertTrue(decision["start"])
        self.assertEqual(decision["source"], "github")
        self.assertIn("Encode the timecode", decision["request"])

    def test_other_labels_are_ignored(self):
        decision = accept_event(
            {
                "source": "github",
                "event": "issue_labeled",
                "labels": ["bug"],
                "title": "Encode the timecode",
            }
        )
        self.assertFalse(decision["start"])

    def test_alert_is_ignored(self):
        decision = accept_event(
            {"source": "alert", "title": "Invalid time value", "body": "stack"}
        )
        self.assertEqual(decision, {"start": False})

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(AcceptError):
            accept_event({"source": "slack", "title": "hi"})


if __name__ == "__main__":
    unittest.main()
