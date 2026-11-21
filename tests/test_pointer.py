import tests

from pathlib import Path
import json
import unittest
import subprocess
import tempfile
import sys

from foreman.pointer import KNOWN_GRAPHS, OPTIONAL, REQUIRED, PointerError, load_pointer

ROOT = Path(__file__).parents[1]
EXAMPLES = ROOT / "examples"


class PointerTest(unittest.TestCase):
    def test_examples_point_at_bookout_labs(self):
        files = sorted(EXAMPLES.glob("*.yaml"))
        self.assertGreaterEqual(len(files), 3)
        for path in files:
            pointer = load_pointer(path)
            self.assertEqual(pointer["source"], "bookout-labs/tools")
            self.assertEqual(pointer["graph"], "delivery")
            self.assertIn("check", pointer)

    def test_schema_matches_the_loader(self):
        schema = json.loads((ROOT / "schemas" / "tools-pointer.schema.json").read_text())
        self.assertEqual(tuple(schema["required"]), REQUIRED)
        self.assertEqual(set(schema["properties"]), set(REQUIRED) | set(OPTIONAL))
        self.assertEqual(tuple(schema["properties"]["graph"]["enum"]), KNOWN_GRAPHS)

    def test_non_string_key_is_a_cli_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "pointer.yaml"
            path.write_text("1: x\n")
            result = subprocess.run(
                [sys.executable, "-c", "from foreman.cli import main; raise SystemExit(main())",
                 "check-pointer", str(path)],
                capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "pointer keys must be strings, got int")
        self.assertNotIn("Traceback", result.stderr)

    def test_bad_pointer_is_rejected(self):
        path = ROOT / "examples" / "missing.yaml"
        path.write_text("source: bookout-labs\nref: main\ngraph: delivery\n")
        try:
            with self.assertRaises(PointerError):
                load_pointer(path)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
