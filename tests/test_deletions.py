from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import re
import unittest

from foreman.cli import main


class DeletionTests(unittest.TestCase):
    def test_retired_names_are_absent_from_production(self):
        root = Path(__file__).resolve().parents[1] / "src" / "foreman"
        retired = re.compile(r"\b(FailoverDispatcher|entry_node|STAGE_ORDER|implement_quota|no_dispatcher)\b")
        for path in root.rglob("*.py"):
            with self.subTest(path=path.name):
                self.assertIsNone(retired.search(path.read_text()))
        for name in ("measure.py", "measure.toml"):
            with self.subTest(name=name):
                self.assertFalse((root / name).exists())

    def test_improve_command_is_rejected(self):
        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as stopped:
            main(["improve", "x"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("invalid choice: 'improve'", error.getvalue())
