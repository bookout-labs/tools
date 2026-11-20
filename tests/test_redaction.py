import os
import unittest
from urllib.parse import quote_plus

from foreman.redaction import MAX_SECRET_BYTES, SecretRedactor, redact_text


class RedactionTest(unittest.TestCase):
    def test_from_environ_adds_sensitive_vars_and_skips_safe_or_trivial_values(self):
        env = {
            "PATH": "/usr/local/bin:/usr/bin",
            "HOME": "/home/runner",
            "XDG_STATE_HOME": "/home/runner/.local/state",
            "CUSTOM_API_TOKEN": "tok-super-secret-value-1234",
            "DEBUG_FLAG": "true",
            "PORT_NUMBER": "808000",
            "SHORT_KEY": "abc",
            "DATABASE_URL": "postgresql://svc:pass123456@db.example.invalid:5432/app",
        }
        redactor = SecretRedactor.from_environ(env)
        text = (
            "PATH=/usr/local/bin:/usr/bin "
            "HOME=/home/runner "
            "flag=true port=808000 short=abc "
            "tok=tok-super-secret-value-1234 "
            f"encoded={quote_plus('tok-super-secret-value-1234')} "
            "db=postgresql://svc:pass123456@db.example.invalid:5432/app"
        )
        scrubbed = redactor.redact(text)
        self.assertIn("PATH=/usr/local/bin:/usr/bin", scrubbed)
        self.assertIn("HOME=/home/runner", scrubbed)
        self.assertIn("port=808000", scrubbed)
        self.assertIn("short=abc", scrubbed)
        self.assertNotIn("tok-super-secret-value-1234", scrubbed)
        self.assertNotIn("pass123456", scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

    def test_token_patterns_and_json_escaped_or_multiline_secrets(self):
        redactor = SecretRedactor()
        redactor.add('line-one-secret\nline-two-"quoted"-secret')
        raw = (
            'header="Authorization: Bearer bearer_example_token_987654321" '
            "gh=ghp_0123456789abcdefghij0123456789 "
            "openai=sk-proj-0123456789abcdefghij "
            "xai=xai-0123456789abcdefghij "
            "aws=AKIAIOSFODNN7EXAMPLE "
            'escaped="line-one-secret\\nline-two-\\"quoted\\"-secret" '
            "split=line-two-\"quoted\"-secret"
        )
        scrubbed = redactor.redact(raw)
        self.assertNotIn("bearer_example_token_987654321", scrubbed)
        self.assertNotIn("ghp_0123456789abcdefghij0123456789", scrubbed)
        self.assertNotIn("sk-proj-0123456789abcdefghij", scrubbed)
        self.assertNotIn("xai-0123456789abcdefghij", scrubbed)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", scrubbed)
        self.assertNotIn("line-one-secret", scrubbed)
        self.assertNotIn('line-two-"quoted"-secret', scrubbed)
        self.assertIn("Authorization: Bearer [REDACTED]", scrubbed)

    def test_optional_pii_scrubbing_preserves_uuids_and_shas(self):
        sample = (
            "contact owner@example.invalid or (555) 234-5678 ext; "
            "uuid=123e4567-e89b-12d3-a456-426614174000 "
            "sha=a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        )
        without_pii = redact_text(sample, env={}, redact_pii=False)
        self.assertIn("owner@example.invalid", without_pii)
        with_pii = redact_text(sample, env={}, redact_pii=True)
        self.assertNotIn("owner@example.invalid", with_pii)
        self.assertNotIn("(555) 234-5678", with_pii)
        self.assertIn("[REDACTED_EMAIL]", with_pii)
        self.assertIn("[REDACTED_PHONE]", with_pii)
        self.assertIn("123e4567-e89b-12d3-a456-426614174000", with_pii)
        self.assertIn("a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", with_pii)

    def test_overflow_fails_closed(self):
        redactor = SecretRedactor()
        redactor.add("x" * (MAX_SECRET_BYTES + 10))
        self.assertEqual(redactor.redact("ordinary text"), "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
