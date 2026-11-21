"""Keep the source type gate strict while PR B removes its six exceptions."""

from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASIC_MODULES = {
    "foreman/stage_output.py",
    "foreman/delivery.py",
    "foreman/dry_run.py",
    "foreman/runner.py",
    "foreman/cli.py",
    "foreman/proof.py",
}


class TypeGateTests(unittest.TestCase):
    def test_strict_configuration(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        config = project["tool"]["pyright"]
        required = {
            "typeCheckingMode": "strict",
            "include": ["src"],
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
        self.assertFalse(config.get("exclude"))
        self.assertFalse(config.get("ignore"))
        self.assertFalse(config.get("executionEnvironments"))
        for key, value in config.items():
            if key.startswith("report"):
                with self.subTest(diagnostic=key):
                    self.assertEqual(value, "error")

    def test_basic_directives_are_limited_to_pr_b(self):
        for path in (ROOT / "src").rglob("*"):
            if not path.is_file() or path.suffix != ".py":
                continue
            for line in path.read_text().splitlines():
                if re.search(r"#\s*pyright:\s*(?:basic|off|standard)\b", line):
                    with self.subTest(path=path.relative_to(ROOT)):
                        self.assertIn(path.relative_to(ROOT / "src").as_posix(), BASIC_MODULES)
                        self.assertRegex(line, r"#\s*pyright:\s*basic\s*$")
