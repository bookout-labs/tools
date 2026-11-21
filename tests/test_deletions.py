from pathlib import Path
import re
import unittest


class DeletionTests(unittest.TestCase):
    def test_retired_names_are_absent_from_production(self):
        root = Path(__file__).resolve().parents[1] / "src" / "foreman"
        retired = re.compile(r"\b(FailoverDispatcher|entry_node|STAGE_ORDER|implement_quota|no_dispatcher)\b")
        for path in root.rglob("*.py"):
            with self.subTest(path=path.name):
                self.assertIsNone(retired.search(path.read_text()))
        self.assertFalse((root / "measure.py").exists())
