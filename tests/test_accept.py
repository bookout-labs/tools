import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import yaml

from foreman.accept import AcceptError, accept_event
from foreman.cli import main


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

    def test_alert_webhook_redacts_dedupes_and_stays_draft_only(self):
        info = accept_event({"source": "alert_webhook", "severity": "info", "title": "Heartbeat"})
        self.assertEqual(info, {"start": False})

        ev1 = accept_event({
            "source": "alert_webhook",
            "severity": "error",
            "service": "api",
            "title": "500 on /deals for owner@example.invalid",
            "body": "token=ghp_0123456789abcdefghij0123456789 phone=(555) 234-5678",
        })
        ev2 = accept_event({
            "source": "alert_webhook",
            "severity": "error",
            "service": "api",
            "title": "500 on /deals for other@example.invalid",
            "body": "retry",
        })
        self.assertTrue(ev1["start"])
        self.assertEqual(ev1["source"], "alert_webhook")
        self.assertTrue(ev1["draft_only"])
        self.assertTrue(ev1["dedupe_key"].startswith("alert:"))
        self.assertEqual(ev1["dedupe_key"], ev2["dedupe_key"])
        self.assertNotIn("owner@example.invalid", ev1["request"])
        self.assertNotIn("ghp_0123456789abcdefghij0123456789", ev1["request"])
        self.assertNotIn("(555) 234-5678", ev1["request"])

        with TemporaryDirectory() as tmpdir:
            ev_path = Path(tmpdir) / "alert.yaml"
            ev_path.write_text(yaml.safe_dump({
                "source": "alert_webhook",
                "severity": "critical",
                "service": "worker",
                "dedupe_key": "worker:oom-1",
                "title": "Worker OOM",
            }))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["accept", str(ev_path)]), 0)
            rendered = out.getvalue()
            self.assertIn("start alert_webhook", rendered)
            self.assertIn("draft_only true", rendered)
            self.assertIn("dedupe_key worker:oom-1", rendered)


if __name__ == "__main__":
    unittest.main()
