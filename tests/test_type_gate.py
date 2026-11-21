"""Keep every source module under the strict type gate."""

from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TypeGateTests(unittest.TestCase):
    def test_strict_configuration(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        config = project["tool"]["pyright"]
        required = {
            "typeCheckingMode": "strict",
            "include": ["src", "typings"],
            "enableTypeIgnoreComments": False,
            "reportUnnecessaryTypeIgnoreComment": "error",
            "reportImplicitOverride": "error",
            "pythonVersion": "3.11",
        }
        for key, value in required.items():
            with self.subTest(setting=key):
                self.assertEqual(config.get(key), value)
        self.assertEqual(project["project"]["requires-python"], ">=3.11")
        self.assertIn("pyright==1.1.414", project["project"]["optional-dependencies"]["dev"])
        self.assertIn("typing-extensions>=4.6.1,<5", project["project"]["dependencies"])
        self.assertFalse(config.get("exclude"))
        self.assertFalse(config.get("ignore"))
        self.assertFalse(config.get("executionEnvironments"))
        for key, value in config.items():
            if key.startswith("report"):
                with self.subTest(diagnostic=key):
                    self.assertEqual(value, "error")

    def test_no_basic_directives(self):
        for path in (p for root in ("src", "typings") for p in (ROOT / root).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".pyi"}:
                continue
            for line in path.read_text().splitlines():
                if re.search(r"#\s*pyright:", line):
                    with self.subTest(path=path.relative_to(ROOT)):
                        self.fail("Source module weakens strict checking")
