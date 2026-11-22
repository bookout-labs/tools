import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "docs" / "graph" / "blueprint"
GENERATED_TEXT = ("code-map.md", "files.txt", "files.yaml", "index.html", "missing-descriptions.txt")
PSTACK_REPO = os.environ.get("FOREMAN_PINNED_SKILL_REPO")


class BlueprintGenerationTest(unittest.TestCase):
    @unittest.skipUnless(PSTACK_REPO, "requires a local checkout with the pstack pin")
    def test_generated_text_matches_blueprint(self):
        expected = {Path(name) for name in GENERATED_TEXT}
        expected.update(path.relative_to(BLUEPRINT) for path in (BLUEPRINT / "views").rglob("*.mmd"))

        with TemporaryDirectory() as scratch:
            output = Path(scratch) / "output"
            revision = yaml.safe_load((BLUEPRINT / "target.yaml").read_text())["baseline"]
            baseline = ROOT
            available = subprocess.run(
                ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
                cwd=ROOT, capture_output=True,
            )
            if available.returncode != 0:
                baseline = Path(scratch) / "baseline"
                subprocess.run(["git", "init", "--quiet", str(baseline)], check=True)
                subprocess.run(
                    ["git", "fetch", "--quiet", "--no-tags", "--depth=1", "--",
                     "https://github.com/bookout-labs/tools", revision],
                    cwd=baseline, check=True,
                )
            subprocess.run(
                [sys.executable, str(BLUEPRINT / "gen.py"), "--pstack", str(PSTACK_REPO),
                 "--baseline-repo", str(baseline), "--output", str(output), "--no-svg"],
                check=True,
            )
            actual = {path.relative_to(output) for path in output.rglob("*") if path.is_file()}
            self.assertEqual(actual, expected)
            for path in sorted(expected):
                with self.subTest(path=path):
                    self.assertEqual((output / path).read_bytes(), (BLUEPRINT / path).read_bytes())
