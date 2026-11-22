import tests
import unittest
from unittest.mock import patch

from foreman.check_output import CheckOutput
from foreman.redaction import redact


class CheckOutputTest(unittest.TestCase):
    def test_window_and_normalization(self):
        for chunks in ([""], ["α" * 10000], ["head\n", "middle\n" * 400, "tail\n"],
                       ["\x1b[31merror\x1b[0m\r\nnext\rlast"], ["\udcff"], ["a" + sep + "b" + sep for sep in
                       ("\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")] * 200):
            with self.subTest(chunks=len(chunks)):
                logged = []
                output = CheckOutput(logged.append)
                for chunk in chunks:
                    output(chunk)
                    self.assertLessEqual(len((output.head + output.tail).encode()), 8192)
                text = output.text
                self.assertLessEqual(len(text.encode()), 8192)
                self.assertLessEqual(len(text.splitlines()), 128)
                self.assertEqual(logged, chunks)
                self.assertNotIn("\x1b", text)
                self.assertNotIn("\r", text)
        self.assertEqual(CheckOutput().text, "[no check output]")
        output = CheckOutput()
        output("start\n" + "noise\n" * 400 + "end\n")
        self.assertTrue(output.text.startswith("start\n"))
        self.assertTrue(output.text.endswith("end\n"))
        self.assertIn("output omitted", output.text)

    def test_full_redaction_precedes_clipping(self):
        secrets = ["ghp_" + "a" * 100, "unlabelledcanary", "bearercanary", "YmFzaWNjYW5hcnk=", "KEYBODY", "cachedcanary"]
        with patch.dict("os.environ", {"GH_TOKEN": secrets[1]}), \
             patch("foreman.check_output.credentials.known_values", return_value=(secrets[5],)):
            output = CheckOutput()
        for line in ["x" * 2000 + secrets[0] + "\n", secrets[1] + "\n", secrets[5] + "\n",
                     "Authorization: Bearer " + secrets[2] + "\n", "Authorization: Basic " + secrets[3] + "\n",
                     "-----BEGIN RSA PRIVATE KEY-----\n", "KEYBODY\n", "-----END RSA PRIVATE KEY-----\n"]:
            output(line)
        for secret in secrets:
            self.assertNotIn(secret, output.text)
        self.assertIn("redacted", output.text)

    def test_private_key_framing_preserves_diagnostics(self):
        for block in ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: synthetic\n\nKEYBODY\n-----END RSA PRIVATE KEY----- trailing secret\n",
                      "PuTTY-User-Key-File-2: ssh-rsa\nComment: key-END 2026\nPrivate-Lines: 1\nKEYBODY\n\n",
                      "-----BEGIN OPENSSH PRIVATE KEY-----\n\nKEYBODY\n-----END WRONG PRIVATE KEY-----\n-----END OPENSSH PRIVATE KEY-----\n"):
            with self.subTest(block=block.splitlines()[0]):
                output = CheckOutput()
                for line in (block + "AssertionError: failed\n- actual\n+ expected\n").splitlines(keepends=True):
                    output(line)
                self.assertEqual(output.text, redact(block + "AssertionError: failed\n- actual\n+ expected\n"))
                self.assertEqual(output.text, "[redacted:Private Key]\n" + ("\n" if block.startswith("PuTTY") else "")
                                 + "AssertionError: failed\n- actual\n+ expected\n")
        for mention in ("Explain BEGIN PRIVATE KEY in the docs", "[redacted:Private Key]"):
            output = CheckOutput()
            output(f"AssertionError: {mention} != expected\n- actual\n+ expected\n")
            self.assertTrue(output.text.endswith("\n- actual\n+ expected\n"))
