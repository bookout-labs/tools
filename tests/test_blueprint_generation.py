from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "docs" / "graph" / "blueprint"
GENERATED_TEXT = ("code-map.md", "files.txt", "files.yaml", "index.html", "missing-descriptions.txt")
PSTACK_REPO = os.environ.get("FOREMAN_PINNED_SKILL_REPO")
MATT_SKILLS_REPO = os.environ.get("FOREMAN_PINNED_MATT_SKILLS")


class BlueprintGenerationTest(unittest.TestCase):
    @unittest.skipUnless(PSTACK_REPO and MATT_SKILLS_REPO, "requires local checkouts with the pstack and mattpocock/skills pins")
    def test_generated_text_matches_blueprint(self):
        expected = {Path(name) for name in GENERATED_TEXT}
        expected.update(path.relative_to(BLUEPRINT) for path in (BLUEPRINT / "views").rglob("*.mmd"))

        with TemporaryDirectory() as scratch:
            output = Path(scratch) / "output"
            subprocess.run(
                [sys.executable, str(BLUEPRINT / "gen.py"), "--pstack", str(PSTACK_REPO),
                 "--matt-skills", str(MATT_SKILLS_REPO), "--output", str(output), "--no-svg"],
                check=True,
            )
            actual = {path.relative_to(output) for path in output.rglob("*") if path.is_file()}
            self.assertEqual(actual, expected)
            for path in sorted(expected):
                with self.subTest(path=path):
                    self.assertEqual((output / path).read_bytes(), (BLUEPRINT / path).read_bytes())

    def test_published_diagrams_match_render_checksums(self):
        views = BLUEPRINT / "views"
        sources = set(views.rglob("*.mmd"))
        renders = set(views.rglob("*.svg"))
        self.assertEqual(renders, {path.with_suffix(".svg") for path in sources})
        checksums = [
            f"{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(views).as_posix()}"
            for path in sorted(sources | renders, key=lambda path: path.as_posix())
        ]
        self.assertEqual((views / "SHA256SUMS").read_text().splitlines(), checksums)
