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

        secret = "ghp_0123456789abcdefghij0123456789"
        ev1 = accept_event({
            "source": "alert_webhook",
            "severity": "error",
            "service": "api",
            "title": "500 on /deals for owner@example.invalid",
            "body": f"token={secret} phone=(555) 234-5678",
        })
        ev2 = accept_event({
            "source": "alert_webhook",
            "severity": "error",
            "service": "api",
            "title": "500 on /deals for owner@example.invalid",
            "body": "retry",
        })
        self.assertTrue(ev1["start"])
        self.assertEqual(ev1["source"], "alert_webhook")
        self.assertTrue(ev1["draft_only"])
        self.assertRegex(ev1["dedupe_key"], r"^alert:[0-9a-f]{16}$")
        self.assertEqual(ev1["dedupe_key"], ev2["dedupe_key"])
        self.assertNotIn("owner@example.invalid", ev1["request"])
        self.assertNotIn(secret, ev1["request"])
        self.assertNotIn("(555) 234-5678", ev1["request"])

    def test_alert_dedupe_key_hashes_raw_seed_before_redaction(self):
        def key(**fields):
            return accept_event({"source": "alert_webhook", "service": "api", **fields})["dedupe_key"]

        # Different secrets redact to the same placeholder, so a post-redaction hash would collide.
        first = key(title="Leaked ghp_0123456789abcdefghij0123456789")
        second = key(title="Leaked ghp_9876543210zyxwvutsrqp9876543210")
        self.assertNotEqual(first, second)

        raw_id = "evt-ghp_0123456789abcdefghij0123456789"
        by_id = key(title="Anything", event_id=raw_id)
        self.assertEqual(by_id, key(title="Other title", event_id=raw_id))
        self.assertNotIn("ghp_", by_id)
        self.assertNotIn("REDACTED", by_id)
        explicit = key(title="Worker OOM", dedupe_key="worker:oom-1")
        self.assertRegex(explicit, r"^alert:[0-9a-f]{16}$")
        self.assertNotIn("worker:oom-1", explicit)

    def test_cli_accept_prints_opaque_alert_key(self):
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
            self.assertRegex(rendered, r"dedupe_key alert:[0-9a-f]{16}")
            self.assertNotIn("worker:oom-1", rendered)


if __name__ == "__main__":
    unittest.main()
