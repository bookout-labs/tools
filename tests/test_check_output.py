import tests
import unittest
from unittest.mock import patch

from foreman.check_output import CheckOutput


class CheckOutputTest(unittest.TestCase):
    def test_window_and_normalization(self):
        for chunks in ([""], ["α" * 10000], ["head\n", "middle\n" * 400, "tail\n"],
                       ["\x1b[31merror\x1b[0m\r\nnext\rlast"], ["\udcff"]):
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
